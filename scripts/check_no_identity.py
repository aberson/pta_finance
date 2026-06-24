#!/usr/bin/env python3
"""CI guard: fail if a service-account key or an identity string is tracked.

This repo is PUBLIC; the data is private. This stdlib-only script scans the set of
git-tracked files (``git ls-files``) for two leak classes and exits non-zero on a hit:

  (a) Any tracked ``*.json`` whose text contains ``"private_key"`` or
      ``"service_account"`` — the fingerprint of a leaked service-account key.
  (b) An optional, operator-supplied denylist of identity substrings, loaded from the
      ``PTA_IDENTITY_DENYLIST`` env var (comma-separated, default empty). Each tracked
      text file is scanned case-insensitively for every denylist entry.

The private ``config.toml`` is gitignored and therefore never appears in ``git
ls-files``; this script does not special-case it.

Exit 0 = clean. Exit 1 = a match was found (message names the file + reason).
"""

from __future__ import annotations

import os
import subprocess
import sys

# Fingerprints of a leaked service-account JSON key.
_SA_KEY_MARKERS = ('"private_key"', '"service_account"')


def _tracked_files() -> list[str]:
    """Return the list of git-tracked file paths (relative to repo root)."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [p for p in result.stdout.split("\0") if p]


def _read_text(path: str) -> str | None:
    """Read a file as UTF-8 text; return None if it is unreadable/binary."""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def _load_denylist() -> list[str]:
    raw = os.environ.get("PTA_IDENTITY_DENYLIST", "")
    return [term.strip() for term in raw.split(",") if term.strip()]


def main() -> int:
    denylist = _load_denylist()
    denylist_lc = [term.casefold() for term in denylist]
    violations: list[str] = []

    for path in _tracked_files():
        is_json = path.casefold().endswith(".json")
        text: str | None = None

        if is_json:
            text = _read_text(path)
            if text is not None:
                for marker in _SA_KEY_MARKERS:
                    if marker in text:
                        violations.append(
                            f"{path}: tracked JSON contains {marker} "
                            "(looks like a service-account key)"
                        )

        if denylist_lc:
            if text is None:
                text = _read_text(path)
            if text is not None:
                haystack = text.casefold()
                for term, term_lc in zip(denylist, denylist_lc, strict=True):
                    if term_lc in haystack:
                        violations.append(f"{path}: contains denylisted identity string {term!r}")

    if violations:
        print("check_no_identity: FAIL — identity/secret leak detected:", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        return 1

    print("check_no_identity: OK — no tracked credential or identity string found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
