from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("google.cloud.firestore_v1")
pytest.importorskip("cryptography")

from scripts.stage_shared_workflow import MANIFEST, ROOT, entries, stage, verify_stage  # noqa: E402


def synthetic_checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    for source, _ in entries(ROOT):
        destination = root / source
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / source, destination)
    (root / MANIFEST).write_bytes((ROOT / MANIFEST).read_bytes())
    for name in (
        "config.toml",
        "secrets/service-account.json",
        "mail_samples/private.eml",
        "snapshots/private.json",
        "reports/output/private.json",
        ".claude/task-state.md",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("PTA_PRIVATE_CANARY: fictional leak sentinel")
    return root


def test_staging_excludes_untracked_canaries_and_refuses_reuse_or_drift(tmp_path: Path) -> None:
    root = synthetic_checkout(tmp_path)
    output = tmp_path / "stage"
    count = stage(output, root)
    assert count == len(entries(root))
    assert all(
        b"PTA_PRIVATE_CANARY: fictional leak sentinel" not in path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    )
    with pytest.raises(ValueError, match="already exists"):
        stage(output, root)
    (output / "unexpected.txt").write_text("PTA_PRIVATE_CANARY")
    with pytest.raises(ValueError, match="Unexpected"):
        verify_stage(output)
    (output / "unexpected.txt").unlink()
    (output / "README.md").write_text("Changed after staging")
    with pytest.raises(ValueError, match="differs"):
        verify_stage(output)


@pytest.mark.parametrize(
    "line",
    [
        "../outside.py outside.py",
        "config.toml config.toml",
        "pta_finance/__init__.py ../outside.py",
        "pta_finance/__init__.py secrets/file.py",
    ],
)
def test_manifest_rejects_traversal_and_private_paths(tmp_path: Path, line: str) -> None:
    root = synthetic_checkout(tmp_path)
    (root / MANIFEST).write_text(line)
    with pytest.raises(ValueError):
        stage(tmp_path / "stage", root)


def test_staging_real_cli_and_reparse_boundary(tmp_path: Path) -> None:
    root = synthetic_checkout(tmp_path)
    script = root / "scripts/stage_shared_workflow.py"
    script.parent.mkdir()
    shutil.copyfile(ROOT / "scripts/stage_shared_workflow.py", script)
    result = subprocess.run(
        [sys.executable, str(script), "--output", str(tmp_path / "cli-stage")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0 and "STAGING PASS" in result.stdout
    verify_stage(tmp_path / "cli-stage")
    target = tmp_path / "outside"
    target.mkdir()
    link = root / "linked"
    if os.name == "nt":
        command = f"New-Item -ItemType Junction -Path '{link}' -Target '{target}' | Out-Null"
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            check=True,
            capture_output=True,
        )
    else:
        link.symlink_to(target, target_is_directory=True)
    (target / "public.py").write_text("# Synthetic public file\n")
    (root / MANIFEST).write_text("linked/public.py public.py\n")
    result = subprocess.run(
        [sys.executable, str(script), "--output", str(tmp_path / "unsafe-stage")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1 and "reparse" in result.stdout


def test_staged_wheel_loads_resources_outside_checkout_and_rejects_production_test_settings(
    tmp_path: Path,
) -> None:
    root = synthetic_checkout(tmp_path)
    staged = tmp_path / "stage"
    stage(staged, root)
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path / "dist")],
        cwd=staged,
        check=True,
        capture_output=True,
    )
    wheel = next((tmp_path / "dist").glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        for resource in (
            "example-request.json",
            "example-request-02.json",
            "example-request-03.json",
            "example-request-04.json",
            "example-request-05.json",
            "example-request-06.json",
            "catalog.py",
            "templates/request.html.j2",
            "templates/queue.html.j2",
            "static/request.js",
            "static/request.css",
            "static/queue.js",
            "static/queue.css",
        ):
            assert "pta_finance/shared_workflow/" + resource in names
        assert all(
            "secrets/" not in name and "scripts/" not in name and "tests/" not in name
            for name in names
        )
        assert all(b"PTA_PRIVATE_CANARY" not in archive.read(name) for name in names)
        assert "pta_finance/cli.py" not in names and "pta_finance/gmail_source.py" not in names
    installed = tmp_path / "installed"
    subprocess.run(
        ["uv", "pip", "install", "--target", str(installed), "--no-deps", str(wheel)],
        check=True,
        capture_output=True,
    )
    code = """import sys; sys.path.insert(0,sys.argv[1])
import os, runpy
from pathlib import Path
from pta_finance.shared_workflow.models import load_source
from pta_finance.shared_workflow.catalog import load_catalog
from pta_finance.shared_workflow import __file__
assert __file__.startswith(sys.argv[1])
assert load_source()['display']['total']=='184.50'
catalog = load_catalog()
assert len(catalog) == 6 and catalog[load_source()['request_id']] == load_source()
# Run the actual image inspector against the installed staged inventory. Its Linux
# non-root assertion is supplied by this local harness; actual Cloud Build remains M8.
if not hasattr(os, 'getuid'):
    os.getuid = lambda: 10001
elif os.getuid() == 0:
    os.getuid = lambda: 10001
inspector = runpy.run_path(str(Path(sys.argv[2]) / 'inspect_image.py'))['inspect']
inspector(Path(sys.argv[2]))
import pta_finance.shared_workflow.catalog as inventory
inventory.ADDITIONAL_SOURCES = inventory.ADDITIONAL_SOURCES[:-1]
try:
    inspector(Path(sys.argv[2]))
except Exception as error:
    assert str(error) == 'FIXTURE_INVALID'
else:
    raise AssertionError('Image inspector failed to validate the complete catalog')
print('WHEEL PASS')
"""
    inspection_env = {
        key: value
        for key, value in os.environ.items()
        if "EMULATOR" not in key
        and not key.startswith("PTA_WORKFLOW_")
        and key != "GOOGLE_APPLICATION_CREDENTIALS"
    }
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(installed), str(staged)],
        cwd=tmp_path,
        env=inspection_env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "six-source catalog loaded" in result.stdout and result.stdout.endswith("WHEEL PASS\n")
    env = {key: value for key, value in os.environ.items() if not key.startswith("PTA_WORKFLOW_")}
    env["FIRESTORE_EMULATOR_HOST"] = "127.0.0.1:8787"
    code = (
        "import sys,runpy; sys.path.insert(0,sys.argv.pop(1)); "
        "runpy.run_module('pta_finance.shared_workflow',run_name='__main__')"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(installed)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1 and "UNSAFE_RUNTIME_ENV" in result.stderr


def test_source_distribution_excludes_local_developer_and_review_receipts(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    for name in ("pyproject.toml", "README.md", "pta_finance/__init__.py"):
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, destination)
    for name in (".build-step/receipt.md", ".review-deep/receipt.md"):
        receipt = root / name
        receipt.parent.mkdir()
        receipt.write_text("PTA_PRIVATE_CANARY: fictional local receipt")
    subprocess.run(
        ["uv", "build", "--sdist", "--out-dir", str(tmp_path / "dist")],
        cwd=root,
        check=True,
        capture_output=True,
    )
    with tarfile.open(next((tmp_path / "dist").glob("*.tar.gz"))) as archive:
        names = archive.getnames()
    assert any(name.endswith("/pta_finance/__init__.py") for name in names)
    assert all("/.build-step/" not in name and "/.review-deep/" not in name for name in names)


def test_recipe_inspects_actual_image_before_publication_and_keeps_test_launcher_out() -> None:
    recipe = (ROOT / "deployment/shared-workflow/cloudbuild.yaml").read_text()
    assert recipe.index("inspect-actual-image") < recipe.index("images:")
    assert "--network=none" in recipe and "--read-only" in recipe and "CLOUD_LOGGING_ONLY" in recipe
    assert "shared_workflow_smoke.py" not in (ROOT / MANIFEST).read_text()
    dockerfile = (ROOT / "deployment/shared-workflow/Dockerfile").read_text()
    assert "--locked --no-dev --extra web --no-editable" in dockerfile
    assert "USER 10001:10001" in dockerfile
    example = json.loads((ROOT / "deployment/shared-workflow/runtime.example.json").read_text())
    assert {user["email"].split("@")[1] for user in example["users"]} == {"example.org"}
