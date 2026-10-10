"""The image decode child: untrusted PNG/JPEG bytes are decoded here and nowhere else.

Run only as ``python -I -m pta_finance.receipt_decode``, one fresh process per asset, by
:func:`pta_finance.receipt_pages.to_pages`. It is never imported to decode: the broker imports
this module only for the wire constants, the strict line parser and :func:`scaled_size`, the
one size/scale derivation both sides use.

The protocol (``documentation/receipt-autofill-plan.md`` § 5A, "The image decode child request
and response"):

1. The broker spawns this child, puts it in its limits (a Windows Job Object), and writes one
   request line carrying every value the child applies.
2. The child reads that line (at most 4 KiB), refuses a ``byte_count`` above
   ``max_source_bytes``, moves Pillow's own bomb check to ``max_source_pixels``, then applies
   and **self-attests** its limits — on Windows it reads its own Job's limits back; on Linux it
   sets ``RLIMIT_AS`` (``memory_bytes`` plus its own measured interpreter baseline) and
   ``RLIMIT_CPU`` with soft = hard, and reads both back. Only then does it write its ready line.
   A failed attestation exits non-zero with no ready line.
3. The broker checks the ready line and only then writes exactly ``byte_count`` asset bytes.
4. The child decodes: header fast paths (pixel and edge ceilings, the mode allowlist, the
   animated-PNG refusal), then load, EXIF/XMP orientation from a fixed transpose table,
   alpha-flatten onto the background, and a LANCZOS downscale to the long-edge limit. It writes
   one response line and, for a decoded page, exactly ``width * height * C`` raw 8-bit pixels.

Every line is one ASCII JSON object ending in exactly one LF, read and written on binary
streams, parsed refusing duplicate keys and non-finite constants, then checked for its exact
key set and the exact type of every value.

**Exit status.** 0 after a response; :data:`EXIT_BUDGET` when a ``MemoryError`` reaches the top
of :func:`main` (a decoder failure handler re-raises it, never maps it to ``unreadable``);
:data:`EXIT_CHILD_ERROR` for any other uncaught exception after the ready line;
:data:`EXIT_NOT_READY` for a refusal before it.

The module imports only the standard library and the stdlib-only
:mod:`pta_finance.process_limits` leaf at module level, and Pillow inside :func:`main` only, so
the protocol can later move into the attested LPAC worker unchanged.
"""

from __future__ import annotations

import ctypes
import io
import json
import math
import os
import sys
from dataclasses import dataclass
from typing import Any, Final, NoReturn

from pta_finance import process_limits

__all__ = [
    "COLOR_MODES",
    "DECODED_KEYS",
    "EXIT_BUDGET",
    "EXIT_CHILD_ERROR",
    "EXIT_NOT_READY",
    "GRAY_MODES",
    "MEDIA_TYPES",
    "PROTOCOL",
    "READY_KEYS",
    "MEMORY_PAGE_BYTES",
    "READY_LINE_MAX_BYTES",
    "REFUSAL_REASONS",
    "REFUSED_KEYS",
    "REQUEST_KEYS",
    "REQUEST_LINE_MAX_BYTES",
    "RESPONSE_HEADER_MAX_BYTES",
    "DecodeRequest",
    "WireError",
    "encode_line",
    "main",
    "parse_line",
    "parse_request",
    "scaled_size",
]

PROTOCOL: Final = "receipt-decode/1"
EXIT_BUDGET: Final = 3
EXIT_CHILD_ERROR: Final = 4
EXIT_NOT_READY: Final = 2

REQUEST_LINE_MAX_BYTES: Final = 4096
READY_LINE_MAX_BYTES: Final = 256
RESPONSE_HEADER_MAX_BYTES: Final = 1024
# Windows rounds a Job's memory limits down to whole 4 KiB pages, so a limit that is not a
# whole number of pages could never read back exactly in the child's attestation.
MEMORY_PAGE_BYTES: Final = 4096

REQUEST_KEYS: Final = frozenset(
    {
        "protocol",
        "media_type",
        "byte_count",
        "max_source_bytes",
        "max_source_pixels",
        "max_source_edge",
        "max_long_edge",
        "background",
        "memory_bytes",
        "cpu_seconds",
    }
)
# The Linux ready line adds "rlimit_as": the address-space limit the child applied.
READY_KEYS: Final = frozenset({"status", "protocol", "pid", "pillow"})
DECODED_KEYS: Final = frozenset(
    {
        "status",
        "mode",
        "width",
        "height",
        "source_width",
        "source_height",
        "orientation",
        "scale",
    }
)
REFUSED_KEYS: Final = frozenset({"status", "reason"})
REFUSAL_REASONS: Final = frozenset(
    {"unreadable", "too-many-pixels", "source-edge", "unsupported-mode", "animated"}
)
# The only Pillow decoder each declared media type may reach, with no fallback. A phone's
# multi-picture JPEG opens through the JPEG decoder as "MPO"; only its primary picture is used.
MEDIA_TYPES: Final = {"png": "PNG", "jpeg": "JPEG"}
# Source mode -> output mode. 16-bit and float pixels would clip rather than scale, so every
# other mode is refused, never guessed.
GRAY_MODES: Final = frozenset({"1", "L", "LA", "La"})
COLOR_MODES: Final = frozenset({"P", "RGB", "RGBA", "RGBa", "CMYK"})

_EXIF_ORIENTATION = 0x0112
# EXIF orientation -> the Pillow transpose that turns stored pixels into the displayed page.
_DISPLAY_TRANSPOSE: Final = {
    2: "FLIP_LEFT_RIGHT",
    3: "ROTATE_180",
    4: "FLIP_TOP_BOTTOM",
    5: "TRANSPOSE",
    6: "ROTATE_270",
    7: "TRANSVERSE",
    8: "ROTATE_90",
}
_SWAPS_SIDES: Final = frozenset({5, 6, 7, 8})


class WireError(ValueError):
    """A protocol line broke the wire rules: cap, terminator, encoding, JSON, keys or types."""


class _NotReady(Exception):
    """The child refuses before its ready line (bad request or unconfirmed limits)."""


@dataclass(frozen=True)
class DecodeRequest:
    """The request line's values; every limit the child applies arrives here."""

    media_type: str
    byte_count: int
    max_source_bytes: int
    max_source_pixels: int
    max_source_edge: int
    max_long_edge: int
    background: tuple[int, int, int]
    memory_bytes: int
    cpu_seconds: int

    def line(self) -> bytes:
        """This request as one wire line."""

        return encode_line(
            {
                "protocol": PROTOCOL,
                "media_type": self.media_type,
                "byte_count": self.byte_count,
                "max_source_bytes": self.max_source_bytes,
                "max_source_pixels": self.max_source_pixels,
                "max_source_edge": self.max_source_edge,
                "max_long_edge": self.max_long_edge,
                "background": list(self.background),
                "memory_bytes": self.memory_bytes,
                "cpu_seconds": self.cpu_seconds,
            }
        )


def scaled_size(
    source_width: int, source_height: int, orientation: int, max_long_edge: int
) -> tuple[int, int, float]:
    """Displayed page size and scale for a source, after orientation and the long-edge limit.

    Orientations 5-8 swap the sides. A page whose long side exceeds ``max_long_edge`` is scaled
    uniformly so that side is exactly the limit and the short side rounds half up; otherwise
    the scale is exactly 1.0. Standard library only: the child applies it and the broker
    validates the child's response against it.
    """

    if orientation in _SWAPS_SIDES:
        width, height = source_height, source_width
    else:
        width, height = source_width, source_height
    long_edge = max(width, height)
    if long_edge <= max_long_edge:
        return width, height, 1.0

    def scaled(side: int) -> int:
        return max(1, (side * max_long_edge * 2 + long_edge) // (long_edge * 2))

    return scaled(width), scaled(height), max_long_edge / long_edge


def encode_line(value: dict[str, Any]) -> bytes:
    """One ASCII JSON object terminated by exactly one LF."""

    text = json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return text.encode("ascii") + b"\n"


def parse_line(line: bytes, *, cap: int) -> dict[str, Any]:
    """Parse one wire line under the shared rules, or raise :class:`WireError`.

    The line, LF included, is at most ``cap`` bytes, ends in exactly one LF with no CR anywhere,
    is ASCII, and holds one JSON object with no duplicate key and no ``NaN`` or ``Infinity``.
    """

    if len(line) > cap:
        raise WireError("protocol line is over its cap")
    if not line.endswith(b"\n") or b"\n" in line[:-1] or b"\r" in line:
        raise WireError("protocol line must end in exactly one LF")
    try:
        text = line[:-1].decode("ascii")
        value = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError):
        raise WireError("protocol line is not one ASCII JSON object") from None
    if type(value) is not dict:
        raise WireError("protocol line is not one ASCII JSON object")
    return value


def wire_int(value: Any) -> int:
    """An int field: ``type(value) is int`` — a bool and a float are both refused."""

    if type(value) is not int:
        raise WireError("protocol field must be an int")
    return value


def wire_float(value: Any) -> float:
    """A float field: ``type(value) is float`` and finite."""

    if type(value) is not float or not math.isfinite(value):
        raise WireError("protocol field must be a finite float")
    return value


def wire_text(value: Any, allowed: frozenset[str] | dict[str, str]) -> str:
    """A string field from its closed set."""

    if type(value) is not str or value not in allowed:
        raise WireError("protocol field is outside its closed set")
    return value


def parse_request(line: bytes) -> DecodeRequest:
    """Validate a request line by exact key-set equality and exact value types."""

    value = parse_line(line, cap=REQUEST_LINE_MAX_BYTES)
    if set(value) != REQUEST_KEYS:
        raise WireError("request key set is not exact")
    wire_text(value["protocol"], frozenset({PROTOCOL}))
    background = value["background"]
    if type(background) is not list or len(background) != 3:
        raise WireError("background must be three ints")
    channels = tuple(wire_int(channel) for channel in background)
    if not all(0 <= channel <= 255 for channel in channels):
        raise WireError("background must be three ints from 0 to 255")
    request = DecodeRequest(
        media_type=wire_text(value["media_type"], MEDIA_TYPES),
        byte_count=wire_int(value["byte_count"]),
        max_source_bytes=wire_int(value["max_source_bytes"]),
        max_source_pixels=wire_int(value["max_source_pixels"]),
        max_source_edge=wire_int(value["max_source_edge"]),
        max_long_edge=wire_int(value["max_long_edge"]),
        background=(channels[0], channels[1], channels[2]),
        memory_bytes=wire_int(value["memory_bytes"]),
        cpu_seconds=wire_int(value["cpu_seconds"]),
    )
    positive = (
        request.byte_count,
        request.max_source_bytes,
        request.max_source_pixels,
        request.max_source_edge,
        request.max_long_edge,
        request.memory_bytes,
        request.cpu_seconds,
    )
    if any(number < 1 for number in positive):
        raise WireError("request limits must be positive")
    if request.memory_bytes % MEMORY_PAGE_BYTES:
        raise WireError("memory_bytes must be a whole number of 4 KiB pages")
    if request.byte_count > request.max_source_bytes:
        raise WireError("asset is larger than the source-byte cap")
    return request


def main() -> int:
    """Serve one request on stdin with one response on stdout; see the module docstring."""

    ready = [False]
    try:
        _serve(ready)
    except MemoryError:
        os._exit(EXIT_BUDGET)
    except Exception:
        os._exit(EXIT_CHILD_ERROR if ready[0] else EXIT_NOT_READY)
    return 0


def _serve(ready: list[bool]) -> None:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    try:
        request = parse_request(stdin.readline(REQUEST_LINE_MAX_BYTES + 1))
    except WireError:
        raise _NotReady from None
    import PIL
    from PIL import Image, JpegImagePlugin, PngImagePlugin

    del JpegImagePlugin, PngImagePlugin  # imported to register exactly these two decoders
    # Pillow's own bomb check moves with the request, so lifting the ceiling lifts it too.
    Image.MAX_IMAGE_PIXELS = request.max_source_pixels
    ready_line: dict[str, Any] = {
        "status": "ready",
        "protocol": PROTOCOL,
        "pid": os.getpid(),
        "pillow": PIL.__version__,
    }
    rlimit_as = _apply_and_attest_limits(request)
    if rlimit_as is not None:
        ready_line["rlimit_as"] = rlimit_as
    stdout.write(encode_line(ready_line))
    stdout.flush()
    ready[0] = True
    body = stdin.read(request.byte_count)
    if len(body) != request.byte_count:
        raise RuntimeError("the broker sent a short asset body")
    header, pixels = _decode(body, request, Image)
    del body
    stdout.write(encode_line(header))
    if pixels is not None:
        stdout.write(pixels)
    stdout.flush()


def _apply_and_attest_limits(request: DecodeRequest) -> int | None:
    """Apply and read back the OS limits; the Linux ``RLIMIT_AS`` applied, else ``None``."""

    if sys.platform == "win32":
        kernel32 = process_limits.load_kernel32()
        limits = process_limits.query_job_limits(kernel32, None)
        required = process_limits.JOB_OBJECT_REQUIRED_LIMIT_FLAGS
        current = kernel32.GetCurrentProcess
        current.argtypes = ()
        current.restype = ctypes.c_void_p
        handle = current()
        if not (
            (limits.limit_flags & required) == required
            and not limits.limit_flags & process_limits.JOB_OBJECT_FORBIDDEN_LIMIT_FLAGS
            and limits.active_process_limit == 1
            and limits.process_memory_limit == request.memory_bytes
            and limits.job_memory_limit == request.memory_bytes
            and limits.per_process_user_time_limit
            == request.cpu_seconds * process_limits.HUNDRED_NANOSECONDS_PER_SECOND
            and handle is not None
            and process_limits.is_process_in_job(kernel32, int(handle), None)
        ):
            _not_ready()
        return None
    if sys.platform == "linux":
        import resource

        # RLIMIT_AS counts the interpreter's own address space too, so the limit is the budget
        # above the baseline this process measures for itself, with Pillow already loaded.
        limit_as = request.memory_bytes + _vm_size_bytes()
        resource.setrlimit(resource.RLIMIT_AS, (limit_as, limit_as))
        resource.setrlimit(resource.RLIMIT_CPU, (request.cpu_seconds, request.cpu_seconds))
        if resource.getrlimit(resource.RLIMIT_AS) != (limit_as, limit_as) or resource.getrlimit(
            resource.RLIMIT_CPU
        ) != (request.cpu_seconds, request.cpu_seconds):
            _not_ready()
        return limit_as
    _not_ready()


def _vm_size_bytes() -> int:
    with open("/proc/self/status", "rb") as status:
        for raw in status:
            if raw.startswith(b"VmSize:"):
                fields = raw.split()
                if len(fields) == 3 and fields[2] == b"kB":
                    return int(fields[1]) * 1024
    _not_ready()


def _decode(
    body: bytes, request: DecodeRequest, image_module: Any
) -> tuple[dict[str, Any], bytes | None]:
    """Decode one asset; a refusal header with no pixels, or a decoded header and its pixels."""

    image_api = image_module
    try:
        source = image_api.open(io.BytesIO(body), formats=[MEDIA_TYPES[request.media_type]])
    except MemoryError:
        raise
    except (image_api.DecompressionBombError, image_api.DecompressionBombWarning):
        return _refused("too-many-pixels"), None
    except Exception:
        return _refused("unreadable"), None
    source_width, source_height = source.size
    if source_width < 1 or source_height < 1:
        return _refused("unreadable"), None
    if source_width * source_height > request.max_source_pixels:
        return _refused("too-many-pixels"), None
    if max(source_width, source_height) > request.max_source_edge:
        return _refused("source-edge"), None
    if source.mode not in GRAY_MODES | COLOR_MODES:
        return _refused("unsupported-mode"), None
    if request.media_type == "png" and getattr(source, "is_animated", False):
        # A browser would show the animation; one fixed frame is not evidence of what it shows.
        return _refused("animated"), None
    try:
        source.load()
        orientation = source.getexif().get(_EXIF_ORIENTATION, 1)
    except MemoryError:
        raise
    except Exception:
        return _refused("unreadable"), None
    if type(orientation) is not int or not 1 <= orientation <= 8:
        orientation = 1
    # From here on the pixels are decoded; a failure is a child bug, not an unreadable asset.
    # Every step rebinds ``image`` so each full-size intermediate is released once the next
    # exists.
    image = source
    del source
    if orientation in _DISPLAY_TRANSPOSE:
        image = image.transpose(image_api.Transpose[_DISPLAY_TRANSPOSE[orientation]])
    target = "L" if image.mode in GRAY_MODES else "RGB"
    if image.has_transparency_data:
        # Flatten before scaling: Pillow resamples palette and bilevel images nearest-neighbour.
        rgba = image if image.mode == "RGBA" else image.convert("RGBA")
        del image
        alpha = rgba.getchannel("A")
        color = rgba.convert(target)
        del rgba
        # The background in the target mode: white stays 255 in L, exactly as converted.
        backdrop = image_api.new("RGB", (1, 1), request.background).convert(target)
        image = image_api.new(target, color.size, backdrop.getpixel((0, 0)))
        image.paste(color, mask=alpha)
        del color, alpha
    elif image.mode != target:
        image = image.convert(target)
    width, height, scale = scaled_size(
        source_width, source_height, orientation, request.max_long_edge
    )
    if image.size != (width, height):
        image = image.resize((width, height), image_api.Resampling.LANCZOS)
    pixels: bytes = image.tobytes()
    header = {
        "status": "decoded",
        "mode": target,
        "width": width,
        "height": height,
        "source_width": source_width,
        "source_height": source_height,
        "orientation": orientation,
        "scale": scale,
    }
    return header, pixels


def _refused(reason: str) -> dict[str, Any]:
    return {"status": "refused", "reason": reason}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise WireError("protocol line repeats a key")
        value[key] = item
    return value


def _no_constant(name: str) -> NoReturn:
    raise WireError("protocol line carries a non-finite number")


def _not_ready() -> NoReturn:
    raise _NotReady


if __name__ == "__main__":
    raise SystemExit(main())
