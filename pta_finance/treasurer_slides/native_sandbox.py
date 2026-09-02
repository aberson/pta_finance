"""Narrow Windows LPAC launcher for the one-shot native statement worker.

This module deliberately accepts only public worker configuration.  The broker owns the
private PDF bytes and may send them only after :func:`start_native_pdf_worker` returns a
ready-attested session.  The worker runs in an LPAC with only the Windows ``registryRead``
capability needed to initialize CPython; it gets no source path, ambient environment,
network capability, or inherited handles.  It first attests through public named control
objects; only then does the broker duplicate the two anonymous PDF-channel handles directly
into that exact LPAC process.
"""

from __future__ import annotations

import atexit
import ctypes
import hmac
import multiprocessing
import os
import secrets
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, NoReturn, Protocol

_READY_FRAME_MAXIMUM_BYTES = 256
_READY_NONCE_BYTES = 32
_APP_CONTAINER_PROFILE_PREFIX = "PtaFinanceNativeWorker"
_STAGED_RUNTIME_PREFIX = "pta-finance-native-runtime-"
_PUBLIC_WORKER_PACKAGE_FILES = (
    Path("__init__.py"),
    Path("treasurer_slides") / "__init__.py",
    Path("treasurer_slides") / "models.py",
    Path("treasurer_slides") / "bank_statements.py",
    Path("treasurer_slides") / "native_worker.py",
)
_WINDOWS_CREATE_SUSPENDED = 0x00000004
_WINDOWS_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_WINDOWS_CREATE_NO_WINDOW = 0x08000000
_WINDOWS_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_WINDOWS_WAIT_OBJECT_0 = 0
_WINDOWS_WAIT_TIMEOUT = 258
_WINDOWS_INFINITE = 0xFFFFFFFF
_WINDOWS_ERROR_INSUFFICIENT_BUFFER = 122
_WINDOWS_TOKEN_QUERY = 0x0008
_WINDOWS_TOKEN_USER = 1
_WINDOWS_TOKEN_IS_APP_CONTAINER = 29
_WINDOWS_TOKEN_APP_CONTAINER_SID = 31
_WINDOWS_SE_GROUP_ENABLED = 0x00000004
_WINDOWS_PROCESS_CREATION_ALL_APPLICATION_PACKAGES_OPT_OUT = 0x00000001
_WINDOWS_PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
_WINDOWS_PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
_WINDOWS_PROC_THREAD_ATTRIBUTE_ALL_APPLICATION_PACKAGES_POLICY = 0x0002000F
_WINDOWS_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_WINDOWS_JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
_WINDOWS_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
_WINDOWS_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_WINDOWS_JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
_WINDOWS_JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
_WINDOWS_HUNDRED_NANOSECONDS_PER_SECOND = 10_000_000
_WINDOWS_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_WINDOWS_SECURITY_DESCRIPTOR_REVISION = 1
_WINDOWS_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_WINDOWS_PAGE_READWRITE = 0x00000004
_WINDOWS_FILE_MAP_WRITE = 0x00000002
_WINDOWS_EVENT_MODIFY_STATE = 0x00000002
_WINDOWS_SYNCHRONIZE = 0x00100000
_WINDOWS_DUPLICATE_SAME_ACCESS = 0x00000002
_WINDOWS_CONTROL_MAPPING_BYTES = 256
_WINDOWS_CONTROL_EVENT_WAIT_MILLISECONDS = 30_000
_WINDOWS_CONTROL_NAME_PREFIX = r"Local\pta-finance-native-"
_CONTROL_HANDLES_MAGIC = b"PTAFINH1"
_CONTROL_HANDLES_FRAME = struct.Struct("<8s64sQQ")


class NativeSandboxUnavailable(RuntimeError):
    """The platform could not create the required native-parser boundary."""


class NativeSandboxConnection(Protocol):
    """The small anonymous-pipe surface shared with the statement broker."""

    def fileno(self) -> int: ...

    def poll(self, timeout: float = ...) -> bool: ...

    def recv_bytes(self, maxlength: int | None = ...) -> bytes: ...

    def send_bytes(self, buffer: bytes) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class NativeWorkerSession:
    """The broker endpoints and lifecycle owner for one ready LPAC worker."""

    request_sender: NativeSandboxConnection
    response_receiver: NativeSandboxConnection
    process: NativeSandboxProcess


@dataclass(frozen=True)
class _StagedRuntime:
    root: Path
    interpreter: Path
    site_root: Path


@dataclass(frozen=True)
class _AppContainerProfile:
    name: str
    sid: int
    sid_text: str
    local_data: Path
    capabilities: _CapabilitySet


@dataclass
class _DeferredStartupArtifacts:
    """Public artifacts retained until a failed startup can be cleaned safely."""

    runtime: Path | None
    profile_name: str | None


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", ctypes.c_ulong),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_ulong),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_ulong),
        ("SchedulingClass", ctypes.c_ulong),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_ulong)]


class _SecurityCapabilities(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.POINTER(_SidAndAttributes)),
        ("CapabilityCount", ctypes.c_ulong),
        ("Reserved", ctypes.c_ulong),
    ]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [
        ("nLength", ctypes.c_ulong),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", ctypes.c_int),
    ]


class _TokenAppContainerInformation(ctypes.Structure):
    _fields_ = [("TokenAppContainer", ctypes.c_void_p)]


@dataclass
class _WorkerControlChannel:
    """Public startup controls; they never contain a statement path or bytes."""

    mapping_name: str
    attested_event_name: str
    handles_ready_event_name: str
    mapping_handle: int | None
    mapping_view: int | None
    attested_event_handle: int | None
    handles_ready_event_handle: int | None


@dataclass
class _CapabilitySet:
    """A one-capability SID allocation retained until CreateProcess has copied it."""

    sid: int
    entries: Any
    local_free: Any

    def close(self) -> None:
        if self.sid != 0:
            self.local_free(ctypes.c_void_p(self.sid))
            self.sid = 0


class _StartupInfoW(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("lpReserved", ctypes.c_wchar_p),
        ("lpDesktop", ctypes.c_wchar_p),
        ("lpTitle", ctypes.c_wchar_p),
        ("dwX", ctypes.c_ulong),
        ("dwY", ctypes.c_ulong),
        ("dwXSize", ctypes.c_ulong),
        ("dwYSize", ctypes.c_ulong),
        ("dwXCountChars", ctypes.c_ulong),
        ("dwYCountChars", ctypes.c_ulong),
        ("dwFillAttribute", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("wShowWindow", ctypes.c_ushort),
        ("cbReserved2", ctypes.c_ushort),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", ctypes.c_void_p),
        ("hStdOutput", ctypes.c_void_p),
        ("hStdError", ctypes.c_void_p),
    ]


class _StartupInfoExW(ctypes.Structure):
    _fields_ = [("StartupInfo", _StartupInfoW), ("lpAttributeList", ctypes.c_void_p)]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", ctypes.c_void_p),
        ("hThread", ctypes.c_void_p),
        ("dwProcessId", ctypes.c_ulong),
        ("dwThreadId", ctypes.c_ulong),
    ]


def _raise_sandbox_unavailable() -> NoReturn:
    raise NativeSandboxUnavailable("native statement sandbox is unavailable") from None


def _windows_dll(name: str) -> Any:
    """Load a Windows DLL without exposing platform-only ctypes members to Linux mypy."""

    loader: Any = getattr(ctypes, "WinDLL", None)
    if loader is None:
        _raise_sandbox_unavailable()
    return loader(name, use_last_error=True)


def _windows_last_error() -> int:
    getter: Any = getattr(ctypes, "get_last_error", None)
    if getter is None:
        _raise_sandbox_unavailable()
    return int(getter())


def _windows_handle_is_inheritable(handle: int) -> bool:
    getter: Any = getattr(os, "get_handle_inheritable", None)
    if getter is None:
        _raise_sandbox_unavailable()
    return bool(getter(handle))


def _windows_apis() -> tuple[Any, Any, Any, Any]:
    if os.name != "nt":
        raise NativeSandboxUnavailable("native statement parsing requires the Windows sandbox")
    return (
        _windows_dll("kernel32"),
        _windows_dll("advapi32"),
        _windows_dll("userenv"),
        _windows_dll("ole32"),
    )


def _close_handle(kernel32: Any, handle: int | None) -> None:
    if handle is None or handle == 0:
        return
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int
    if not close_handle(ctypes.c_void_p(handle)):
        _raise_sandbox_unavailable()


def _free_sid(advapi32: Any, sid: int | None) -> None:
    if sid is None or sid == 0:
        return
    free_sid = advapi32.FreeSid
    free_sid.argtypes = (ctypes.c_void_p,)
    free_sid.restype = ctypes.c_void_p
    # FreeSid returns NULL after it releases the allocation, so its return value is not a
    # Boolean success signal.  Correct pointer-width declarations are essential here.
    free_sid(ctypes.c_void_p(sid))


def _pointer_value(value: Any) -> int:
    if isinstance(value, int):
        return value
    if value is None:
        return 0
    return int(ctypes.cast(value, ctypes.c_void_p).value or 0)


def _local_free(local_free: Any, value: int | None) -> None:
    if value not in (None, 0):
        local_free(ctypes.c_void_p(value))


def _registry_read_capability() -> _CapabilitySet:
    """Derive the one non-network LPAC capability required by the staged CPython runtime."""

    kernel32 = _windows_dll("kernel32")
    kernelbase = _windows_dll("kernelbase")
    local_free = kernel32.LocalFree
    local_free.argtypes = (ctypes.c_void_p,)
    local_free.restype = ctypes.c_void_p
    derive = kernelbase.DeriveCapabilitySidsFromName
    derive.argtypes = (
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
        ctypes.POINTER(ctypes.c_ulong),
    )
    derive.restype = ctypes.c_int
    groups: Any = ctypes.POINTER(ctypes.c_void_p)()
    capability_sids: Any = ctypes.POINTER(ctypes.c_void_p)()
    group_count = ctypes.c_ulong()
    capability_count = ctypes.c_ulong()
    capability_sid = 0
    try:
        if not derive(
            "registryRead",
            ctypes.byref(groups),
            ctypes.byref(group_count),
            ctypes.byref(capability_sids),
            ctypes.byref(capability_count),
        ):
            _raise_sandbox_unavailable()
        if group_count.value != 1 or capability_count.value != 1:
            _raise_sandbox_unavailable()
        group_sid = _pointer_value(groups[0])
        capability_sid = _pointer_value(capability_sids[0])
        if group_sid == 0 or capability_sid == 0:
            _raise_sandbox_unavailable()
        _local_free(local_free, group_sid)
        _local_free(local_free, _pointer_value(groups))
        groups = None
        _local_free(local_free, _pointer_value(capability_sids))
        capability_sids = None
        entries = (_SidAndAttributes * 1)(
            _SidAndAttributes(
                Sid=ctypes.c_void_p(capability_sid),
                Attributes=_WINDOWS_SE_GROUP_ENABLED,
            )
        )
        return _CapabilitySet(
            sid=capability_sid,
            entries=entries,
            local_free=local_free,
        )
    except BaseException:
        if groups is not None:
            for index in range(group_count.value):
                _local_free(local_free, _pointer_value(groups[index]))
            _local_free(local_free, _pointer_value(groups))
        if capability_sids is not None:
            for index in range(capability_count.value):
                _local_free(local_free, _pointer_value(capability_sids[index]))
            _local_free(local_free, _pointer_value(capability_sids))
        elif capability_sid != 0:
            _local_free(local_free, capability_sid)
        _raise_sandbox_unavailable()


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        _raise_sandbox_unavailable()
    return bool(attributes & _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT)


def _absolute_path_without_reparse_components(path: Path) -> Path:
    """Return an absolute path only after every existing component passed lstat."""

    absolute_path = path.absolute()
    path_parts = absolute_path.parts
    if not path_parts:
        _raise_sandbox_unavailable()
    current_component = Path(path_parts[0])
    if _is_reparse_point(current_component):
        _raise_sandbox_unavailable()
    for part in path_parts[1:]:
        current_component = current_component / part
        if _is_reparse_point(current_component):
            _raise_sandbox_unavailable()
    return absolute_path


def _canonical_public_root(root: Path) -> Path:
    """Canonicalize a trusted installed-root junction, then reject nested reparses."""

    try:
        canonical_root = root.resolve(strict=True)
    except OSError:
        _raise_sandbox_unavailable()
    return _absolute_path_without_reparse_components(canonical_root)


def _assert_public_tree_has_no_reparse_points(root: Path) -> None:
    """Refuse to materialize a symlink/junction target in the LPAC-readable stage."""

    absolute_root = _absolute_path_without_reparse_components(root)
    if not absolute_root.is_dir():
        _raise_sandbox_unavailable()

    def walk_error(_: OSError) -> NoReturn:
        _raise_sandbox_unavailable()

    for current, directory_names, file_names in os.walk(
        absolute_root,
        followlinks=False,
        onerror=walk_error,
    ):
        current_path = Path(current)
        for name in (*directory_names, *file_names):
            if _is_reparse_point(current_path / name):
                _raise_sandbox_unavailable()


def _copy_public_tree(source: Path, destination: Path) -> None:
    canonical_source = _canonical_public_root(source)
    _assert_public_tree_has_no_reparse_points(canonical_source)
    shutil.copytree(
        canonical_source,
        destination,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )


def _copy_public_worker_package(source_root: Path, destination: Path) -> None:
    """Stage the worker's closed public Python-module set, not the whole project."""

    canonical_source_root = _canonical_public_root(source_root)
    for relative_path in _PUBLIC_WORKER_PACKAGE_FILES:
        if relative_path.is_absolute() or ".." in relative_path.parts:
            _raise_sandbox_unavailable()
        source = _absolute_path_without_reparse_components(canonical_source_root / relative_path)
        if not source.is_file():
            _raise_sandbox_unavailable()
        target = destination / relative_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target, follow_symlinks=False)
        except OSError:
            _raise_sandbox_unavailable()


def _write_isolated_python_path_file(interpreter_directory: Path) -> None:
    """Prevent CPython from accepting ambient environment/module search paths."""

    version = f"{sys.version_info.major}{sys.version_info.minor}"
    path_file = interpreter_directory / f"python{version}._pth"
    path_file.write_text(
        f"python{version}.zip\n.\nLib\nDLLs\n..\\site\n",
        encoding="utf-8",
    )


def _stage_public_runtime(profile_local_data: Path) -> _StagedRuntime:
    """Copy public runtime code into the new LPAC profile's private local-data tree."""

    if not profile_local_data.is_dir():
        _raise_sandbox_unavailable()
    root = Path(tempfile.mkdtemp(prefix=_STAGED_RUNTIME_PREFIX, dir=profile_local_data))
    try:
        base_prefix = Path(sys.base_prefix)
        interpreter_source = base_prefix / "python.exe"
        if not interpreter_source.is_file():
            _raise_sandbox_unavailable()
        site_root = root / "site"
        _copy_public_tree(base_prefix, root / "python")
        _write_isolated_python_path_file(root / "python")
        package_root = Path(__file__).parents[1]
        _copy_public_worker_package(package_root, site_root / "pta_finance")
        distribution = metadata.distribution("pypdfium2")
        for package_name in ("pypdfium2", "pypdfium2_raw", "pypdfium2_cfg"):
            package_path = Path(str(distribution.locate_file(package_name)))
            if package_path.is_dir():
                _copy_public_tree(package_path, site_root / package_name)
        if not (site_root / "pypdfium2").is_dir():
            _raise_sandbox_unavailable()
        interpreter = root / "python" / "python.exe"
        if not interpreter.is_file():
            _raise_sandbox_unavailable()
        return _StagedRuntime(root=root, interpreter=interpreter, site_root=site_root)
    except Exception:
        try:
            _remove_runtime(root)
        except NativeSandboxUnavailable:
            # The caller has not received this path, so retain it here rather than
            # dropping the only cleanup ownership on a transient filesystem failure.
            _defer_startup_artifacts(runtime=root, profile_name=None)
        _raise_sandbox_unavailable()


def _remove_runtime(root: Path) -> None:
    try:
        if root.exists():
            shutil.rmtree(root)
    except OSError:
        _raise_sandbox_unavailable()


def _grant_runtime_access(runtime: _StagedRuntime, sid_text: str) -> None:
    """Give only this run's AppContainer SID read/execute access to public staged files."""

    kernel32, _, _, _ = _windows_apis()
    windows_directory = _trusted_windows_directory(kernel32)
    icacls = windows_directory / "System32" / "icacls.exe"
    if not icacls.is_file():
        _raise_sandbox_unavailable()
    try:
        result = subprocess.run(
            [
                str(icacls),
                str(runtime.root),
                "/grant:r",
                f"*{sid_text}:(OI)(CI)(RX)",
                "/T",
                "/C",
            ],
            check=False,
            capture_output=True,
            text=False,
            cwd=str(windows_directory),
            env={
                "ComSpec": str(windows_directory / "System32" / "cmd.exe"),
                "SystemRoot": str(windows_directory),
                "WINDIR": str(windows_directory),
            },
        )
    except OSError:
        _raise_sandbox_unavailable()
    if result.returncode != 0:
        _raise_sandbox_unavailable()


def _create_app_container_profile() -> _AppContainerProfile:
    kernel32, advapi32, userenv, ole32 = _windows_apis()
    del kernel32
    create_profile = userenv.CreateAppContainerProfile
    create_profile.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.POINTER(_SidAndAttributes),
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_void_p),
    )
    create_profile.restype = ctypes.c_long
    name = f"{_APP_CONTAINER_PROFILE_PREFIX}-{secrets.token_hex(16)}"
    capabilities = _registry_read_capability()
    sid = ctypes.c_void_p()
    profile_created = False
    try:
        if (
            create_profile(
                name,
                name,
                "PTA Finance native statement worker",
                ctypes.cast(capabilities.entries, ctypes.POINTER(_SidAndAttributes)),
                1,
                ctypes.byref(sid),
            )
            != 0
        ):
            _raise_sandbox_unavailable()
        profile_created = True
        sid_value = sid.value
        if sid_value is None or sid_value == 0:
            _raise_sandbox_unavailable()
        sid_text = _sid_to_text(advapi32, int(sid_value))
        local_data = _app_container_local_data(userenv, ole32, sid_text)
        return _AppContainerProfile(
            name=name,
            sid=int(sid_value),
            sid_text=sid_text,
            local_data=local_data,
            capabilities=capabilities,
        )
    except Exception:
        sid_value = sid.value
        if sid_value is not None and sid_value != 0:
            _free_sid(advapi32, sid_value)
        if profile_created:
            try:
                _delete_app_container_profile(name)
            except NativeSandboxUnavailable:
                # No caller can know this generated profile name when creation itself
                # fails.  Keep an explicit retry owner instead of leaving it orphaned.
                _defer_startup_artifacts(runtime=None, profile_name=name)
        capabilities.close()
        raise


def _sid_to_text(advapi32: Any, sid: int) -> str:
    kernel32, _, _, _ = _windows_apis()
    converter = advapi32.ConvertSidToStringSidW
    converter.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p))
    converter.restype = ctypes.c_int
    value = ctypes.c_wchar_p()
    if not converter(ctypes.c_void_p(sid), ctypes.byref(value)) or not value.value:
        _raise_sandbox_unavailable()
    try:
        result = value.value
        if result is None:
            _raise_sandbox_unavailable()
        return result
    finally:
        local_free = kernel32.LocalFree
        local_free(ctypes.cast(value, ctypes.c_void_p))


def _app_container_local_data(userenv: Any, ole32: Any, sid_text: str) -> Path:
    getter = userenv.GetAppContainerFolderPath
    getter.argtypes = (ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_wchar_p))
    getter.restype = ctypes.c_long
    value = ctypes.c_wchar_p()
    if getter(sid_text, ctypes.byref(value)) != 0 or not value.value:
        _raise_sandbox_unavailable()
    try:
        result = value.value
        if result is None:
            _raise_sandbox_unavailable()
        return Path(result)
    finally:
        co_task_mem_free = ole32.CoTaskMemFree
        co_task_mem_free(ctypes.cast(value, ctypes.c_void_p))


def _delete_app_container_profile(name: str) -> None:
    _, _, userenv, _ = _windows_apis()
    delete_profile = userenv.DeleteAppContainerProfile
    delete_profile.argtypes = (ctypes.c_wchar_p,)
    delete_profile.restype = ctypes.c_long
    if delete_profile(name) != 0:
        _raise_sandbox_unavailable()


def _trusted_windows_directory(kernel32: Any) -> Path:
    """Resolve the OS directory from Win32, never from a caller-controlled environment."""

    getter = kernel32.GetWindowsDirectoryW
    getter.argtypes = (ctypes.c_wchar_p, ctypes.c_uint)
    getter.restype = ctypes.c_uint
    buffer_size = 260
    for _ in range(4):
        buffer = ctypes.create_unicode_buffer(buffer_size)
        length = getter(buffer, buffer_size)
        if length == 0:
            _raise_sandbox_unavailable()
        if length < buffer_size:
            directory = Path(buffer.value)
            if directory.is_dir() and (directory / "System32" / "cmd.exe").is_file():
                return directory
            _raise_sandbox_unavailable()
        buffer_size = int(length) + 1
    _raise_sandbox_unavailable()


def _build_environment(
    profile: _AppContainerProfile,
    runtime: _StagedRuntime,
    windows_directory: Path,
) -> ctypes.Array[ctypes.c_wchar]:
    system_path = windows_directory
    interpreter_directory = runtime.interpreter.parent
    if (
        not system_path.is_dir()
        or not profile.local_data.is_dir()
        or not interpreter_directory.is_dir()
    ):
        _raise_sandbox_unavailable()
    values = {
        "APPDATA": str(profile.local_data),
        "ComSpec": str(system_path / "System32" / "cmd.exe"),
        "LOCALAPPDATA": str(profile.local_data),
        "Path": f"{interpreter_directory}{os.pathsep}{system_path / 'System32'}",
        "SystemRoot": str(system_path),
        "TEMP": str(profile.local_data),
        "TMP": str(profile.local_data),
        "USERPROFILE": str(profile.local_data),
        "WINDIR": str(system_path),
    }
    block = "\0".join(
        f"{key}={value}" for key, value in sorted(values.items(), key=lambda item: item[0].lower())
    )
    return ctypes.create_unicode_buffer(f"{block}\0\0")


def _make_job_object(kernel32: Any, *, memory_bytes: int, cpu_seconds: int) -> int:
    if memory_bytes < 1 or cpu_seconds < 1:
        _raise_sandbox_unavailable()
    creator = kernel32.CreateJobObjectW
    creator.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
    creator.restype = ctypes.c_void_p
    setter = kernel32.SetInformationJobObject
    setter.argtypes = (ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong)
    setter.restype = ctypes.c_int
    handle = creator(None, None)
    if not handle:
        _raise_sandbox_unavailable()
    handle_value = int(handle)
    try:
        information = _ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = (
            _WINDOWS_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | _WINDOWS_JOB_OBJECT_LIMIT_PROCESS_TIME
            | _WINDOWS_JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | _WINDOWS_JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | _WINDOWS_JOB_OBJECT_LIMIT_JOB_MEMORY
            | _WINDOWS_JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        )
        information.BasicLimitInformation.PerProcessUserTimeLimit = (
            cpu_seconds * _WINDOWS_HUNDRED_NANOSECONDS_PER_SECOND
        )
        information.BasicLimitInformation.ActiveProcessLimit = 1
        information.ProcessMemoryLimit = memory_bytes
        information.JobMemoryLimit = memory_bytes
        if not setter(
            ctypes.c_void_p(handle_value),
            _WINDOWS_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            _raise_sandbox_unavailable()
        return handle_value
    except Exception:
        try:
            _close_handle(kernel32, handle_value)
        except NativeSandboxUnavailable:
            # No child exists yet, but retain the Job handle rather than silently
            # abandoning it if the close itself transiently fails.
            _defer_sandbox_process(
                NativeSandboxProcess(
                    kernel32=kernel32,
                    process_handle=None,
                    job_handle=handle_value,
                    runtime=None,
                    profile_name=None,
                )
            )
        raise


def _make_attribute_list(kernel32: Any, count: int) -> tuple[Any, int]:
    initializer = kernel32.InitializeProcThreadAttributeList
    initializer.argtypes = (
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_size_t),
    )
    initializer.restype = ctypes.c_int
    size = ctypes.c_size_t()
    if initializer(None, count, 0, ctypes.byref(size)) or (
        _windows_last_error() != _WINDOWS_ERROR_INSUFFICIENT_BUFFER
    ):
        _raise_sandbox_unavailable()
    if size.value < 1:
        _raise_sandbox_unavailable()
    storage = ctypes.create_string_buffer(size.value)
    if not initializer(ctypes.byref(storage), count, 0, ctypes.byref(size)):
        _raise_sandbox_unavailable()
    return storage, ctypes.addressof(storage)


def _update_attribute(
    kernel32: Any,
    attribute_list: int,
    attribute: int,
    value: Any,
    size: int,
) -> None:
    updater = kernel32.UpdateProcThreadAttribute
    updater.argtypes = (
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    updater.restype = ctypes.c_int
    if not updater(
        ctypes.c_void_p(attribute_list),
        0,
        attribute,
        ctypes.byref(value),
        size,
        None,
        None,
    ):
        _raise_sandbox_unavailable()


def _delete_attribute_list(kernel32: Any, attribute_list: int | None) -> None:
    if attribute_list is None:
        return
    deleter = kernel32.DeleteProcThreadAttributeList
    deleter(ctypes.c_void_p(attribute_list))


def _worker_command(
    runtime: _StagedRuntime,
    *,
    control_mapping: str,
    attested_event: str,
    handles_ready_event: str,
    document_ordinal: int,
    limits_json: str,
    ready_nonce: str,
) -> list[str]:
    runner = (
        "import sys; "
        f"sys.path.insert(0, {str(runtime.site_root)!r}); "
        "from pta_finance.treasurer_slides.native_worker import main; "
        "raise SystemExit(main())"
    )
    return [
        str(runtime.interpreter),
        "-I",
        "-S",
        "-B",
        "-c",
        runner,
        "--control-mapping",
        control_mapping,
        "--attested-event",
        attested_event,
        "--handles-ready-event",
        handles_ready_event,
        "--document-ordinal",
        str(document_ordinal),
        "--limits-json",
        limits_json,
        "--ready-nonce",
        ready_nonce,
    ]


def _current_process_user_sid_text() -> str:
    """Return the broker identity needed by an AppContainer restricted-token check."""

    kernel32, advapi32, _, _ = _windows_apis()
    current_process = kernel32.GetCurrentProcess()
    token = ctypes.c_void_p()
    opener = advapi32.OpenProcessToken
    opener.argtypes = (ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p))
    opener.restype = ctypes.c_int
    if not opener(current_process, _WINDOWS_TOKEN_QUERY, ctypes.byref(token)):
        _raise_sandbox_unavailable()
    try:
        returned = ctypes.c_ulong()
        getter = advapi32.GetTokenInformation
        getter.argtypes = (
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
        )
        getter.restype = ctypes.c_int
        if (
            getter(
                token,
                _WINDOWS_TOKEN_USER,
                None,
                0,
                ctypes.byref(returned),
            )
            or _windows_last_error() != _WINDOWS_ERROR_INSUFFICIENT_BUFFER
        ):
            _raise_sandbox_unavailable()
        if returned.value < ctypes.sizeof(_SidAndAttributes):
            _raise_sandbox_unavailable()
        buffer = ctypes.create_string_buffer(returned.value)
        if not getter(
            token,
            _WINDOWS_TOKEN_USER,
            buffer,
            returned.value,
            ctypes.byref(returned),
        ):
            _raise_sandbox_unavailable()
        sid = ctypes.cast(buffer, ctypes.POINTER(_SidAndAttributes)).contents.Sid
        sid_value = _pointer_value(sid)
        if sid_value == 0:
            _raise_sandbox_unavailable()
        return _sid_to_text(advapi32, sid_value)
    finally:
        _close_handle(kernel32, _pointer_value(token))


def _control_security_descriptor(profile_sid_text: str) -> int:
    """Allocate a Low-IL DACL for public control metadata, not PDF-channel handles."""

    if not profile_sid_text.startswith("S-1-15-2-"):
        _raise_sandbox_unavailable()
    _, advapi32, _, _ = _windows_apis()
    converter = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    converter.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_ulong),
    )
    converter.restype = ctypes.c_int
    descriptor = ctypes.c_void_p()
    # Restricted-token checks require both the base user SID and the AppContainer SID
    # to be granted access.  LPAC is Low IL, so these public startup objects need a
    # Low mandatory label.  Their payload contains only generated names, nonce, and
    # child-local handle numbers; the private PDF channel remains anonymous.
    user_sid_text = _current_process_user_sid_text()
    sddl = f"D:P(A;;GA;;;{user_sid_text})(A;;GA;;;{profile_sid_text})S:(ML;;NW;;;LW)"
    if not converter(
        sddl,
        _WINDOWS_SECURITY_DESCRIPTOR_REVISION,
        ctypes.byref(descriptor),
        None,
    ):
        _raise_sandbox_unavailable()
    descriptor_value = descriptor.value
    if descriptor_value is None or descriptor_value == 0:
        _raise_sandbox_unavailable()
    return int(descriptor_value)


def _remaining_timeout_milliseconds(deadline: float) -> int:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _raise_sandbox_unavailable()
    return max(1, min(_WINDOWS_INFINITE - 1, int(remaining * 1000)))


def _worker_control_name(role: str) -> str:
    if role not in {"mapping", "attested", "handles-ready"}:
        _raise_sandbox_unavailable()
    return f"{_WINDOWS_CONTROL_NAME_PREFIX}{secrets.token_hex(32)}-{role}"


def _create_worker_control_channel(profile_sid_text: str) -> _WorkerControlChannel:
    """Create only public, non-inheritable control objects for the LPAC handshake."""

    kernel32, _, _, _ = _windows_apis()
    channel = _WorkerControlChannel(
        mapping_name=_worker_control_name("mapping"),
        attested_event_name=_worker_control_name("attested"),
        handles_ready_event_name=_worker_control_name("handles-ready"),
        mapping_handle=None,
        mapping_view=None,
        attested_event_handle=None,
        handles_ready_event_handle=None,
    )
    descriptor = _control_security_descriptor(profile_sid_text)
    attributes = _SecurityAttributes(
        nLength=ctypes.sizeof(_SecurityAttributes),
        lpSecurityDescriptor=ctypes.c_void_p(descriptor),
        bInheritHandle=0,
    )
    try:
        mapping_creator = kernel32.CreateFileMappingW
        mapping_creator.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(_SecurityAttributes),
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_wchar_p,
        )
        mapping_creator.restype = ctypes.c_void_p
        mapping_handle = _pointer_value(
            mapping_creator(
                ctypes.c_void_p(_WINDOWS_INVALID_HANDLE_VALUE),
                ctypes.byref(attributes),
                _WINDOWS_PAGE_READWRITE,
                0,
                _WINDOWS_CONTROL_MAPPING_BYTES,
                channel.mapping_name,
            )
        )
        if mapping_handle in (0, _WINDOWS_INVALID_HANDLE_VALUE):
            _raise_sandbox_unavailable()
        channel.mapping_handle = mapping_handle
        mapper = kernel32.MapViewOfFile
        mapper.argtypes = (
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_size_t,
        )
        mapper.restype = ctypes.c_void_p
        mapping_view = _pointer_value(
            mapper(
                ctypes.c_void_p(mapping_handle),
                _WINDOWS_FILE_MAP_WRITE,
                0,
                0,
                _WINDOWS_CONTROL_MAPPING_BYTES,
            )
        )
        if mapping_view == 0:
            _raise_sandbox_unavailable()
        channel.mapping_view = mapping_view
        event_creator = kernel32.CreateEventW
        event_creator.argtypes = (
            ctypes.POINTER(_SecurityAttributes),
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_wchar_p,
        )
        event_creator.restype = ctypes.c_void_p
        attested_event = _pointer_value(
            event_creator(ctypes.byref(attributes), 1, 0, channel.attested_event_name)
        )
        if attested_event in (0, _WINDOWS_INVALID_HANDLE_VALUE):
            _raise_sandbox_unavailable()
        channel.attested_event_handle = attested_event
        handles_ready_event = _pointer_value(
            event_creator(ctypes.byref(attributes), 1, 0, channel.handles_ready_event_name)
        )
        if handles_ready_event in (
            0,
            _WINDOWS_INVALID_HANDLE_VALUE,
        ):
            _raise_sandbox_unavailable()
        channel.handles_ready_event_handle = handles_ready_event
        return channel
    except BaseException:
        try:
            _close_worker_control_channel(channel)
        except BaseException:
            pass
        raise
    finally:
        local_free = kernel32.LocalFree
        local_free.argtypes = (ctypes.c_void_p,)
        local_free.restype = ctypes.c_void_p
        local_free(ctypes.c_void_p(descriptor))


def _wait_for_control_event(handle: int | None, *, deadline: float) -> None:
    if handle is None:
        _raise_sandbox_unavailable()
    kernel32, _, _, _ = _windows_apis()
    waiter = kernel32.WaitForSingleObject
    waiter.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
    waiter.restype = ctypes.c_ulong
    if (
        waiter(ctypes.c_void_p(handle), _remaining_timeout_milliseconds(deadline))
        != _WINDOWS_WAIT_OBJECT_0
    ):
        _raise_sandbox_unavailable()


def _write_worker_pipe_handles(
    channel: _WorkerControlChannel,
    *,
    request_handle: int,
    response_handle: int,
    nonce: str,
) -> None:
    if (
        channel.mapping_view is None
        or request_handle < 1
        or response_handle < 1
        or len(nonce) != _READY_NONCE_BYTES * 2
    ):
        _raise_sandbox_unavailable()
    try:
        nonce_bytes = nonce.encode("ascii", "strict")
    except UnicodeEncodeError:
        _raise_sandbox_unavailable()
    payload = _CONTROL_HANDLES_FRAME.pack(
        _CONTROL_HANDLES_MAGIC,
        nonce_bytes,
        request_handle,
        response_handle,
    )
    if len(payload) > _WINDOWS_CONTROL_MAPPING_BYTES:
        _raise_sandbox_unavailable()
    ctypes.memset(channel.mapping_view, 0, _WINDOWS_CONTROL_MAPPING_BYTES)
    ctypes.memmove(channel.mapping_view, payload, len(payload))


def _signal_control_event(handle: int | None) -> None:
    if handle is None:
        _raise_sandbox_unavailable()
    kernel32, _, _, _ = _windows_apis()
    setter = kernel32.SetEvent
    setter.argtypes = (ctypes.c_void_p,)
    setter.restype = ctypes.c_int
    if not setter(ctypes.c_void_p(handle)):
        _raise_sandbox_unavailable()


def _close_worker_control_channel(channel: _WorkerControlChannel | None) -> None:
    if channel is None:
        return
    kernel32, _, _, _ = _windows_apis()
    cleanup_error = False
    view = channel.mapping_view
    if view is not None:
        channel.mapping_view = None
        unmapper = kernel32.UnmapViewOfFile
        unmapper.argtypes = (ctypes.c_void_p,)
        unmapper.restype = ctypes.c_int
        if not unmapper(ctypes.c_void_p(view)):
            cleanup_error = True
    for attribute in (
        "mapping_handle",
        "attested_event_handle",
        "handles_ready_event_handle",
    ):
        handle = getattr(channel, attribute)
        if handle is None:
            continue
        setattr(channel, attribute, None)
        try:
            _close_handle(kernel32, handle)
        except NativeSandboxUnavailable:
            cleanup_error = True
    if cleanup_error:
        _raise_sandbox_unavailable()


class NativeSandboxProcess:
    """A process-like owner that closes the job, profile, and public runtime together."""

    def __init__(
        self,
        *,
        kernel32: Any,
        process_handle: int | None,
        job_handle: int | None,
        runtime: _StagedRuntime | None,
        profile_name: str | None,
        process_id: int | None = None,
    ) -> None:
        self._kernel32 = kernel32
        self._process_handle: int | None = process_handle
        self._job_handle: int | None = job_handle
        self._runtime: Path | None = None if runtime is None else runtime.root
        self._profile_name: str | None = profile_name
        self._process_id: int | None = process_id
        self._cleanup_lock = threading.Lock()

    @property
    def process_id(self) -> int:
        """Return the OS PID whose token is verified before direct handle transfer."""

        if self._process_id is None or self._process_id < 1:
            _raise_sandbox_unavailable()
        return self._process_id

    def join(self, timeout: float | None = None) -> None:
        handle = self._process_handle
        if handle is None:
            return
        wait = self._kernel32.WaitForSingleObject
        wait.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        wait.restype = ctypes.c_ulong
        if timeout is None:
            milliseconds = _WINDOWS_INFINITE
        else:
            milliseconds = max(0, min(_WINDOWS_INFINITE - 1, int(timeout * 1000)))
        result = wait(ctypes.c_void_p(handle), milliseconds)
        if result not in (_WINDOWS_WAIT_OBJECT_0, _WINDOWS_WAIT_TIMEOUT):
            _raise_sandbox_unavailable()

    def is_alive(self) -> bool:
        handle = self._process_handle
        if handle is None:
            return False
        wait = self._kernel32.WaitForSingleObject
        wait.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        wait.restype = ctypes.c_ulong
        result = wait(ctypes.c_void_p(handle), 0)
        if result == _WINDOWS_WAIT_TIMEOUT:
            return True
        if result == _WINDOWS_WAIT_OBJECT_0:
            return False
        _raise_sandbox_unavailable()

    def terminate(self) -> None:
        self._terminate_job()

    def kill(self) -> None:
        self._terminate_job()

    def _terminate_job(self) -> None:
        handle = self._job_handle
        if handle is None:
            return
        terminator = self._kernel32.TerminateJobObject
        terminator.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        terminator.restype = ctypes.c_int
        if not terminator(ctypes.c_void_p(handle), 1):
            _raise_sandbox_unavailable()

    def close(self) -> None:
        try:
            with self._cleanup_lock:
                self._close_once()
        except BaseException:
            # A caller may be about to convert this into a generic extraction failure.
            # Keep the job/process/profile owner alive for bounded retries instead of
            # losing the only handles that can prove the child has exited.
            _defer_sandbox_process(self)
            raise

    def _retry_close_once(self) -> None:
        """Retry a retained owner without racing another close on its raw handles."""

        with self._cleanup_lock:
            self._close_once()

    def _close_once(self) -> None:
        cleanup_error = False
        job_handle = self._job_handle
        process_handle = self._process_handle
        if job_handle is not None:
            try:
                self._terminate_handle(job_handle)
            except NativeSandboxUnavailable:
                cleanup_error = True
        elif process_handle is not None:
            try:
                self._terminate_process_handle(process_handle)
            except NativeSandboxUnavailable:
                cleanup_error = True
        if process_handle is not None:
            try:
                if not self._wait_for_exit(process_handle, 5.0):
                    cleanup_error = True
            except NativeSandboxUnavailable:
                cleanup_error = True
        if cleanup_error:
            _raise_sandbox_unavailable()
        if process_handle is not None:
            try:
                _close_handle(self._kernel32, process_handle)
            except NativeSandboxUnavailable:
                _raise_sandbox_unavailable()
            self._process_handle = None
        if job_handle is not None:
            try:
                _close_handle(self._kernel32, job_handle)
            except NativeSandboxUnavailable:
                _raise_sandbox_unavailable()
            self._job_handle = None
        runtime = self._runtime
        if runtime is not None:
            _remove_runtime(runtime)
            self._runtime = None
        profile_name = self._profile_name
        if profile_name is not None:
            if not self._delete_profile_with_retry(profile_name):
                _raise_sandbox_unavailable()
            self._profile_name = None

    def _delete_profile_with_retry(self, profile_name: str) -> bool:
        return _delete_profile_with_retry(profile_name)

    def _terminate_handle(self, handle: int) -> None:
        terminator = self._kernel32.TerminateJobObject
        terminator.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        terminator.restype = ctypes.c_int
        if not terminator(ctypes.c_void_p(handle), 1):
            _raise_sandbox_unavailable()

    def _terminate_process_handle(self, handle: int) -> None:
        terminator = self._kernel32.TerminateProcess
        terminator.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        terminator.restype = ctypes.c_int
        if not terminator(ctypes.c_void_p(handle), 1):
            _raise_sandbox_unavailable()

    def _wait_for_exit(self, handle: int, timeout: float) -> bool:
        wait = self._kernel32.WaitForSingleObject
        wait.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        wait.restype = ctypes.c_ulong
        result = wait(ctypes.c_void_p(handle), int(timeout * 1000))
        if result == _WINDOWS_WAIT_OBJECT_0:
            return True
        if result == _WINDOWS_WAIT_TIMEOUT:
            return False
        _raise_sandbox_unavailable()


def _token_app_container_sid_matches(
    advapi32: Any,
    token: ctypes.c_void_p,
    expected_sid: int,
) -> bool:
    if expected_sid == 0:
        return False
    returned = ctypes.c_ulong()
    getter = advapi32.GetTokenInformation
    getter.argtypes = (
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    )
    getter.restype = ctypes.c_int
    if (
        getter(
            token,
            _WINDOWS_TOKEN_APP_CONTAINER_SID,
            None,
            0,
            ctypes.byref(returned),
        )
        or _windows_last_error() != _WINDOWS_ERROR_INSUFFICIENT_BUFFER
    ):
        return False
    if returned.value < ctypes.sizeof(_TokenAppContainerInformation):
        return False
    buffer = ctypes.create_string_buffer(returned.value)
    if not getter(
        token,
        _WINDOWS_TOKEN_APP_CONTAINER_SID,
        buffer,
        returned.value,
        ctypes.byref(returned),
    ):
        return False
    actual_sid = ctypes.cast(
        buffer, ctypes.POINTER(_TokenAppContainerInformation)
    ).contents.TokenAppContainer
    if _pointer_value(actual_sid) == 0:
        return False
    equal_sid = advapi32.EqualSid
    equal_sid.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    equal_sid.restype = ctypes.c_int
    return bool(equal_sid(actual_sid, ctypes.c_void_p(expected_sid)))


def _attest_launched_worker_before_handle_transfer(
    process: NativeSandboxProcess,
    *,
    expected_profile_sid: int,
) -> None:
    """Verify the exact child token and Job before it can receive PDF-channel handles."""

    process_handle = process._process_handle
    job_handle = process._job_handle
    if process_handle is None or job_handle is None or expected_profile_sid == 0:
        _raise_sandbox_unavailable()
    kernel32, advapi32, _, _ = _windows_apis()
    token = ctypes.c_void_p()
    opener = advapi32.OpenProcessToken
    opener.argtypes = (ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p))
    opener.restype = ctypes.c_int
    if not opener(ctypes.c_void_p(process_handle), _WINDOWS_TOKEN_QUERY, ctypes.byref(token)):
        _raise_sandbox_unavailable()
    try:
        value = ctypes.c_ulong()
        returned = ctypes.c_ulong()
        getter = advapi32.GetTokenInformation
        getter.argtypes = (
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
        )
        getter.restype = ctypes.c_int
        if (
            not getter(
                token,
                _WINDOWS_TOKEN_IS_APP_CONTAINER,
                ctypes.byref(value),
                ctypes.sizeof(value),
                ctypes.byref(returned),
            )
            or value.value == 0
            or not _token_app_container_sid_matches(advapi32, token, expected_profile_sid)
        ):
            _raise_sandbox_unavailable()
        # This imports the broker's installed, trusted verifier rather than anything
        # inside the staged worker runtime.  It proves the exact LPAC capability and
        # no-All-Application-Packages token property before direct duplication.
        from pta_finance.treasurer_slides import native_worker

        if not native_worker._has_only_registry_read_capability(advapi32, kernel32, token):
            _raise_sandbox_unavailable()
        if not native_worker._has_no_all_application_packages_policy(advapi32, token):
            _raise_sandbox_unavailable()
        in_job = ctypes.c_int()
        is_in_job = kernel32.IsProcessInJob
        is_in_job.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
        )
        is_in_job.restype = ctypes.c_int
        if (
            not is_in_job(
                ctypes.c_void_p(process_handle),
                ctypes.c_void_p(job_handle),
                ctypes.byref(in_job),
            )
            or not in_job.value
        ):
            _raise_sandbox_unavailable()
    finally:
        _close_handle(kernel32, _pointer_value(token))


def _duplicate_handle_into_worker(source_handle: int, process: NativeSandboxProcess) -> int:
    """Grant one non-inheritable endpoint directly to the already-attested LPAC PID."""

    target_process_handle = process._process_handle
    if source_handle < 1 or target_process_handle is None:
        _raise_sandbox_unavailable()
    kernel32, _, _, _ = _windows_apis()
    duplicate = kernel32.DuplicateHandle
    duplicate.argtypes = (
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_ulong,
    )
    duplicate.restype = ctypes.c_int
    target_handle = ctypes.c_void_p()
    if not duplicate(
        kernel32.GetCurrentProcess(),
        ctypes.c_void_p(source_handle),
        ctypes.c_void_p(target_process_handle),
        ctypes.byref(target_handle),
        0,
        False,
        _WINDOWS_DUPLICATE_SAME_ACCESS,
    ):
        _raise_sandbox_unavailable()
    duplicated = _pointer_value(target_handle)
    if duplicated == 0:
        _raise_sandbox_unavailable()
    return duplicated


class _WorkerLaunchFailure(RuntimeError):
    """Carry the only cleanup owner out of a failed CreateProcess sequence."""

    def __init__(self, process: NativeSandboxProcess) -> None:
        super().__init__()
        self.process = process


_DEFERRED_CLEANUP_LOCK = threading.Lock()
_DEFERRED_SANDBOX_PROCESSES: list[NativeSandboxProcess] = []
_DEFERRED_STARTUP_ARTIFACTS: list[_DeferredStartupArtifacts] = []


def _defer_sandbox_process(process: NativeSandboxProcess) -> None:
    """Retain a failed cleanup owner until its job has demonstrably gone away."""

    with _DEFERRED_CLEANUP_LOCK:
        if not any(existing is process for existing in _DEFERRED_SANDBOX_PROCESSES):
            _DEFERRED_SANDBOX_PROCESSES.append(process)


def _defer_startup_artifacts(*, runtime: Path | None, profile_name: str | None) -> None:
    """Retain public setup artifacts that a failed call otherwise could not return."""

    if runtime is None and profile_name is None:
        return
    with _DEFERRED_CLEANUP_LOCK:
        _DEFERRED_STARTUP_ARTIFACTS.append(
            _DeferredStartupArtifacts(runtime=runtime, profile_name=profile_name)
        )


def _cleanup_startup_artifacts_once(artifacts: _DeferredStartupArtifacts) -> None:
    """Delete public staged files before their generated AppContainer profile."""

    if artifacts.runtime is not None:
        _remove_runtime(artifacts.runtime)
        artifacts.runtime = None
    if artifacts.profile_name is not None:
        if not _delete_profile_with_retry(artifacts.profile_name):
            _raise_sandbox_unavailable()
        artifacts.profile_name = None


def _delete_profile_with_retry(profile_name: str) -> bool:
    for attempt in range(3):
        try:
            _delete_app_container_profile(profile_name)
            return True
        except NativeSandboxUnavailable:
            if attempt < 2:
                time.sleep(0.05)
    return False


def _retry_deferred_sandbox_cleanup() -> bool:
    """Retry prior cleanup before a new parser may be armed with private bytes."""

    with _DEFERRED_CLEANUP_LOCK:
        processes = tuple(_DEFERRED_SANDBOX_PROCESSES)
        artifacts = tuple(_DEFERRED_STARTUP_ARTIFACTS)
    for process in processes:
        try:
            process._retry_close_once()
        except BaseException:
            continue
        with _DEFERRED_CLEANUP_LOCK:
            if process in _DEFERRED_SANDBOX_PROCESSES:
                _DEFERRED_SANDBOX_PROCESSES.remove(process)
    for artifact in artifacts:
        try:
            _cleanup_startup_artifacts_once(artifact)
        except BaseException:
            continue
        with _DEFERRED_CLEANUP_LOCK:
            for index, existing in enumerate(_DEFERRED_STARTUP_ARTIFACTS):
                if existing is artifact:
                    del _DEFERRED_STARTUP_ARTIFACTS[index]
                    break
    with _DEFERRED_CLEANUP_LOCK:
        return not _DEFERRED_SANDBOX_PROCESSES and not _DEFERRED_STARTUP_ARTIFACTS


def _retry_deferred_sandbox_cleanup_at_exit() -> None:
    """Best-effort last cleanup; startup itself remains strict and fail-closed."""

    try:
        _retry_deferred_sandbox_cleanup()
    except BaseException:
        pass


atexit.register(_retry_deferred_sandbox_cleanup_at_exit)


def _create_worker_process_without_inherited_handles(
    creator: Any,
    *,
    application_name: str,
    command_buffer: Any,
    environment: Any,
    working_directory: Path,
    startup: _StartupInfoExW,
    process_information: _ProcessInformation,
) -> None:
    """Call CreateProcessW with inheritance disabled as a testable security invariant."""

    if not creator(
        application_name,
        command_buffer,
        None,
        None,
        False,
        _WINDOWS_CREATE_SUSPENDED
        | _WINDOWS_CREATE_UNICODE_ENVIRONMENT
        | _WINDOWS_CREATE_NO_WINDOW
        | _WINDOWS_EXTENDED_STARTUPINFO_PRESENT,
        environment,
        str(working_directory),
        ctypes.byref(startup.StartupInfo),
        ctypes.byref(process_information),
    ):
        _raise_sandbox_unavailable()


def _launch_worker(
    runtime: _StagedRuntime,
    profile: _AppContainerProfile,
    *,
    control_mapping: str,
    attested_event: str,
    handles_ready_event: str,
    document_ordinal: int,
    limits_json: str,
    ready_nonce: str,
    worker_memory_bytes: int,
    worker_cpu_seconds: int,
) -> NativeSandboxProcess:
    kernel32, _, _, _ = _windows_apis()
    job_handle: int | None = None
    process_handle: int | None = None
    thread_handle: int | None = None
    attribute_list: int | None = None
    try:
        job_handle = _make_job_object(
            kernel32,
            memory_bytes=worker_memory_bytes,
            cpu_seconds=worker_cpu_seconds,
        )
        attribute_storage, attribute_list = _make_attribute_list(kernel32, 3)
        security_capabilities = _SecurityCapabilities(
            AppContainerSid=ctypes.c_void_p(profile.sid),
            Capabilities=ctypes.cast(
                profile.capabilities.entries,
                ctypes.POINTER(_SidAndAttributes),
            ),
            CapabilityCount=1,
            Reserved=0,
        )
        job_list = (ctypes.c_void_p * 1)(job_handle)
        lpac_policy = ctypes.c_ulong(_WINDOWS_PROCESS_CREATION_ALL_APPLICATION_PACKAGES_OPT_OUT)
        _update_attribute(
            kernel32,
            attribute_list,
            _WINDOWS_PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES,
            security_capabilities,
            ctypes.sizeof(security_capabilities),
        )
        _update_attribute(
            kernel32,
            attribute_list,
            _WINDOWS_PROC_THREAD_ATTRIBUTE_JOB_LIST,
            job_list,
            ctypes.sizeof(job_list),
        )
        _update_attribute(
            kernel32,
            attribute_list,
            _WINDOWS_PROC_THREAD_ATTRIBUTE_ALL_APPLICATION_PACKAGES_POLICY,
            lpac_policy,
            ctypes.sizeof(lpac_policy),
        )
        startup = _StartupInfoExW()
        startup.StartupInfo.cb = ctypes.sizeof(_StartupInfoExW)
        startup.lpAttributeList = ctypes.c_void_p(attribute_list)
        process_information = _ProcessInformation()
        command = subprocess.list2cmdline(
            _worker_command(
                runtime,
                control_mapping=control_mapping,
                attested_event=attested_event,
                handles_ready_event=handles_ready_event,
                document_ordinal=document_ordinal,
                limits_json=limits_json,
                ready_nonce=ready_nonce,
            )
        )
        command_buffer = ctypes.create_unicode_buffer(command)
        windows_directory = _trusted_windows_directory(kernel32)
        environment = _build_environment(profile, runtime, windows_directory)
        creator = kernel32.CreateProcessW
        creator.argtypes = (
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.POINTER(_StartupInfoW),
            ctypes.POINTER(_ProcessInformation),
        )
        creator.restype = ctypes.c_int
        _create_worker_process_without_inherited_handles(
            creator,
            application_name=str(runtime.interpreter),
            command_buffer=command_buffer,
            environment=environment,
            working_directory=windows_directory,
            startup=startup,
            process_information=process_information,
        )
        process_handle = int(process_information.hProcess)
        thread_handle = int(process_information.hThread)
        resume = kernel32.ResumeThread
        resume.argtypes = (ctypes.c_void_p,)
        resume.restype = ctypes.c_ulong
        if resume(ctypes.c_void_p(thread_handle)) == 0xFFFFFFFF:
            _raise_sandbox_unavailable()
        _close_handle(kernel32, thread_handle)
        thread_handle = None
        return NativeSandboxProcess(
            kernel32=kernel32,
            process_handle=process_handle,
            job_handle=job_handle,
            runtime=runtime,
            profile_name=profile.name,
            process_id=int(process_information.dwProcessId),
        )
    except BaseException as error:
        if thread_handle is not None:
            try:
                _close_handle(kernel32, thread_handle)
            except NativeSandboxUnavailable:
                pass
        if process_handle is not None or job_handle is not None:
            # Do not terminate and discard raw handles here.  The caller needs the
            # same owner to wait for actual process exit before deleting the public
            # runtime/profile; otherwise a failed READY handshake can race cleanup.
            cleanup_owner = NativeSandboxProcess(
                kernel32=kernel32,
                process_handle=process_handle,
                job_handle=job_handle,
                runtime=runtime,
                profile_name=profile.name,
                process_id=int(process_information.dwProcessId),
            )
            raise _WorkerLaunchFailure(cleanup_owner) from error
        raise
    finally:
        _delete_attribute_list(kernel32, attribute_list)


def _expected_ready_frame(nonce: str) -> bytes:
    return f'{{"status":"ready","nonce":"{nonce}"}}'.encode("ascii")


def _verify_worker_ready(
    connection: NativeSandboxConnection, nonce: str, timeout_seconds: float
) -> None:
    if timeout_seconds <= 0 or not connection.poll(timeout_seconds):
        _raise_sandbox_unavailable()
    try:
        frame = connection.recv_bytes(_READY_FRAME_MAXIMUM_BYTES)
    except (EOFError, OSError):
        _raise_sandbox_unavailable()
    if not hmac.compare_digest(frame, _expected_ready_frame(nonce)):
        _raise_sandbox_unavailable()


def _close_connection(connection: NativeSandboxConnection | None) -> None:
    if connection is None:
        return
    try:
        connection.close()
    except OSError:
        pass


def start_native_pdf_worker(
    *,
    document_ordinal: int,
    limits_json: str,
    worker_memory_bytes: int,
    worker_cpu_seconds: int,
    ready_timeout_seconds: float,
) -> NativeWorkerSession:
    """Start and attest the Windows-only worker before any source bytes may be read."""

    if (
        not isinstance(document_ordinal, int)
        or isinstance(document_ordinal, bool)
        or document_ordinal < 1
        or not isinstance(limits_json, str)
        or not limits_json
        or worker_memory_bytes < 1
        or worker_cpu_seconds < 1
        or ready_timeout_seconds <= 0
    ):
        _raise_sandbox_unavailable()
    if os.name != "nt":
        _raise_sandbox_unavailable()
    # A previously failed cleanup must be resolved before we create another worker
    # that could later receive statement bytes.  This prevents a transient teardown
    # fault from becoming an unbounded collection of inaccessible child processes.
    if not _retry_deferred_sandbox_cleanup():
        _raise_sandbox_unavailable()
    runtime: _StagedRuntime | None = None
    profile: _AppContainerProfile | None = None
    control: _WorkerControlChannel | None = None
    request_receiver: NativeSandboxConnection | None = None
    request_sender: NativeSandboxConnection | None = None
    response_receiver: NativeSandboxConnection | None = None
    response_sender: NativeSandboxConnection | None = None
    process: NativeSandboxProcess | None = None
    profile_sid_owned = False
    try:
        profile = _create_app_container_profile()
        profile_sid_owned = True
        runtime = _stage_public_runtime(profile.local_data)
        _grant_runtime_access(runtime, profile.sid_text)
        control = _create_worker_control_channel(profile.sid_text)
        nonce = secrets.token_hex(_READY_NONCE_BYTES)
        process = _launch_worker(
            runtime,
            profile,
            control_mapping=control.mapping_name,
            attested_event=control.attested_event_name,
            handles_ready_event=control.handles_ready_event_name,
            document_ordinal=document_ordinal,
            limits_json=limits_json,
            ready_nonce=nonce,
            worker_memory_bytes=worker_memory_bytes,
            worker_cpu_seconds=worker_cpu_seconds,
        )
        profile.capabilities.close()
        deadline = time.monotonic() + ready_timeout_seconds
        _wait_for_control_event(
            control.attested_event_handle,
            deadline=deadline,
        )
        _attest_launched_worker_before_handle_transfer(
            process,
            expected_profile_sid=profile.sid,
        )
        _, advapi32, _, _ = _windows_apis()
        _free_sid(advapi32, profile.sid)
        profile_sid_owned = False
        context = multiprocessing.get_context("spawn")
        request_receiver, request_sender = context.Pipe(duplex=False)
        response_receiver, response_sender = context.Pipe(duplex=False)
        request_handle = int(request_receiver.fileno())
        response_handle = int(response_sender.fileno())
        if _windows_handle_is_inheritable(request_handle) or _windows_handle_is_inheritable(
            response_handle
        ):
            _raise_sandbox_unavailable()
        request_child_handle = _duplicate_handle_into_worker(request_handle, process)
        response_child_handle = _duplicate_handle_into_worker(response_handle, process)
        _write_worker_pipe_handles(
            control,
            request_handle=request_child_handle,
            response_handle=response_child_handle,
            nonce=nonce,
        )
        _signal_control_event(control.handles_ready_event_handle)
        _close_worker_control_channel(control)
        control = None
        _close_connection(request_receiver)
        request_receiver = None
        _close_connection(response_sender)
        response_sender = None
        remaining_ready_seconds = deadline - time.monotonic()
        if remaining_ready_seconds <= 0:
            _raise_sandbox_unavailable()
        _verify_worker_ready(response_receiver, nonce, remaining_ready_seconds)
        session = NativeWorkerSession(
            request_sender=request_sender,
            response_receiver=response_receiver,
            process=process,
        )
        request_sender = None
        response_receiver = None
        process = None
        runtime = None
        profile = None
        return session
    except BaseException as error:
        if isinstance(error, _WorkerLaunchFailure):
            # `_launch_worker` is the only frame that owns raw CreateProcess handles
            # before a session exists.  Adopt that owner so cleanup waits for the
            # child before deleting its staged runtime/profile.
            process = error.process
        try:
            _close_worker_control_channel(control)
        except BaseException:
            pass
        _close_connection(request_receiver)
        _close_connection(request_sender)
        _close_connection(response_receiver)
        _close_connection(response_sender)
        if profile is not None:
            try:
                profile.capabilities.close()
            except BaseException:
                pass
        if profile is not None and profile_sid_owned:
            try:
                _, advapi32, _, _ = _windows_apis()
                _free_sid(advapi32, profile.sid)
            except BaseException:
                pass
        if process is not None:
            try:
                process.close()
            except BaseException:
                pass
        else:
            artifacts = _DeferredStartupArtifacts(
                runtime=None if runtime is None else runtime.root,
                profile_name=None if profile is None else profile.name,
            )
            # A nested staging failure may already have retained its runtime path.
            # Drain that owner first so profile deletion never races the public tree.
            if not _retry_deferred_sandbox_cleanup():
                _defer_startup_artifacts(
                    runtime=artifacts.runtime,
                    profile_name=artifacts.profile_name,
                )
            else:
                try:
                    _cleanup_startup_artifacts_once(artifacts)
                except BaseException:
                    _defer_startup_artifacts(
                        runtime=artifacts.runtime,
                        profile_name=artifacts.profile_name,
                    )
        _raise_sandbox_unavailable()
