"""Load + validate the private ``config.toml`` (stdlib ``tomllib``).

Identity (org/school name, emails, spreadsheet/Drive IDs, grade labels, fiscal-year
setting) lives ONLY in ``config.toml`` (gitignored) — never in committed code. This
module reads that private file into typed, frozen dataclasses and fails fast with a
clear :class:`ConfigError` naming the missing field.

Secrets posture: this module resolves the service-account key *path* (relative to the
config file's directory) but NEVER reads, logs, or prints the key's contents.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "ConfigError",
    "Organization",
    "Contacts",
    "FiscalYear",
    "Grades",
    "Sheets",
    "Google",
    "Config",
    "load_config",
]


class ConfigError(Exception):
    """Raised when a required config field is missing or malformed.

    ``ConfigError.field`` names the offending field (dotted path) so the operator
    knows exactly what to fix in ``config.toml``.
    """

    def __init__(self, field: str, message: str | None = None) -> None:
        self.field = field
        super().__init__(message or f"missing or invalid required config field: {field}")


@dataclass(frozen=True)
class Organization:
    name: str
    school_name: str
    school_email: str


@dataclass(frozen=True)
class Contacts:
    president: tuple[str, ...]
    treasurer: str
    cfo: str
    account_holders: tuple[str, ...]


@dataclass(frozen=True)
class FiscalYear:
    start_month: int


@dataclass(frozen=True)
class Grades:
    labels: tuple[str, ...]


@dataclass(frozen=True)
class Sheets:
    spreadsheet_id: str
    test_spreadsheet_id: str
    drive_receipts_folder_id: str
    drive_reports_folder_id: str


@dataclass(frozen=True)
class Google:
    service_account_file: str
    # Absolute path to the service-account JSON, resolved relative to the config
    # file's directory. The path only — contents are never read here.
    service_account_path: Path


@dataclass(frozen=True)
class Config:
    organization: Organization
    contacts: Contacts
    fiscal_year: FiscalYear
    grades: Grades
    sheets: Sheets
    google: Google


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    if not isinstance(value, dict):
        raise ConfigError(name, f"missing required config section: [{name}]")
    return value


def _require(section: dict[str, Any], key: str, dotted: str) -> Any:
    if key not in section:
        raise ConfigError(dotted)
    value = section[key]
    if value is None:
        raise ConfigError(dotted)
    return value


def _require_str(section: dict[str, Any], key: str, dotted: str) -> str:
    value = _require(section, key, dotted)
    if not isinstance(value, str) or value == "":
        raise ConfigError(dotted, f"expected a non-empty string for {dotted}")
    return value


def _require_int(section: dict[str, Any], key: str, dotted: str) -> int:
    value = _require(section, key, dotted)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(dotted, f"expected an integer for {dotted}")
    return value


def _require_str_list(section: dict[str, Any], key: str, dotted: str) -> tuple[str, ...]:
    value = _require(section, key, dotted)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(dotted, f"expected a list of strings for {dotted}")
    return tuple(value)


def load_config(path: Path) -> Config:
    """Read, validate, and return the typed :class:`Config` from a ``config.toml``.

    Raises :class:`ConfigError` (naming the field) on any missing/invalid required
    field, and ``FileNotFoundError`` if ``path`` does not exist.
    """
    path = Path(path)
    with path.open("rb") as fh:
        data = tomllib.load(fh)

    org_s = _section(data, "organization")
    organization = Organization(
        name=_require_str(org_s, "name", "organization.name"),
        school_name=_require_str(org_s, "school_name", "organization.school_name"),
        school_email=_require_str(org_s, "school_email", "organization.school_email"),
    )

    con_s = _section(data, "contacts")
    contacts = Contacts(
        president=_require_str_list(con_s, "president", "contacts.president"),
        treasurer=_require_str(con_s, "treasurer", "contacts.treasurer"),
        cfo=_require_str(con_s, "cfo", "contacts.cfo"),
        account_holders=_require_str_list(con_s, "account_holders", "contacts.account_holders"),
    )

    fy_s = _section(data, "fiscal_year")
    start_month = _require_int(fy_s, "start_month", "fiscal_year.start_month")
    if not 1 <= start_month <= 12:
        raise ConfigError(
            "fiscal_year.start_month",
            "fiscal_year.start_month must be in 1..12",
        )
    fiscal_year = FiscalYear(start_month=start_month)

    grades_s = _section(data, "grades")
    grades = Grades(labels=_require_str_list(grades_s, "labels", "grades.labels"))

    sheets_s = _section(data, "sheets")
    sheets = Sheets(
        spreadsheet_id=_require_str(sheets_s, "spreadsheet_id", "sheets.spreadsheet_id"),
        test_spreadsheet_id=_require_str(
            sheets_s, "test_spreadsheet_id", "sheets.test_spreadsheet_id"
        ),
        drive_receipts_folder_id=_require_str(
            sheets_s, "drive_receipts_folder_id", "sheets.drive_receipts_folder_id"
        ),
        drive_reports_folder_id=_require_str(
            sheets_s, "drive_reports_folder_id", "sheets.drive_reports_folder_id"
        ),
    )

    google_s = _section(data, "google")
    service_account_file = _require_str(
        google_s, "service_account_file", "google.service_account_file"
    )
    sa_path = Path(service_account_file)
    if not sa_path.is_absolute():
        sa_path = (path.parent / sa_path).resolve()
    google = Google(
        service_account_file=service_account_file,
        service_account_path=sa_path,
    )

    return Config(
        organization=organization,
        contacts=contacts,
        fiscal_year=fiscal_year,
        grades=grades,
        sheets=sheets,
        google=google,
    )
