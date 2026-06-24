"""Static guards on the unattended monthly-report GitHub Actions workflow.

These assertions read ``.github/workflows/monthly-report.yml`` as TEXT (stdlib only — no
``pyyaml`` dependency in the base suite) and lock in the contract that matters for an
unattended, PUBLIC-repo scheduled job:

* it is scheduled monthly (``0 9 1 * *``) AND manually dispatchable (``workflow_dispatch``);
* it runs the report generator with ``--variant both``;
* **secret safety** — the base64 secrets are decoded to FILES via redirect, and the decoded
  (or raw) secret is NEVER echoed/cat'd to stdout (a leaked secret in a public-repo run log is
  unrecoverable);
* it uploads ``reports/output/`` as an ephemeral artifact (reports are never committed) and
  references the ``.github/last-run.txt`` keepalive that resets the 60-day scheduler timer.

A structural YAML parse (the file loads as a mapping) runs only when a YAML parser happens to be
importable, so the base suite stays dependency-free.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "monthly-report.yml"


def _workflow_text() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


def _workflow_lines() -> list[str]:
    return _workflow_text().splitlines()


def test_workflow_file_exists() -> None:
    assert _WORKFLOW.is_file(), f"missing workflow file: {_WORKFLOW}"


def test_schedule_and_dispatch_triggers() -> None:
    text = _workflow_text()
    assert "0 9 1 * *" in text, "monthly cron schedule '0 9 1 * *' missing"
    assert "workflow_dispatch" in text, "manual workflow_dispatch trigger missing"


def test_runs_report_with_both_variants() -> None:
    text = _workflow_text()
    assert "pta-finance report" in text, "workflow must invoke the report CLI"
    assert "--variant both" in text, "report must be generated with --variant both"


def test_secrets_decoded_to_files_via_redirect() -> None:
    text = _workflow_text()
    # Each base64 secret is decoded to a file via a redirect (never to stdout).
    assert "base64 -d > secrets/service-account.json" in text
    assert "base64 -d > config.toml" in text


def test_no_decoded_secret_is_echoed_or_cat() -> None:
    """No line may print a decoded/raw secret or a decoded file to stdout.

    A leaked credential in a PUBLIC-repo run log is unrecoverable, so this is the load-bearing
    safety assertion. We scan command-shaped fragments (after stripping the leading YAML
    ``run:``/``|`` noise and indentation) for any ``echo``/``cat``/``printf``-to-stdout of a
    secret variable or a decoded secret file.
    """
    forbidden = (
        'echo "$GOOGLE_SA_KEY_B64"',
        'echo "$PTA_CONFIG_B64"',
        "echo $GOOGLE_SA_KEY_B64",
        "echo $PTA_CONFIG_B64",
        "cat config.toml",
        "cat secrets/",
        "cat secrets/service-account.json",
    )
    for line in _workflow_lines():
        stripped = line.strip()
        for needle in forbidden:
            assert needle not in stripped, f"secret-leaking command found: {stripped!r}"
    # printf is used ONLY to feed the base64 decoder via a pipe, never to print a decoded value.
    for line in _workflow_lines():
        stripped = line.strip()
        if stripped.startswith("printf"):
            assert "| base64 -d >" in stripped, (
                f"printf must only pipe a raw secret into base64 -d, got: {stripped!r}"
            )


def test_uploads_artifact_not_commit() -> None:
    text = _workflow_text()
    assert "actions/upload-artifact@v4" in text, "report output must be uploaded as an artifact"
    assert "reports/output/" in text, "the artifact must be the gitignored reports/output/ dir"


def test_keepalive_pushes_last_run_marker() -> None:
    text = _workflow_text()
    assert ".github/last-run.txt" in text, "keepalive must touch .github/last-run.txt"
    assert "git push" in text, "keepalive must push to reset the 60-day scheduler timer"
    # Guarded commit so an empty diff does not fail the job.
    assert "git diff --staged --quiet ||" in text


def test_contents_write_permission() -> None:
    text = _workflow_text()
    assert "contents: write" in text, "the keepalive push requires contents: write permission"


def test_workflow_parses_as_mapping_if_yaml_available() -> None:
    """Structural sanity: the file loads as a YAML mapping. Skipped when no parser is installed
    (keeps the base suite dependency-free — no pyyaml is added)."""
    yaml = pytest.importorskip("yaml")
    loaded = yaml.safe_load(_workflow_text())
    assert isinstance(loaded, dict), "workflow did not parse as a top-level mapping"
    assert "jobs" in loaded, "workflow has no jobs section"
