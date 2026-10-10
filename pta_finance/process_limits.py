"""The Windows Job Object primitive shared by every resource-capped child process.

This leaf is ctypes and the standard library only, and it imports nothing from
``pta_finance``, so the LPAC launcher (:mod:`pta_finance.treasurer_slides.native_sandbox`), the
receipt page broker (:mod:`pta_finance.receipt_pages`) and the image decode child
(:mod:`pta_finance.receipt_decode`, which self-attests its own Job) all use one definition of
the Job structures, the limit flags and the calls that create, assign, query and end a Job.

Every ``kernel32`` argument is loaded by the caller as ``WinDLL("kernel32",
use_last_error=True)`` (see :func:`load_kernel32`), so ``ctypes.get_last_error()`` yields the
Win32 code a :class:`ProcessLimitsError` carries.

**Error contract.** A failing Win32 call raises :class:`ProcessLimitsError` (an ``OSError``
carrying the Win32 code in ``errno`` and ``winerror_code``) and nothing else, except
:func:`close_handle`, which reports failure by returning ``False``, and the queries, whose "no"
answer is a value rather than an error. On a host that is not Windows each Windows function
raises :class:`ProcessLimitsError` without touching ``ctypes.windll``, so this module imports
everywhere. :func:`make_job_object` refuses a limit below 1 before it creates any handle, so a
direct caller can never create an unlimited Job. When it fails after creating the Job it
closes the handle itself; only if that close also fails does it set
:attr:`ProcessLimitsError.handle`, and the caller then owns that handle.

``pta_finance/treasurer_slides/native_worker.py`` keeps its own copy of the flags and
structures because the staged LPAC worker allowlist excludes this module; a parity test pins
the two copies together.
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass
from typing import Any

__all__ = [
    "HUNDRED_NANOSECONDS_PER_SECOND",
    "JOB_OBJECT_FORBIDDEN_LIMIT_FLAGS",
    "JOB_OBJECT_LIMIT_ACTIVE_PROCESS",
    "JOB_OBJECT_LIMIT_BREAKAWAY_OK",
    "JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION",
    "JOB_OBJECT_LIMIT_JOB_MEMORY",
    "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE",
    "JOB_OBJECT_LIMIT_PROCESS_MEMORY",
    "JOB_OBJECT_LIMIT_PROCESS_TIME",
    "JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK",
    "JOB_OBJECT_REQUIRED_LIMIT_FLAGS",
    "JobAccounting",
    "JobLimits",
    "ProcessLimitsError",
    "assign_process",
    "close_handle",
    "is_process_in_job",
    "load_kernel32",
    "make_job_object",
    "query_job_accounting",
    "query_job_limits",
    "terminate_job",
]

JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
# Every Job this module creates carries exactly these flags; a self-attesting process requires
# all of them and refuses a Job that lets a process break away.
JOB_OBJECT_REQUIRED_LIMIT_FLAGS = (
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    | JOB_OBJECT_LIMIT_PROCESS_TIME
    | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    | JOB_OBJECT_LIMIT_PROCESS_MEMORY
    | JOB_OBJECT_LIMIT_JOB_MEMORY
    | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
)
JOB_OBJECT_FORBIDDEN_LIMIT_FLAGS = (
    JOB_OBJECT_LIMIT_BREAKAWAY_OK | JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK
)
HUNDRED_NANOSECONDS_PER_SECOND = 10_000_000

_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_ERROR_INVALID_PARAMETER = 87
_ERROR_NOT_SUPPORTED = 50


class ProcessLimitsError(OSError):
    """A Job Object call failed; ``winerror_code`` is its Win32 error code.

    ``handle`` is a Job handle this module created but could not close; the caller then owns
    it and must keep it referenced (or close it later). It is ``None`` otherwise.
    """

    handle: int | None
    winerror_code: int

    def __init__(self, message: str, *, code: int, handle: int | None = None) -> None:
        super().__init__(code, message)
        self.winerror_code = code
        self.handle = handle


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


class _BasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", ctypes.c_ulong),
        ("TotalProcesses", ctypes.c_ulong),
        ("ActiveProcesses", ctypes.c_ulong),
        ("TotalTerminatedProcesses", ctypes.c_ulong),
    ]


@dataclass(frozen=True)
class JobLimits:
    """What ``QueryInformationJobObject(JobObjectExtendedLimitInformation)`` reports.

    Times are in 100-nanosecond units and memory in bytes, exactly as Windows reports them.
    ``peak_process_memory_used`` is the peak commit of any process in the Job.
    """

    limit_flags: int
    active_process_limit: int
    process_memory_limit: int
    job_memory_limit: int
    per_process_user_time_limit: int
    peak_process_memory_used: int


@dataclass(frozen=True)
class JobAccounting:
    """What ``QueryInformationJobObject(JobObjectBasicAccountingInformation)`` reports.

    Times are in 100-nanosecond units: ``total_user_time`` is what
    ``PerProcessUserTimeLimit`` counts.
    """

    total_user_time: int
    total_kernel_time: int
    active_processes: int


def load_kernel32() -> Any:
    """Load ``kernel32`` as ``WinDLL("kernel32", use_last_error=True)``.

    Read through ``getattr`` so a non-Windows type check never sees a Windows-only ctypes
    member; elsewhere it raises :class:`ProcessLimitsError`.
    """

    loader: Any = getattr(ctypes, "WinDLL", None) if sys.platform == "win32" else None
    if loader is None:
        raise ProcessLimitsError("Job Objects exist only on Windows", code=_ERROR_NOT_SUPPORTED)
    try:
        return loader("kernel32", use_last_error=True)
    except OSError as exc:
        raise ProcessLimitsError("kernel32 could not be loaded", code=_win32_code(exc)) from None


def make_job_object(
    kernel32: Any, *, memory_bytes: int, cpu_seconds: int, active_processes: int = 1
) -> int:
    """Create a Job that limits memory, per-process user time and the process count.

    Process and Job memory are both ``memory_bytes``, ``PerProcessUserTimeLimit`` is
    ``cpu_seconds``, and the Job kills its processes when its last handle closes and on an
    unhandled exception. ``active_processes`` defaults to 1, which every production caller
    uses. Returns the Job handle, which the caller owns.
    """

    for value in (memory_bytes, cpu_seconds, active_processes):
        if type(value) is not int or value < 1:
            raise ProcessLimitsError(
                "Job limits must be positive integers", code=_ERROR_INVALID_PARAMETER
            )
    _require_windows()
    creator = kernel32.CreateJobObjectW
    creator.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
    creator.restype = ctypes.c_void_p
    handle = creator(None, None)
    if not handle:
        raise ProcessLimitsError("CreateJobObjectW failed", code=_last_error())
    handle_value = int(handle)
    information = _ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = JOB_OBJECT_REQUIRED_LIMIT_FLAGS
    information.BasicLimitInformation.PerProcessUserTimeLimit = (
        cpu_seconds * HUNDRED_NANOSECONDS_PER_SECOND
    )
    information.BasicLimitInformation.ActiveProcessLimit = active_processes
    information.ProcessMemoryLimit = memory_bytes
    information.JobMemoryLimit = memory_bytes
    setter = kernel32.SetInformationJobObject
    setter.argtypes = (ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong)
    setter.restype = ctypes.c_int
    if not setter(
        ctypes.c_void_p(handle_value),
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        code = _last_error()
        # No process is in the Job yet. If the handle cannot be closed either, hand it to the
        # caller rather than silently abandoning it.
        carried = None if close_handle(kernel32, handle_value) else handle_value
        raise ProcessLimitsError("SetInformationJobObject failed", code=code, handle=carried)
    return handle_value


def assign_process(kernel32: Any, job: int, process_handle: int) -> None:
    """Assign a process to a Job (``AssignProcessToJobObject``)."""

    _require_windows()
    assigner = kernel32.AssignProcessToJobObject
    assigner.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    assigner.restype = ctypes.c_int
    if not assigner(ctypes.c_void_p(job), ctypes.c_void_p(process_handle)):
        raise ProcessLimitsError("AssignProcessToJobObject failed", code=_last_error())


def is_process_in_job(kernel32: Any, process_handle: int, job: int | None) -> bool:
    """Whether the process is in ``job`` (``IsProcessInJob``; ``None`` means any Job)."""

    _require_windows()
    in_job = ctypes.c_int()
    checker = kernel32.IsProcessInJob
    checker.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int))
    checker.restype = ctypes.c_int
    if not checker(
        ctypes.c_void_p(process_handle),
        None if job is None else ctypes.c_void_p(job),
        ctypes.byref(in_job),
    ):
        raise ProcessLimitsError("IsProcessInJob failed", code=_last_error())
    return bool(in_job.value)


def query_job_limits(kernel32: Any, job: int | None) -> JobLimits:
    """Read a Job's extended limits; ``None`` reads the calling process's immediate Job."""

    _require_windows()
    information = _ExtendedLimitInformation()
    _query(kernel32, job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, information)
    basic = information.BasicLimitInformation
    return JobLimits(
        limit_flags=int(basic.LimitFlags),
        active_process_limit=int(basic.ActiveProcessLimit),
        process_memory_limit=int(information.ProcessMemoryLimit),
        job_memory_limit=int(information.JobMemoryLimit),
        per_process_user_time_limit=int(basic.PerProcessUserTimeLimit),
        peak_process_memory_used=int(information.PeakProcessMemoryUsed),
    )


def query_job_accounting(kernel32: Any, job: int) -> JobAccounting:
    """Read a Job's basic accounting: the CPU it has used and its live process count."""

    _require_windows()
    information = _BasicAccountingInformation()
    _query(kernel32, job, _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION, information)
    return JobAccounting(
        total_user_time=int(information.TotalUserTime),
        total_kernel_time=int(information.TotalKernelTime),
        active_processes=int(information.ActiveProcesses),
    )


def terminate_job(kernel32: Any, job: int) -> None:
    """End every process in a Job (``TerminateJobObject``, exit code 1)."""

    _require_windows()
    terminator = kernel32.TerminateJobObject
    terminator.argtypes = (ctypes.c_void_p, ctypes.c_uint)
    terminator.restype = ctypes.c_int
    if not terminator(ctypes.c_void_p(job), 1):
        raise ProcessLimitsError("TerminateJobObject failed", code=_last_error())


def close_handle(kernel32: Any, handle: int) -> bool:
    """Close a handle; ``False`` on failure (including on a non-Windows host). Never raises."""

    if sys.platform != "win32" or type(handle) is not int or handle == 0:
        return False
    try:
        closer = kernel32.CloseHandle
        closer.argtypes = (ctypes.c_void_p,)
        closer.restype = ctypes.c_int
        return bool(closer(ctypes.c_void_p(handle)))
    except Exception:
        return False


def _query(kernel32: Any, job: int | None, information_class: int, information: Any) -> None:
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
        None if job is None else ctypes.c_void_p(job),
        information_class,
        ctypes.byref(information),
        ctypes.sizeof(information),
        None,
    ):
        raise ProcessLimitsError("QueryInformationJobObject failed", code=_last_error())


def _require_windows() -> None:
    if sys.platform != "win32":
        raise ProcessLimitsError("Job Objects exist only on Windows", code=_ERROR_NOT_SUPPORTED)


def _last_error() -> int:
    getter: Any = getattr(ctypes, "get_last_error", None)
    return int(getter()) if getter is not None else 0


def _win32_code(exc: OSError) -> int:
    code = getattr(exc, "winerror", None)
    return code if type(code) is int and code > 0 else _ERROR_NOT_SUPPORTED
