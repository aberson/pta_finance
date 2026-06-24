"""Tests for pta_finance.config — typed load + fail-fast on missing required field."""

from __future__ import annotations

from pathlib import Path

import pytest

from pta_finance.config import ConfigError, load_config

# A complete, fake config used as the baseline for the happy-path and the
# field-removal tests. Identity is obviously-fake placeholders only.
_FULL_CONFIG = """\
[organization]
name = "Example PTA"
school_name = "Example Elementary"
school_email = "office@example.org"

[contacts]
president = ["president@example.org"]
treasurer = "treasurer@example.org"
cfo = "cfo@example.org"
account_holders = ["president@example.org", "treasurer@example.org"]

[fiscal_year]
start_month = 1

[grades]
labels = ["K", "1", "2", "3", "4", "5"]

[sheets]
spreadsheet_id = "fake-spreadsheet-id"
test_spreadsheet_id = "fake-test-sheet-id"
drive_receipts_folder_id = "fake-receipts-folder-id"
drive_reports_folder_id = "fake-reports-folder-id"

[google]
service_account_file = "secrets/service-account.json"
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "config.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_load_config_full(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _FULL_CONFIG))

    assert cfg.organization.name == "Example PTA"
    assert cfg.organization.school_name == "Example Elementary"
    assert cfg.organization.school_email == "office@example.org"

    assert cfg.contacts.president == ("president@example.org",)
    assert cfg.contacts.treasurer == "treasurer@example.org"
    assert cfg.contacts.cfo == "cfo@example.org"
    assert cfg.contacts.account_holders == (
        "president@example.org",
        "treasurer@example.org",
    )

    assert cfg.fiscal_year.start_month == 1
    assert cfg.grades.labels == ("K", "1", "2", "3", "4", "5")

    assert cfg.sheets.spreadsheet_id == "fake-spreadsheet-id"
    assert cfg.sheets.test_spreadsheet_id == "fake-test-sheet-id"
    assert cfg.sheets.drive_receipts_folder_id == "fake-receipts-folder-id"
    assert cfg.sheets.drive_reports_folder_id == "fake-reports-folder-id"

    assert cfg.google.service_account_file == "secrets/service-account.json"
    # SA path is resolved relative to the config file's directory; contents untouched.
    assert (
        cfg.google.service_account_path == (tmp_path / "secrets" / "service-account.json").resolve()
    )


def test_missing_required_field_raises_naming_field(tmp_path: Path) -> None:
    # Drop `treasurer` from [contacts]; load_config must raise ConfigError naming it.
    text = _FULL_CONFIG.replace('treasurer = "treasurer@example.org"\n', "")
    with pytest.raises(ConfigError) as exc_info:
        load_config(_write(tmp_path, text))
    assert exc_info.value.field == "contacts.treasurer"
    assert "contacts.treasurer" in str(exc_info.value)


def test_missing_required_section_raises(tmp_path: Path) -> None:
    # Remove the entire [google] section.
    text = _FULL_CONFIG.split("[google]")[0]
    with pytest.raises(ConfigError) as exc_info:
        load_config(_write(tmp_path, text))
    assert exc_info.value.field == "google"


def test_bad_start_month_raises(tmp_path: Path) -> None:
    text = _FULL_CONFIG.replace("start_month = 1", "start_month = 13")
    with pytest.raises(ConfigError) as exc_info:
        load_config(_write(tmp_path, text))
    assert exc_info.value.field == "fiscal_year.start_month"


def test_absolute_sa_path_preserved(tmp_path: Path) -> None:
    abs_path = (tmp_path / "elsewhere" / "sa.json").resolve()
    text = _FULL_CONFIG.replace(
        'service_account_file = "secrets/service-account.json"',
        f'service_account_file = "{abs_path.as_posix()}"',
    )
    cfg = load_config(_write(tmp_path, text))
    assert cfg.google.service_account_path == abs_path
