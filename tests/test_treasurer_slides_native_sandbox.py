"""Public-boundary coverage for the Windows native-statement sandbox."""

from __future__ import annotations

import ctypes
import inspect
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from pta_finance.treasurer_slides import bank_statements, native_sandbox, native_worker


class _ReadyConnection:
    def __init__(self, *, frame: bytes | None, ready: bool = True) -> None:
        self.frame = frame
        self.ready = ready
        self.maximum: int | None = None

    def poll(self, timeout: float = 0.0) -> bool:
        assert timeout > 0
        return self.ready

    def recv_bytes(self, maxlength: int | None = None) -> bytes:
        self.maximum = maxlength
        if self.frame is None:
            raise EOFError
        return self.frame


def test_ready_attestation_accepts_only_the_exact_nonce_frame() -> None:
    nonce = "a" * (native_sandbox._READY_NONCE_BYTES * 2)
    connection = _ReadyConnection(frame=native_sandbox._expected_ready_frame(nonce))

    native_sandbox._verify_worker_ready(connection, nonce, timeout_seconds=0.1)

    assert connection.maximum == native_sandbox._READY_FRAME_MAXIMUM_BYTES


@pytest.mark.parametrize(
    ("frame", "ready"),
    (
        (b'{"status":"ready","nonce":"wrong"}', True),
        (b"x" * (native_sandbox._READY_FRAME_MAXIMUM_BYTES + 1), True),
        (None, True),
        (b'{"status":"ready"}', False),
    ),
)
def test_ready_attestation_rejects_wrong_or_missing_worker_proofs(
    frame: bytes | None, ready: bool
) -> None:
    connection = _ReadyConnection(frame=frame, ready=ready)

    with pytest.raises(native_sandbox.NativeSandboxUnavailable):
        native_sandbox._verify_worker_ready(connection, "b" * 64, timeout_seconds=0.1)


def test_native_sandbox_has_no_source_path_or_bytes_parameter() -> None:
    parameter_names = set(inspect.signature(native_sandbox.start_native_pdf_worker).parameters)

    assert parameter_names == {
        "document_ordinal",
        "limits_json",
        "worker_memory_bytes",
        "worker_cpu_seconds",
        "ready_timeout_seconds",
    }


def test_worker_command_only_carries_public_configuration() -> None:
    runtime = native_sandbox._StagedRuntime(
        root=Path("C:/public-stage"),
        interpreter=Path("C:/public-stage/python/python.exe"),
        site_root=Path("C:/public-stage/site"),
    )
    command = native_sandbox._worker_command(
        runtime,
        control_mapping=r"Local\pta-finance-native-control-mapping",
        attested_event=r"Local\pta-finance-native-control-attested",
        handles_ready_event=r"Local\pta-finance-native-control-handles-ready",
        document_ordinal=1,
        limits_json='{"max_pdf_bytes":1}',
        ready_nonce="c" * 64,
    )

    assert "--source-path" not in command
    assert "--source-bytes" not in command
    assert "--request-handle" not in command
    assert "--response-handle" not in command
    assert "--request-pipe" not in command
    assert "--response-pipe" not in command
    assert "--control-mapping" in command
    assert "--attested-event" in command
    assert "--handles-ready-event" in command
    assert "--document-ordinal" in command
    assert "--limits-json" in command
    assert "--ready-nonce" in command
    assert command[6::2] == [
        "--control-mapping",
        "--attested-event",
        "--handles-ready-event",
        "--document-ordinal",
        "--limits-json",
        "--ready-nonce",
    ]


def test_native_worker_rejects_an_unrecognized_source_path_argument() -> None:
    with pytest.raises(ValueError):
        native_worker._parse_arguments(
            [
                "--control-mapping",
                r"Local\pta-finance-native-control-mapping",
                "--attested-event",
                r"Local\pta-finance-native-control-attested",
                "--handles-ready-event",
                r"Local\pta-finance-native-control-handles-ready",
                "--document-ordinal",
                "1",
                "--limits-json",
                "{}",
                "--ready-nonce",
                "c" * 64,
                "--source-path",
                "C:/private-statement.pdf",
            ]
        )


def test_control_handle_frame_binds_the_child_local_handles_to_the_ready_nonce() -> None:
    storage = ctypes.create_string_buffer(native_sandbox._WINDOWS_CONTROL_MAPPING_BYTES)
    channel = native_sandbox._WorkerControlChannel(
        mapping_name=r"Local\pta-finance-native-control-mapping",
        attested_event_name=r"Local\pta-finance-native-control-attested",
        handles_ready_event_name=r"Local\pta-finance-native-control-handles-ready",
        mapping_handle=None,
        mapping_view=ctypes.addressof(storage),
        attested_event_handle=None,
        handles_ready_event_handle=None,
    )
    nonce = "c" * (native_sandbox._READY_NONCE_BYTES * 2)

    native_sandbox._write_worker_pipe_handles(
        channel,
        request_handle=101,
        response_handle=102,
        nonce=nonce,
    )

    worker_control = native_worker._WorkerControl(
        mapping_handle=None,
        mapping_view=ctypes.addressof(storage),
        attested_event_handle=None,
        handles_ready_event_handle=None,
    )
    assert native_worker._worker_pipe_handles(worker_control, nonce) == (101, 102)
    with pytest.raises(RuntimeError):
        native_worker._worker_pipe_handles(worker_control, "d" * 64)


def test_direct_handle_transfer_is_explicitly_non_inheritable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[object] = []

    class _DuplicateHandle:
        def __call__(self, *args: object) -> int:
            recorded.extend(args)
            target = ctypes.cast(args[3], ctypes.POINTER(ctypes.c_void_p))
            target.contents.value = 303
            return 1

    class _Kernel32:
        DuplicateHandle = _DuplicateHandle()

        @staticmethod
        def GetCurrentProcess() -> int:
            return 1

    process = native_sandbox.NativeSandboxProcess(
        kernel32=_Kernel32(),
        process_handle=202,
        job_handle=None,
        runtime=None,
        profile_name=None,
        process_id=404,
    )
    monkeypatch.setattr(
        native_sandbox,
        "_windows_apis",
        lambda: (_Kernel32(), object(), object(), object()),
    )

    assert native_sandbox._duplicate_handle_into_worker(101, process) == 303
    assert recorded[4:] == [0, False, native_sandbox._WINDOWS_DUPLICATE_SAME_ACCESS]


def test_worker_process_creation_disables_handle_inheritance() -> None:
    calls: list[tuple[object, ...]] = []

    def creator(*args: object) -> int:
        calls.append(args)
        return 1

    native_sandbox._create_worker_process_without_inherited_handles(
        creator,
        application_name=r"C:\\public\\python.exe",
        command_buffer=ctypes.create_unicode_buffer("public-worker-command"),
        environment=ctypes.create_unicode_buffer("\0\0"),
        working_directory=Path(r"C:\\Windows"),
        startup=native_sandbox._StartupInfoExW(),
        process_information=native_sandbox._ProcessInformation(),
    )

    assert len(calls) == 1
    assert len(calls[0]) == 10
    assert calls[0][2] is None
    assert calls[0][3] is None
    assert calls[0][4] is False


def test_native_launcher_has_no_temporary_handle_inheritance_path() -> None:
    launcher_source = inspect.getsource(native_sandbox._launch_worker)
    startup_source = inspect.getsource(native_sandbox.start_native_pdf_worker)

    assert "HANDLE_LIST" not in launcher_source
    assert "set_handle_inheritable" not in startup_source
    assert "bInheritHandles=True" not in startup_source
    assert "_create_worker_process_without_inherited_handles(" in launcher_source
    assert startup_source.index("_attest_launched_worker_before_handle_transfer") < (
        startup_source.index("_duplicate_handle_into_worker")
    )


def test_token_attribute_pointer_spans_must_stay_inside_the_kernel_buffer() -> None:
    buffer = ctypes.create_string_buffer(16)
    start = ctypes.addressof(buffer)
    end = start + ctypes.sizeof(buffer)

    assert native_worker._pointer_span_is_within_buffer(
        start,
        16,
        buffer_start=start,
        buffer_end=end,
    )
    assert not native_worker._pointer_span_is_within_buffer(
        start - 1,
        1,
        buffer_start=start,
        buffer_end=end,
    )
    assert not native_worker._pointer_span_is_within_buffer(
        end,
        1,
        buffer_start=start,
        buffer_end=end,
    )
    assert not native_worker._pointer_span_is_within_buffer(
        start,
        -1,
        buffer_start=start,
        buffer_end=end,
    )


def test_worker_rejects_a_job_that_allows_breakaway() -> None:
    limits = bank_statements._native_extraction_limits(document_ordinal=1)

    class _QueryInformationJobObject:
        def __call__(
            self,
            job: object,
            information_class: int,
            information: object,
            information_length: int,
            returned_length: object,
        ) -> int:
            del job, information_class, information_length, returned_length
            value = ctypes.cast(
                information,
                ctypes.POINTER(native_worker._ExtendedLimitInformation),
            ).contents
            value.BasicLimitInformation.LimitFlags = (
                native_worker._JOB_OBJECT_REQUIRED_LIMIT_FLAGS
                | native_worker._JOB_OBJECT_LIMIT_BREAKAWAY_OK
            )
            value.BasicLimitInformation.PerProcessUserTimeLimit = (
                limits.worker_cpu_seconds * native_worker._HUNDRED_NANOSECONDS_PER_SECOND
            )
            value.BasicLimitInformation.ActiveProcessLimit = 1
            value.ProcessMemoryLimit = limits.worker_memory_bytes
            value.JobMemoryLimit = limits.worker_memory_bytes
            return 1

    class _Kernel32:
        QueryInformationJobObject = _QueryInformationJobObject()

    assert not native_worker._job_limits_match(limits, _Kernel32())


def test_worker_environment_is_allowlisted_and_ignores_ambient_python_paths(tmp_path: Path) -> None:
    profile_data = tmp_path / "profile"
    profile_data.mkdir()
    interpreter = tmp_path / "runtime" / "python" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    windows_directory = tmp_path / "Windows"
    windows_directory.mkdir()
    runtime = native_sandbox._StagedRuntime(
        root=tmp_path / "runtime",
        interpreter=interpreter,
        site_root=tmp_path / "runtime" / "site",
    )
    profile = SimpleNamespace(local_data=profile_data)

    environment = native_sandbox._build_environment(profile, runtime, windows_directory)
    pairs = ("".join(environment)).rstrip("\0").split("\0")
    values = dict(pair.split("=", 1) for pair in pairs if pair)

    assert set(values) == {
        "APPDATA",
        "ComSpec",
        "LOCALAPPDATA",
        "Path",
        "SystemRoot",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
    assert "PYTHONPATH" not in values
    assert "VIRTUAL_ENV" not in values
    assert values["APPDATA"] == str(profile_data)
    assert values["Path"].startswith(str(interpreter.parent))


def test_public_runtime_copy_rejects_reparse_points_before_copying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    (source / "linked").mkdir(parents=True)

    def is_reparse_point(path: Path) -> bool:
        return path.name == "linked"

    monkeypatch.setattr(native_sandbox, "_is_reparse_point", is_reparse_point)

    with pytest.raises(native_sandbox.NativeSandboxUnavailable):
        native_sandbox._assert_public_tree_has_no_reparse_points(source)


@pytest.mark.skipif(
    os.name != "nt", reason="Windows reparse-point attributes are the enforcement API"
)
def test_public_runtime_copy_rejects_an_actual_directory_symlink_when_supported(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    link = source / "linked"
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError as error:
        pytest.skip(
            "directory symlinks are unavailable in this test environment: "
            f"{getattr(error, 'winerror', None)}"
        )

    with pytest.raises(native_sandbox.NativeSandboxUnavailable):
        native_sandbox._assert_public_tree_has_no_reparse_points(source)


def test_public_runtime_stages_only_the_native_worker_module_closure(tmp_path: Path) -> None:
    source_root = tmp_path / "source" / "pta_finance"
    destination = tmp_path / "stage" / "pta_finance"
    for relative_path in native_sandbox._PUBLIC_WORKER_PACKAGE_FILES:
        source = source_root / relative_path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(relative_path.as_posix(), encoding="utf-8")
    unrelated = source_root / "unrelated_private_surface.py"
    unrelated.write_text("must not be staged", encoding="utf-8")

    native_sandbox._copy_public_worker_package(source_root, destination)

    assert {path.relative_to(destination) for path in destination.rglob("*.py")} == set(
        native_sandbox._PUBLIC_WORKER_PACKAGE_FILES
    )
    assert not (destination / unrelated.name).exists()


def test_runtime_acl_setup_uses_the_verified_system_icacls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    windows_directory = tmp_path / "Windows"
    system_directory = windows_directory / "System32"
    system_directory.mkdir(parents=True)
    icacls = system_directory / "icacls.exe"
    cmd = system_directory / "cmd.exe"
    icacls.touch()
    cmd.touch()
    runtime = native_sandbox._StagedRuntime(
        root=runtime_root,
        interpreter=runtime_root / "python.exe",
        site_root=runtime_root / "site",
    )
    calls: list[tuple[list[str], dict[str, object]]] = []

    class _CompletedProcess:
        returncode = 0

    def run(command: list[str], **kwargs: object) -> _CompletedProcess:
        calls.append((command, kwargs))
        return _CompletedProcess()

    monkeypatch.setattr(
        native_sandbox, "_windows_apis", lambda: (object(), object(), object(), object())
    )
    monkeypatch.setattr(
        native_sandbox,
        "_trusted_windows_directory",
        lambda _kernel32: windows_directory,
    )
    monkeypatch.setattr(native_sandbox.subprocess, "run", run)

    native_sandbox._grant_runtime_access(runtime, "S-1-15-2-123")

    assert calls == [
        (
            [
                str(icacls),
                str(runtime_root),
                "/grant:r",
                "*S-1-15-2-123:(OI)(CI)(RX)",
                "/T",
                "/C",
            ],
            {
                "check": False,
                "capture_output": True,
                "text": False,
                "cwd": str(windows_directory),
                "env": {
                    "ComSpec": str(cmd),
                    "SystemRoot": str(windows_directory),
                    "WINDIR": str(windows_directory),
                },
            },
        )
    ]


def test_non_windows_native_sandbox_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native_sandbox, "os", SimpleNamespace(name="posix"))

    with pytest.raises(native_sandbox.NativeSandboxUnavailable):
        native_sandbox.start_native_pdf_worker(
            document_ordinal=1,
            limits_json="{}",
            worker_memory_bytes=1,
            worker_cpu_seconds=1,
            ready_timeout_seconds=0.1,
        )


def test_failed_cleanup_is_retained_and_blocks_a_new_worker_before_profile_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed teardown remains owned and blocks later private parsing attempts."""

    process = native_sandbox.NativeSandboxProcess(
        kernel32=object(),
        process_handle=None,
        job_handle=None,
        runtime=None,
        profile_name=None,
    )
    attempts = 0

    def fail_cleanup() -> None:
        nonlocal attempts
        attempts += 1
        raise native_sandbox.NativeSandboxUnavailable

    def finish_cleanup() -> None:
        nonlocal attempts
        attempts += 1

    monkeypatch.setattr(process, "_close_once", fail_cleanup)
    with pytest.raises(native_sandbox.NativeSandboxUnavailable):
        process.close()
    assert process in native_sandbox._DEFERRED_SANDBOX_PROCESSES
    assert not native_sandbox._retry_deferred_sandbox_cleanup()

    profile_creation_attempted = False

    def unexpected_profile_creation() -> object:
        nonlocal profile_creation_attempted
        profile_creation_attempted = True
        raise AssertionError("deferred cleanup must fail before profile creation")

    monkeypatch.setattr(native_sandbox, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        native_sandbox, "_create_app_container_profile", unexpected_profile_creation
    )
    with pytest.raises(native_sandbox.NativeSandboxUnavailable):
        native_sandbox.start_native_pdf_worker(
            document_ordinal=1,
            limits_json="{}",
            worker_memory_bytes=1,
            worker_cpu_seconds=1,
            ready_timeout_seconds=0.1,
        )
    assert not profile_creation_attempted

    monkeypatch.setattr(process, "_close_once", finish_cleanup)
    try:
        assert native_sandbox._retry_deferred_sandbox_cleanup()
    finally:
        monkeypatch.setattr(process, "_close_once", finish_cleanup)
        native_sandbox._retry_deferred_sandbox_cleanup()
    assert attempts >= 3


@pytest.mark.skipif(os.name != "nt", reason="LPAC is a Windows-only enforcement boundary")
def test_windows_worker_is_ready_before_any_private_pdf_bytes_are_sent() -> None:
    limits = bank_statements._native_extraction_limits(document_ordinal=1)
    session = native_sandbox.start_native_pdf_worker(
        document_ordinal=1,
        limits_json=bank_statements._serialize_native_limits(limits),
        worker_memory_bytes=limits.worker_memory_bytes,
        worker_cpu_seconds=limits.worker_cpu_seconds,
        ready_timeout_seconds=5.0,
    )

    try:
        assert session.process.is_alive()
        assert not os.get_handle_inheritable(session.request_sender.fileno())
        assert not os.get_handle_inheritable(session.response_receiver.fileno())
    finally:
        session.request_sender.close()
        session.response_receiver.close()
        session.process.close()
    assert not session.process.is_alive()


@pytest.mark.skipif(os.name != "nt", reason="LPAC is a Windows-only enforcement boundary")
def test_windows_worker_rejects_a_launcher_without_the_lpac_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_update_attribute = native_sandbox._update_attribute
    duplicate_attempted = False

    def omit_lpac_policy(
        kernel32: object,
        attribute_list: int,
        attribute: int,
        value: object,
        size: int,
    ) -> None:
        if (
            attribute
            == native_sandbox._WINDOWS_PROC_THREAD_ATTRIBUTE_ALL_APPLICATION_PACKAGES_POLICY
        ):
            return
        original_update_attribute(kernel32, attribute_list, attribute, value, size)

    monkeypatch.setattr(native_sandbox, "_update_attribute", omit_lpac_policy)

    def unexpected_duplicate(source_handle: int, process: object) -> int:
        del source_handle, process
        nonlocal duplicate_attempted
        duplicate_attempted = True
        raise AssertionError("an unverified worker must not receive a PDF-channel handle")

    monkeypatch.setattr(native_sandbox, "_duplicate_handle_into_worker", unexpected_duplicate)
    limits = bank_statements._native_extraction_limits(document_ordinal=1)

    session: native_sandbox.NativeWorkerSession | None = None
    try:
        with pytest.raises(native_sandbox.NativeSandboxUnavailable):
            session = native_sandbox.start_native_pdf_worker(
                document_ordinal=1,
                limits_json=bank_statements._serialize_native_limits(limits),
                worker_memory_bytes=limits.worker_memory_bytes,
                worker_cpu_seconds=limits.worker_cpu_seconds,
                ready_timeout_seconds=5.0,
            )
    finally:
        if session is not None:
            session.request_sender.close()
            session.response_receiver.close()
            session.process.close()
    assert not duplicate_attempted
