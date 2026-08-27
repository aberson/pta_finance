"""Tests for pta_finance.config — typed load + fail-fast on missing required field."""

from __future__ import annotations

from datetime import date
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


# --------------------------------------------------------------------------------------
# The OPTIONAL [gmail] section (feature-plan Design Decision 5). Every test above loads a
# config with NO [gmail] block, which is itself the regression guard: making the section
# required would break all of them, plus conftest.py and test_reports.py.
# --------------------------------------------------------------------------------------

_GMAIL_SECTION = """
[gmail]
client_secrets_file = "secrets/gmail-client-secret.json"
token_file = "secrets/gmail-token.json"
inbox_dir = "mail_samples"
"""


def test_missing_gmail_section_yields_none(tmp_path: Path) -> None:
    # Absent [gmail] must NOT raise (unlike every required section) and must leave
    # Config.gmail as None, so an org that never wires up Gmail is unaffected.
    cfg = load_config(_write(tmp_path, _FULL_CONFIG))
    assert cfg.gmail is None


def test_gmail_section_parsed_with_resolved_paths(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _FULL_CONFIG + _GMAIL_SECTION))

    assert cfg.gmail is not None
    assert cfg.gmail.client_secrets_file == "secrets/gmail-client-secret.json"
    assert cfg.gmail.token_file == "secrets/gmail-token.json"
    assert cfg.gmail.inbox_dir == "mail_samples"
    # Paths resolve against the config file's directory, exactly like the SA key path;
    # contents are never read here.
    assert (
        cfg.gmail.client_secrets_path
        == (tmp_path / "secrets" / "gmail-client-secret.json").resolve()
    )
    assert cfg.gmail.token_path == (tmp_path / "secrets" / "gmail-token.json").resolve()
    assert cfg.gmail.inbox_path == (tmp_path / "mail_samples").resolve()


def test_absolute_gmail_paths_preserved(tmp_path: Path) -> None:
    abs_token = (tmp_path / "elsewhere" / "gmail-token.json").resolve()
    text = (_FULL_CONFIG + _GMAIL_SECTION).replace(
        'token_file = "secrets/gmail-token.json"',
        f'token_file = "{abs_token.as_posix()}"',
    )
    cfg = load_config(_write(tmp_path, text))
    assert cfg.gmail is not None
    assert cfg.gmail.token_path == abs_token


def test_present_gmail_section_still_requires_every_key(tmp_path: Path) -> None:
    # Optional section, but not optional keys: a half-filled block fails fast by name.
    text = (_FULL_CONFIG + _GMAIL_SECTION).replace('token_file = "secrets/gmail-token.json"\n', "")
    with pytest.raises(ConfigError) as exc_info:
        load_config(_write(tmp_path, text))
    assert exc_info.value.field == "gmail.token_file"


def test_gmail_section_that_is_not_a_table_raises(tmp_path: Path) -> None:
    # Root-level key (must precede the first section header, or TOML nests it).
    text = 'gmail = "secrets/gmail-token.json"\n' + _FULL_CONFIG
    with pytest.raises(ConfigError) as exc_info:
        load_config(_write(tmp_path, text))
    assert exc_info.value.field == "gmail"


# --------------------------------------------------------------------------------------
# The OPTIONAL [receipt_mapping] section. Its received_since value is the authoritative,
# inclusive ledger-membership cutoff; older configs without the section remain all-history.
# --------------------------------------------------------------------------------------

_RECEIPT_MAPPING_SECTION = """
[receipt_mapping]
received_since = "2030-09-01"
"""


def test_missing_receipt_mapping_section_yields_none(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _FULL_CONFIG))
    assert cfg.receipt_mapping is None


def test_receipt_mapping_received_since_parses_iso_date(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _FULL_CONFIG + _RECEIPT_MAPPING_SECTION))

    assert cfg.receipt_mapping is not None
    assert cfg.receipt_mapping.received_since == date(2030, 9, 1)


@pytest.mark.parametrize("value", ['"not-a-date"', '""', "123"])
def test_malformed_receipt_mapping_cutoff_names_field(tmp_path: Path, value: str) -> None:
    text = _FULL_CONFIG + f"\n[receipt_mapping]\nreceived_since = {value}\n"

    with pytest.raises(ConfigError) as exc_info:
        load_config(_write(tmp_path, text))

    assert exc_info.value.field == "receipt_mapping.received_since"
    assert "receipt_mapping.received_since" in str(exc_info.value)
