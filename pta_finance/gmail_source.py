"""Gmail read-only source — OAuth credential load/refresh behind a pinned scope.

This module is the toolkit's ONLY Gmail surface: nothing else imports ``google.oauth2``,
``google_auth_oauthlib``, or ``googleapiclient``, and this module imports NEITHER
:mod:`pta_finance.receipt_ingest` nor any other parsing code. The fetcher writes ``.eml``
files, the parser reads them, and the two never touch — so the parser stays
credential-free and unit-testable, and a Gmail-side change can never break parsing.

**The scope is pinned.** :data:`SCOPES` is a module-level ``Final`` tuple with exactly
one entry — ``gmail.readonly``. The toolkit never sends, replies, labels, archives, or
deletes mail; correspondence stays a human action. Widening the tuple fails
``tests/test_gmail_source.py`` (which asserts EXACT equality against the literal) rather
than silently widening what a freshly minted token may do. The compile-time pin governs
what is REQUESTED; the runtime checks below govern what is HONOURED.

**Invariant governing every runtime scope check in this module — read before adding one:**

    A scope check is real only if the checked value's PROVENANCE is independent of the
    pin. A value derived from :data:`SCOPES` — however many hops away — makes the check
    ``SCOPES == SCOPES``, which can never fail.

That invariant is not obvious, because google-auth launders provenance in two places:
``from_authorized_user_info`` ignores the token file's own ``scopes`` whenever the caller
passes ``scopes=`` explicitly, as this module always does
(``google/oauth2/credentials.py:505-508``), and ``to_json`` then serialises
``self.scopes`` — the pin — never the granted scopes (``credentials.py:564``). Two
earlier revisions of this module shipped checks that looked independent and were not
(``creds.scopes``, one hop; ``json.loads(creds.to_json())["scopes"]``, two hops through a
serialise/parse wash). Hence the table — every check site, and where its value comes from:

===========================  =====================================================
Check site                   Provenance (independent of ``SCOPES``)
===========================  =====================================================
fresh load                   the raw token-file JSON, read before any ``Credentials``
                             object exists (:func:`_recorded_scopes` on ``info``)
after a refresh              ``creds.granted_scopes`` — assigned ONLY from the token
                             endpoint's ``grant_response`` (``credentials.py:452-454``)
before persisting            the ``verified_scopes`` PARAMETER threaded in from one of
                             the two above — never re-derived from the credentials
===========================  =====================================================

The persisted record is then built FROM ``verified_scopes`` rather than from ``to_json``'s
pin echo, so what lands on disk is the grant that was actually verified, by construction.

*Design-decision note.* The feature plan's DD2 requires exactly one runtime re-check —
"``load_credentials()`` additionally re-checks the granted scopes at runtime and refuses
to proceed if the stored token carries anything beyond ``SCOPES``" — and that is the
fresh-load site, which reads the disk record directly. The other two sites are
defence-in-depth beyond DD2, and the pre-persist one is a PRECONDITION on its caller (it
cannot fire for the callers in this module, which pass an already-verified grant; it
exists so a future caller handed a differently-built credentials object cannot write an
unverified grant to disk). DD2 is satisfied by the first site alone.

**Secrets posture.** The OAuth client-secrets file and the minted token file are both
gitignored, and this module never prints, logs, or embeds their CONTENTS — not in an
error message, not on success. :class:`GmailAuthError` carries PATHS and REMEDIATION
only, so an operator can paste a failure anywhere without leaking a refresh token.

Scope of this module today: credential loading only. The message query/list/fetch helpers
and the ``fetch-mail`` CLI — including the first-consent browser flow that MINTS the token
file read here — land alongside it in a later step. Nothing here opens a browser, and the
only file it ever writes is the refreshed token, back to the path it was read from.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from pta_finance.config import Config, Gmail

__all__ = [
    "SCOPES",
    "GmailAuthError",
    "load_credentials",
]

#: The single source of truth for this toolkit's OAuth scope. READ-ONLY, permanently —
#: one entry, asserted by exact equality in the test suite. Adding a `gmail.send` or
#: `gmail.modify` entry here is a security change and must fail CI, not pass quietly.
SCOPES: Final[tuple[str, ...]] = ("https://www.googleapis.com/auth/gmail.readonly",)

#: The command an operator runs to (re-)mint the token file. Named in remediation text so
#: every failure ends with something to actually type.
_CONSENT_CMD = "pta-finance fetch-mail --since <YYYY-MM-DD> --dry-run"

#: Where a Google account's third-party grants are revoked, for the over-scoped case.
_REVOKE_URL = "https://myaccount.google.com/permissions"


class GmailAuthError(Exception):
    """Gmail credentials are missing, malformed, over-scoped, or dead.

    Every instance carries a ``remediation``: the exact next action the operator should
    take — which config key to add, which file to place where, which command to re-run to
    re-consent. It is a REQUIRED constructor argument precisely so no raise site can ship
    a dead-end message. ``str(exc)`` renders as ``"<problem> Fix: <remediation>"``.

    Messages carry PATHS only. The contents of the client-secrets file and of the token
    file (access token, refresh token, client secret) are NEVER interpolated into a
    message — see this module's secrets posture.
    """

    def __init__(self, problem: str, remediation: str) -> None:
        self.problem = problem
        self.remediation = remediation
        super().__init__(f"{problem} Fix: {remediation}")


def _scope_list() -> str:
    """Render :data:`SCOPES` for an error message (scope URLs are not secret)."""
    return ", ".join(SCOPES)


def _require_gmail_section(cfg: Config) -> Gmail:
    """Return ``cfg.gmail``, or raise if the OPTIONAL ``[gmail]`` block is absent."""
    if cfg.gmail is None:
        raise GmailAuthError(
            "config.toml has no [gmail] section, so there is nowhere to look for the "
            "OAuth client, the token, or the inbox directory.",
            "add a [gmail] section with client_secrets_file, token_file and inbox_dir — "
            "the commented-out block in config.example.toml is the template.",
        )
    return cfg.gmail


def _read_token_info(token_path: Path, client_secrets_path: Path) -> dict[str, Any]:
    """Read + parse the stored token file. Its contents never leave this process."""
    if not token_path.is_file():
        if not client_secrets_path.is_file():
            raise GmailAuthError(
                f"no Gmail OAuth client-secrets file at {client_secrets_path}, and no "
                f"token at {token_path} — this machine has never been set up for mail.",
                "in your Google Cloud project, enable the Gmail API and create an OAuth "
                f"'Desktop app' client, download its JSON to {client_secrets_path}, then "
                f"run `{_CONSENT_CMD}` once to consent.",
            )
        raise GmailAuthError(
            f"no Gmail token file at {token_path} — consent has not been granted on this "
            "machine yet (or the token file was deleted).",
            f"run `{_CONSENT_CMD}` once and approve the read-only request in the browser; "
            "that mints the token file.",
        )

    try:
        raw_bytes = token_path.read_bytes()
    except OSError as exc:
        raise GmailAuthError(
            f"the Gmail token file at {token_path} exists but could not be read.",
            "check that file's permissions, or delete it and run "
            f"`{_CONSENT_CMD}` to mint a fresh one.",
        ) from exc

    # Decoded EXPLICITLY rather than via read_text: UnicodeDecodeError is a ValueError,
    # NOT an OSError, so a truncated/corrupt token would otherwise escape as a raw
    # traceback and break this module's "every failure is a GmailAuthError" contract.
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        # Deliberately does NOT quote the offending bytes — those bytes are the token.
        raise GmailAuthError(
            f"the Gmail token file at {token_path} is not valid UTF-8; it is corrupt or "
            "was truncated by an interrupted write.",
            f"delete that file and run `{_CONSENT_CMD}` to mint a fresh one.",
        ) from exc

    try:
        info = json.loads(text)
    except json.JSONDecodeError as exc:
        # Deliberately does NOT quote the offending text — that text is the token.
        raise GmailAuthError(
            f"the Gmail token file at {token_path} is not valid JSON.",
            f"delete that file and run `{_CONSENT_CMD}` to mint a fresh one.",
        ) from exc

    if not isinstance(info, dict):
        raise GmailAuthError(
            f"the Gmail token file at {token_path} does not hold a JSON object.",
            f"delete that file and run `{_CONSENT_CMD}` to mint a fresh one.",
        )
    return info


def _recorded_scopes(info: Mapping[str, Any]) -> Sequence[str] | None:
    """The scopes a token's JSON records as GRANTED, or ``None`` if it records none.

    This reads the RAW parsed token JSON on purpose. It must never be replaced by
    ``Credentials.scopes``: :func:`_credentials_from_info` passes the pinned tuple to
    ``from_authorized_user_info``, and that constructor only falls back to
    ``info["scopes"]`` when the caller passes ``scopes=None``
    (``google/oauth2/credentials.py:505-508``, google-auth 2.55.0). So ``creds.scopes``
    is ALWAYS the pin and can never disagree with it — checking it would be a tautology.

    Google writes a list; a space-delimited string is also accepted on the way in. Any
    other entry type is stringified rather than dropped, so a malformed entry surfaces as
    an unexpected scope (rejected) instead of vanishing (silently accepted).
    """
    raw = info.get("scopes")
    if isinstance(raw, str):
        return raw.split()
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return None


def _check_granted_scopes(
    granted: Sequence[str] | None, subject: str, token_path: Path
) -> Sequence[str]:
    """Refuse any grant that is not EXACTLY :data:`SCOPES` — wider OR narrower.

    ``subject`` is the noun phrase naming what is being checked (the stored token, the
    grant a refresh came back with, the payload about to be persisted), so the operator
    can tell the three apart.

    Returns the grant it just accepted, narrowed to non-``None``. Callers thread THAT
    onward: it means no caller ever needs a "if we could not determine the scopes, assume
    the pin" fallback, which would re-introduce the pin-derived provenance this module's
    invariant forbids.
    """
    if not granted:
        raise GmailAuthError(
            f"{subject} records no granted scopes, so what it is allowed to do cannot be verified.",
            f"delete {token_path} and run `{_CONSENT_CMD}` to re-consent to exactly "
            f"{_scope_list()}.",
        )

    extra = sorted(set(granted) - set(SCOPES))
    if extra:
        raise GmailAuthError(
            f"{subject} grants access beyond this toolkit's read-only pin: "
            f"{', '.join(extra)}. This toolkit never sends, labels, or deletes mail, so "
            "it refuses to hold a wider grant.",
            f"revoke this app's access at {_REVOKE_URL}, delete {token_path}, then run "
            f"`{_CONSENT_CMD}` and approve ONLY {_scope_list()}.",
        )

    # Unreachable while SCOPES holds a single entry (a non-empty grant with no extras is
    # then necessarily equal to SCOPES), but the check must not silently depend on that:
    # it is what makes a future multi-scope pin fail closed. Covered by a test that
    # widens SCOPES.
    missing = sorted(set(SCOPES) - set(granted))
    if missing:
        raise GmailAuthError(
            f"{subject} does not grant {', '.join(missing)}, which this toolkit needs in "
            "order to read mail.",
            f"delete {token_path} and run `{_CONSENT_CMD}` to re-consent to {_scope_list()}.",
        )

    return granted


def _credentials_from_info(info: Mapping[str, Any]) -> Credentials:
    """Build OAuth credentials from parsed token JSON.

    A seam: tests substitute this to exercise the refresh/expiry branches without a
    network or a real grant. Raises ``ValueError`` (translated by the caller) when the
    JSON is not in Google's authorized-user format.
    """
    # google-auth ships py.typed but leaves this constructor unannotated.
    creds: Credentials = Credentials.from_authorized_user_info(  # type: ignore[no-untyped-call]
        dict(info), list(SCOPES)
    )
    return creds


def _request() -> Request:
    """Build the HTTP transport used to refresh a token (a seam for tests)."""
    return Request()


def _save_token(creds: Credentials, token_path: Path, verified_scopes: Sequence[str]) -> None:
    """Persist a refreshed token back to the file it came from. Never logged.

    ``verified_scopes`` is the grant that was independently verified upstream — the token
    endpoint's ``granted_scopes``, or the disk record read before any credentials object
    existed. It is BOTH what gets checked here and what gets written, which is what keeps
    this a real check rather than the pin comparing itself (see the module docstring's
    invariant). ``Credentials.to_json`` serialises ``self.scopes`` — the pin — never the
    granted scopes (``google/oauth2/credentials.py:564``, google-auth 2.55.0), so the
    payload's ``scopes`` entry is REPLACED with ``verified_scopes`` instead of trusted.

    The check is a precondition on the caller: for callers inside this module it cannot
    fire, because they pass a grant already checked. It exists for the next caller.

    **The write is atomic.** The token file is this module's SOLE persisted credential; a
    kill between open and flush (cron timeout, Ctrl-C, power loss) would truncate it. The
    payload goes to a temp file in the SAME directory (``mkstemp``, mode 0600 where the
    platform honours it) and is moved into place with ``os.replace``. A failure therefore
    leaves the previous token intact, and the temp file is removed.

    Residual risk, accepted deliberately: there is no cross-process lock, so two
    concurrent runs can each refresh and race to publish. On POSIX the last rename simply
    wins and both tokens are valid, so the loser is wasted work. On Windows — this
    project's platform — ``os.replace`` instead raises an ``OSError`` when the target is
    open elsewhere (measured here: ``PermissionError``, WinError 5), so the losing run
    FAILS with a ``GmailAuthError`` naming the file and must be re-run. Either way the
    file is never corrupted and no grant is lost; the Windows case is a visible failure
    rather than a silent one, so the POSIX "merely wasted work" reasoning does NOT carry
    over to the platform this actually runs on.
    """
    _check_granted_scopes(
        verified_scopes, "the grant about to be written to the Gmail token file", token_path
    )

    tmp_path: Path | None = None
    try:
        # google-auth ships py.typed but leaves its public methods unannotated.
        serialised: str = creds.to_json()  # type: ignore[no-untyped-call]
        record = json.loads(serialised)
        if not isinstance(record, dict):  # pragma: no cover - upstream always emits an object
            raise TypeError("Credentials.to_json did not produce a JSON object")
        # The verified grant is what lands on disk, so the next run's fresh-load check
        # reads back a real observation rather than an echo of the pin.
        record["scopes"] = list(verified_scopes)
        payload = json.dumps(record)

        token_path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(
            dir=token_path.parent, prefix=f"{token_path.name}.", suffix=".tmp"
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, token_path)
        tmp_path = None  # ownership transferred; nothing left to clean up
    except OSError as exc:
        raise GmailAuthError(
            f"the refreshed Gmail token could not be written back to {token_path} (the "
            "previous token file is untouched).",
            "make sure that directory exists and is writable, and that no other run of "
            f"this command is holding {token_path.name} open; then re-run.",
        ) from exc
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink()


def load_credentials(cfg: Config) -> Credentials:
    """Load, scope-verify, and (if stale) refresh the stored Gmail OAuth credentials.

    The token file is minted by the one-time consent flow; this function only ever reads
    it, refreshes it in place, and hands back usable credentials. It does not open a
    browser and does not mint anything.

    Order of operations:

    1. ``[gmail]`` must be configured (the section is optional; using it is not).
    2. The token file must exist, decode as UTF-8, and parse as a JSON object.
    3. **Runtime scope re-check** (this is DD2's) — the scopes the stored token FILE
       records as granted, read from the raw JSON before any credentials object exists.
       So an over-scoped token is never presented to Google at all.
    4. An expired token is refreshed; the grant the endpoint actually returned is checked,
       and that same verified grant is what :func:`_save_token` writes back.

    Raises:
        GmailAuthError: on EVERY failure path, always with a ``remediation`` — guaranteed
            structurally, not by enumeration. This function is a translation boundary: it
            converts anything the body raises into a :class:`GmailAuthError`. The handlers
            inside the body exist only to give a *better-targeted* remediation for the
            failures worth naming (dead refresh token, network down, malformed token
            file); correctness does not depend on that list being complete. Three separate
            escapes shipped while the contract WAS an enumeration —
            ``UnicodeDecodeError``, ``AttributeError`` from a non-string ``expiry``
            (``credentials.py:496-500``), and ``TransportError`` from any network failure
            (a SIBLING of ``RefreshError``, not a subclass) — which is why it no longer is.
    """
    try:
        return _load_credentials(cfg)
    except GmailAuthError:
        raise
    except Exception as exc:
        # The positive half of the boundary: anything the body did not name still leaves
        # as a GmailAuthError. `from exc` keeps the original for the traceback; the
        # exception's own text is NOT interpolated, since a foreign library could put
        # token material in it.
        token_path = cfg.gmail.token_path if cfg.gmail is not None else None
        where = f" while using {token_path}" if token_path is not None else ""
        raise GmailAuthError(
            f"the Gmail credentials could not be loaded{where}: an unexpected "
            f"{type(exc).__name__} came back from the Google auth library.",
            "re-run the command; if it persists, delete the token file named above and "
            f"run `{_CONSENT_CMD}` to mint a fresh one.",
        ) from exc


def _load_credentials(cfg: Config) -> Credentials:
    """The body of :func:`load_credentials`, which owns the translation boundary."""
    gmail = _require_gmail_section(cfg)
    token_path = gmail.token_path

    info = _read_token_info(token_path, gmail.client_secrets_path)
    disk_scopes = _check_granted_scopes(
        _recorded_scopes(info), f"the Gmail token at {token_path}", token_path
    )

    try:
        creds = _credentials_from_info(info)
    except Exception as exc:
        # Positive boundary, not a deny-list: ANY failure to turn this file into
        # credentials means the file is malformed. `except ValueError` used to miss the
        # AttributeError a non-string `expiry` raises at credentials.py:496-500.
        raise GmailAuthError(
            f"the Gmail token file at {token_path} is not in Google's authorized-user "
            "format — a required field is missing or has the wrong type.",
            f"delete that file and run `{_CONSENT_CMD}` to mint a fresh one.",
        ) from exc

    if not creds.valid:
        if not creds.refresh_token:
            raise GmailAuthError(
                f"the Gmail access token at {token_path} has expired and the file "
                "carries no refresh token, so it cannot be renewed unattended.",
                f"delete that file and run `{_CONSENT_CMD}` to re-consent.",
            )
        try:
            # Unannotated upstream, as above.
            creds.refresh(_request())  # type: ignore[no-untyped-call]
        except RefreshError as exc:
            raise GmailAuthError(
                f"Google refused to refresh the Gmail token at {token_path}: the refresh "
                "token is expired or was revoked. An OAuth consent screen left in "
                "'Testing' expires refresh tokens after 7 days, which is the usual cause.",
                "set the consent screen to Production (or list the account as a test "
                f"user), delete {token_path}, then run `{_CONSENT_CMD}` to re-consent.",
            ) from exc
        except TransportError as exc:
            # A SIBLING of RefreshError, not a subclass (google/auth/exceptions.py), and
            # raised for every ordinary connectivity failure — DNS, connection refused,
            # timeout, TLS (google/auth/transport/requests.py). The credentials are FINE;
            # the network is not, so the remediation must not tell anyone to re-consent.
            raise GmailAuthError(
                f"Google could not be reached while refreshing the Gmail token at "
                f"{token_path}. The stored credentials are intact — this is a network "
                "failure, not an authorisation one.",
                "check the network connection (or VPN/proxy) and re-run the command. Do "
                "NOT delete the token file; nothing is wrong with it.",
            ) from exc

        # `creds.granted_scopes` — NOT `creds.scopes`, and NOT anything read back out of
        # `to_json()`. Both of those are the pin (see the module docstring's invariant);
        # `granted_scopes` is assigned ONLY from the endpoint's `grant_response`
        # (`self._granted_scopes = grant_response["scope"].split()`,
        # google/oauth2/credentials.py:452-454, google-auth 2.55.0).
        #
        # It is None when the response omitted `scope`, which the endpoint is allowed to
        # do (`if scopes and "scope" in grant_response`, credentials.py:452). That is
        # ACCEPTED as "unchanged", deliberately: RFC 6749 section 5.1 defines an omitted
        # `scope` as identical to the scope requested, the refresh requests exactly SCOPES
        # (`scopes = self._scopes ...`, credentials.py:391), a refresh_token grant cannot
        # WIDEN what consent already fixed, and the disk record was verified above. In
        # that case the disk record IS the freshest independent observation, so it is what
        # gets threaded onward. Rejecting here would break every refresh against a server
        # that omits the field.
        granted = creds.granted_scopes
        if granted is not None:
            verified_scopes: Sequence[str] = _check_granted_scopes(
                granted,
                f"the grant Google returned when refreshing {token_path}",
                token_path,
            )
        else:
            # The disk record, already checked above, is then the freshest independent
            # observation available. NOT `list(SCOPES)` — a fallback to the pin is exactly
            # the pin-derived provenance this module's invariant forbids.
            verified_scopes = disk_scopes
        _save_token(creds, token_path, verified_scopes)

    if not creds.valid:
        raise GmailAuthError(
            f"the Gmail credentials at {token_path} are still unusable after a refresh.",
            f"delete that file and run `{_CONSENT_CMD}` to re-consent.",
        )
    return creds
