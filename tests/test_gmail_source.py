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

import datetime
import json
import re
from pathlib import Path
from typing import Any

import pytest
from google.auth.exceptions import RefreshError, TransportError
from google.oauth2 import reauth

from pta_finance import gmail_source, receipt_ingest
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


def _load(tmp_path: Path, *, gmail: bool = True) -> Config:
    """Write a config to ``tmp_path`` and load it through the production loader."""
    text = _BASE_CONFIG + (_GMAIL_SECTION if gmail else "")
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


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
