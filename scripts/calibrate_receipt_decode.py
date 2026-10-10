#!/usr/bin/env python3
"""Calibrate the receipt decode child's limits on this host (plan § 5A, "Limits").

Drives the production ``receipt_pages.to_pages`` and the real decode child through the outer
harness of ``tests/test_receipt_decode_budget.py``, which it loads **by path** together with the
known-good anchor generators, so the calibration measures exactly what the gate tests. Each
anchor runs N = 3 times at production limits, and each reading is taken in the unit the host's
limit counts:

* Windows memory — the child Job's ``PeakProcessMemoryUsed`` (commit);
* Linux memory — the smallest ``memory_bytes`` above the runtime baseline at which the anchor
  still yields a page, found by bisection at 64 MiB resolution (``RLIMIT_AS`` counts address
  space, not commit);
* CPU — Windows the Job's ``TotalUserTime``; Linux ``ru_utime + ru_stime`` across the reap;
* wall — the broker's ``time.monotonic()`` from spawn to reap.

It prints one JSON calibration record: host, CPU model, Pillow version, every reading with its
per-anchor maximum and spread (max / min), the Linux Pillow-loaded interpreter baseline, and the
values § 5A's exact formulas give from this host alone. A spread above 1.2 marks the record
invalid: run it again, never relax the factor. The chosen limits take the largest reading over
every host's record. Writes only temporary files; never touches private data.

Usage: ``uv run python scripts/calibrate_receipt_decode.py [--runs 3]``
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import platform
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_HARNESS = _ROOT / "tests" / "test_receipt_decode_budget.py"
_MiB = 1024 * 1024
_STEP = 64 * _MiB
_MAX_SPREAD = 1.2
# Windows Job CPU accounting advances in 15.625 ms clock ticks, so a reading of a few ticks has a
# spread that is quantization, not run-to-run variance. The spread rule therefore applies to time
# readings whose smallest run is at least this long (32 ticks, about 3% quantization) and to every
# memory reading; shorter time readings are reported with their spread but marked unresolved.
_RESOLVABLE_SECONDS = 0.5


def _load_harness() -> ModuleType:
    spec = importlib.util.spec_from_file_location("receipt_decode_budget_harness", _HARNESS)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {_HARNESS}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cpu_model() -> str:
    if sys.platform == "linux":
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or platform.machine()


def _run(harness: ModuleType, name: str, overrides: dict[str, Any], root: Path) -> dict[str, Any]:
    data, media_type = harness.ANCHORS[name]()
    with tempfile.TemporaryDirectory(dir=root) as scratch:
        try:
            result: dict[str, Any] = harness.run_in_outer(
                data, media_type, overrides=overrides, root=Path(scratch)
            )
        except BaseException as breach:  # pytest.fail raises a BaseException subclass
            if isinstance(breach, KeyboardInterrupt):
                raise
            return {"outcome": "breach", "detail": str(breach)[:300]}
    return result


def _bisect_memory(harness: ModuleType, name: str, root: Path, production: int) -> int:
    """Smallest multiple of 64 MiB at which the anchor still yields a page (Linux)."""

    def yields_page(memory: int) -> bool:
        return _run(harness, name, {"memory_bytes": memory}, root)["outcome"] == "page"

    high = max(_STEP, production // _STEP * _STEP)
    while not yields_page(high):
        high *= 2
        if high > 16 * 1024 * _MiB:
            raise SystemExit(f"{name}: no page even at 16 GiB")
    low = 0  # in steps; low never yields a page (0 is not a valid limit)
    high_steps = high // _STEP
    while high_steps - low > 1:
        middle = (low + high_steps) // 2
        if yields_page(middle * _STEP):
            high_steps = middle
        else:
            low = middle
    return high_steps * _STEP


def _summary(values: list[float], *, seconds: bool) -> dict[str, Any]:
    largest, smallest = max(values), min(values)
    spread = largest / smallest if smallest > 0 else math.inf
    resolved = not seconds or smallest >= _RESOLVABLE_SECONDS
    return {
        "readings": values,
        "max": largest,
        "spread": round(spread, 4),
        "spread_checked": resolved,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, default=3, help="runs per anchor (plan: N = 3)")
    parser.add_argument("--anchor", action="append", help="limit to these anchors")
    args = parser.parse_args(argv)
    if sys.platform not in ("win32", "linux"):
        raise SystemExit("calibration runs only where the decode child's limits are enforced")
    harness = _load_harness()
    from PIL import __version__ as pillow

    norm = harness.NORMALIZATION
    names = args.anchor or list(harness.ANCHORS)
    record: dict[str, Any] = {
        "host": sys.platform,
        "cpu_model": _cpu_model(),
        "python": platform.python_version(),
        "pillow": pillow,
        "n": args.runs,
        "production": {
            "memory_bytes": norm.memory_bytes,
            "cpu_seconds": norm.cpu_seconds,
            "wall_seconds": norm.wall_seconds,
        },
        "memory_unit": "job peak commit" if sys.platform == "win32" else "bisected RLIMIT_AS",
        "anchors": {},
    }
    baselines: list[int] = []
    valid = True
    with tempfile.TemporaryDirectory(prefix="pta-receipt-calibration-") as scratch:
        root = Path(scratch)
        for name in names:
            memory: list[float] = []
            cpu: list[float] = []
            wall: list[float] = []
            for _ in range(args.runs):
                result = _run(harness, name, {}, root)
                if result.get("outcome") != "page":
                    raise SystemExit(f"{name}: no page at production limits: {result}")
                usage = result["usage"]
                cpu.append(usage["cpu_seconds"])
                wall.append(usage["wall_seconds"])
                if sys.platform == "win32":
                    memory.append(float(usage["peak_memory_bytes"]))
                else:
                    rlimit_as = result["child"].get("rlimit_as")
                    if isinstance(rlimit_as, int):
                        baselines.append(rlimit_as - norm.memory_bytes)
                    memory.append(float(_bisect_memory(harness, name, root, norm.memory_bytes)))
            entry = {
                "memory_bytes": _summary(memory, seconds=False),
                "cpu_seconds": _summary(cpu, seconds=True),
                "wall_seconds": _summary(wall, seconds=True),
            }
            valid = valid and all(
                entry[key]["spread"] <= _MAX_SPREAD for key in entry if entry[key]["spread_checked"]
            )
            record["anchors"][name] = entry
    anchors = record["anchors"].values()
    largest_memory = max(entry["memory_bytes"]["max"] for entry in anchors)
    largest_cpu = max(entry["cpu_seconds"]["max"] for entry in anchors)
    largest_wall = max(entry["wall_seconds"]["max"] for entry in anchors)
    cpu_seconds = max(5, math.ceil(1.5 * largest_cpu))
    record["largest"] = {
        "memory_bytes": largest_memory,
        "cpu_seconds": largest_cpu,
        "wall_seconds": largest_wall,
    }
    record["this_host_alone"] = {
        "memory_bytes": math.ceil(1.5 * largest_memory / _STEP) * _STEP,
        "cpu_seconds": cpu_seconds,
        "wall_seconds": max(cpu_seconds, math.ceil(1.5 * largest_wall)),
    }
    if baselines:
        record["linux_pillow_loaded_baseline_bytes"] = {
            "readings": baselines,
            "max": max(baselines),
            "max_as_baseline": norm.max_as_baseline,
            "at_least_4x": norm.max_as_baseline >= 4 * max(baselines),
        }
    record["valid"] = valid
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
