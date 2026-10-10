"""Decode-budget corpus: untrusted decode cost is a measured property of the production entry.

Every case runs the production ``receipt_pages.to_pages`` inside an **outer** test process that
is itself limited (a Windows Job Object, or Linux rlimits) and timed, so a regression that moved
decoding back into the broker fails as "outer budget breached" instead of exhausting CI. No test
substitutes an in-process decode for the decode child (plan § 5A, § 9).

Inputs are synthesized here from ``hashlib.shake_256`` seeds; nothing binary is committed.
``scripts/calibrate_receipt_decode.py`` loads this module by path for its anchor generators and
outer harness, so the calibration and the gate measure exactly the same thing.
"""

from __future__ import annotations

import ctypes
import dataclasses
import functools
import hashlib
import io
import json
import math
import os
import struct
import subprocess
import sys
import zlib
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from pta_finance import process_limits, receipt_decode
from pta_finance.receipt_geometry import NORMALIZATION

MiB = 1024 * 1024
LIFTED = 2**31 - 1
# The amplifier corpus runs under these pinned limits; every generated input stays small.
CORPUS_MEMORY_BYTES = 512 * MiB
CORPUS_CPU_SECONDS = 2
CORPUS_WALL_SECONDS = 6
CORPUS_MAX_INPUT_BYTES = 4 * MiB
# A lowered child must still clear its own interpreter baseline, so a budget refusal is never
# really a failed start.
MIN_TEST_MEMORY_BYTES = 256 * MiB
SPAWN_SLACK_S = 5
BROKER_GROWTH_ALLOWANCE = 64 * MiB
OUTER_THREAD_AS_ALLOWANCE = 512 * MiB
# Sentinel sizes, measured on the Windows dev box (Pillow 12.2.0) and recorded in the step
# checkpoint: the 400-scan generator at CPU_BOUND_SIZE needs about 15 s of CPU uncapped, at
# WALL_CLOCK_SIZE about 24 s. The strip's edge is lengthened if memory_bytes approaches its need.
STRIP_EDGE = 44_700_000
CPU_BOUND_SIZE = (8000, 6000)
WALL_CLOCK_SIZE = (10000, 7900)
CORPUS_SCANS = 400
MOTION_TRAILER_BYTES = 8 * MiB


def _headroom_overrides() -> dict[str, int]:
    """Plan § 5A's tolerance band: 80% of each production limit, carried as integers."""

    memory = NORMALIZATION.memory_bytes * 4 // 5 // MiB * MiB
    return {
        "memory_bytes": memory,
        "cpu_seconds": math.ceil(0.8 * NORMALIZATION.cpu_seconds),
        "wall_seconds": math.ceil(0.8 * NORMALIZATION.wall_seconds),
    }


_TEST_MEMORY_VALUES = (CORPUS_MEMORY_BYTES, _headroom_overrides()["memory_bytes"])
assert all(value >= MIN_TEST_MEMORY_BYTES for value in _TEST_MEMORY_VALUES)
assert _headroom_overrides()["cpu_seconds"] < NORMALIZATION.cpu_seconds
assert _headroom_overrides()["wall_seconds"] < NORMALIZATION.wall_seconds


# --- The outer harness ---------------------------------------------------------------------

HARNESS = r"""
import ctypes, dataclasses, json, os, sys, time

job = json.loads(sys.stdin.buffer.readline())
overrides = dict(job["overrides"])
if "background" in overrides:
    overrides["background"] = tuple(overrides["background"])

def proc_status(field):
    with open("/proc/self/status", "rb") as status:
        for line in status:
            if line.startswith(field.encode() + b":"):
                return int(line.split()[1]) * 1024
    raise RuntimeError(field)

from pta_finance import receipt_geometry
norm = dataclasses.replace(receipt_geometry.NORMALIZATION, **overrides)
if sys.platform == "linux":
    import resource
    limit_as = 2 * norm.memory_bytes + proc_status("VmSize") + job["thread_allowance"]
    resource.setrlimit(resource.RLIMIT_AS, (limit_as, limit_as))
    cpu = 2 * norm.cpu_seconds
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))

from dataclasses import dataclass
from pathlib import Path
import PIL.Image
from pta_finance import receipt_pages

receipt_pages.NORMALIZATION = norm
seen = {}
real_map = receipt_pages._map_child_exit
def observing_map(result, request):
    seen.update(returncode=result.returncode, timed_out=result.timed_out,
                overflowed=result.overflowed)
    return real_map(result, request)
receipt_pages._map_child_exit = observing_map
real_ready = receipt_pages._parse_ready_line
def observing_ready(line, **options):
    value = real_ready(line, **options)
    seen.update(pid=options["expected_pid"], rlimit_as=value)
    if sys.platform == "linux":
        # Read while the child is alive and waiting for its body.
        with open(f"/proc/{options['expected_pid']}/limits") as limits:
            rows = {row[:26].strip(): row[26:].split()[:2] for row in limits}
        seen["rlimits"] = {"cpu": rows["Max cpu time"], "as": rows["Max address space"]}
    return value
receipt_pages._parse_ready_line = observing_ready
real_job = receipt_pages.process_limits.make_job_object
def observing_job(kernel32, **limits):
    seen["job_limits"] = limits
    return real_job(kernel32, **limits)
receipt_pages.process_limits.make_job_object = observing_job

if sys.platform == "win32":
    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
            (name, ctypes.c_size_t) for name in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                "PagefileUsage", "PeakPagefileUsage")]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    getter = kernel32.K32GetProcessMemoryInfo
    getter.argtypes = (ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong)
    def counters():
        value = Counters(); value.cb = ctypes.sizeof(Counters)
        assert getter(kernel32.GetCurrentProcess(), ctypes.byref(value), value.cb)
        return value
    before = counters().PagefileUsage
    def growth():
        return counters().PeakPagefileUsage - before
    def child_alive(pid):
        opener = kernel32.OpenProcess
        opener.restype = ctypes.c_void_p
        handle = opener(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return code.value == 259
else:
    try:
        with open("/proc/self/clear_refs", "w") as refs:
            refs.write("5")
    except OSError:
        pass
    before = proc_status("VmRSS")
    def growth():
        return proc_status("VmHWM") - before
    def child_alive(pid):
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

@dataclass(frozen=True)
class Asset:
    asset_id: str
    media_type: str
    path: Path

record = {"input_bytes": os.path.getsize(job["path"])}
started = time.monotonic()
try:
    (page,) = receipt_pages.to_pages(
        Asset(job["asset_id"], job["media_type"], Path(job["path"])),
        pages_dir=Path(job["pages_dir"]), ticket_ref="EX-01", asset_ordinal=1,
        progress=lambda event: record.update(
            usage=dataclasses.asdict(event.decode_usage),
            orientation=event.orientation,
        ),
    )
    record.update(outcome="page", width=page.width, height=page.height, sha256=page.sha256)
except receipt_pages.ReceiptPageError as refusal:
    record.update(outcome="refused", reason=refusal.reason,
                  usage=None if refusal.usage is None else dataclasses.asdict(refusal.usage))
except BaseException as error:
    record.update(outcome="error", error=type(error).__name__)
record.update(
    outer_wall=time.monotonic() - started,
    broker_growth=growth(),
    child=seen,
    child_alive_after=bool(seen.get("pid")) and child_alive(seen["pid"]),
)
sys.stdout.write(json.dumps(record) + "\n")
sys.stdout.flush()
"""


def run_in_outer(
    data: bytes, media_type: str, *, overrides: dict[str, Any], root: Path
) -> dict[str, Any]:
    """Run production ``to_pages`` on ``data`` in a limited outer process; its result line.

    ``overrides`` replace ``NORMALIZATION`` fields inside the outer process (so they reach the
    request the real child receives). The outer is capped at twice the child's memory and CPU
    and timed at ``wall_seconds + SPAWN_SLACK_S``; it is launched as one process exactly like
    the child and limited before it reads its input line.
    """

    norm = dataclasses.replace(NORMALIZATION, **overrides)
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    path = root / f"{digest}.{'jpg' if media_type == 'jpeg' else 'png'}"
    path.write_bytes(data)
    job = {
        "overrides": {
            key: list(value) if key == "background" else value for key, value in overrides.items()
        },
        "asset_id": f"asset:v1:{digest}",
        "media_type": media_type,
        "path": str(path),
        "pages_dir": str(root / "pages"),
        "thread_allowance": OUTER_THREAD_AS_ALLOWANCE,
    }
    temp = root / "tmp"
    temp.mkdir(exist_ok=True)
    timeout = norm.wall_seconds + SPAWN_SLACK_S
    if sys.platform == "win32":
        kernel32 = process_limits.load_kernel32()
        outer_job = process_limits.make_job_object(
            kernel32,
            memory_bytes=2 * norm.memory_bytes,
            cpu_seconds=2 * norm.cpu_seconds,
            active_processes=2,
        )
        command = [sys._base_executable, "-I", "-c", HARNESS]
        environment = {
            "SYSTEMROOT": os.environ["SYSTEMROOT"],
            "__PYVENV_LAUNCHER__": sys.executable,
            "TEMP": str(temp),
            "TMP": str(temp),
        }
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            creationflags=0x08000000,
        )
        try:
            handle = int(process._handle)
            process_limits.assign_process(kernel32, outer_job, handle)
            assert process_limits.is_process_in_job(kernel32, handle, outer_job)
            stdout, stderr = process.communicate((json.dumps(job) + "\n").encode(), timeout=timeout)
        except subprocess.TimeoutExpired:
            process_limits.terminate_job(kernel32, outer_job)
            process.communicate()
            pytest.fail(f"outer budget breached: no result within {timeout} s")
        finally:
            process_limits.close_handle(kernel32, outer_job)
    else:
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", HARNESS],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"TMPDIR": str(temp)},
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate((json.dumps(job) + "\n").encode(), timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, 9)
            process.communicate()
            pytest.fail(f"outer budget breached: no result within {timeout} s")
    lines = stdout.decode("ascii", "replace").splitlines()
    if process.returncode != 0 or not lines:
        pytest.fail(
            f"outer budget breached: exit {process.returncode}; "
            f"{stderr.decode('utf-8', 'replace')[-600:]}"
        )
    result: dict[str, Any] = json.loads(lines[-1])
    result["norm"] = dataclasses.asdict(norm)
    return result


def assert_within_budget(result: dict[str, Any]) -> None:
    """A page or a per-asset refusal, inside the wall clock, with the cost in the child."""

    norm = result["norm"]
    assert result["outcome"] in {"page", "refused"}, result
    assert result["outer_wall"] <= norm["wall_seconds"] + SPAWN_SLACK_S, result
    page = result["width"] * result["height"] * 3 if result["outcome"] == "page" else 0
    allowance = result["input_bytes"] + page + BROKER_GROWTH_ALLOWANCE
    assert result["broker_growth"] <= allowance, f"decode cost reached the broker: {result}"
    assert not result["child_alive_after"], result


# --- Input generators (fictional, seeded, synthesized here) ---------------------------------


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _png(
    width: int,
    height: int,
    rows: Callable[[int], bytes],
    *,
    color: int = 0,
    before_idat: bytes = b"",
    after_idat: bytes = b"",
    raw: Iterable[bytes] | None = None,
) -> bytes:
    """A PNG streamed through zlib, so large anchors never exist unpacked here.

    ``rows(y)`` gives each row's pixels (filter type 0 is prepended), or ``raw`` gives the whole
    filtered stream in chunks.
    """

    compressor = zlib.compressobj(6)
    stream = raw if raw is not None else (b"\x00" + rows(y) for y in range(height))
    parts = [compressor.compress(chunk) for chunk in stream]
    parts.append(compressor.flush())
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, color, 0, 0, 0))
        + before_idat
        + _png_chunk(b"IDAT", b"".join(parts))
        + after_idat
        + _png_chunk(b"IEND", b"")
    )


def _small_png() -> bytes:
    return _png(64, 32, lambda y: bytes([255 - 4 * y]) * 64)


def _small_jpeg(**options: Any) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 48), "white").save(buffer, "JPEG", **options)
    return buffer.getvalue()


def _app(marker: int, body: bytes) -> bytes:
    return bytes([0xFF, marker]) + struct.pack(">H", len(body) + 2) + body


def _after_soi(jpeg: bytes, segments: bytes) -> bytes:
    return jpeg[:2] + segments + jpeg[2:]


def progressive_scans(
    size: tuple[int, int], scans: int = CORPUS_SCANS, *, layout: str = "plain"
) -> bytes:
    """A white progressive JPEG whose final scan repeats until the file holds ``scans`` scans.

    ``layout`` inserts bytes libjpeg and Pillow tolerate: a junk byte or a stuffed ``FF 00``
    after JFIF, or a zero-length APP15 segment.
    """

    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(buffer, "JPEG", progressive=True)
    data = buffer.getvalue()
    last = data.rindex(b"\xff\xda")
    extra = scans - data.count(b"\xff\xda")
    data = data[:-2] + data[last:-2] * extra + b"\xff\xd9"
    insert = {"plain": b"", "junk": b"\x00", "stuffed": b"\xff\x00", "short": b"\xff\xef\x00\x00"}
    app0_end = 4 + int.from_bytes(data[4:6], "big")
    return data[:app0_end] + insert[layout] + data[app0_end:]


def _exif_ifd_flood() -> bytes:
    # Four EXIF segments, 256 KiB in all, each an IFD whose entries all point at one large value.
    count, value = 2700, 32_000
    ifd = struct.pack(">H", count)
    data_offset = 8 + 2 + 12 * count + 4
    for index in range(count):
        ifd += struct.pack(">HHII", 0x9000 + index % 64, 7, value, data_offset)
    tiff = b"MM\x00*" + struct.pack(">I", 8) + ifd + b"\x00" * 4 + bytes(value)
    segment = _app(0xE1, b"Exif\x00\x00" + tiff[: 65_533 - 6])
    return _after_soi(_small_jpeg(), segment * 4)


def _mpf_short_index() -> bytes:
    # A 64 KiB multi-picture index whose entries are SHORT-typed and share one large value.
    count, value_shorts = 2700, 16_000
    ifd = struct.pack(">H", count)
    data_offset = 8 + 2 + 12 * count + 4
    for index in range(count):
        ifd += struct.pack(">HHII", 0xB000 + index % 3, 3, value_shorts, data_offset)
    tiff = b"MM\x00*" + struct.pack(">I", 8) + ifd + b"\x00" * 4 + bytes(2 * value_shorts)
    return _after_soi(_small_jpeg(), _app(0xE2, b"MPF\x00" + tiff[: 65_533 - 4]))


@functools.cache
def strip_png(wide: bool, edge: int = STRIP_EDGE) -> bytes:
    """A 1-pixel-high or 1-pixel-wide grayscale strip ``edge`` pixels long."""

    if wide:
        return _png(edge, 1, lambda y: bytes(edge))
    rows, remainder = divmod(edge, 1_000_000)
    raw = [b"\x00\xff" * 1_000_000] * rows + [b"\x00\xff" * remainder]
    return _png(1, edge, lambda y: b"", raw=raw)


def _compressed_text_chunks(kind: bytes, prefix: bytes, count: int) -> bytes:
    inflated = zlib.compress(bytes(1_000_000), 9)
    return _png_chunk(kind, prefix + inflated) * count


def _iccp_flood(after_idat: bool = False) -> bytes:
    chunks = _compressed_text_chunks(b"iCCP", b"fictional\x00\x00", 3000)
    if after_idat:
        return _png(64, 32, lambda y: bytes(64), after_idat=chunks)
    return _png(64, 32, lambda y: bytes(64), before_idat=chunks)


def _ztxt_empty_key_flood() -> bytes:
    chunks = _compressed_text_chunks(b"zTXt", b"\x00\x00", 3000)
    return _png(64, 32, lambda y: bytes(64), before_idat=chunks)


def _itxt_invalid_utf8_flood() -> bytes:
    chunks = _compressed_text_chunks(b"iTXt", b"\xff\xfe\x00\x01\x00\x00\x00", 3000)
    return _png(64, 32, lambda y: bytes(64), before_idat=chunks)


def _repeated_sof() -> bytes:
    jpeg = _small_jpeg()
    components = 21_000
    body = struct.pack(">BHHB", 8, 48, 64, components % 256) + b"\x01\x11\x00" * components
    return _after_soi(jpeg, _app(0xC0, body[:65_533]) * 60)


def _empty_app_flood() -> bytes:
    return _after_soi(_small_jpeg(), b"\xff\xe5\x00\x02" * (CORPUS_MAX_INPUT_BYTES // 4 - 512))


def _ff_fill_flood() -> bytes:
    jpeg = _small_jpeg()
    return jpeg[:2] + b"\xff" * (CORPUS_MAX_INPUT_BYTES - len(jpeg)) + jpeg[2:]


def _ff00_flood() -> bytes:
    jpeg = _small_jpeg()
    return jpeg[:2] + b"\xff\x00" * ((CORPUS_MAX_INPUT_BYTES - len(jpeg)) // 2) + jpeg[2:]


def _giant_chrm() -> bytes:
    chunk = _png_chunk(b"cHRM", hashlib.shake_256(b"fictional-chrm").digest(4 * MiB - 1024))
    return _png(64, 32, lambda y: bytes(64), before_idat=chunk)


def _private_chunk_flood() -> bytes:
    payload = hashlib.shake_256(b"fictional-private").digest(1000)
    chunks = _png_chunk(b"prVt", payload) * 1900
    return _png(64, 32, lambda y: bytes(64), before_idat=chunks, after_idat=chunks)


def _icc_fragment_flood() -> bytes:
    fragment = b"ICC_PROFILE\x00\x01\xff" + hashlib.shake_256(b"fictional-icc").digest(16_000)
    return _after_soi(_small_jpeg(), _app(0xE2, fragment) * 250)


AMPLIFIERS: dict[str, Callable[[], tuple[bytes, str]]] = {
    "progressive-400-scans": lambda: (progressive_scans((2000, 1500)), "jpeg"),
    "progressive-400-scans-junk": lambda: (progressive_scans((2000, 1500), layout="junk"), "jpeg"),
    "progressive-400-scans-stuffed": lambda: (
        progressive_scans((2000, 1500), layout="stuffed"),
        "jpeg",
    ),
    "progressive-400-scans-short-length": lambda: (
        progressive_scans((2000, 1500), layout="short"),
        "jpeg",
    ),
    "exif-ifd-flood": lambda: (_exif_ifd_flood(), "jpeg"),
    "mpf-short-index": lambda: (_mpf_short_index(), "jpeg"),
    "png-strip-wide": lambda: (strip_png(wide=True), "png"),
    "png-strip-tall": lambda: (strip_png(wide=False), "png"),
    "png-iccp-flood": lambda: (_iccp_flood(), "png"),
    "png-ztxt-empty-key-flood": lambda: (_ztxt_empty_key_flood(), "png"),
    "png-itxt-invalid-utf8-flood": lambda: (_itxt_invalid_utf8_flood(), "png"),
    "png-post-idat-iccp-flood": lambda: (_iccp_flood(after_idat=True), "png"),
    "jpeg-repeated-sof": lambda: (_repeated_sof(), "jpeg"),
    "jpeg-empty-appn-flood": lambda: (_empty_app_flood(), "jpeg"),
    "jpeg-ff-fill-flood": lambda: (_ff_fill_flood(), "jpeg"),
    "jpeg-ff00-flood": lambda: (_ff00_flood(), "jpeg"),
    "png-giant-chrm": lambda: (_giant_chrm(), "png"),
    "png-private-chunk-flood": lambda: (_private_chunk_flood(), "png"),
    "jpeg-icc-fragment-flood": lambda: (_icc_fragment_flood(), "jpeg"),
}


@functools.cache
def _rgba_80mp() -> bytes:
    # 10000 x 8000 receipt-like RGBA: opaque white, dark text bands, a transparent margin.
    width, height = 10_000, 8_000
    tile = hashlib.shake_256(b"fictional-rgba-ink").digest(64)
    ink = b"".join(bytes([tile[i % 64] // 4] * 3) + b"\xff" for i in range(width // 2))
    margin = b"\xff\xff\xff\x00" * (width - width // 2)
    white = b"\xff\xff\xff\xff" * (width // 2) + margin
    text = ink + margin
    return _png(width, height, lambda y: text if (y // 40) % 9 == 0 else white, color=6)


@functools.cache
def _cmyk_80mp_progressive() -> bytes:
    image = Image.new("CMYK", (10_000, 8_000), (0, 0, 0, 0))
    for y in range(500, 8_000, 700):
        image.paste((0, 0, 0, 255), (800, y, 9_200, y + 60))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", progressive=True, quality=90)
    del image
    data = buffer.getvalue()
    last = data.rindex(b"\xff\xda")
    return data[:-2] + data[last:-2] * (64 - data.count(b"\xff\xda")) + b"\xff\xd9"


def _photo(size: tuple[int, int], seed: bytes) -> Image.Image:
    # A smooth fictional "photo": a seeded colour gradient, which compresses like a real one.
    small = Image.frombytes("RGB", (16, 12), hashlib.shake_256(seed).digest(16 * 12 * 3))
    return small.resize(size, Image.Resampling.BICUBIC)


@functools.cache
def _motion_photo() -> bytes:
    buffer = io.BytesIO()
    _photo((4032, 3024), b"fictional-still").save(buffer, "JPEG", quality=90)
    trailer = hashlib.shake_256(b"fictional-motion-clip").digest(MOTION_TRAILER_BYTES)
    return buffer.getvalue() + trailer


@functools.cache
def _mpo_per_frame_exif() -> bytes:
    frames = []
    for index in range(3):
        exif = Image.Exif()
        exif[0x0112] = 1
        exif[0x010E] = f"fictional frame {index}"
        exif[0x927C] = bytes(20_000)
        frame = _photo((1600, 1200), b"fictional-frame-%d" % index)
        frame.encoderinfo = {"exif": exif.tobytes()}
        frames.append((frame, exif.tobytes()))
    buffer = io.BytesIO()
    primary, primary_exif = frames[0]
    primary.save(
        buffer,
        "MPO",
        save_all=True,
        append_images=[frame for frame, _ in frames[1:]],
        exif=primary_exif,
        quality=90,
    )
    return buffer.getvalue()


@functools.cache
def _phone_exif_thumbnail() -> bytes:
    thumb = io.BytesIO()
    _photo((160, 120), b"fictional-thumb").save(thumb, "JPEG")
    thumbnail = thumb.getvalue()
    maker_note = bytes(60_000)
    ifd0 = 8
    ifd1 = ifd0 + 2 + 2 * 12 + 4
    maker_offset = ifd1 + 2 + 2 * 12 + 4
    thumb_offset = maker_offset + len(maker_note)
    tiff = b"MM\x00*" + struct.pack(">I", ifd0)
    tiff += struct.pack(">H", 2)
    tiff += struct.pack(">HHIHH", 0x0112, 3, 1, 6, 0)
    tiff += struct.pack(">HHII", 0x927C, 7, len(maker_note), maker_offset)
    tiff += struct.pack(">I", ifd1)
    tiff += struct.pack(">H", 2)
    tiff += struct.pack(">HHII", 0x0201, 4, 1, thumb_offset)
    tiff += struct.pack(">HHII", 0x0202, 4, 1, len(thumbnail))
    tiff += struct.pack(">I", 0) + maker_note + thumbnail
    buffer = io.BytesIO()
    _photo((4000, 3000), b"fictional-phone").save(
        buffer, "JPEG", quality=92, exif=b"Exif\x00\x00" + tiff
    )
    return buffer.getvalue()


ANCHORS: dict[str, Callable[[], tuple[bytes, str]]] = {
    "png-80mp-rgba": lambda: (_rgba_80mp(), "png"),
    "jpeg-80mp-cmyk-progressive": lambda: (_cmyk_80mp_progressive(), "jpeg"),
    "jpeg-motion-photo": lambda: (_motion_photo(), "jpeg"),
    "mpo-per-frame-exif": lambda: (_mpo_per_frame_exif(), "jpeg"),
    "jpeg-phone-exif-thumbnail": lambda: (_phone_exif_thumbnail(), "jpeg"),
}
HEADROOM_ANCHORS = ("png-80mp-rgba", "jpeg-80mp-cmyk-progressive")

SENTINELS: dict[str, tuple[Callable[[], tuple[bytes, str]], dict[str, Any]]] = {
    # Memory: the LANCZOS coefficient buffer of a 1 x 44.7M strip, with the fast paths lifted so
    # the strip reaches the resampler, at the production memory limit.
    "lanczos-strip": (
        lambda: (strip_png(wide=False), "png"),
        {"max_source_edge": LIFTED, "max_source_pixels": LIFTED},
    ),
    # CPU: the 400-scan generator under an explicitly lowered 1 s CPU limit.
    "cpu-bound": (lambda: (progressive_scans(CPU_BOUND_SIZE), "jpeg"), {"cpu_seconds": 1}),
    # Wall: CPU raised far above the decode's need, the wall clock lowered below it.
    "wall-clock": (
        lambda: (progressive_scans(WALL_CLOCK_SIZE), "jpeg"),
        {"cpu_seconds": 60, "wall_seconds": 8},
    ),
}


def _corpus_overrides(ceilings: str) -> dict[str, Any]:
    overrides: dict[str, Any] = {
        "memory_bytes": CORPUS_MEMORY_BYTES,
        "cpu_seconds": CORPUS_CPU_SECONDS,
        "wall_seconds": CORPUS_WALL_SECONDS,
    }
    if ceilings == "lifted":
        overrides.update(max_source_pixels=LIFTED, max_source_edge=LIFTED)
    return overrides


# --- Tests ----------------------------------------------------------------------------------


def test_the_corpus_inputs_are_small_and_the_floor_holds() -> None:
    for name, generate in AMPLIFIERS.items():
        data, _ = generate()
        assert len(data) <= CORPUS_MAX_INPUT_BYTES, name
    motion, _ = ANCHORS["jpeg-motion-photo"]()
    assert motion.count(b"\xff\xda") > 64, "a raw scan count would have refused this photo"
    assert all(len(ANCHORS[name]()[0]) <= NORMALIZATION.max_source_bytes for name in ANCHORS)


@pytest.mark.parametrize("ceilings", ["fast-paths", "lifted"])
@pytest.mark.parametrize("vector", list(AMPLIFIERS))
def test_amplifier_ends_within_budget(tmp_path: Path, vector: str, ceilings: str) -> None:
    # Guard independence: the same corpus with every numeric fast-path ceiling lifted must stay
    # within budget too, which proves the invariant is the boundary, not the guards.
    data, media_type = AMPLIFIERS[vector]()
    result = run_in_outer(data, media_type, overrides=_corpus_overrides(ceilings), root=tmp_path)
    assert_within_budget(result)
    if vector.startswith("png-strip") and ceilings == "fast-paths":
        assert result["reason"] == "source-edge", "the strip is refused from its header"


@pytest.mark.parametrize("anchor", list(ANCHORS))
def test_known_good_anchor_yields_page(tmp_path: Path, anchor: str) -> None:
    data, media_type = ANCHORS[anchor]()
    result = run_in_outer(data, media_type, overrides={}, root=tmp_path)
    assert result["outcome"] == "page", result
    assert_within_budget(result)


@pytest.mark.parametrize("anchor", HEADROOM_ANCHORS)
def test_known_good_anchor_has_headroom(tmp_path: Path, anchor: str) -> None:
    data, media_type = ANCHORS[anchor]()
    result = run_in_outer(data, media_type, overrides=_headroom_overrides(), root=tmp_path)
    assert result["outcome"] == "page", f"the calibrated margin has eroded: {result}"
    assert_within_budget(result)


@pytest.mark.parametrize("sentinel", list(SENTINELS))
def test_known_garbage_sentinel_is_refused(tmp_path: Path, sentinel: str) -> None:
    generate, overrides = SENTINELS[sentinel]
    data, media_type = generate()
    result = run_in_outer(data, media_type, overrides=overrides, root=tmp_path)
    assert result["outcome"] == "refused" and result["reason"] == "budget", result
    assert_within_budget(result)
    usage = result["usage"]
    assert usage is not None, "the child ran and was measured"
    if sentinel == "lanczos-strip":
        # A MemoryError at the resampler, not any non-zero exit and not a fast-path refusal.
        assert result["child"]["returncode"] == receipt_decode.EXIT_BUDGET, result
    elif sentinel == "cpu-bound":
        limit = overrides["cpu_seconds"]
        assert usage["cpu_seconds"] >= 0.9 * limit, "it ran up to its CPU limit"
        if sys.platform == "win32":
            # The child's own Job carried the lowered limit, and a Job limit ended the child
            # (STATUS_QUOTA_EXCEEDED). Windows enforces PerProcessUserTimeLimit with a lag, so
            # the measured CPU cannot separate the child's limit from the outer's doubled one.
            assert result["child"]["job_limits"]["cpu_seconds"] == limit, result
            assert result["child"]["returncode"] == 0xC0000044, result
        else:
            # RLIMIT_CPU with soft = hard is a SIGKILL at the limit, precisely: the child's own
            # limit ended it, not the outer process's doubled one.
            assert result["child"]["rlimits"]["cpu"] == [str(limit)] * 2, result
            assert result["child"]["returncode"] == -9, result
            assert usage["cpu_seconds"] < 2 * limit, result
    else:
        assert result["child"]["timed_out"] is True, result
        assert usage["wall_seconds"] < overrides["cpu_seconds"], "the wall clock ended it"


def test_production_fast_paths_refuse_the_strip_from_its_header(tmp_path: Path) -> None:
    result = run_in_outer(strip_png(wide=False), "png", overrides={}, root=tmp_path)
    assert (result["outcome"], result["reason"]) == ("refused", "source-edge"), result
    assert result["child"]["returncode"] == 0


def test_decoding_pid_is_the_limited_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pta_finance import receipt_pages

    observed: dict[str, Any] = {}
    jobs: list[int] = []
    real_assign = process_limits.assign_process
    real_ready = receipt_pages._parse_ready_line

    def recording_assign(kernel32: Any, job: int, process_handle: int) -> None:
        jobs.append(job)
        real_assign(kernel32, job, process_handle)

    def checking_ready(line: bytes | None, **options: Any) -> int | None:
        rlimit_as = real_ready(line, **options)
        assert line is not None
        pid = json.loads(line)["pid"]
        observed["pid"], observed["popen_pid"] = pid, options["expected_pid"]
        # The child is alive and blocked on its body: inspect the process that holds the pid.
        if sys.platform == "win32":
            kernel32 = process_limits.load_kernel32()
            opener = kernel32.OpenProcess
            opener.restype = ctypes.c_void_p
            handle = opener(0x1000, False, pid)
            assert handle, "the ready line's pid must name a live process"
            try:
                (job,) = jobs
                assert process_limits.is_process_in_job(kernel32, int(handle), job)
            finally:
                process_limits.close_handle(kernel32, int(handle))
            limits = process_limits.query_job_limits(kernel32, job)
            observed["limits"] = (
                limits.active_process_limit,
                limits.process_memory_limit,
                limits.job_memory_limit,
                limits.per_process_user_time_limit,
            )
        else:
            rows = Path(f"/proc/{pid}/limits").read_text().splitlines()
            values = {row[:26].strip(): row[26:].split()[:2] for row in rows[1:]}
            observed["limits"] = (values["Max address space"], values["Max cpu time"])
            observed["rlimit_as"] = rlimit_as
        return rlimit_as

    monkeypatch.setattr(process_limits, "assign_process", recording_assign)
    monkeypatch.setattr(receipt_pages, "_parse_ready_line", checking_ready)
    data = io.BytesIO()
    Image.new("RGB", (300, 200), "white").save(data, "PNG")
    digest = hashlib.sha256(data.getvalue()).hexdigest()
    path = tmp_path / f"{digest}.png"
    path.write_bytes(data.getvalue())

    @dataclasses.dataclass(frozen=True)
    class Asset:
        asset_id: str
        media_type: str
        path: Path

    (page,) = receipt_pages.to_pages(
        Asset(f"asset:v1:{digest}", "png", path),
        pages_dir=tmp_path / "pages",
        ticket_ref="EX-01",
        asset_ordinal=1,
    )
    assert page.path.is_file()
    assert observed["pid"] == observed["popen_pid"], "a launcher would hold the limits instead"
    memory, cpu = NORMALIZATION.memory_bytes, NORMALIZATION.cpu_seconds
    if sys.platform == "win32":
        assert observed["limits"] == (1, memory, memory, cpu * 10_000_000)
    else:
        rlimit_as = observed["rlimit_as"]
        assert memory <= rlimit_as <= memory + NORMALIZATION.max_as_baseline
        assert observed["limits"] == ([str(rlimit_as)] * 2, [str(cpu)] * 2)
