from __future__ import annotations

import ast
import ctypes
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from pta_finance import process_limits
from pta_finance.treasurer_slides import native_sandbox, native_worker

_ROOT = Path(__file__).resolve().parents[1]
_LEAF = _ROOT / "pta_finance" / "process_limits.py"
_MiB = 1024 * 1024


class _Untouchable:
    """A kernel32 stand-in that fails the test if any Win32 entry point is reached."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"{name} was reached")


class _FakeKernel32:
    """Fictional kernel32 entry points that record their calls."""

    def __init__(self, *, set_ok: bool = True, close_ok: bool = True) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.limits: process_limits._ExtendedLimitInformation | None = None
        set_result, close_result = int(set_ok), int(close_ok)
        fake = self

        def create(*args: Any) -> int:
            fake.calls.append(("CreateJobObjectW", args))
            return 4242

        def set_information(*args: Any) -> int:
            fake.calls.append(("SetInformationJobObject", args))
            pointer = ctypes.POINTER(process_limits._ExtendedLimitInformation)
            fake.limits = ctypes.cast(args[2], pointer).contents
            return set_result

        def close(*args: Any) -> int:
            fake.calls.append(("CloseHandle", (args[0].value,)))
            return close_result

        self.CreateJobObjectW = _Function(create)
        self.SetInformationJobObject = _Function(set_information)
        self.CloseHandle = _Function(close)


class _Function:
    """A callable that accepts the argtypes/restype assignments a ctypes function takes."""

    def __init__(self, body: Any) -> None:
        self._body = body
        self.argtypes: Any = None
        self.restype: Any = None

    def __call__(self, *args: Any) -> Any:
        return self._body(*args)


def _as_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    # The fictional kernel32 above stands in for Windows on any host.
    monkeypatch.setattr(sys, "platform", "win32")


def _layout(structure: type[ctypes.Structure]) -> list[tuple[str, Any, int, int]]:
    """Field names in order, each leaf's scalar ctypes type (nested layouts recursively), offsets
    and sizes — what two separately declared copies must share."""

    rows: list[tuple[str, Any, int, int]] = []
    for name, field_type in structure._fields_:  # type: ignore[misc]
        descriptor = getattr(structure, name)
        if isinstance(field_type, type) and issubclass(field_type, ctypes.Structure):
            kind: Any = ("struct", ctypes.sizeof(field_type), tuple(_layout(field_type)))
        else:
            kind = field_type
        rows.append((name, kind, descriptor.offset, descriptor.size))
    return rows


def test_the_leaf_imports_nothing_from_pta_finance() -> None:
    tree = ast.parse(_LEAF.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "ctypes", "dataclasses", "sys", "typing"}
    script = (
        "import sys\nimport pta_finance.process_limits\n"
        "print(sorted(m for m in sys.modules if m.startswith('pta_finance')))"
    )
    loaded = subprocess.run(
        [sys.executable, "-I", "-c", script], capture_output=True, text=True, check=True
    ).stdout
    assert loaded.strip() == "['pta_finance', 'pta_finance.process_limits']"


def test_job_structures_match_the_native_worker_copy_recursively() -> None:
    # native_worker restates these because the staged LPAC allowlist excludes the leaf; `is` is
    # impossible across that boundary, so the layout is compared field by field instead.
    pairs = [
        (process_limits._IoCounters, native_worker._IoCounters),
        (process_limits._BasicLimitInformation, native_worker._BasicLimitInformation),
        (process_limits._ExtendedLimitInformation, native_worker._ExtendedLimitInformation),
    ]
    for leaf, worker in pairs:
        assert leaf is not worker
        assert _layout(leaf) == _layout(worker)
        assert ctypes.sizeof(leaf) == ctypes.sizeof(worker)
    # The nested structures are each module's own classes, so they are compared by layout too.
    assert ctypes.sizeof(process_limits._ExtendedLimitInformation) == (
        ctypes.sizeof(native_worker._BasicLimitInformation)
        + ctypes.sizeof(native_worker._IoCounters)
        + 4 * ctypes.sizeof(ctypes.c_size_t)
    )


def test_job_flags_match_the_native_worker_copy() -> None:
    names = [
        "KILL_ON_JOB_CLOSE",
        "PROCESS_TIME",
        "ACTIVE_PROCESS",
        "PROCESS_MEMORY",
        "JOB_MEMORY",
        "DIE_ON_UNHANDLED_EXCEPTION",
        "BREAKAWAY_OK",
        "SILENT_BREAKAWAY_OK",
    ]
    for name in names:
        leaf = getattr(process_limits, f"JOB_OBJECT_LIMIT_{name}")
        assert leaf == getattr(native_worker, f"_JOB_OBJECT_LIMIT_{name}"), name
    assert process_limits.JOB_OBJECT_REQUIRED_LIMIT_FLAGS == (
        native_worker._JOB_OBJECT_REQUIRED_LIMIT_FLAGS
    )
    assert process_limits.JOB_OBJECT_FORBIDDEN_LIMIT_FLAGS == (
        native_worker._JOB_OBJECT_FORBIDDEN_LIMIT_FLAGS
    )
    assert process_limits.HUNDRED_NANOSECONDS_PER_SECOND == (
        native_worker._HUNDRED_NANOSECONDS_PER_SECOND
    )
    assert process_limits._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION == (
        native_worker._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION
    )


def test_native_sandbox_uses_the_leaf_rather_than_restating_it() -> None:
    assert native_sandbox.process_limits is process_limits
    restated = [
        name
        for name in vars(native_sandbox)
        if "LimitInformation" in name or name.startswith("_WINDOWS_JOB_OBJECT_")
    ]
    assert restated == []


@pytest.mark.parametrize(
    "limits",
    [
        {"memory_bytes": 0, "cpu_seconds": 1},
        {"memory_bytes": 1, "cpu_seconds": 0},
        {"memory_bytes": 1, "cpu_seconds": 1, "active_processes": 0},
        {"memory_bytes": True, "cpu_seconds": 1},
    ],
    ids=["memory", "cpu", "active-processes", "bool"],
)
def test_limits_below_one_raise_before_any_handle_exists(limits: dict[str, Any]) -> None:
    with pytest.raises(process_limits.ProcessLimitsError) as caught:
        process_limits.make_job_object(_Untouchable(), **limits)
    assert caught.value.winerror_code == 87 and caught.value.errno == 87
    assert caught.value.handle is None


def test_non_windows_hosts_raise_without_touching_windll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    kernel32 = _Untouchable()
    calls = [
        lambda: process_limits.load_kernel32(),
        lambda: process_limits.make_job_object(kernel32, memory_bytes=1, cpu_seconds=1),
        lambda: process_limits.assign_process(kernel32, 1, 2),
        lambda: process_limits.is_process_in_job(kernel32, 1, None),
        lambda: process_limits.query_job_limits(kernel32, None),
        lambda: process_limits.query_job_accounting(kernel32, 1),
        lambda: process_limits.terminate_job(kernel32, 1),
    ]
    for call in calls:
        with pytest.raises(process_limits.ProcessLimitsError):
            call()
    assert process_limits.close_handle(kernel32, 1) is False


def test_active_processes_and_every_limit_are_set_on_the_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _as_windows(monkeypatch)
    kernel32 = _FakeKernel32()
    for active, expected in ((None, 1), (2, 2)):
        options = {} if active is None else {"active_processes": active}
        handle = process_limits.make_job_object(
            kernel32, memory_bytes=300 * _MiB, cpu_seconds=7, **options
        )
        assert handle == 4242
        limits = kernel32.limits
        assert limits is not None
        basic = limits.BasicLimitInformation
        assert basic.ActiveProcessLimit == expected
        assert basic.LimitFlags == process_limits.JOB_OBJECT_REQUIRED_LIMIT_FLAGS
        assert basic.PerProcessUserTimeLimit == 7 * 10_000_000
        assert limits.ProcessMemoryLimit == limits.JobMemoryLimit == 300 * _MiB
    assert "CloseHandle" not in [name for name, _ in kernel32.calls]


@pytest.mark.parametrize("close_ok", [True, False], ids=["closed", "unclosable"])
def test_a_failed_limit_closes_the_job_or_hands_it_to_the_caller(
    monkeypatch: pytest.MonkeyPatch, close_ok: bool
) -> None:
    _as_windows(monkeypatch)
    kernel32 = _FakeKernel32(set_ok=False, close_ok=close_ok)
    with pytest.raises(process_limits.ProcessLimitsError) as caught:
        process_limits.make_job_object(kernel32, memory_bytes=1, cpu_seconds=1)
    assert ("CloseHandle", (4242,)) in kernel32.calls
    assert caught.value.handle == (None if close_ok else 4242)


def test_close_handle_reports_failure_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    _as_windows(monkeypatch)
    assert process_limits.close_handle(_FakeKernel32(close_ok=False), 4242) is False
    assert process_limits.close_handle(_Untouchable(), 4242) is False
    assert process_limits.close_handle(_FakeKernel32(), 0) is False
    assert process_limits.close_handle(_FakeKernel32(), 4242) is True


@pytest.mark.skipif(os.name != "nt", reason="Job Objects are a Windows-only enforcement boundary")
def test_a_real_job_limits_accounts_and_ends_a_child_process() -> None:
    kernel32 = process_limits.load_kernel32()
    job = process_limits.make_job_object(
        kernel32, memory_bytes=256 * _MiB, cpu_seconds=5, active_processes=2
    )
    # The base interpreter is one process; the venv launcher would start a second one.
    child = subprocess.Popen(
        [sys._base_executable, "-I", "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE,
    )
    handle = int(child._handle)
    try:
        assert not process_limits.is_process_in_job(kernel32, handle, job)
        process_limits.assign_process(kernel32, job, handle)
        assert process_limits.is_process_in_job(kernel32, handle, job)
        limits = process_limits.query_job_limits(kernel32, job)
        assert limits.active_process_limit == 2
        assert limits.process_memory_limit == limits.job_memory_limit == 256 * _MiB
        assert limits.per_process_user_time_limit == 5 * 10_000_000
        required = process_limits.JOB_OBJECT_REQUIRED_LIMIT_FLAGS
        assert limits.limit_flags & required == required
        assert process_limits.query_job_accounting(kernel32, job).active_processes == 1
        process_limits.terminate_job(kernel32, job)
        assert child.wait(10) == 1
        assert process_limits.query_job_accounting(kernel32, job).active_processes == 0
    finally:
        if child.poll() is None:
            child.kill()
        assert process_limits.close_handle(kernel32, job)


def test_the_native_job_wrapper_keeps_its_own_argument_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def leaf(*args: Any, **kwargs: Any) -> int:
        raise AssertionError("the wrapper must refuse before the leaf")

    monkeypatch.setattr(process_limits, "make_job_object", leaf)
    for limits in ({"memory_bytes": 0, "cpu_seconds": 1}, {"memory_bytes": 1, "cpu_seconds": 0}):
        with pytest.raises(native_sandbox.NativeSandboxUnavailable):
            native_sandbox._make_job_object(_Untouchable(), **limits)


@pytest.mark.parametrize("carried", [None, 99], ids=["closed", "carried-handle"])
def test_the_native_job_wrapper_maps_leaf_errors_and_retains_a_carried_handle(
    monkeypatch: pytest.MonkeyPatch, carried: int | None
) -> None:
    def leaf(kernel32: Any, *, memory_bytes: int, cpu_seconds: int) -> int:
        raise process_limits.ProcessLimitsError("fictional failure", code=5, handle=carried)

    deferred: list[native_sandbox.NativeSandboxProcess] = []
    monkeypatch.setattr(process_limits, "make_job_object", leaf)
    monkeypatch.setattr(native_sandbox, "_defer_sandbox_process", deferred.append)
    with pytest.raises(native_sandbox.NativeSandboxUnavailable) as caught:
        native_sandbox._make_job_object(_Untouchable(), memory_bytes=1, cpu_seconds=1)
    assert not isinstance(caught.value, OSError), "no OSError may escape the wrapper"
    assert [process._job_handle for process in deferred] == ([] if carried is None else [99])


def test_the_native_job_wrapper_returns_the_leaf_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[int, int]] = []

    def leaf(kernel32: Any, *, memory_bytes: int, cpu_seconds: int) -> int:
        seen.append((memory_bytes, cpu_seconds))
        return 77

    monkeypatch.setattr(process_limits, "make_job_object", leaf)
    assert native_sandbox._make_job_object(_Untouchable(), memory_bytes=5, cpu_seconds=6) == 77
    assert seen == [(5, 6)]


@pytest.mark.parametrize("answer", ["raises", "no", "yes"])
def test_worker_attestation_maps_the_leaf_job_check(
    monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    def token_information(*args: Any) -> int:
        ctypes.cast(args[2], ctypes.POINTER(ctypes.c_ulong)).contents.value = 1
        return 1

    class _Advapi32:
        OpenProcessToken = _Function(lambda *args: 1)
        GetTokenInformation = _Function(token_information)

    def in_job(kernel32: Any, process_handle: int, job: int | None) -> bool:
        assert (process_handle, job) == (11, 12)
        if answer == "raises":
            raise process_limits.ProcessLimitsError("fictional failure", code=5)
        return answer == "yes"

    monkeypatch.setattr(
        native_sandbox, "_windows_apis", lambda: (_Untouchable(), _Advapi32(), None, None)
    )
    monkeypatch.setattr(native_sandbox, "_token_app_container_sid_matches", lambda *a: True)
    monkeypatch.setattr(native_worker, "_has_only_registry_read_capability", lambda *a: True)
    monkeypatch.setattr(native_worker, "_has_no_all_application_packages_policy", lambda *a: True)
    monkeypatch.setattr(process_limits, "is_process_in_job", in_job)
    process = native_sandbox.NativeSandboxProcess(
        kernel32=_Untouchable(), process_handle=11, job_handle=12, runtime=None, profile_name=None
    )
    if answer == "yes":
        native_sandbox._attest_launched_worker_before_handle_transfer(
            process, expected_profile_sid=1
        )
        return
    with pytest.raises(native_sandbox.NativeSandboxUnavailable) as caught:
        native_sandbox._attest_launched_worker_before_handle_transfer(
            process, expected_profile_sid=1
        )
    assert not isinstance(caught.value, OSError)
