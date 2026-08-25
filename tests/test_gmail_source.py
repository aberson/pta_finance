"""Tests for pta_finance.gmail_source — the pinned read-only scope + actionable failures.

Nothing here touches the network or a real credential, but nothing here fakes a
production component either. Credentials are always built by the module's own factory and
refreshed by the real ``Credentials.refresh``; the ONLY thing substituted is
``google.oauth2.reauth.refresh_grant`` — the single function that performs the outbound
HTTP call (``google/oauth2/credentials.py:435``). Faking at that seam keeps the real
``_perform_refresh_token`` in the path, which is what assigns ``_granted_scopes`` and
deliberately leaves ``_scopes`` alone, and keeps the real ``to_json``.

That matters more than usual for this module: two earlier revisions shipped a scope check
that could never fail, and both times the covering test was green only because it replaced
``_credentials_from_info`` — the very function whose scope-pinning behaviour was the
question. Substituting the component under question is how a mock hides producer-consumer
drift (``dev/.claude/rules/code-quality.md``), so this file does not do it.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import re
import webbrowser
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from google.auth.exceptions import RefreshError, TransportError
from google.oauth2 import reauth
from google.oauth2.credentials import Credentials

from pta_finance import cli, gmail_source, receipt_ingest
from pta_finance.config import Config, load_config
from pta_finance.gmail_source import SCOPES, GmailAuthError, load_credentials

_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
_SEND = "https://www.googleapis.com/auth/gmail.send"
_METADATA = "https://www.googleapis.com/auth/gmail.metadata"

# A complete, fake config. Identity is obviously-fake placeholders only (this is a
# PUBLIC repo); the [gmail] block mirrors the commented-out one in config.example.toml.
_BASE_CONFIG = """\
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

_GMAIL_SECTION = """
[gmail]
client_secrets_file = "secrets/gmail-client-secret.json"
token_file = "secrets/gmail-token.json"
inbox_dir = "mail_samples"
"""

# Fake token material — placeholder values only, never echoed by the module.
_FAKE_REFRESH_TOKEN = "fake-refresh-token-value"
_FAKE_CLIENT_SECRET = "fake-client-secret-value"
_FAKE_ACCESS_TOKEN = "fake-refreshed-access-token"

# A far-future expiry makes the loaded credentials valid; omitting it makes them expired
# (`from_authorized_user_info` back-dates a missing expiry, credentials.py:496-502).
_VALID_EXPIRY = "2999-01-01T00:00:00Z"


def _utcnow() -> datetime.datetime:
    """Naive UTC, matching ``google.auth._helpers.utcnow`` (which strips tzinfo)."""
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


def _config_path(tmp_path: Path, *, gmail: bool = True) -> Path:
    """Write a config to ``tmp_path`` and return its path (what the CLI's --config takes)."""
    text = _BASE_CONFIG + (_GMAIL_SECTION if gmail else "")
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _load(tmp_path: Path, *, gmail: bool = True) -> Config:
    """Write a config to ``tmp_path`` and load it through the production loader."""
    return load_config(_config_path(tmp_path, gmail=gmail))


def _write_client_secrets(tmp_path: Path) -> Path:
    path = tmp_path / "secrets" / "gmail-client-secret.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"installed": {"client_id": "fake-client-id"}}), encoding="utf-8")
    return path


def _write_token(
    tmp_path: Path,
    *,
    scopes: list[str] | str | None = None,
    expiry: str | int | list[str] | None = _VALID_EXPIRY,
    refresh_token: str | None = _FAKE_REFRESH_TOKEN,
    drop: tuple[str, ...] = (),
    text: str | None = None,
) -> Path:
    """Write a fake authorized-user token file (and the client-secrets file beside it)."""
    _write_client_secrets(tmp_path)
    path = tmp_path / "secrets" / "gmail-token.json"
    if text is not None:
        path.write_text(text, encoding="utf-8")
        return path
    payload: dict[str, Any] = {
        "token": "fake-access-token",
        "refresh_token": refresh_token,
        "client_id": "fake-client-id.apps.googleusercontent.com",
        "client_secret": _FAKE_CLIENT_SECRET,
        "scopes": [_READONLY] if scopes is None else scopes,
    }
    if expiry is not None:
        payload["expiry"] = expiry
    for key in drop:
        payload.pop(key, None)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _install_grant(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scope: str | None = _READONLY,
    expires_in: datetime.timedelta = datetime.timedelta(hours=1),
    error: Exception | None = None,
) -> list[dict[str, Any]]:
    """Substitute ONLY the outbound HTTP call inside ``Credentials.refresh``.

    ``credentials.py:435`` calls ``reauth.refresh_grant(...)`` and unpacks
    ``(access_token, refresh_token, expiry, grant_response, rapt_token)``. Everything
    above that — the real ``_perform_refresh_token``, which assigns ``_granted_scopes``
    from ``grant_response["scope"]`` and never touches ``_scopes``, and the real
    ``to_json`` — stays in the path. ``scope=None`` models a response that omits the
    ``scope`` field, which the endpoint is allowed to do.

    Returns a list recording each call, so a test can assert a refresh did NOT happen.
    """
    calls: list[dict[str, Any]] = []

    def _grant(
        request: Any,
        token_uri: str,
        refresh_token: str,
        client_id: str,
        client_secret: str,
        **kwargs: Any,
    ) -> tuple[Any, ...]:
        calls.append({"requested_scopes": kwargs.get("scopes")})
        if error is not None:
            raise error
        response: dict[str, Any] = {"access_token": _FAKE_ACCESS_TOKEN}
        if scope is not None:
            response["scope"] = scope
        return (_FAKE_ACCESS_TOKEN, refresh_token, _utcnow() + expires_in, response, None)

    monkeypatch.setattr(reauth, "refresh_grant", _grant)
    return calls


def _saved(token_path: Path) -> dict[str, Any]:
    record: dict[str, Any] = json.loads(token_path.read_text(encoding="utf-8"))
    return record


# --------------------------------------------------------------------------------------
# The scope pin (Design Decision 2) — the security regression tests.
# --------------------------------------------------------------------------------------


def test_scopes_is_pinned_to_exactly_readonly() -> None:
    # EXACT equality against the literal, not a substring or subset check: adding
    # gmail.send / gmail.modify must fail here rather than widen the grant quietly.
    assert gmail_source.SCOPES == ("https://www.googleapis.com/auth/gmail.readonly",)
    assert isinstance(gmail_source.SCOPES, tuple)


def test_stored_token_with_extra_scope_is_rejected(tmp_path: Path) -> None:
    _write_token(tmp_path, scopes=[_READONLY, _SEND])

    with pytest.raises(GmailAuthError) as exc_info:
        load_credentials(_load(tmp_path))

    message = str(exc_info.value)
    assert _SEND in message  # names the offending scope
    assert exc_info.value.remediation
    # The token's secret material must never appear in the message.
    assert _FAKE_REFRESH_TOKEN not in message
    assert _FAKE_CLIENT_SECRET not in message


def test_over_scoped_token_is_rejected_before_any_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The scope re-check runs BEFORE credentials are built, so an over-scoped token is
    # never presented to Google at all. Tracked by counting factory calls.
    _write_token(tmp_path, scopes=[_READONLY, _SEND])
    calls: list[Any] = []
    monkeypatch.setattr(gmail_source, "_credentials_from_info", lambda info: calls.append(info))

    with pytest.raises(GmailAuthError):
        load_credentials(_load(tmp_path))
    assert calls == []


def test_space_delimited_extra_scope_is_rejected(tmp_path: Path) -> None:
    # Google also accepts a space-delimited scope string; it must not slip past the check.
    _write_token(tmp_path, scopes=f"{_READONLY} {_SEND}")

    with pytest.raises(GmailAuthError) as exc_info:
        load_credentials(_load(tmp_path))
    assert _SEND in str(exc_info.value)


def test_token_granting_only_a_different_scope_is_rejected_as_over_scoped(
    tmp_path: Path,
) -> None:
    # With a single-entry SCOPES, ANY non-empty grant that lacks gmail.readonly also
    # carries something outside the pin, so it is the "beyond the pin" branch that fires.
    # Asserted on the DISTINGUISHING wording so this test cannot silently pass on the
    # other branch's message.
    _write_token(tmp_path, scopes=[_METADATA])

    with pytest.raises(GmailAuthError) as exc_info:
        load_credentials(_load(tmp_path))
    message = str(exc_info.value)
    assert _METADATA in message
    assert "beyond this toolkit's read-only pin" in message
    assert "does not grant" not in message
    assert exc_info.value.remediation


def test_narrowed_grant_is_rejected_when_the_pin_holds_several_scopes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The "missing scope" branch is unreachable while SCOPES has one entry, but it is
    # what makes a future multi-scope pin fail closed — so exercise it directly with a
    # widened pin rather than leaving it unverified.
    monkeypatch.setattr(gmail_source, "SCOPES", (_READONLY, _METADATA))

    with pytest.raises(GmailAuthError) as exc_info:
        gmail_source._check_granted_scopes([_READONLY], "the test grant", tmp_path / "token.json")
    message = str(exc_info.value)
    assert "does not grant" in message
    assert _METADATA in message
    assert "beyond this toolkit's read-only pin" not in message


def test_token_recording_no_scopes_is_rejected(tmp_path: Path) -> None:
    _write_token(tmp_path, scopes=[])

    with pytest.raises(GmailAuthError) as exc_info:
        load_credentials(_load(tmp_path))
    assert "scope" in str(exc_info.value).lower()


# --------------------------------------------------------------------------------------
# The provenance invariant: a scope check is real only if the checked value's source is
# independent of SCOPES. These are the tests that catch a re-derived (tautological) check.
# --------------------------------------------------------------------------------------


def test_real_credentials_scopes_are_the_pin_not_the_token_file(tmp_path: Path) -> None:
    """Pin the first half of the laundering chain.

    ``from_authorized_user_info`` only falls back to ``info["scopes"]`` when the caller
    passes ``scopes=None`` (``credentials.py:505-508``), and this module always passes the
    pin. So ``creds.scopes`` equals SCOPES even for a token file recording a wider grant.
    If google-auth ever changes this, this test fails and says the assumption moved.
    """
    _write_token(tmp_path, scopes=[_READONLY, _SEND])
    info = json.loads((tmp_path / "secrets" / "gmail-token.json").read_text(encoding="utf-8"))

    creds = gmail_source._credentials_from_info(info)

    assert list(creds.scopes) == list(SCOPES)  # the pin, NOT the file's [readonly, send]
    assert _SEND not in list(creds.scopes)
    assert creds.granted_scopes is None  # only a refresh response populates this


def test_real_to_json_echoes_the_pin_not_the_granted_scopes(tmp_path: Path) -> None:
    """Pin the second half: ``to_json`` is not an independent observation either.

    ``to_json`` serialises ``self.scopes`` (``credentials.py:564``), so even with a WIDE
    ``_granted_scopes`` set exactly as a refresh would set it, the serialised record still
    reports the pin. Any check reading scopes back out of ``to_json()`` is therefore
    ``SCOPES == SCOPES`` — this test is the proof, and it is why :func:`_save_token`
    takes the verified grant as a parameter instead.
    """
    _write_token(tmp_path)
    info = json.loads((tmp_path / "secrets" / "gmail-token.json").read_text(encoding="utf-8"))
    creds = gmail_source._credentials_from_info(info)

    creds._granted_scopes = [_READONLY, _SEND]  # exactly what credentials.py:454 assigns
    echoed = json.loads(creds.to_json())["scopes"]

    assert echoed == list(SCOPES)
    assert _SEND not in echoed


def test_on_disk_extra_scope_rejected_even_though_credentials_would_be_pinned(
    tmp_path: Path,
) -> None:
    # Site 1 (fresh load) shown able to go red, with no substitution at all: the token
    # FILE records an extra scope, the Credentials object would carry the pin, and
    # load_credentials must still refuse.
    token_path = _write_token(tmp_path, scopes=[_READONLY, _SEND])
    before = token_path.read_text(encoding="utf-8")

    with pytest.raises(GmailAuthError) as exc_info:
        load_credentials(_load(tmp_path))

    assert _SEND in str(exc_info.value)
    # Untouched: the over-scoped record is never rewritten, i.e. never laundered into a
    # file that claims the pinned scope.
    assert token_path.read_text(encoding="utf-8") == before
    assert _SEND in json.loads(before)["scopes"]


def test_scope_widened_by_a_refresh_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Site 2 (post-refresh) shown able to go red through the REAL refresh machinery: the
    # stored token is clean, the token endpoint hands back a WIDER grant. Only
    # `granted_scopes` reports it — `scopes` and `to_json()` both still say the pin — so
    # a check reading either of those could not catch this.
    token_path = _write_token(tmp_path, expiry=None)  # expired ⇒ a refresh happens
    before = token_path.read_text(encoding="utf-8")
    _install_grant(monkeypatch, scope=f"{_READONLY} {_SEND}")

    with pytest.raises(GmailAuthError) as exc_info:
        load_credentials(_load(tmp_path))

    assert _SEND in str(exc_info.value)
    assert token_path.read_text(encoding="utf-8") == before  # widened grant not persisted


def test_save_token_rejects_an_unverified_grant(tmp_path: Path) -> None:
    # Site 3 (pre-persist) shown able to go red — the thing the two previous revisions of
    # this module could not do at all. The credentials object is real and pinned; the
    # grant handed in is not, and that parameter is what gets checked.
    _write_token(tmp_path)
    info = json.loads((tmp_path / "secrets" / "gmail-token.json").read_text(encoding="utf-8"))
    creds = gmail_source._credentials_from_info(info)
    target = tmp_path / "secrets" / "written.json"

    with pytest.raises(GmailAuthError) as exc_info:
        gmail_source._save_token(creds, target, [_READONLY, _SEND])

    assert _SEND in str(exc_info.value)
    assert not target.exists()  # nothing was written


def test_saved_record_carries_the_verified_grant_not_the_credentials_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Proves the persisted value's provenance is the PARAMETER, not `to_json`'s pin echo.
    # The check enforces set equality, so order is the observable difference: the pin is
    # [readonly, metadata] and the verified grant is the same two in the other order.
    monkeypatch.setattr(gmail_source, "SCOPES", (_READONLY, _METADATA))
    _write_token(tmp_path, scopes=[_READONLY, _METADATA])
    info = json.loads((tmp_path / "secrets" / "gmail-token.json").read_text(encoding="utf-8"))
    creds = gmail_source._credentials_from_info(info)
    assert list(creds.scopes) == [_READONLY, _METADATA]  # what to_json would have written
    target = tmp_path / "secrets" / "written.json"

    gmail_source._save_token(creds, target, [_METADATA, _READONLY])

    assert _saved(target)["scopes"] == [_METADATA, _READONLY]


def test_no_scope_check_reads_a_pin_derived_value() -> None:
    """The invariant itself, as a tripwire on the module's source.

    Every ``_check_granted_scopes`` call site must pass a value whose provenance is an
    independent observation. The two refuted revisions passed ``creds.scopes`` and a
    ``to_json()`` read-back; both would fail this test. A NEW argument name fails it too,
    which is the point: adding one forces the author to state where the value came from.
    """
    source = Path(gmail_source.__file__).read_text(encoding="utf-8")
    args = re.findall(r"(?<!def )_check_granted_scopes\(\s*([A-Za-z_][\w.()\[\]]*)", source)

    assert args, "expected at least one _check_granted_scopes call site"
    independent = {
        "_recorded_scopes(info)",  # raw token-file JSON, read before any Credentials exist
        "granted",  # creds.granted_scopes — assigned only from the endpoint response
        "verified_scopes",  # threaded parameter, originating at one of the two above
    }
    assert set(args) <= independent, f"scope check on a value of unstated provenance: {args}"
    for banned in ("creds.scopes", "to_json", "SCOPES"):
        assert not any(banned in arg for arg in args), f"scope check reads the pin: {banned}"


# --------------------------------------------------------------------------------------
# Configuration and token-file failures.
# --------------------------------------------------------------------------------------


def test_missing_gmail_section_raises_actionable_error(tmp_path: Path) -> None:
    cfg = _load(tmp_path, gmail=False)
    assert cfg.gmail is None  # optional section: absent, not an error

    with pytest.raises(GmailAuthError) as exc_info:
        load_credentials(cfg)
    assert "[gmail]" in str(exc_info.value)
    assert "client_secrets_file" in exc_info.value.remediation


def test_missing_token_file_names_the_consent_command(tmp_path: Path) -> None:
    _write_client_secrets(tmp_path)  # client set up, but consent never granted
    cfg = _load(tmp_path)
    assert cfg.gmail is not None

    with pytest.raises(GmailAuthError) as exc_info:
        load_credentials(cfg)
    assert str(cfg.gmail.token_path) in str(exc_info.value)
    assert "fetch-mail" in exc_info.value.remediation


def test_missing_client_secrets_file_names_the_download_step(tmp_path: Path) -> None:
    cfg = _load(tmp_path)  # neither file exists
    assert cfg.gmail is not None

    with pytest.raises(GmailAuthError) as exc_info:
        load_credentials(cfg)
    assert str(cfg.gmail.client_secrets_path) in str(exc_info.value)
    assert "Desktop app" in exc_info.value.remediation


def test_malformed_token_file_does_not_echo_its_contents(tmp_path: Path) -> None:
    secret_ish = "not-json-but-secret-looking"
    _write_token(tmp_path, text=secret_ish)

    with pytest.raises(GmailAuthError) as exc_info:
        load_credentials(_load(tmp_path))
    assert "JSON" in str(exc_info.value)
    assert secret_ish not in str(exc_info.value)
    assert exc_info.value.remediation


def test_non_utf8_token_file_raises_instead_of_a_raw_traceback(tmp_path: Path) -> None:
    # UnicodeDecodeError is a ValueError, not an OSError, so an explicit handler is the
    # only thing keeping a corrupt/truncated token from escaping as a raw traceback.
    _write_client_secrets(tmp_path)
    token_path = tmp_path / "secrets" / "gmail-token.json"
    token_path.write_bytes(b'{"token": "\xff\xfe\x00not-utf8"}')

    with pytest.raises(GmailAuthError) as exc_info:
        load_credentials(_load(tmp_path))

    message = str(exc_info.value)
    assert "UTF-8" in message
    assert exc_info.value.remediation
    assert "not-utf8" not in message  # no bytes from the file are echoed back


def test_token_file_holding_a_json_list_is_rejected(tmp_path: Path) -> None:
    _write_token(tmp_path, text='["not", "an", "object"]')

    with pytest.raises(GmailAuthError) as exc_info:
        load_credentials(_load(tmp_path))
    assert exc_info.value.remediation


def test_token_missing_refresh_token_field_is_translated(tmp_path: Path) -> None:
    # The REAL google-auth constructor rejects this shape with a ValueError, which must
    # surface as a remediation-bearing GmailAuthError.
    _write_token(tmp_path, drop=("refresh_token",))

    with pytest.raises(GmailAuthError) as exc_info:
        load_credentials(_load(tmp_path))
    assert "authorized-user" in str(exc_info.value)
    assert exc_info.value.remediation


def test_non_string_expiry_is_translated(tmp_path: Path) -> None:
    # Valid UTF-8, valid JSON, legal JSON types — but `from_authorized_user_info` does
    # `expiry.rstrip("Z")` with no type check (credentials.py:496-500), so a numeric or
    # list expiry raises AttributeError/TypeError. Neither is a ValueError, so an
    # enumerated handler missed them.
    for bad_expiry in (12345, ["2999-01-01T00:00:00Z"]):
        _write_token(tmp_path, expiry=bad_expiry)

        with pytest.raises(GmailAuthError) as exc_info:
            load_credentials(_load(tmp_path))
        assert "authorized-user" in str(exc_info.value)
        assert exc_info.value.remediation


# --------------------------------------------------------------------------------------
# Load / refresh behaviour, through the real Credentials machinery.
# --------------------------------------------------------------------------------------


def test_valid_token_loads_through_real_google_auth(tmp_path: Path) -> None:
    # Far-future expiry ⇒ the credentials are already valid ⇒ no refresh, no rewrite.
    token_path = _write_token(tmp_path)
    before = token_path.read_text(encoding="utf-8")

    creds = load_credentials(_load(tmp_path))

    assert creds.valid
    assert list(creds.scopes) == list(SCOPES)
    assert token_path.read_text(encoding="utf-8") == before


def test_valid_token_is_not_refreshed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_token(tmp_path)
    calls = _install_grant(monkeypatch)

    assert load_credentials(_load(tmp_path)).valid
    assert calls == []  # the token endpoint was never contacted


def test_expired_token_is_refreshed_and_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = _write_token(tmp_path, expiry=None)
    calls = _install_grant(monkeypatch)

    creds = load_credentials(_load(tmp_path))

    assert creds.valid
    assert len(calls) == 1
    assert calls[0]["requested_scopes"] == list(SCOPES)  # only the pin is ever requested
    assert creds.granted_scopes == [_READONLY]
    # The refreshed token is written back, carrying the grant the endpoint returned.
    record = _saved(token_path)
    assert record["token"] == _FAKE_ACCESS_TOKEN
    assert record["scopes"] == [_READONLY]


def test_refresh_response_omitting_scope_is_accepted_as_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Documented choice: the endpoint may omit `scope`, leaving granted_scopes None
    # (credentials.py:452 only assigns it `if scopes and "scope" in grant_response`).
    # RFC 6749 section 5.1 defines that as "identical to the requested scope", the request
    # asked for exactly SCOPES, and a refresh cannot widen what consent fixed — so it is
    # ACCEPTED, and the disk-verified record is what gets threaded onward and persisted.
    token_path = _write_token(tmp_path, expiry=None)
    _install_grant(monkeypatch, scope=None)

    creds = load_credentials(_load(tmp_path))

    assert creds.granted_scopes is None
    record = _saved(token_path)
    assert record["token"] == _FAKE_ACCESS_TOKEN
    assert record["scopes"] == [_READONLY]


def test_dead_refresh_token_raises_remediation_bearing_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_token(tmp_path, expiry=None)
    _install_grant(monkeypatch, error=RefreshError("invalid_grant: token revoked"))
    cfg = _load(tmp_path)
    assert cfg.gmail is not None

    with pytest.raises(GmailAuthError) as exc_info:
        load_credentials(cfg)

    message = str(exc_info.value)
    assert str(cfg.gmail.token_path) in message
    assert "fetch-mail" in exc_info.value.remediation
    assert "re-consent" in exc_info.value.remediation
    assert _FAKE_REFRESH_TOKEN not in message
    assert isinstance(exc_info.value.__cause__, RefreshError)


def test_network_failure_during_refresh_is_reported_as_a_network_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # TransportError is a SIBLING of RefreshError, not a subclass, and it is what every
    # ordinary connectivity failure surfaces as (google/auth/transport/requests.py wraps
    # every requests exception). For a monthly cron this is the likeliest failure of all,
    # and its remediation must NOT tell the operator to delete a perfectly good token.
    token_path = _write_token(tmp_path, expiry=None)
    before = token_path.read_text(encoding="utf-8")
    _install_grant(monkeypatch, error=TransportError("connection refused"))

    with pytest.raises(GmailAuthError) as exc_info:
        load_credentials(_load(tmp_path))

    remediation = exc_info.value.remediation
    assert "network" in str(exc_info.value).lower()
    assert "Do NOT delete" in remediation
    assert "re-consent" not in remediation  # distinct from the dead-token remediation
    assert isinstance(exc_info.value.__cause__, TransportError)
    assert token_path.read_text(encoding="utf-8") == before


def test_expired_token_without_refresh_token_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A null refresh_token passes google-auth's key-presence check but leaves nothing to
    # renew with — a real, reachable token-file shape.
    _write_token(tmp_path, expiry=None, refresh_token=None)
    calls = _install_grant(monkeypatch)

    with pytest.raises(GmailAuthError) as exc_info:
        load_credentials(_load(tmp_path))
    assert calls == []
    assert exc_info.value.remediation


def test_still_invalid_after_refresh_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The endpoint answers, but with an already-expired access token.
    _write_token(tmp_path, expiry=None)
    _install_grant(monkeypatch, expires_in=datetime.timedelta(hours=-1))

    with pytest.raises(GmailAuthError) as exc_info:
        load_credentials(_load(tmp_path))
    assert "still unusable" in str(exc_info.value)
    assert exc_info.value.remediation


def test_unwritable_token_destination_raises_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = _write_token(tmp_path, expiry=None)
    before = token_path.read_text(encoding="utf-8")
    _install_grant(monkeypatch)
    cfg = _load(tmp_path)

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(gmail_source.tempfile, "mkstemp", _boom)

    with pytest.raises(GmailAuthError) as exc_info:
        load_credentials(cfg)
    assert exc_info.value.remediation
    assert token_path.read_text(encoding="utf-8") == before


def test_interrupted_token_write_leaves_the_previous_token_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The token file is this module's sole persisted credential: a write that dies partway
    # must not truncate it. The payload goes to a temp file and only an atomic os.replace
    # publishes it, so a failure at that point leaves the old token byte-identical and
    # drops no debris behind.
    token_path = _write_token(tmp_path, expiry=None)
    before = token_path.read_text(encoding="utf-8")
    _install_grant(monkeypatch)
    cfg = _load(tmp_path)

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("interrupted before the rename")

    monkeypatch.setattr(gmail_source.os, "replace", _boom)

    with pytest.raises(GmailAuthError) as exc_info:
        load_credentials(cfg)

    assert "untouched" in str(exc_info.value)
    assert token_path.read_text(encoding="utf-8") == before
    assert list(token_path.parent.glob("*.tmp")) == []  # temp file cleaned up


# --------------------------------------------------------------------------------------
# Structural guards.
# --------------------------------------------------------------------------------------


def test_unexpected_exception_is_translated_to_gmail_auth_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exception contract as an invariant, not an enumeration.

    Three foreign exceptions escaped while ``load_credentials`` guaranteed its contract by
    listing exception types (``UnicodeDecodeError``, ``AttributeError``,
    ``TransportError``). This asserts the CLASS is closed: an exception nothing names still
    leaves as a ``GmailAuthError``, with the original preserved as ``__cause__``.
    """
    _write_token(tmp_path, expiry=None)
    sentinel = RuntimeError("something no handler names")

    def _boom() -> Any:
        raise sentinel

    monkeypatch.setattr(gmail_source, "_request", _boom)

    with pytest.raises(GmailAuthError) as exc_info:
        load_credentials(_load(tmp_path))

    assert exc_info.value.__cause__ is sentinel
    assert exc_info.value.remediation
    assert "RuntimeError" in str(exc_info.value)
    # The foreign exception's own text is not interpolated (it could carry token material).
    assert "something no handler names" not in str(exc_info.value)


def test_every_auth_error_carries_a_remediation() -> None:
    # The remediation is a REQUIRED constructor argument, so no raise site can ship a
    # dead-end message.
    err = GmailAuthError("something broke.", "do this next.")
    assert err.remediation == "do this next."
    assert "do this next." in str(err)
    with pytest.raises(TypeError):
        GmailAuthError("no remediation")  # type: ignore[call-arg]


def test_fetcher_and_parser_do_not_import_each_other() -> None:
    # Design Decision 4: the Gmail surface writes files, the parser reads them, and the
    # two never import each other — that is what keeps the parser credential-free.
    fetcher = Path(gmail_source.__file__).read_text(encoding="utf-8")
    parser = Path(receipt_ingest.__file__).read_text(encoding="utf-8")
    assert "import receipt_ingest" not in fetcher
    assert "receipt_ingest import" not in fetcher
    assert "gmail_source" not in parser


# ======================================================================================
# The faked Gmail service.
#
# A pure in-test double: no network, no credentials, and `googleapiclient` is never
# imported here. It is INJECTED (the CLI command takes an optional `service=`), rather
# than monkeypatched over a private import path, so what the tests drive is the real
# production call chain — `service.users().messages().list(...).execute()` — right down to
# the shape of the responses.
# ======================================================================================


class _FakeRequest:
    """One prepared API call. ``execute()`` hands back the canned response, or raises."""

    def __init__(self, response: Any, error: BaseException | None = None) -> None:
        self._response = response
        self._error = error

    def execute(self) -> Any:
        if self._error is not None:
            raise self._error
        return self._response


class _FakeMessages:
    """``users().messages()`` — records every call so a test can assert what was asked."""

    def __init__(self, pages: list[Any], raws: dict[str, bytes | None]) -> None:
        self.pages = pages
        self.raws = raws
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[str] = []
        # Injectable failures. `*_build_error` fires while the request object is being
        # CONSTRUCTED, `*_execute_error` while it runs — googleapiclient can raise at
        # either point, and the module's exception boundary has to cover both.
        self.list_execute_error: BaseException | None = None
        self.get_build_error: BaseException | None = None
        self.get_execute_error: BaseException | None = None

    def list(self, *, userId: str, q: str, pageToken: str | None = None) -> _FakeRequest:
        self.list_calls.append({"userId": userId, "q": q, "pageToken": pageToken})
        page = self.pages[0 if pageToken is None else int(pageToken)]
        return _FakeRequest(page, self.list_execute_error)

    def get(self, *, userId: str, id: str, format: str) -> _FakeRequest:
        assert format == "raw", "the fetcher must always ask for the raw RFC-822 bytes"
        if self.get_build_error is not None:
            raise self.get_build_error
        self.get_calls.append(id)
        if self.get_execute_error is not None:
            return _FakeRequest(None, self.get_execute_error)
        payload = self.raws[id]
        if payload is None:
            return _FakeRequest({"id": id})  # a response that omits `raw` entirely
        # Gmail base64url-encodes the payload and MAY omit the `=` padding, so this double
        # always strips it — a fetcher that forgets to re-pad fails here, not in production.
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        return _FakeRequest({"id": id, "raw": encoded})


class _FakeUsers:
    def __init__(self, messages: _FakeMessages) -> None:
        self._messages = messages

    def messages(self) -> _FakeMessages:
        return self._messages


class FakeGmail:
    """A whole fake Gmail service, paginated at ``page_size`` so pagination is always live."""

    def __init__(self, raws: dict[str, bytes | None], *, page_size: int = 2) -> None:
        ids = list(raws)
        pages: list[Any] = []
        for start in range(0, max(len(ids), 1), page_size):
            page: dict[str, Any] = {
                "messages": [{"id": message_id} for message_id in ids[start : start + page_size]]
            }
            if start + page_size < len(ids):
                page["nextPageToken"] = str(len(pages) + 1)
            pages.append(page)
        self.messages_resource = _FakeMessages(pages, raws)
        self._users = _FakeUsers(self.messages_resource)

    def users(self) -> _FakeUsers:
        return self._users


# Fixture mail. Obviously-fake identity only, and every field a privacy test asserts is
# ABSENT from stdout is named here so the assertion cannot drift from the fixture.
_SAMPLE_SUBJECT = "Main Reimbursement Form got a new submission"
_SAMPLE_SENDER = "forms@example.com"
_SAMPLE_BODY = "Requestor: Jane Doe. Total Amount $: 12.34.\n"


def _raw_message(message_id: str | None = "<sample-1@example.org>") -> bytes:
    """The raw RFC-822 bytes of one message, with or without a ``Message-ID`` header."""
    msg = EmailMessage()
    msg["Subject"] = _SAMPLE_SUBJECT
    msg["From"] = _SAMPLE_SENDER
    msg["To"] = "treasurer@example.org"
    msg["Date"] = "Sun, 28 Jun 2026 09:09:00 -0700"
    if message_id is not None:
        msg["Message-ID"] = message_id
    msg.set_content(_SAMPLE_BODY)
    return bytes(msg)


def _mailbox(count: int) -> dict[str, bytes | None]:
    """``{gmail-id: raw bytes}`` for ``count`` distinct messages."""
    return {f"id-{n}": _raw_message(f"<m{n}@example.org>") for n in range(count)}


def _fetch_mail(config_path: Path, service: Any, *argv: str) -> int:
    """Drive ``fetch-mail`` through the REAL parser and its REAL dispatch target.

    Resolving the command through ``args.func`` (rather than calling ``_cmd_fetch_mail``
    directly) is what makes this an integration test of the wiring: a subparser that never
    got its ``set_defaults(func=...)``, or an argument spelled differently from what the
    command reads, fails here.
    """
    parser = cli.build_parser()
    args = parser.parse_args(["fetch-mail", "--config", str(config_path), *argv])
    assert args.func is cli._cmd_fetch_mail
    result: int = args.func(args, service=service)
    return result


# --------------------------------------------------------------------------------------
# build_query — the date window.
# --------------------------------------------------------------------------------------


def test_build_query_renders_after_and_before_from_dates() -> None:
    query = gmail_source.build_query(datetime.date(2026, 7, 1), datetime.date(2026, 8, 1))
    assert query == "after:2026/07/01 before:2026/08/01"
    # Zero-padded, single-digit month AND day (a `%-d`-style renderer would fail here).
    assert gmail_source.build_query(datetime.date(2026, 1, 9)) == "after:2026/01/09"


def test_build_query_omits_before_when_until_is_none() -> None:
    assert gmail_source.build_query(datetime.date(2026, 7, 1)) == "after:2026/07/01"


def test_build_query_appends_operator_supplied_extra() -> None:
    query = gmail_source.build_query(
        datetime.date(2026, 7, 1), datetime.date(2026, 8, 1), extra="  has:attachment  "
    )
    assert query == "after:2026/07/01 before:2026/08/01 has:attachment"
    # A blank --query must not leave a stray whitespace term in the search string.
    assert gmail_source.build_query(datetime.date(2026, 7, 1), extra="   ") == "after:2026/07/01"
    assert gmail_source.build_query(datetime.date(2026, 7, 1), extra=None) == "after:2026/07/01"


# --------------------------------------------------------------------------------------
# list_message_ids — pagination.
# --------------------------------------------------------------------------------------


def test_list_message_ids_walks_every_page_token() -> None:
    service = FakeGmail(_mailbox(5), page_size=2)
    assert list(gmail_source.list_message_ids(service, "after:2026/07/01")) == [
        f"id-{n}" for n in range(5)
    ]
    calls = service.messages_resource.list_calls
    assert [call["pageToken"] for call in calls] == [None, "1", "2"]
    assert {call["q"] for call in calls} == {"after:2026/07/01"}
    assert {call["userId"] for call in calls} == {"me"}


def test_list_message_ids_yields_each_id_once_across_pages() -> None:
    # A mailbox that changes between page fetches can serve the same id on two pages.
    service = FakeGmail({}, page_size=2)
    service.messages_resource.pages = [
        {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "1"},
        {"messages": [{"id": "b"}, {"id": "c"}]},
    ]
    assert list(gmail_source.list_message_ids(service, "q")) == ["a", "b", "c"]


def test_list_message_ids_limit_stops_the_pagination() -> None:
    service = FakeGmail(_mailbox(5), page_size=2)
    assert list(gmail_source.list_message_ids(service, "q", limit=2)) == ["id-0", "id-1"]
    # The cap is not a post-filter: the second page was never requested.
    assert len(service.messages_resource.list_calls) == 1


def test_list_message_ids_limit_of_zero_asks_for_nothing() -> None:
    service = FakeGmail(_mailbox(5), page_size=2)
    assert list(gmail_source.list_message_ids(service, "q", limit=0)) == []
    assert service.messages_resource.list_calls == []


def test_list_message_ids_handles_an_empty_window() -> None:
    service = FakeGmail({})
    assert list(gmail_source.list_message_ids(service, "q")) == []


def test_list_message_ids_ignores_malformed_entries() -> None:
    service = FakeGmail({})
    service.messages_resource.pages = [
        {"messages": [{"id": "a"}, {"no-id": 1}, "junk", {"id": ""}]}
    ]
    assert list(gmail_source.list_message_ids(service, "q")) == ["a"]


def test_a_non_object_api_response_becomes_an_actionable_error() -> None:
    service = FakeGmail({})
    service.messages_resource.pages = ["not-a-json-object"]
    with pytest.raises(gmail_source.GmailFetchError) as exc_info:
        list(gmail_source.list_message_ids(service, "q"))
    assert exc_info.value.remediation


# --------------------------------------------------------------------------------------
# fetch_raw — the RFC-822 bytes.
# --------------------------------------------------------------------------------------


def test_fetch_raw_returns_the_exact_rfc822_bytes() -> None:
    raw = _raw_message()
    service = FakeGmail({"id-0": raw})
    assert gmail_source.fetch_raw(service, "id-0") == raw
    assert service.messages_resource.get_calls == ["id-0"]


def test_fetch_raw_repads_a_base64url_payload_with_the_padding_stripped() -> None:
    # 4 bytes encode to 6 base64 characters plus 2 '=' — decoding the stripped form without
    # re-padding raises binascii.Error, so this is the case that catches a missing re-pad.
    service = FakeGmail({"id-0": b"xxxx"})
    encoded = service.messages_resource.get(userId="me", id="id-0", format="raw").execute()["raw"]
    assert len(encoded) % 4 != 0, "the double must really strip the padding for this to prove it"
    assert gmail_source.fetch_raw(service, "id-0") == b"xxxx"


def test_fetch_raw_rejects_a_response_without_a_raw_payload() -> None:
    service = FakeGmail({"id-0": None})
    with pytest.raises(gmail_source.GmailFetchError) as exc_info:
        gmail_source.fetch_raw(service, "id-0")
    assert exc_info.value.remediation


def test_fetch_raw_rejects_an_undecodable_payload() -> None:
    service = FakeGmail({"id-0": b""})
    service.messages_resource.raws = {}
    service.messages_resource.get = lambda **kwargs: _FakeRequest({"raw": "!!! not base64 !!!"})  # type: ignore[method-assign]
    with pytest.raises(gmail_source.GmailFetchError) as exc_info:
        gmail_source.fetch_raw(service, "id-0")
    assert exc_info.value.remediation


# --------------------------------------------------------------------------------------
# The filename rule — the on-disk idempotency key.
# --------------------------------------------------------------------------------------


def test_eml_filename_strips_brackets_sanitises_and_appends_the_hash() -> None:
    # The suffix hashes the FULL RAW MESSAGE BYTES, never the Message-ID: every bounded
    # extraction of the id this connector shipped had its own truncation vector, and each
    # one silently overwrote a message. `_message_with_id_field` is the shared corpus
    # builder defined with the property test below.
    raw = _message_with_id_field(b"<abc.DEF-1@example.org>")
    digest = hashlib.sha256(raw).hexdigest()[:8]
    assert gmail_source.eml_filename(raw) == f"abc.DEF-1_example.org-{digest}.eml"


def test_eml_filename_is_a_bare_name_for_a_path_hostile_message_id(tmp_path: Path) -> None:
    """A Message-ID is attacker-influenceable text that becomes a filename."""
    hostile = "<../../etc/passwd\\..\\windows:stream  spåce" + "L" * 200 + "@example.org>"
    raw = b"body"
    name = gmail_source.eml_filename(raw, hostile)

    assert name == Path(name).name  # no directory component at all
    assert not any(bad in name for bad in ("/", "\\", ":", " ", "å"))
    assert name not in (".", "..")
    # The definitive traversal check: joining it cannot escape the inbox.
    assert (tmp_path / name).resolve().parent == tmp_path.resolve()
    # And the OS actually accepts it, at a bounded length.
    written = gmail_source.write_eml(raw, tmp_path, hostile)
    assert written.path.parent == tmp_path
    assert written.path.name == name  # write_eml and eml_filename agree on the same message
    assert written.path.read_bytes() == raw
    assert len(name) == 80 + 1 + 8 + len(".eml")


def test_eml_filename_truncates_the_stem_but_keeps_ids_distinct() -> None:
    long_a = _message_with_id_field(b"<" + b"a" * 200 + b"@example.org>")
    long_b = _message_with_id_field(b"<" + b"a" * 200 + b"@example.net>")
    name_a = gmail_source.eml_filename(long_a)
    name_b = gmail_source.eml_filename(long_b)
    assert name_a.split("-")[0] == name_b.split("-")[0]  # same 80-char truncated stem
    assert name_a != name_b  # ...and the hash suffix is what keeps them apart


def test_eml_filename_separates_case_only_variants() -> None:
    # On a case-insensitive filesystem (this project's platform) the stems collide; the
    # hash of the raw message bytes is what stops the two messages sharing one file.
    upper = gmail_source.eml_filename(_message_with_id_field(b"<ABC@example.org>"))
    lower = gmail_source.eml_filename(_message_with_id_field(b"<abc@example.org>"))
    assert upper.casefold() != lower.casefold()


def test_eml_filename_without_a_message_id_hashes_the_raw_bytes() -> None:
    raw = b"a message with no Message-ID header"
    expected = f"nomsgid-{hashlib.sha256(raw).hexdigest()[:16]}.eml"
    assert gmail_source.eml_filename(raw) == expected
    assert gmail_source.eml_filename(raw, "   ") == expected  # blank counts as absent
    assert gmail_source.eml_filename(b"different bytes") != expected


def test_eml_filename_is_stable_across_calls() -> None:
    raw = _raw_message()
    assert gmail_source.eml_filename(raw) == gmail_source.eml_filename(raw)
    assert gmail_source.eml_filename(b"", "<m@example.org>") == gmail_source.eml_filename(
        b"", "<m@example.org>"
    )


def test_message_id_of_reads_the_header_and_reports_its_absence() -> None:
    assert gmail_source.message_id_of(_raw_message("<m1@example.org>")) == "<m1@example.org>"
    assert gmail_source.message_id_of(_raw_message(None)) is None


# --------------------------------------------------------------------------------------
# write_eml — skip-if-identical.
# --------------------------------------------------------------------------------------


def test_write_eml_names_the_file_from_the_messages_own_header(tmp_path: Path) -> None:
    raw = _raw_message("<m1@example.org>")
    written = gmail_source.write_eml(raw, tmp_path)
    assert written.status == "new"
    assert written.path.name == gmail_source.eml_filename(raw)
    assert written.path.read_bytes() == raw


def test_write_eml_skips_a_byte_identical_file(tmp_path: Path) -> None:
    raw = _raw_message()
    first = gmail_source.write_eml(raw, tmp_path)
    os.utime(first.path, (1_000_000, 1_000_000))
    stamp = first.path.stat().st_mtime_ns

    second = gmail_source.write_eml(raw, tmp_path)
    assert second.status == "unchanged"
    assert second.path == first.path
    # mtime, not just equal bytes: proves the file was NOT rewritten with the same content.
    assert second.path.stat().st_mtime_ns == stamp


def test_write_eml_rewrites_when_the_bytes_differ(tmp_path: Path) -> None:
    raw = _raw_message()
    first = gmail_source.write_eml(raw, tmp_path)
    first.path.write_bytes(b"truncated by an interrupted run")

    again = gmail_source.write_eml(raw, tmp_path)
    assert again.status == "rewritten"
    assert again.path.read_bytes() == raw


def test_write_eml_creates_the_directory_and_leaves_no_temp_files(tmp_path: Path) -> None:
    out_dir = tmp_path / "mail_samples"
    gmail_source.write_eml(_raw_message(), out_dir)
    assert sorted(p.suffix for p in out_dir.iterdir()) == [".eml"]


def test_write_eml_reports_an_unwritable_destination_actionably(tmp_path: Path) -> None:
    blocker = tmp_path / "mail_samples"
    blocker.write_text("not a directory", encoding="utf-8")
    with pytest.raises(gmail_source.GmailFetchError) as exc_info:
        gmail_source.write_eml(_raw_message(), blocker)
    assert exc_info.value.remediation


# --------------------------------------------------------------------------------------
# The one-time consent flow (no browser is ever opened here).
# --------------------------------------------------------------------------------------


def _consented_credentials(granted: list[str] | str | None = None) -> Credentials:
    """Credentials shaped like what ``InstalledAppFlow`` returns after a real consent.

    ``granted_scopes`` mirrors ``google_auth_oauthlib.helpers.credentials_from_session``,
    which sets it from the token endpoint's response — the independent provenance the scope
    check requires (never ``scopes``, which is the pin).
    """
    creds = Credentials(
        token=_FAKE_ACCESS_TOKEN,
        refresh_token=_FAKE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id="fake-client-id.apps.googleusercontent.com",
        client_secret=_FAKE_CLIENT_SECRET,
        scopes=list(SCOPES),
        granted_scopes=[_READONLY] if granted is None else granted,
    )
    creds.expiry = _utcnow() + datetime.timedelta(hours=1)
    return creds


def test_consent_runs_only_when_the_token_file_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_client_secrets(tmp_path)  # client configured, consent never granted
    cfg = _load(tmp_path)
    assert cfg.gmail is not None
    assert gmail_source.needs_consent(cfg) is True

    flows = 0

    def _flow(gmail: Any) -> Credentials:
        nonlocal flows
        flows += 1
        return _consented_credentials()

    monkeypatch.setattr(gmail_source, "_run_consent_flow", _flow)
    creds = gmail_source.load_or_mint_credentials(cfg)

    assert creds.valid
    assert flows == 1
    # The grant the ENDPOINT reported is what landed on disk, so the next run's fresh-load
    # check reads a real observation rather than an echo of the pin.
    assert _saved(cfg.gmail.token_path)["scopes"] == [_READONLY]

    assert gmail_source.needs_consent(cfg) is False
    gmail_source.load_or_mint_credentials(cfg)
    assert flows == 1  # a second run loads the token; no second browser


def test_consent_refuses_an_over_scoped_grant_before_persisting_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_client_secrets(tmp_path)
    cfg = _load(tmp_path)
    assert cfg.gmail is not None
    monkeypatch.setattr(
        gmail_source,
        "_run_consent_flow",
        lambda gmail: _consented_credentials(granted=[_READONLY, _SEND]),
    )

    with pytest.raises(GmailAuthError) as exc_info:
        gmail_source.load_or_mint_credentials(cfg)
    assert _SEND in str(exc_info.value)
    assert not cfg.gmail.token_path.exists()  # nothing was written


def test_consent_accepts_a_space_delimited_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The OAuth specs describe `scope` as a space-delimited string; oauthlib usually hands
    # over a list. Both must work, and neither may be split into characters.
    _write_client_secrets(tmp_path)
    cfg = _load(tmp_path)
    assert cfg.gmail is not None
    monkeypatch.setattr(
        gmail_source, "_run_consent_flow", lambda gmail: _consented_credentials(granted=_READONLY)
    )
    gmail_source.load_or_mint_credentials(cfg)
    assert _saved(cfg.gmail.token_path)["scopes"] == [_READONLY]


def test_needs_consent_is_false_without_a_client_secrets_file(tmp_path: Path) -> None:
    cfg = _load(tmp_path)  # neither file exists: a browser cannot help
    assert gmail_source.needs_consent(cfg) is False
    with pytest.raises(GmailAuthError) as exc_info:
        gmail_source.load_or_mint_credentials(cfg)
    assert "Desktop app" in exc_info.value.remediation  # the better-targeted failure


def test_a_failed_consent_flow_becomes_an_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_client_secrets(tmp_path)
    cfg = _load(tmp_path)
    sentinel = RuntimeError("no browser on this machine")

    def _boom(gmail: Any) -> Credentials:
        raise sentinel

    monkeypatch.setattr(gmail_source, "_run_consent_flow", _boom)
    with pytest.raises(GmailAuthError) as exc_info:
        gmail_source.mint_credentials(cfg)
    assert exc_info.value.__cause__ is sentinel
    assert exc_info.value.remediation
    # The foreign exception's own text is not interpolated (it could carry token material).
    assert "no browser on this machine" not in str(exc_info.value)


def test_the_error_hierarchy_is_one_catchable_surface() -> None:
    assert issubclass(GmailAuthError, gmail_source.GmailError)
    assert issubclass(gmail_source.GmailFetchError, gmail_source.GmailError)
    with pytest.raises(TypeError):
        gmail_source.GmailFetchError("no remediation")  # type: ignore[call-arg]


def test_missing_gmail_section_is_an_error_from_every_entry_point(tmp_path: Path) -> None:
    cfg = _load(tmp_path, gmail=False)
    for call in (gmail_source.inbox_dir, gmail_source.needs_consent, gmail_source.mint_credentials):
        with pytest.raises(GmailAuthError) as exc_info:
            call(cfg)
        assert "[gmail]" in str(exc_info.value)


# --------------------------------------------------------------------------------------
# The `fetch-mail` command, end to end against the faked service.
# --------------------------------------------------------------------------------------


def test_fetch_mail_writes_eml_files_into_the_configured_inbox_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _config_path(tmp_path)
    raws = _mailbox(3)
    service = FakeGmail(raws, page_size=2)

    assert _fetch_mail(config_path, service, "--since", "2026-07-01", "--until", "2026-08-01") == 0

    # Design Decision 10: `[gmail] inbox_dir` ITSELF, never a subdirectory of it, because
    # `receipt_ingest.iter_source` globs non-recursively and the two sources must be mapped
    # in ONE `map-receipts` run.
    inbox = tmp_path / "mail_samples"
    assert sorted(p.name for p in inbox.iterdir()) == sorted(
        gmail_source.eml_filename(raws[f"id-{n}"] or b"") for n in range(3)
    )
    for n in range(3):
        name = gmail_source.eml_filename(raws[f"id-{n}"] or b"")
        assert (inbox / name).read_bytes() == raws[f"id-{n}"]

    assert service.messages_resource.list_calls[0]["q"] == "after:2026/07/01 before:2026/08/01"
    out = capsys.readouterr().out
    assert "after:2026/07/01 before:2026/08/01" in out
    assert "3 message(s) matched" in out
    assert "3 new, 0 unchanged, 0 rewritten" in out


def test_fetched_eml_files_are_readable_by_the_parser(tmp_path: Path) -> None:
    """The producer -> consumer round trip, end to end, with no import between the two.

    ``fetch-mail`` writes the files and ``receipt_ingest`` reads them, and neither module
    imports the other (Design Decision 4) — so nothing but a test can catch the two drifting
    apart. This exercises every piece of the contract at once: the non-recursive
    ``glob("*.eml")`` finds the files (so the ``.eml`` suffix and the flat destination are
    right), the bytes parse as RFC-822, and the ``Message-ID`` the parser reads back is the
    one the filename was derived from — which is the key ``receipt_map`` dedups on.
    """
    config_path = _config_path(tmp_path)
    service = FakeGmail(_mailbox(3), page_size=2)
    assert _fetch_mail(config_path, service, "--since", "2026-07-01") == 0

    inbox = tmp_path / "mail_samples"
    read_back = list(receipt_ingest.iter_source(inbox))
    assert len(read_back) == 3
    assert {str(msg.get("Message-ID", "")).strip() for _label, msg in read_back} == {
        f"<m{n}@example.org>" for n in range(3)
    }
    # And each file's name is the one the pinned rule derives from that same header.
    for label, _msg in read_back:
        assert label == gmail_source.eml_filename((inbox / label).read_bytes())


def test_fetch_mail_prints_no_subject_sender_or_body(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A privacy requirement of the connector, not a display preference.

    The window is date-scoped (no sender/subject filter, plan DD8), so unrelated personal
    mail lands on disk too — stdout must therefore carry counts and nothing else.
    """
    config_path = _config_path(tmp_path)
    assert _fetch_mail(config_path, FakeGmail(_mailbox(3)), "--since", "2026-07-01") == 0

    out = capsys.readouterr().out
    for leak in (_SAMPLE_SUBJECT, _SAMPLE_SENDER, _SAMPLE_BODY.strip(), "treasurer@example.org"):
        assert leak not in out
    for n in range(3):
        assert f"id-{n}" not in out  # not even the opaque Gmail message ids
        assert f"m{n}@example.org" not in out


def test_fetch_mail_second_run_reports_zero_new_and_rewrites_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _config_path(tmp_path)
    raws = _mailbox(3)
    assert _fetch_mail(config_path, FakeGmail(raws, page_size=2), "--since", "2026-07-01") == 0

    inbox = tmp_path / "mail_samples"
    for path in inbox.glob("*.eml"):
        os.utime(path, (1_000_000, 1_000_000))
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in inbox.glob("*.eml")}
    capsys.readouterr()

    # The same window again — the overlap the operating procedure deliberately creates.
    assert _fetch_mail(config_path, FakeGmail(raws, page_size=2), "--since", "2026-07-01") == 0
    assert "0 new, 3 unchanged, 0 rewritten" in capsys.readouterr().out
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in inbox.glob("*.eml")}
    assert after == before  # byte-identical AND untouched


def test_fetch_mail_dry_run_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _config_path(tmp_path)
    service = FakeGmail(_mailbox(3), page_size=2)

    assert _fetch_mail(config_path, service, "--since", "2026-07-01", "--dry-run") == 0

    assert not (tmp_path / "mail_samples").exists()
    assert service.messages_resource.get_calls == []  # not even downloaded
    out = capsys.readouterr().out
    assert "3 message(s) match" in out
    assert "no .eml files written" in out


def test_fetch_mail_limit_caps_what_is_fetched(tmp_path: Path) -> None:
    config_path = _config_path(tmp_path)
    service = FakeGmail(_mailbox(5), page_size=2)

    assert _fetch_mail(config_path, service, "--since", "2026-07-01", "--limit", "2") == 0

    assert len(list((tmp_path / "mail_samples").glob("*.eml"))) == 2
    assert service.messages_resource.get_calls == ["id-0", "id-1"]
    assert len(service.messages_resource.list_calls) == 1  # pagination stopped too


def test_fetch_mail_out_overrides_the_default_destination(tmp_path: Path) -> None:
    config_path = _config_path(tmp_path)
    dest = tmp_path / "elsewhere"

    service = FakeGmail(_mailbox(2))
    assert _fetch_mail(config_path, service, "--since", "2026-07-01", "--out", str(dest)) == 0

    assert len(list(dest.glob("*.eml"))) == 2
    assert not (tmp_path / "mail_samples").exists()


def test_fetch_mail_appends_the_operator_query(tmp_path: Path) -> None:
    config_path = _config_path(tmp_path)
    service = FakeGmail(_mailbox(1))

    argv = ("--since", "2026-07-01", "--query", "has:attachment")
    assert _fetch_mail(config_path, service, *argv) == 0

    assert service.messages_resource.list_calls[0]["q"] == "after:2026/07/01 has:attachment"


def test_fetch_mail_without_a_gmail_section_exits_1_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Through ``cli.main`` — no injected service, no credentials, no network."""
    config_path = _config_path(tmp_path, gmail=False)
    assert cli.main(["fetch-mail", "--config", str(config_path), "--since", "2026-07-01"]) == 1
    out = capsys.readouterr().out
    assert "[gmail]" in out
    assert "Fix:" in out
    assert "client_secrets_file" in out


def test_fetch_mail_rejects_a_backwards_window(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _config_path(tmp_path)
    assert (
        cli.main(
            [
                "fetch-mail",
                "--config",
                str(config_path),
                "--since",
                "2026-08-01",
                "--until",
                "2026-07-01",
            ]
        )
        == 1
    )
    assert "EXCLUSIVE" in capsys.readouterr().out


def test_fetch_mail_rejects_a_non_iso_date(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _config_path(tmp_path)
    assert cli.main(["fetch-mail", "--config", str(config_path), "--since", "07/01/2026"]) == 1
    assert "YYYY-MM-DD" in capsys.readouterr().out


# ======================================================================================
# Review-iteration-2 regressions: folded Message-IDs, API-error privacy, the CLI glue.
# ======================================================================================

# Two DISTINCT messages whose Message-IDs differ only PAST an RFC-5322 fold point. Read
# through the structured header parser these both truncate to the same '<foldedAAA', which
# would make `eml_filename` hash an already-collided value and quietly overwrite one
# message with the other. Kept as raw bytes because the fold is the whole point.
_FOLDED_ONE = (
    b"Subject: a\r\nMessage-ID: <foldedAAA\r\n one@example.org>\r\n"
    b"Date: Sun, 28 Jun 2026 09:09:00 -0700\r\n\r\nfirst message\r\n"
)
_FOLDED_TWO = (
    b"Subject: a\r\nMessage-ID: <foldedAAA\r\n two@example.org>\r\n"
    b"Date: Sun, 28 Jun 2026 09:09:00 -0700\r\n\r\nsecond message\r\n"
)
# The SAME Message-ID as _FOLDED_ONE, rendered without the fold.
_UNFOLDED_ONE = (
    b"Subject: a\r\nMessage-ID: <foldedAAA one@example.org>\r\n"
    b"Date: Sun, 28 Jun 2026 09:09:00 -0700\r\n\r\nfirst message\r\n"
)


def test_folded_message_ids_do_not_collide(tmp_path: Path) -> None:
    """Two distinct messages folded at the same column must never share a filename.

    Regression for a silent-data-loss bug: ``email.policy.default`` runs Message-ID through
    its STRUCTURED parser, which truncated both of these to ``'<foldedAAA'``. The pinned
    filename rule was fine — its INPUT was corrupted one layer upstream, so the hash suffix
    that normally guarantees collision-safety was hashing an already-collided value.
    ``write_eml`` then reported an ordinary ``rewritten`` and the first message was gone.
    """
    one = gmail_source.message_id_of(_FOLDED_ONE)
    two = gmail_source.message_id_of(_FOLDED_TWO)
    assert one == "<foldedAAA one@example.org>"
    assert two == "<foldedAAA two@example.org>"
    assert gmail_source.eml_filename(_FOLDED_ONE) != gmail_source.eml_filename(_FOLDED_TWO)

    # End to end: both survive on disk, and neither is reported as a rewrite of the other.
    first = gmail_source.write_eml(_FOLDED_ONE, tmp_path)
    second = gmail_source.write_eml(_FOLDED_TWO, tmp_path)
    assert first.status == "new"
    assert second.status == "new"
    assert first.path != second.path
    assert first.path.read_bytes() == _FOLDED_ONE
    assert second.path.read_bytes() == _FOLDED_TWO


def test_a_folded_and_unfolded_rendering_get_different_filenames(tmp_path: Path) -> None:
    """Different RENDERINGS of one logical id now get different files. Deliberate trade.

    This assertion is the inverse of what an earlier iteration asserted, and the change is
    intentional. Cross-rendering stability and injectivity cannot both hold once the hash is
    computed over the raw message bytes, and injectivity is the property the plan actually
    pins ("the
    hash suffix is what makes the rule collision-safe"). The plan's idempotency clauses
    (DD6, ``build_query``'s overlap rule, Step 11's gate) all quantify over RE-FETCHES,
    which Gmail's byte-stable ``format="raw"`` output makes byte-identical — that case is
    covered by ``test_the_same_raw_bytes_always_give_the_same_filename`` below and still
    holds.

    So the cost of this direction is ONE DUPLICATE FILE, which ``receipt_map``'s Message-ID
    + content-hash dedup absorbs (DD6 assigns it there explicitly). The cost of the other
    direction was a SILENTLY DESTROYED MESSAGE. The stem still shows they are the same id.
    """
    assert gmail_source.message_id_of(_FOLDED_ONE) == gmail_source.message_id_of(_UNFOLDED_ONE)
    assert gmail_source.eml_filename(_FOLDED_ONE) != gmail_source.eml_filename(_UNFOLDED_ONE)

    first = gmail_source.write_eml(_FOLDED_ONE, tmp_path)
    other = gmail_source.write_eml(_UNFOLDED_ONE, tmp_path)
    assert first.status == "new"
    assert other.status == "new"
    assert other.path != first.path
    # Both messages survive; neither overwrote the other. The shared, readable stem is what
    # shows a human they are the same logical id.
    assert first.path.read_bytes() == _FOLDED_ONE
    assert other.path.read_bytes() == _UNFOLDED_ONE
    assert first.path.name.split("-")[0] == other.path.name.split("-")[0]


def test_a_message_id_folded_after_the_colon_still_normalises(tmp_path: Path) -> None:
    # The most common real fold: the value starts on the continuation line.
    raw = b"Subject: a\r\nMessage-ID:\r\n <plain@example.org>\r\n\r\nbody\r\n"
    assert gmail_source.message_id_of(raw) == "<plain@example.org>"


# --- API errors must never carry a Gmail message id out of the module -------------------

# The shape `googleapiclient.errors.HttpError` has: its str()/repr() embed the full request
# URI, and the URI of a raw fetch contains the message id. Built here rather than imported
# so the test needs no googleapiclient at all.
_LEAKY_URI = "https://gmail.googleapis.com/gmail/v1/users/me/messages/id-0?format=raw&alt=json"


class _FakeHttpError(Exception):
    """Shaped like ``googleapiclient.errors.HttpError``: URI in the text, status on ``resp``."""

    def __init__(self, uri: str = _LEAKY_URI, status: int = 429) -> None:
        self.uri = uri
        self.resp = SimpleNamespace(status=status)
        super().__init__(f'<HttpError {status} when requesting {uri} returned "Rate Limit">')


def _assert_no_leak(text: str) -> None:
    """Nothing that could identify a message may appear in operator-facing text."""
    assert "id-0" not in text
    assert _LEAKY_URI not in text
    assert "messages/" not in text
    assert "Rate Limit" not in text  # the foreign exception's own text, not ours


def test_an_api_error_at_execute_time_never_leaks_the_message_id() -> None:
    service = FakeGmail({"id-0": _raw_message()})
    service.messages_resource.get_execute_error = _FakeHttpError()

    with pytest.raises(gmail_source.GmailFetchError) as exc_info:
        gmail_source.fetch_raw(service, "id-0")

    _assert_no_leak(str(exc_info.value))
    assert "429" in str(exc_info.value)  # the bare status IS carried: it names the fix
    assert exc_info.value.remediation


def test_an_api_error_at_request_build_time_is_also_translated() -> None:
    # googleapiclient validates parameters when the request object is CONSTRUCTED, so the
    # boundary has to cover the build as well as the execute.
    service = FakeGmail({"id-0": _raw_message()})
    service.messages_resource.get_build_error = _FakeHttpError(status=500)

    with pytest.raises(gmail_source.GmailFetchError) as exc_info:
        gmail_source.fetch_raw(service, "id-0")

    _assert_no_leak(str(exc_info.value))
    assert "500" in str(exc_info.value)


def test_an_api_error_while_listing_is_translated() -> None:
    service = FakeGmail(_mailbox(3))
    service.messages_resource.list_execute_error = _FakeHttpError(status=503)

    with pytest.raises(gmail_source.GmailFetchError) as exc_info:
        list(gmail_source.list_message_ids(service, "q"))

    _assert_no_leak(str(exc_info.value))
    assert exc_info.value.remediation


def test_an_api_error_without_a_status_is_still_translated() -> None:
    service = FakeGmail({"id-0": _raw_message()})
    service.messages_resource.get_execute_error = RuntimeError("connection reset")

    with pytest.raises(gmail_source.GmailFetchError) as exc_info:
        gmail_source.fetch_raw(service, "id-0")
    assert "RuntimeError" in str(exc_info.value)
    assert "connection reset" not in str(exc_info.value)


def test_fetch_mail_reports_an_api_failure_without_leaking_the_message_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The end-to-end privacy claim, on the most likely real-world failure path.

    A rate limit or a transient 5xx mid-run must not put a Gmail message id on the operator's
    terminal — uncaught, ``HttpError``'s repr would, via the default excepthook.
    """
    config_path = _config_path(tmp_path)
    service = FakeGmail({"id-0": _raw_message("<m0@example.org>")})
    service.messages_resource.get_execute_error = _FakeHttpError()

    assert _fetch_mail(config_path, service, "--since", "2026-07-01") == 1

    out = capsys.readouterr().out
    _assert_no_leak(out)
    assert "m0@example.org" not in out
    assert "Fix:" in out  # still actionable


# --- The production glue: `service is None`, i.e. the branch the suite never took --------


def test_fetch_mail_builds_its_own_service_and_may_mint_on_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Drives ``cli.main`` with NO injected service, so the real wiring line runs.

    Every other CLI test injects ``service=``, which skips ``needs_consent`` ->
    ``load_or_mint_credentials`` -> ``build_service`` entirely — a wrong call order or a
    swallowed exception there would pass the whole suite. Only the two browser/network
    seams are substituted.

    It also pins the ``--dry-run`` contract that plan Step M4 depends on: no ``.eml`` output,
    but the token file IS minted, because Step M4 tells the operator to run exactly this
    command to trigger first consent.
    """
    config_path = _config_path(tmp_path)
    _write_client_secrets(tmp_path)  # client configured, consent never granted

    consents: list[Path] = []
    built: list[Credentials] = []
    service = FakeGmail(_mailbox(2))

    def _flow(gmail: Any) -> Credentials:
        consents.append(gmail.client_secrets_path)
        return _consented_credentials()

    def _build(creds: Credentials) -> Any:
        built.append(creds)
        return service

    monkeypatch.setattr(gmail_source, "_run_consent_flow", _flow)
    monkeypatch.setattr(gmail_source, "build_service", _build)

    argv = ["fetch-mail", "--config", str(config_path), "--since", "2026-07-01", "--dry-run"]
    assert cli.main(argv) == 0

    # The glue ran, in order, with the right values threaded through it.
    assert len(consents) == 1
    assert consents[0].is_file()
    assert len(built) == 1
    assert built[0].valid, "the minted credentials are what reached build_service"
    assert service.messages_resource.list_calls[0]["q"] == "after:2026/07/01"

    # --dry-run writes no .eml OUTPUT and downloads nothing...
    assert not (tmp_path / "mail_samples").exists()
    assert service.messages_resource.get_calls == []
    # ...but DOES leave the token behind: that is what plan Step M4 relies on.
    assert (tmp_path / "secrets" / "gmail-token.json").is_file()

    out = capsys.readouterr().out
    assert "one-time" in out  # the browser was announced before it appeared
    assert "2 message(s) match" in out

    # A second run takes the load path: no second browser.
    assert cli.main(argv) == 0
    assert len(consents) == 1
    assert len(built) == 2


def test_fetch_mail_surfaces_a_service_build_failure_as_exit_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other half of the glue line: a build failure exits 1 instead of raising.

    ``build_service`` raises the ``GmailFetchError`` its real implementation produces —
    substituting it with a RAW library exception would monkeypatch away the very
    translation under test, and the test would then be asserting the mock's behaviour
    instead of the module's. The real translation is covered separately by
    ``test_build_service_translates_a_library_failure``, which leaves ``build_service``
    itself in the path.
    """
    config_path = _config_path(tmp_path)
    _write_client_secrets(tmp_path)
    monkeypatch.setattr(gmail_source, "_run_consent_flow", lambda gmail: _consented_credentials())

    def _boom(creds: Credentials) -> Any:
        raise gmail_source.GmailFetchError(
            "the Gmail API client could not be built.", "check the network and re-run."
        )

    monkeypatch.setattr(gmail_source, "build_service", _boom)
    argv = ["fetch-mail", "--config", str(config_path), "--since", "2026-07-01", "--dry-run"]
    assert cli.main(argv) == 1
    out = capsys.readouterr().out
    _assert_no_leak(out)
    assert "Fix:" in out


def test_build_service_translates_a_library_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # `build_service` is the real function here — only the library call under it is broken,
    # via the discovery module the function imports lazily.
    import googleapiclient.discovery

    monkeypatch.setattr(
        googleapiclient.discovery,
        "build",
        lambda *args, **kwargs: (_ for _ in ()).throw(_FakeHttpError(status=403)),
    )
    with pytest.raises(gmail_source.GmailFetchError) as exc_info:
        gmail_source.build_service(_consented_credentials())
    _assert_no_leak(str(exc_info.value))
    assert exc_info.value.remediation


# --- The consent flow must fail fast rather than hang -----------------------------------


def test_consent_fails_fast_when_no_browser_is_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_local_server`` would otherwise wait forever for a redirect that cannot arrive.

    This exercises the REAL ``_run_consent_flow`` (not the seam) up to its guard, so no
    browser opens and no local server starts.
    """
    _write_client_secrets(tmp_path)
    cfg = _load(tmp_path)
    assert cfg.gmail is not None

    def _no_browser(*args: Any, **kwargs: Any) -> Any:
        raise webbrowser.Error("could not locate runnable browser")

    monkeypatch.setattr(webbrowser, "get", _no_browser)

    with pytest.raises(GmailAuthError) as exc_info:
        gmail_source.mint_credentials(cfg)

    assert "browser" in str(exc_info.value)
    assert exc_info.value.remediation
    assert not cfg.gmail.token_path.exists()  # nothing was written


def test_the_consent_wait_is_bounded() -> None:
    # A bound is what keeps "the operator walked away" from being an unkillable hang.
    assert isinstance(gmail_source._CONSENT_TIMEOUT_SECONDS, int)
    assert 0 < gmail_source._CONSENT_TIMEOUT_SECONDS <= 900
    source = Path(gmail_source.__file__).read_text(encoding="utf-8")
    assert "timeout_seconds=_CONSENT_TIMEOUT_SECONDS" in source


# ======================================================================================
# The key invariant, as a PROPERTY.
#
# Every collision this connector has shipped was one instance of a single root cause: the
# hash was computed over a BOUNDED DERIVATION of the Message-ID, so `sha256` faithfully
# preserved an input that had already collided. FIVE vectors reached production one at a
# time — and the fifth appeared in the extraction written to fix the first four, which is
# the evidence that the class cannot be closed by enumerating vectors.
#
# It is closed structurally instead: the hash is `sha256` of the FULL RAW MESSAGE BYTES,
# with no extraction to get wrong (`test_exactly_one_function_derives_the_hash_source`
# pins that there is exactly one derivation site and that it takes `raw` whole). The table
# below is therefore no longer the defence — it is the ADVERSARIAL CORPUS that demonstrates
# the property, and it deliberately carries every historical vector plus the framing bytes
# that broke each bounded extraction.
# ======================================================================================

#: Byte-level-DISTINCT Message-ID field values. Pairs that share a comment are the ones a
#: given parsing step used to flatten together; all identities are fake placeholders.
_DISTINCT_MESSAGE_ID_FIELDS: dict[str, bytes] = {
    # RFC-5322 fold: the structured parser truncated both at the fold point.
    "fold-one": b"<foldedAAA\r\n one@example.org>",
    "fold-two": b"<foldedAAA\r\n two@example.org>",
    # A bare CR / bare LF is NOT a legal fold, so unfolding never saw it; the parser took
    # each as a header-line boundary and both became '<a'.
    "bare-cr": b"<a\rb@example.org>",
    "bare-lf": b"<a\nb@example.org>",
    # A quoted-string local part may legitimately carry a run of spaces; collapsing every
    # whitespace run flattened these two together.
    "quoted-one-space": b'<"a b"@example.org>',
    "quoted-two-spaces": b'<"a  b"@example.org>',
    # Two different invalid header bytes both decoded to U+FFFD.
    "raw-byte-e9": b"<caf\xe9@example.org>",
    "raw-byte-ff": b"<caf\xff@example.org>",
    # Case-only variants: named by the plan itself, for case-insensitive filesystems.
    "case-upper": b"<ABC@example.org>",
    "case-lower": b"<abc@example.org>",
    # A doubled bare CR reads as a blank line, so a header-block scan ended the "headers"
    # INSIDE the value and the rest never reached the hash.
    "cr-cr-one": b"<x\r\rAAA@example.org>",
    "cr-cr-two": b"<x\r\rBBB@example.org>",
    # The same, for a doubled bare LF.
    "lf-lf-one": b"<y\n\nAAA@example.org>",
    "lf-lf-two": b"<y\n\nBBB@example.org>",
    # ...and for a genuine CRLFCRLF, the canonical end-of-headers marker.
    "crlf-crlf-one": b"<w\r\n\r\nA@example.org>",
    "crlf-crlf-two": b"<w\r\n\r\nB@example.org>",
    # A continuation that LOOKS like the start of a new header field: a field-name
    # heuristic stopped the extraction here, mid-value.
    "fieldish-one": b"<z\rTo: A@example.org>",
    "fieldish-two": b"<z\rTo: B@example.org>",
    # Over-long values that share their first 80 characters, so only the hash can separate
    # them once the stem is truncated.
    "over-long-org": b"<" + b"a" * 200 + b"@example.org>",
    "over-long-net": b"<" + b"a" * 200 + b"@example.net>",
    # Ordinary ids, as a control.
    "plain": b"<sample-1@example.org>",
    "plain-other": b"<sample-2@example.org>",
}


def _message_with_id_field(field_value: bytes) -> bytes:
    """A raw message carrying ``field_value`` verbatim as its Message-ID field."""
    return (
        b"Subject: Example subject\r\n"
        b"From: forms@example.com\r\n"
        b"Message-ID: " + field_value + b"\r\n"
        b"Date: Sun, 28 Jun 2026 09:09:00 -0700\r\n"
        b"\r\n"
        b"body\r\n"
    )


def test_byte_distinct_message_ids_never_share_a_filename() -> None:
    """Injectivity: byte-distinct messages -> distinct filenames. Pairwise, over a corpus.

    The corpus varies only the Message-ID field, because that is where every historical
    vector lived; the guarantee itself is broader (the hash is over the whole message).

    Asserted over the whole table rather than as a handful of named cases, because the
    property is what matters — every past collision was a different route to violating this
    one invariant, and the fix (hash the FULL RAW MESSAGE BYTES, never any extraction of
    the Message-ID) is what makes the property hold for routes nobody has thought of yet.
    """
    names: dict[str, list[str]] = {}
    for label, field_value in _DISTINCT_MESSAGE_ID_FIELDS.items():
        name = gmail_source.eml_filename(_message_with_id_field(field_value))
        names.setdefault(name, []).append(label)

    collisions = {name: labels for name, labels in names.items() if len(labels) > 1}
    assert not collisions, f"distinct Message-IDs sharing a filename: {collisions}"
    assert len(names) == len(_DISTINCT_MESSAGE_ID_FIELDS)


def test_every_table_entry_is_really_byte_distinct() -> None:
    # Guards the test above from going vacuously green if two rows are ever made identical.
    values = list(_DISTINCT_MESSAGE_ID_FIELDS.values())
    assert len(set(values)) == len(values)


def test_exactly_one_function_derives_the_hash_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """ONE derivation site for the hash, and it takes the raw bytes WHOLE.

    ``dev/.claude/rules/code-quality.md`` — one source of truth for a shared key shape,
    asserted by IDENTITY rather than equality. Five collisions shipped because the hash
    input was derived somewhere other than at the hashing site and arrived
    already-collided; a second derivation site is the exact shape the sixth would take. So
    this asserts the structure instead of listing vectors:

    * exactly one ``hashlib.sha256(`` call exists in the module, and its argument is ``raw``
      — no extraction, no slice, no parse, nothing that can be cut short;
    * BOTH filename branches route through that ONE function object, proved by replacing
      the single attribute and seeing both outputs change — impossible if either branch
      held a private copy;
    * what reaches it is the FULL raw bytes of each message, unmodified.
    """
    source = Path(gmail_source.__file__).read_text(encoding="utf-8")
    assert source.count("hashlib.sha256(") == 1, "a second hash derivation site appeared"
    assert "return hashlib.sha256(raw).hexdigest()" in source

    seen: list[bytes] = []

    def _spy(raw: bytes) -> str:
        seen.append(raw)
        return "f" * 64

    monkeypatch.setattr(gmail_source, "_message_digest", _spy)
    assert gmail_source._message_digest is _spy  # identity, not equality

    with_id = _message_with_id_field(b"<a@example.org>")
    without_id = b"Subject: a\r\nFrom: forms@example.com\r\n\r\nbody\r\n"
    assert gmail_source.eml_filename(with_id) == "a_example.org-ffffffff.eml"
    assert gmail_source.eml_filename(without_id) == "nomsgid-ffffffffffffffff.eml"
    assert seen == [with_id, without_id]


def test_write_eml_yields_one_file_per_byte_distinct_message(tmp_path: Path) -> None:
    """N byte-distinct messages -> N files on disk and N ``new`` statuses.

    The property that actually protects the data, asserted at the production boundary. A
    filename comparison can be green while the writer still loses a message; what every one
    of the five collision vectors did was overwrite a file and report an innocuous
    ``rewritten``, so the count of surviving files is the assertion that would have caught
    all five.
    """
    shared_header = b"Subject: a\r\nMessage-ID: <shared@example.org>\r\n\r\n"
    corpus: list[bytes] = [
        *(_message_with_id_field(field) for field in _DISTINCT_MESSAGE_ID_FIELDS.values()),
        # Same header block, different bodies — still two distinct messages.
        shared_header + b"first body\r\n",
        shared_header + b"second body\r\n",
        # No Message-ID header at all: the `nomsgid-` branch must be injective too.
        b"Subject: a\r\n\r\nno id, first\r\n",
        b"Subject: a\r\n\r\nno id, second\r\n",
    ]
    assert len(set(corpus)) == len(corpus), "the corpus must really be byte-distinct"

    written = [gmail_source.write_eml(raw, tmp_path) for raw in corpus]

    assert [entry.status for entry in written] == ["new"] * len(corpus)
    assert len({entry.path for entry in written}) == len(corpus)
    assert len(list(tmp_path.glob("*.eml"))) == len(corpus)
    for entry, raw in zip(written, corpus, strict=True):
        assert entry.path.read_bytes() == raw


def test_the_same_raw_bytes_always_give_the_same_filename(tmp_path: Path) -> None:
    """Idempotency, the direction the plan actually quantifies over.

    Gmail's ``format="raw"`` output is byte-stable per message, so an overlapping re-fetch
    hands `write_eml` the identical bytes it saw last time — which must be a no-op, or the
    "overlap windows, never tile them" operating rule would cost a rewrite per message.
    """
    for field_value in _DISTINCT_MESSAGE_ID_FIELDS.values():
        raw = _message_with_id_field(field_value)
        assert gmail_source.eml_filename(raw) == gmail_source.eml_filename(raw)

    raw = _message_with_id_field(_DISTINCT_MESSAGE_ID_FIELDS["plain"])
    assert gmail_source.write_eml(raw, tmp_path).status == "new"
    assert gmail_source.write_eml(raw, tmp_path).status == "unchanged"
    assert len(list(tmp_path.glob("*.eml"))) == 1


def test_the_lossy_stem_is_not_what_gets_hashed() -> None:
    """The root-cause invariant, stated directly.

    ``message_id_of`` is lossy and always will be. What makes that harmless is that it
    feeds the STEM only. Here two messages whose parsed strings are EQUAL still get
    different filenames, which can only be true if the hash saw something else.
    """
    one = _message_with_id_field(b"<caf\xe9@example.org>")
    two = _message_with_id_field(b"<caf\xff@example.org>")
    assert gmail_source.message_id_of(one) == gmail_source.message_id_of(two)
    assert gmail_source.eml_filename(one) != gmail_source.eml_filename(two)


def test_the_first_message_id_field_is_the_one_used() -> None:
    # A duplicated field is malformed; taking the first matches what `email` does, so the
    # stem and the hash always describe the same field.
    raw = (
        b"Subject: a\r\n"
        b"Message-ID: <first@example.org>\r\n"
        b"Message-ID: <second@example.org>\r\n"
        b"\r\nbody\r\n"
    )
    assert gmail_source.message_id_of(raw) == "<first@example.org>"
    assert gmail_source.eml_filename(raw).startswith("first_example.org-")


def test_a_body_that_looks_like_a_header_cannot_hijack_the_key(tmp_path: Path) -> None:
    """A forged body line cannot make one message masquerade as another.

    **The equality this test used to assert is deliberately inverted.** Its earlier form
    asserted that two messages sharing a header block but differing in their BODIES got the
    SAME filename, because the hash source was bounded to the header block. That bound was
    the fifth collision vector this connector shipped: every bounded extraction of the
    Message-ID has a truncation vector, so the amended rule hashes the full raw bytes, and
    body-distinct messages now get distinct filenames.

    That is the pinned direction. The plan pins injectivity — "the hash suffix is what makes
    the rule collision-safe" — and never pinned body-independence. The cost of the flip is
    ONE DUPLICATE FILE if a message is ever re-rendered, which ``receipt_map``'s Message-ID
    + content-hash dedup absorbs (DD6 assigns it there); the cost of the old direction was a
    SILENTLY DESTROYED MESSAGE.

    What genuinely needed guarding is unchanged and asserted below: the STEM is still read
    from the header block only, so a body line spelled ``Message-ID: <evil@example.org>``
    cannot put itself in the name, and — because the suffix now depends on the whole message
    — it cannot steer the file onto another message's path either.
    """
    header = b"Subject: a\r\nMessage-ID: <x@example.org>\r\n\r\n"
    forged = header + b"Message-ID: <evil@example.org>\r\n"
    ordinary = header + b"totally different body\r\n"

    # The stem comes from the real header field, never from the body's forgery.
    assert gmail_source.eml_filename(forged).startswith("x_example.org-")
    assert gmail_source.eml_filename(ordinary).startswith("x_example.org-")
    assert "evil" not in gmail_source.eml_filename(forged)

    # And the two byte-distinct messages never share a file, so neither can overwrite the
    # other — the property that actually protects the data.
    assert gmail_source.eml_filename(forged) != gmail_source.eml_filename(ordinary)
    first = gmail_source.write_eml(forged, tmp_path)
    second = gmail_source.write_eml(ordinary, tmp_path)
    assert first.status == "new"
    assert second.status == "new"
    assert first.path != second.path
    assert first.path.read_bytes() == forged
    assert second.path.read_bytes() == ordinary


# --- Block B: the pagination loop must always make forward progress ---------------------


def test_list_message_ids_refuses_to_spin_on_a_repeated_page_token() -> None:
    """A service echoing the same ``nextPageToken`` forever used to hang the command.

    Every page yielded zero new ids (all already in ``seen``), ``page_token`` never changed,
    and nothing returned or raised — an unkillable loop, not a slow one. The guard is LOUD
    rather than a silent ``return`` because a truncated window that looks successful leaves
    a permanent, invisible gap in the archive.
    """
    service = FakeGmail({})
    # Page 1 hands back the very token that fetched it, so the walk cannot advance. (The
    # token stays a page index because that is what this double resolves pages by.)
    service.messages_resource.pages = [
        {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "1"},
        {"messages": [{"id": "a"}, {"id": "b"}], "nextPageToken": "1"},
    ]

    with pytest.raises(gmail_source.GmailFetchError) as exc_info:
        list(gmail_source.list_message_ids(service, "q"))

    assert "same page token" in str(exc_info.value)
    assert "NOT fully fetched" in str(exc_info.value)
    assert exc_info.value.remediation
    # It stopped rather than looping: two calls, not hundreds.
    assert len(service.messages_resource.list_calls) == 2


def test_a_genuinely_advancing_token_is_not_mistaken_for_a_spin() -> None:
    # The guard must not fire on ordinary pagination, which is the common case.
    service = FakeGmail(_mailbox(5), page_size=2)
    assert list(gmail_source.list_message_ids(service, "q")) == [f"id-{n}" for n in range(5)]


def test_fetch_mail_surfaces_a_pagination_stall_as_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _config_path(tmp_path)
    service = FakeGmail({})
    service.messages_resource.pages = [
        {"messages": [{"id": "a"}], "nextPageToken": "1"},
        {"messages": [{"id": "a"}], "nextPageToken": "1"},
    ]
    assert _fetch_mail(config_path, service, "--since", "2026-07-01") == 1
    assert "Fix:" in capsys.readouterr().out


# --- Nit 2: mint_credentials translates a persist failure too ---------------------------


def test_a_token_persist_failure_during_consent_is_translated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `_save_token` translates its own OSError, but a TypeError out of `to_json`/`json`
    # used to escape `mint_credentials` untranslated.
    _write_client_secrets(tmp_path)
    cfg = _load(tmp_path)
    monkeypatch.setattr(gmail_source, "_run_consent_flow", lambda gmail: _consented_credentials())

    def _boom(creds: Any, token_path: Path, verified_scopes: Any) -> None:
        raise TypeError("to_json produced something unexpected")

    monkeypatch.setattr(gmail_source, "_save_token", _boom)
    with pytest.raises(GmailAuthError) as exc_info:
        gmail_source.mint_credentials(cfg)
    assert exc_info.value.remediation
    assert "TypeError" in str(exc_info.value)


def test_an_api_error_does_not_chain_the_leaky_cause() -> None:
    # Nit 1: `from None`, so even an escaped GmailFetchError cannot print HttpError's repr
    # (which embeds the request URI, which embeds the message id) via the excepthook.
    service = FakeGmail({"id-0": _raw_message()})
    service.messages_resource.get_execute_error = _FakeHttpError()
    with pytest.raises(gmail_source.GmailFetchError) as exc_info:
        gmail_source.fetch_raw(service, "id-0")
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is not None  # still visible while handling
    assert "429" in str(exc_info.value)  # debuggability preserved in OUR text
