"""Create a NEW allowlisted build context locally; never read runtime config or call cloud APIs.

uv run python scripts/stage_shared_workflow.py --output <new-directory>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "deployment/shared-workflow/source-manifest.txt"
RECEIPT = "source-content-manifest.json"
FORBIDDEN = {
    "secrets",
    "mail_samples",
    "snapshots",
    ".git",
    ".claude",
    ".build-step",
    "config.toml",
    "reports",
    "gmail-token.json",
    "service-account.json",
}


def plain_path(path: Path) -> None:
    for component in (path, *path.parents):
        if component.exists() or component.is_symlink():
            info = component.lstat()
            if (
                component.is_symlink()
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise ValueError("Symlinks and reparse points are not accepted.")


def relative(raw: str) -> PurePosixPath:
    path = PurePosixPath(raw)
    if (
        path.is_absolute()
        or "\\" in raw
        or ":" in raw
        or ".." in path.parts
        or str(path) != raw
        or any(part.casefold() in FORBIDDEN for part in path.parts)
    ):
        raise ValueError("The source manifest contains an unsafe path.")
    return path


def entries(root: Path) -> list[tuple[str, str]]:
    plain_path(root / MANIFEST)
    result = []
    for line in (root / MANIFEST).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ValueError("Each manifest row requires one source and destination.")
        source, destination = (str(relative(part)) for part in parts)
        source_path = root / source
        plain_path(source_path)
        if not source_path.is_file() or not source_path.resolve().is_relative_to(root.resolve()):
            raise ValueError("A required source file is unavailable or outside the repository.")
        result.append((source, destination))
    if not result or len({dest.casefold() for _, dest in result}) != len(result):
        raise ValueError("The source manifest is empty or has duplicate destinations.")
    return result


def verify_stage(output: Path) -> None:
    plain_path(output)
    manifest = json.loads((output / RECEIPT).read_text(encoding="utf-8"))
    expected = set(manifest) | {RECEIPT}
    actual = set()
    for path in output.rglob("*"):
        plain_path(path)
        if path.is_file():
            actual.add(path.relative_to(output).as_posix())
    if actual != expected:
        raise ValueError("Unexpected or missing staged files.")
    for name, digest in manifest.items():
        relative(name)
        if hashlib.sha256((output / name).read_bytes()).hexdigest() != digest:
            raise ValueError("Staged content differs from its manifest.")


def stage(output: Path, root: Path = ROOT) -> int:
    output = output.absolute()
    plain_path(output)
    declared = entries(root)
    if output.exists():
        raise ValueError("The output already exists; choose a new directory.")
    output.mkdir(parents=True, exist_ok=False)
    manifest = {}
    for source, destination in declared:
        target = output / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / source, target)
        manifest[destination] = hashlib.sha256(target.read_bytes()).hexdigest()
    (output / RECEIPT).write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    verify_stage(output)
    return len(manifest)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        count = stage(args.output)
    except (OSError, ValueError) as exc:
        print(f"STAGING FAILED: {exc}")
        return 1
    print(f"STAGING PASS: {count} allowlisted files and verified content receipt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
