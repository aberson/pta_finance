"""Public one-shot entry point for the LPAC native statement worker.

It is intentionally invoked only from :mod:`native_sandbox` with a staged public runtime.
Arguments contain generated public control-object names, bounded public limits, an ordinal,
and a random readiness nonce; they never contain a statement path, document bytes, or
inherited handles.
"""

from __future__ import annotations

import ctypes
import os
import struct
import sys
from dataclasses import dataclass
from typing import Any

from pta_finance.treasurer_slides import bank_statements

_TOKEN_QUERY = 0x0008
_TOKEN_IS_APP_CONTAINER = 29
_TOKEN_CAPABILITIES = 30
_TOKEN_SECURITY_ATTRIBUTES = 39
_SE_GROUP_ENABLED = 0x00000004
_TOKEN_SECURITY_ATTRIBUTE_TYPE_UINT64 = 0x0002
_NO_ALL_APPLICATION_PACKAGES_ATTRIBUTE = "WIN://NOALLAPPPKG"
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
_JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
_JOB_OBJECT_REQUIRED_LIMIT_FLAGS = (
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    | _JOB_OBJECT_LIMIT_PROCESS_TIME
    | _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    | _JOB_OBJECT_LIMIT_PROCESS_MEMORY
    | _JOB_OBJECT_LIMIT_JOB_MEMORY
    | _JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
)
_JOB_OBJECT_FORBIDDEN_LIMIT_FLAGS = (
    _JOB_OBJECT_LIMIT_BREAKAWAY_OK | _JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
)
_HUNDRED_NANOSECONDS_PER_SECOND = 10_000_000
_FILE_MAP_READ = 0x0004
_EVENT_MODIFY_STATE = 0x0002
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0
_CONTROL_EVENT_WAIT_MILLISECONDS = 30_000
_CONTROL_MAPPING_BYTES = 256
_CONTROL_NAME_PREFIX = r"Local\pta-finance-native-"
_CONTROL_HANDLES_MAGIC = b"PTAFINH1"
_CONTROL_HANDLES_FRAME = struct.Struct("<8s64sQQ")


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_ulong)]


class _TokenGroups(ctypes.Structure):
    _fields_ = [("GroupCount", ctypes.c_ulong), ("Groups", _SidAndAttributes * 1)]


class _TokenSecurityAttributeValues(ctypes.Union):
    _fields_ = [
        ("p_int64", ctypes.POINTER(ctypes.c_longlong)),
        ("p_uint64", ctypes.POINTER(ctypes.c_ulonglong)),
    ]


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_ushort),
        ("MaximumLength", ctypes.c_ushort),
        ("Buffer", ctypes.c_void_p),
    ]


class _TokenSecurityAttributeV1(ctypes.Structure):
    _fields_ = [
        ("Name", _UnicodeString),
        ("ValueType", ctypes.c_ushort),
        ("Reserved", ctypes.c_ushort),
        ("Flags", ctypes.c_ulong),
        ("ValueCount", ctypes.c_ulong),
        ("Values", _TokenSecurityAttributeValues),
    ]


class _TokenSecurityAttributesInformation(ctypes.Structure):
    _fields_ = [
        ("Version", ctypes.c_ushort),
        ("Reserved", ctypes.c_ushort),
        ("AttributeCount", ctypes.c_ulong),
        ("Attribute", ctypes.POINTER(_TokenSecurityAttributeV1)),
    ]


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


def _windows_dll(name: str) -> Any:
    """Load a Windows DLL while keeping this entry point importable on other hosts."""

    loader: Any = getattr(ctypes, "WinDLL", None)
    if loader is None:
        raise RuntimeError
    return loader(name, use_last_error=True)


def _windows_last_error() -> int:
    getter: Any = getattr(ctypes, "get_last_error", None)
    if getter is None:
        raise RuntimeError
    return int(getter())


def _pipe_connection_from_handle(handle: int, *, readable: bool, writable: bool) -> Any:
    from multiprocessing import connection

    pipe_connection: Any = getattr(connection, "PipeConnection", None)
    if pipe_connection is None:
        raise RuntimeError
    return pipe_connection(handle, readable=readable, writable=writable)


def _argument_value(arguments: list[str], flag: str) -> str:
    try:
        index = arguments.index(flag)
    except ValueError:
        raise ValueError from None
    if index + 1 >= len(arguments) or arguments.count(flag) != 1:
        raise ValueError
    return arguments[index + 1]


def _parse_arguments(arguments: list[str]) -> tuple[str, str, str, int, str, str]:
    expected_flags = {
        "--control-mapping",
        "--attested-event",
        "--handles-ready-event",
        "--document-ordinal",
        "--limits-json",
        "--ready-nonce",
    }
    if len(arguments) != len(expected_flags) * 2 or set(arguments[::2]) != expected_flags:
        raise ValueError
    control_mapping = _argument_value(arguments, "--control-mapping")
    attested_event = _argument_value(arguments, "--attested-event")
    handles_ready_event = _argument_value(arguments, "--handles-ready-event")
    document_ordinal = int(_argument_value(arguments, "--document-ordinal"), 10)
    limits_json = _argument_value(arguments, "--limits-json")
    nonce = _argument_value(arguments, "--ready-nonce")
    if (
        not control_mapping.startswith(_CONTROL_NAME_PREFIX)
        or not control_mapping.endswith("-mapping")
        or not attested_event.startswith(_CONTROL_NAME_PREFIX)
        or not attested_event.endswith("-attested")
        or not handles_ready_event.startswith(_CONTROL_NAME_PREFIX)
        or not handles_ready_event.endswith("-handles-ready")
        or len({control_mapping, attested_event, handles_ready_event}) != 3
        or document_ordinal < 1
    ):
        raise ValueError
    bank_statements._native_worker_ready_frame(nonce)
    return (
        control_mapping,
        attested_event,
        handles_ready_event,
        document_ordinal,
        limits_json,
        nonce,
    )


@dataclass
class _WorkerControl:
    mapping_handle: int | None
    mapping_view: int | None
    attested_event_handle: int | None
    handles_ready_event_handle: int | None


def _close_worker_control(control: _WorkerControl | None) -> None:
    if control is None:
        return
    kernel32 = _windows_dll("kernel32")
    view = control.mapping_view
    if view is not None:
        control.mapping_view = None
        unmapper = kernel32.UnmapViewOfFile
        unmapper.argtypes = (ctypes.c_void_p,)
        unmapper.restype = ctypes.c_int
        unmapper(ctypes.c_void_p(view))
    closer = kernel32.CloseHandle
    closer.argtypes = (ctypes.c_void_p,)
    closer.restype = ctypes.c_int
    for attribute in (
        "mapping_handle",
        "attested_event_handle",
        "handles_ready_event_handle",
    ):
        handle = getattr(control, attribute)
        if handle is not None:
            setattr(control, attribute, None)
            closer(ctypes.c_void_p(handle))


def _open_worker_control(
    mapping_name: str,
    attested_event_name: str,
    handles_ready_event_name: str,
) -> _WorkerControl:
    """Open only public startup control objects before LPAC attestation."""

    kernel32 = _windows_dll("kernel32")
    control = _WorkerControl(None, None, None, None)
    try:
        mapping_opener = kernel32.OpenFileMappingW
        mapping_opener.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p)
        mapping_opener.restype = ctypes.c_void_p
        mapping_handle = _pointer_value(mapping_opener(_FILE_MAP_READ, False, mapping_name))
        if mapping_handle == 0:
            raise RuntimeError
        control.mapping_handle = mapping_handle
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
                _FILE_MAP_READ,
                0,
                0,
                _CONTROL_MAPPING_BYTES,
            )
        )
        if mapping_view == 0:
            raise RuntimeError
        control.mapping_view = mapping_view
        event_opener = kernel32.OpenEventW
        event_opener.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_wchar_p)
        event_opener.restype = ctypes.c_void_p
        attested_event_handle = _pointer_value(
            event_opener(_EVENT_MODIFY_STATE, False, attested_event_name)
        )
        if attested_event_handle == 0:
            raise RuntimeError
        control.attested_event_handle = attested_event_handle
        handles_ready_event_handle = _pointer_value(
            event_opener(_SYNCHRONIZE, False, handles_ready_event_name)
        )
        if handles_ready_event_handle == 0:
            raise RuntimeError
        control.handles_ready_event_handle = handles_ready_event_handle
        return control
    except BaseException:
        _close_worker_control(control)
        raise


def _worker_pipe_handles(control: _WorkerControl, nonce: str) -> tuple[int, int]:
    view = control.mapping_view
    if view is None:
        raise RuntimeError
    frame = ctypes.string_at(view, _CONTROL_HANDLES_FRAME.size)
    magic, frame_nonce, request_handle, response_handle = _CONTROL_HANDLES_FRAME.unpack(frame)
    if (
        magic != _CONTROL_HANDLES_MAGIC
        or frame_nonce != nonce.encode("ascii", "strict")
        or request_handle < 1
        or response_handle < 1
    ):
        raise RuntimeError
    return request_handle, response_handle


def _signal_attested_and_wait_for_handles(control: _WorkerControl) -> None:
    if control.attested_event_handle is None or control.handles_ready_event_handle is None:
        raise RuntimeError
    kernel32 = _windows_dll("kernel32")
    setter = kernel32.SetEvent
    setter.argtypes = (ctypes.c_void_p,)
    setter.restype = ctypes.c_int
    if not setter(ctypes.c_void_p(control.attested_event_handle)):
        raise RuntimeError
    waiter = kernel32.WaitForSingleObject
    waiter.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
    waiter.restype = ctypes.c_ulong
    if (
        waiter(
            ctypes.c_void_p(control.handles_ready_event_handle),
            _CONTROL_EVENT_WAIT_MILLISECONDS,
        )
        != _WAIT_OBJECT_0
    ):
        raise RuntimeError


def _pointer_value(value: Any) -> int:
    if isinstance(value, int):
        return value
    if value is None:
        return 0
    return int(ctypes.cast(value, ctypes.c_void_p).value or 0)


def _local_free(local_free: Any, value: int | None) -> None:
    if value not in (None, 0):
        local_free(ctypes.c_void_p(value))


def _has_only_registry_read_capability(
    advapi32: Any, kernel32: Any, token: ctypes.c_void_p
) -> bool:
    """Require the one runtime capability and rule out any silently added capability."""

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
    try:
        if not derive(
            "registryRead",
            ctypes.byref(groups),
            ctypes.byref(group_count),
            ctypes.byref(capability_sids),
            ctypes.byref(capability_count),
        ):
            return False
        if group_count.value != 1 or capability_count.value != 1:
            return False
        expected_capability = _pointer_value(capability_sids[0])
        return _token_capabilities_match_registry_read(advapi32, token, expected_capability)
    finally:
        if groups:
            for index in range(group_count.value):
                _local_free(local_free, _pointer_value(groups[index]))
            _local_free(local_free, _pointer_value(groups))
        if capability_sids:
            for index in range(capability_count.value):
                _local_free(local_free, _pointer_value(capability_sids[index]))
            _local_free(local_free, _pointer_value(capability_sids))


def _token_capabilities_match_registry_read(
    advapi32: Any, token: ctypes.c_void_p, expected_capability: int
) -> bool:
    if expected_capability == 0:
        return False
    returned = ctypes.c_ulong()
    get_token_information = advapi32.GetTokenInformation
    get_token_information.argtypes = (
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    )
    get_token_information.restype = ctypes.c_int
    if get_token_information(token, _TOKEN_CAPABILITIES, None, 0, ctypes.byref(returned)) or (
        _windows_last_error() != 122
    ):
        return False
    if returned.value < ctypes.sizeof(_TokenGroups):
        return False
    buffer = ctypes.create_string_buffer(returned.value)
    if not get_token_information(
        token,
        _TOKEN_CAPABILITIES,
        buffer,
        returned.value,
        ctypes.byref(returned),
    ):
        return False
    groups = ctypes.cast(buffer, ctypes.POINTER(_TokenGroups)).contents
    if groups.GroupCount != 1 or not (groups.Groups[0].Attributes & _SE_GROUP_ENABLED):
        return False
    equal_sid = advapi32.EqualSid
    equal_sid.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    equal_sid.restype = ctypes.c_int
    return bool(
        equal_sid(
            groups.Groups[0].Sid,
            ctypes.c_void_p(expected_capability),
        )
    )


def _pointer_span_is_within_buffer(
    pointer: int,
    length: int,
    *,
    buffer_start: int,
    buffer_end: int,
) -> bool:
    return (
        length >= 0
        and pointer >= buffer_start
        and pointer <= buffer_end
        and length <= buffer_end - pointer
    )


def _unicode_string_text(
    value: _UnicodeString,
    *,
    buffer_start: int,
    buffer_end: int,
) -> str | None:
    character_width = ctypes.sizeof(ctypes.c_wchar)
    pointer = _pointer_value(value.Buffer)
    if (
        value.Length == 0
        or value.Length > value.MaximumLength
        or value.Length % character_width != 0
        or pointer == 0
        or not _pointer_span_is_within_buffer(
            pointer,
            value.Length,
            buffer_start=buffer_start,
            buffer_end=buffer_end,
        )
    ):
        return None
    try:
        return ctypes.wstring_at(pointer, value.Length // character_width)
    except (ValueError, OSError):
        return None


def _has_no_all_application_packages_policy(advapi32: Any, token: ctypes.c_void_p) -> bool:
    """Require the kernel's LPAC policy attribute, not an ambiguous SID membership check."""

    returned = ctypes.c_ulong()
    get_token_information = advapi32.GetTokenInformation
    get_token_information.argtypes = (
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    )
    get_token_information.restype = ctypes.c_int
    if get_token_information(
        token, _TOKEN_SECURITY_ATTRIBUTES, None, 0, ctypes.byref(returned)
    ) or (_windows_last_error() != 122):
        return False
    if returned.value < ctypes.sizeof(_TokenSecurityAttributesInformation):
        return False
    buffer = ctypes.create_string_buffer(returned.value)
    if not get_token_information(
        token,
        _TOKEN_SECURITY_ATTRIBUTES,
        buffer,
        returned.value,
        ctypes.byref(returned),
    ):
        return False
    buffer_start = ctypes.addressof(buffer)
    buffer_end = buffer_start + ctypes.sizeof(buffer)
    information = ctypes.cast(
        buffer,
        ctypes.POINTER(_TokenSecurityAttributesInformation),
    ).contents
    if (
        information.Version != 1
        or information.Reserved != 0
        or information.AttributeCount < 1
        or information.AttributeCount > 16
    ):
        return False
    attributes_pointer = _pointer_value(information.Attribute)
    if not _pointer_span_is_within_buffer(
        attributes_pointer,
        information.AttributeCount * ctypes.sizeof(_TokenSecurityAttributeV1),
        buffer_start=buffer_start,
        buffer_end=buffer_end,
    ):
        return False
    for index in range(information.AttributeCount):
        attribute = information.Attribute[index]
        if attribute.Reserved != 0:
            return False
        if (
            _unicode_string_text(
                attribute.Name,
                buffer_start=buffer_start,
                buffer_end=buffer_end,
            )
            != _NO_ALL_APPLICATION_PACKAGES_ATTRIBUTE
        ):
            continue
        values_pointer = _pointer_value(attribute.Values.p_uint64)
        if (
            attribute.ValueType != _TOKEN_SECURITY_ATTRIBUTE_TYPE_UINT64
            or attribute.Flags != 0
            or attribute.ValueCount != 1
            or not _pointer_span_is_within_buffer(
                values_pointer,
                ctypes.sizeof(ctypes.c_ulonglong),
                buffer_start=buffer_start,
                buffer_end=buffer_end,
            )
        ):
            return False
        return int(attribute.Values.p_uint64[0]) == 1
    return False


def _job_limits_match(limits: bank_statements._NativeExtractionLimits, kernel32: Any) -> bool:
    information = _ExtendedLimitInformation()
    query = kernel32.QueryInformationJobObject
    query.argtypes = (
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
    )
    query.restype = ctypes.c_int
    if not query(
        None,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(information),
        ctypes.sizeof(information),
        None,
    ):
        return False
    basic = information.BasicLimitInformation
    return bool(
        (basic.LimitFlags & _JOB_OBJECT_REQUIRED_LIMIT_FLAGS) == _JOB_OBJECT_REQUIRED_LIMIT_FLAGS
        and not (basic.LimitFlags & _JOB_OBJECT_FORBIDDEN_LIMIT_FLAGS)
        and basic.PerProcessUserTimeLimit
        == limits.worker_cpu_seconds * _HUNDRED_NANOSECONDS_PER_SECOND
        and basic.ActiveProcessLimit == 1
        and information.ProcessMemoryLimit == limits.worker_memory_bytes
        and information.JobMemoryLimit == limits.worker_memory_bytes
    )


def _attest_sandboxed_process(limits: bank_statements._NativeExtractionLimits) -> bool:
    """Confirm the child is an AppContainer and preassigned to a Job before readiness."""

    if os.name != "nt":
        return False
    kernel32 = _windows_dll("kernel32")
    advapi32 = _windows_dll("advapi32")
    current_process = kernel32.GetCurrentProcess()
    token = ctypes.c_void_p()
    open_token = advapi32.OpenProcessToken
    open_token.argtypes = (ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_void_p))
    open_token.restype = ctypes.c_int
    if not open_token(current_process, _TOKEN_QUERY, ctypes.byref(token)):
        return False
    try:
        value = ctypes.c_ulong()
        returned = ctypes.c_ulong()
        get_token_information = advapi32.GetTokenInformation
        get_token_information.argtypes = (
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
        )
        get_token_information.restype = ctypes.c_int
        if (
            not get_token_information(
                token,
                _TOKEN_IS_APP_CONTAINER,
                ctypes.byref(value),
                ctypes.sizeof(value),
                ctypes.byref(returned),
            )
            or value.value == 0
        ):
            return False
        if not _has_only_registry_read_capability(advapi32, kernel32, token):
            return False
        if not _has_no_all_application_packages_policy(advapi32, token):
            return False
        in_job = ctypes.c_int()
        is_process_in_job = kernel32.IsProcessInJob
        is_process_in_job.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
        )
        is_process_in_job.restype = ctypes.c_int
        return bool(
            is_process_in_job(current_process, None, ctypes.byref(in_job))
            and in_job.value
            and _job_limits_match(limits, kernel32)
        )
    finally:
        kernel32.CloseHandle(token)


def main(arguments: list[str] | None = None) -> int:
    """Attest the preinstalled boundary, signal readiness, then process one framed request."""

    control: _WorkerControl | None = None
    request: Any | None = None
    response: Any | None = None
    try:
        (
            control_mapping,
            attested_event,
            handles_ready_event,
            document_ordinal,
            limits_json,
            nonce,
        ) = _parse_arguments(list(sys.argv[1:] if arguments is None else arguments))
        control = _open_worker_control(
            control_mapping,
            attested_event,
            handles_ready_event,
        )
        limits = bank_statements._deserialize_native_limits(limits_json, document_ordinal)
        if not _attest_sandboxed_process(limits):
            raise RuntimeError
        _signal_attested_and_wait_for_handles(control)
        request_handle, response_handle = _worker_pipe_handles(control, nonce)
        request = _pipe_connection_from_handle(request_handle, readable=True, writable=False)
        response = _pipe_connection_from_handle(response_handle, readable=False, writable=True)
        _close_worker_control(control)
        control = None
        response.send_bytes(bank_statements._native_worker_ready_frame(nonce))
        bank_statements._native_page_extraction_after_limits(
            request,
            response,
            document_ordinal,
            limits,
        )
        request = None
        response = None
        return 0
    except BaseException:
        if response is not None:
            bank_statements._send_worker_failure(response)
        return 1
    finally:
        _close_worker_control(control)
        if request is not None:
            request.close()
        if response is not None:
            response.close()


if __name__ == "__main__":
    raise SystemExit(main())
