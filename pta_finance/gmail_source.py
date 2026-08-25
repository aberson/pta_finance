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
error message, not on success. :class:`GmailError` carries PATHS and REMEDIATION
only, so an operator can paste a failure anywhere without leaking a refresh token.

**Scope of this module.** The whole Gmail surface: credential load/refresh/mint, the search
query, the id listing, the raw fetch, and the ``.eml`` writer. It reads mail and writes
files; it never sends, labels, archives, or deletes, and it never parses a reimbursement
form. Exactly two functions touch anything other than the mailbox: :func:`write_eml` (the
``.eml`` files) and :func:`_save_token` (the token, back to the path it was read from), and
exactly one opens a browser (:func:`mint_credentials`, only when no token file exists yet).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import email.policy
import hashlib
import json
import os
import re
import tempfile
import webbrowser
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import date
from email.parser import BytesHeaderParser
from functools import partial
from pathlib import Path
from typing import Any, Final, Literal, NamedTuple

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from pta_finance.config import Config, Gmail

__all__ = [
    "SCOPES",
    "EmlWrite",
    "GmailAuthError",
    "GmailError",
    "GmailFetchError",
    "build_query",
    "build_service",
    "eml_filename",
    "fetch_raw",
    "inbox_dir",
    "list_message_ids",
    "load_credentials",
    "load_or_mint_credentials",
    "message_id_of",
    "mint_credentials",
    "needs_consent",
    "write_eml",
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

#: How long the local consent server waits for the browser redirect before giving up.
#: Unbounded, a first run in a session that cannot complete the redirect blocks FOREVER —
#: the one failure shape in this module that would not be actionable. On expiry
#: ``run_local_server`` raises, and :func:`mint_credentials`'s translation boundary turns
#: that into a :class:`GmailAuthError` like every other failure here.
_CONSENT_TIMEOUT_SECONDS: Final[int] = 300


class GmailError(Exception):
    """Base class for every failure this module raises — all of them actionable.

    Every instance carries a ``remediation``: the exact next action the operator should
    take — which config key to add, which file to place where, which command to re-run to
    re-consent. It is a REQUIRED constructor argument precisely so no raise site can ship
    a dead-end message. ``str(exc)`` renders as ``"<problem> Fix: <remediation>"``.

    Messages carry PATHS only. The contents of the client-secrets file and of the token
    file (access token, refresh token, client secret) are NEVER interpolated into a
    message — see this module's secrets posture. Neither is any message CONTENT: a
    subject, a sender, or a body must never reach a log line or an exception string.

    One base, two leaves, so a caller can catch the whole surface with one ``except``
    while a reader can still tell an authorisation problem from a transport one.
    """

    def __init__(self, problem: str, remediation: str) -> None:
        self.problem = problem
        self.remediation = remediation
        super().__init__(f"{problem} Fix: {remediation}")


class GmailAuthError(GmailError):
    """Gmail credentials are missing, malformed, over-scoped, or dead."""


class GmailFetchError(GmailError):
    """A fetch could not be completed or its result could not be written.

    Distinct from :class:`GmailAuthError` because the remediation is different in kind:
    nothing is wrong with the grant, so "re-consent" is never the right advice. Covers a
    malformed API response, an undecodable payload, and an unwritable ``.eml``.
    """


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


# ======================================================================================
# Consent: the one-time browser flow that MINTS the token file `load_credentials` reads.
# ======================================================================================


def inbox_dir(cfg: Config) -> Path:
    """The configured ``.eml`` landing directory, or a :class:`GmailAuthError` if unset.

    This is the ONLY resolved-path accessor the CLI needs, which is why it exists rather
    than the CLI reaching into ``cfg.gmail`` itself: reading the attribute directly would
    have to re-implement the "``[gmail]`` is optional, so ``cfg.gmail`` may be ``None``"
    guard, and a second copy of that guard is a second chance to raise ``AttributeError``
    at an operator instead of a remediation.
    """
    return _require_gmail_section(cfg).inbox_path


def needs_consent(cfg: Config) -> bool:
    """Whether the next credential load would open a browser (no token file yet).

    Callers use this to ANNOUNCE the browser before it appears — a command that silently
    pops a consent screen looks like it hung. ``False`` when the client-secrets file is
    also missing, because that case is a configuration failure with a much better error
    (:func:`_read_token_info`), not something a browser can fix.
    """
    gmail = _require_gmail_section(cfg)
    return not gmail.token_path.is_file() and gmail.client_secrets_path.is_file()


def _as_scope_sequence(value: object) -> Sequence[str] | None:
    """Normalise a ``granted_scopes`` value to a sequence of strings, or ``None``.

    The consent flow's credentials take ``granted_scopes`` from the token endpoint's
    response (``google_auth_oauthlib/helpers.py``: ``session.token.get("scope")``), which
    oauthlib normally hands over as a list but which the OAuth specs describe as a
    space-delimited string. Both are accepted; anything else becomes ``None``, which
    :func:`_check_granted_scopes` REJECTS. ``str`` is tested first because a bare string is
    itself a ``Sequence`` and would otherwise be split into characters.
    """
    if isinstance(value, str):
        return value.split()
    if isinstance(value, Sequence):
        return [str(item) for item in value]
    return None


def _run_consent_flow(gmail: Gmail) -> Credentials:
    """Open a browser, request exactly :data:`SCOPES`, return the granted credentials.

    A seam: tests substitute this so no test ever opens a browser or reaches the network,
    exactly as they already substitute :func:`_request` and :func:`_credentials_from_info`.
    ``google_auth_oauthlib`` is imported lazily so that merely importing this module — which
    ``cli`` does for EVERY subcommand — does not pay for the OAuth flow machinery.

    ``port=0`` asks the OS for a free loopback port, so a second local service already
    sitting on the library's default port cannot break consent.

    Two guards keep this from being the module's one un-actionable failure. A missing
    browser is detected BEFORE the local server starts, because ``run_local_server`` would
    otherwise sit waiting for a redirect that can never arrive; and the wait itself is
    bounded by :data:`_CONSENT_TIMEOUT_SECONDS` for every other reason consent never
    completes (the operator closed the tab, approved into a different account, walked away).
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    try:
        webbrowser.get()
    except webbrowser.Error as exc:
        raise GmailAuthError(
            "granting Gmail consent needs a browser on this machine, and none could be "
            "found — this looks like a headless or non-interactive session.",
            f"run `{_CONSENT_CMD}` once from a desktop session that has a browser. The "
            "token it mints is what every later run reads, so this is needed only once; "
            "headless runs work fine afterwards.",
        ) from exc

    flow = InstalledAppFlow.from_client_secrets_file(str(gmail.client_secrets_path), list(SCOPES))
    creds: Credentials = flow.run_local_server(port=0, timeout_seconds=_CONSENT_TIMEOUT_SECONDS)
    return creds


def mint_credentials(cfg: Config) -> Credentials:
    """Run the ONE-TIME consent flow and persist the token it mints.

    The only function in this toolkit that opens a browser, and it is reached only when
    there is no token file yet. What comes back is scope-checked against the grant the
    TOKEN ENDPOINT reported — ``granted_scopes``, never ``creds.scopes`` — before anything
    is written, so a consent screen where the user unticked (or a client configured to
    request more than) :data:`SCOPES` fails here rather than persisting a wrong grant.

    Raises:
        GmailAuthError: on every failure, always with a remediation. Like
            :func:`load_credentials` this is a translation boundary rather than an
            enumeration: anything the flow raises leaves as a ``GmailAuthError``, and the
            foreign exception's own text is NOT interpolated (it can carry token material).
    """
    gmail = _require_gmail_section(cfg)
    try:
        creds = _run_consent_flow(gmail)
        granted = _as_scope_sequence(creds.granted_scopes)
        verified_scopes = _check_granted_scopes(
            granted, "the grant the Gmail consent flow returned", gmail.token_path
        )
        # Inside the boundary on purpose: `_save_token` translates its own OSError, but a
        # TypeError or ValueError out of `to_json`/`json` would otherwise escape this
        # function untranslated and break its "every failure is a GmailAuthError" contract.
        _save_token(creds, gmail.token_path, verified_scopes)
    except GmailError:
        raise
    except Exception as exc:
        raise GmailAuthError(
            f"the Gmail consent flow did not complete: an unexpected "
            f"{type(exc).__name__} came back from the Google OAuth library. No usable token "
            "was written.",
            "make sure a browser can open on this machine and that "
            f"{gmail.client_secrets_path} is the JSON of an OAuth 'Desktop app' client, "
            f"then run `{_CONSENT_CMD}` again.",
        ) from exc
    return creds


def load_or_mint_credentials(cfg: Config) -> Credentials:
    """Usable credentials — minting them via consent on the first run, loading them after.

    This is what the ``fetch-mail`` command calls. It delegates to :func:`mint_credentials`
    ONLY when a browser could actually help: a configured ``[gmail]`` section, a
    client-secrets file present, and no token file yet. Every other shape falls through to
    :func:`load_credentials`, whose failures name the precise missing piece.
    """
    if needs_consent(cfg):
        return mint_credentials(cfg)
    return load_credentials(cfg)


def build_service(creds: Credentials) -> Any:
    """Build the Gmail API client — the seam an in-test double replaces.

    Returns the untyped ``googleapiclient`` resource, which is why every helper below
    validates the SHAPE of what comes back rather than trusting it. ``googleapiclient`` is
    imported lazily for the same reason as the OAuth flow: ``cli`` imports this module for
    every subcommand, and only ``fetch-mail`` needs the discovery machinery.
    """
    from googleapiclient.discovery import build

    try:
        service: Any = build("gmail", "v1", credentials=creds, cache_discovery=False)
    except Exception as exc:
        # Same translation boundary as everywhere else in this module: a raw
        # googleapiclient exception reaching the default excepthook would print its own
        # text, and this module never lets a foreign library choose what is shown.
        raise GmailFetchError(
            f"the Gmail API client could not be built: an unexpected "
            f"{type(exc).__name__} came back from the Google API library.",
            "check the network connection and that the Gmail API is enabled in the Google "
            "Cloud project, then re-run.",
        ) from exc
    return service


# ======================================================================================
# The search query.
# ======================================================================================


def _gmail_date(value: date) -> str:
    """Render a date as Gmail's ``after:``/``before:`` operators expect it: ``YYYY/MM/DD``.

    Formatted explicitly rather than with ``strftime``, whose zero-padding and locale
    behaviour for ``%Y``/``%m``/``%d`` vary by platform — and this project's platform is
    Windows, the one that varies.
    """
    return f"{value.year:04d}/{value.month:02d}/{value.day:02d}"


def build_query(since: date, until: date | None = None, extra: str | None = None) -> str:
    """Compose the Gmail search string for one fetch window.

    ``since`` renders as ``after:YYYY/MM/DD``; ``until`` renders as ``before:YYYY/MM/DD``
    and is omitted entirely when ``None`` (an open-ended window up to now). ``extra`` is
    the operator's raw ``--query`` text, appended verbatim so any Gmail operator
    (``has:attachment``, ``-in:chats``, ``larger:5M``) can be used without this function
    having to know it exists.

    **Window semantics — read this before choosing dates.** The two ends are NOT
    symmetric. ``after:`` is INCLUSIVE: a message timestamped on ``since`` matches.
    ``before:`` is EXCLUSIVE: a message timestamped on ``until`` does NOT match, so
    ``until`` names the first day that is *not* fetched. Gmail also evaluates both against
    the message's date rendered in the ACCOUNT's own timezone, not UTC, so a message near
    local midnight can fall on either side of a boundary.

    The operating rule that follows is: **overlap successive windows, never tile them.**
    An overlap costs nothing — :func:`write_eml`'s deterministic filename makes a re-fetch
    a no-op — while a gap is silent and permanent.

    The fetch is deliberately date-scoped only, with no sender or subject filter: in one
    real triage round 6 of 13 reimbursement cases did not arrive through the submission
    form at all, so any such filter would have silently dropped them (plan DD8).
    """
    terms = [f"after:{_gmail_date(since)}"]
    if until is not None:
        terms.append(f"before:{_gmail_date(until)}")
    if extra is not None and extra.strip():
        terms.append(extra.strip())
    return " ".join(terms)


# ======================================================================================
# Listing and fetching. The API resource is untyped, so every response is shape-checked.
# ======================================================================================


def _http_status(exc: BaseException) -> int | None:
    """The HTTP status carried by a ``googleapiclient`` error, if any. NEVER its text.

    ``HttpError`` exposes the response as ``exc.resp.status``. A bare status code names
    the failure class (429 rate limit, 5xx transient, 401 revoked grant) while carrying
    no message id, no URI, and no content — which is the whole reason it is the ONLY
    thing lifted out of a foreign exception here.
    """
    status = getattr(getattr(exc, "resp", None), "status", None)
    return status if isinstance(status, int) else None


def _execute(build: Callable[[], Any], what: str) -> Mapping[str, Any]:
    """Build one Gmail API request, run it, and return its response as a mapping.

    **The request is BUILT inside this function, not passed in already built.** Both
    halves can raise — ``googleapiclient`` validates parameters when the request object is
    constructed and performs the HTTP call in ``execute()`` — so taking a thunk is what
    puts the whole call under one exception boundary. Passing a built request would leave
    the construction outside it.

    That boundary is a PRIVACY control, not just tidiness. ``HttpError`` is raised for any
    non-2xx (429 rate limit, transient 5xx, a revoked grant, a 404) and its ``str``/``repr``
    embed the full request URI — which for a message fetch is
    ``.../users/me/messages/<message_id>?format=raw``. Uncaught, that reaches the default
    excepthook and prints a Gmail message id to the terminal, breaking this connector's
    "counts only, never a message id" contract on its most likely real-world failure path.
    So every foreign exception is translated here, and only :func:`_http_status` — a bare
    status code — is carried over from it.

    The ``googleapiclient`` resource is untyped, so what ``execute()`` returns is ``Any``;
    validating its shape here is also what keeps every caller below honestly typed instead
    of laundering ``Any`` into a ``bytes`` or a ``str`` return.
    """
    try:
        response: object = build().execute()
    except GmailError:
        raise
    except Exception as exc:
        status = _http_status(exc)
        detail = f" (HTTP {status})" if status is not None else ""
        # Deliberately does NOT interpolate `exc` — its text carries the request URI, and
        # the URI carries the message id. `from None` for the same reason: a chained
        # `__cause__` would print that repr if a GmailFetchError ever escaped to the default
        # excepthook. The CLI catches GmailError on every call it makes, so this is
        # defence-in-depth — but the privacy requirement here is absolute, and the exception
        # TYPE NAME and HTTP status are carried in our own message, so debuggability is not
        # traded away.
        raise GmailFetchError(
            f"the Gmail API call failed while {what}{detail}: an unexpected "
            f"{type(exc).__name__} came back from the Google API library.",
            "re-run the command — anything already written is skipped. A rate limit or a "
            "transient server error clears on its own; if it persists, check the Gmail "
            "API's status and that it is still enabled in the Google Cloud project.",
        ) from None

    if not isinstance(response, Mapping):
        raise GmailFetchError(
            f"Gmail returned something other than a JSON object while {what}.",
            "re-run the command; if it persists, check the Gmail API's status and that "
            "the Gmail API is still enabled in the Google Cloud project.",
        )
    return response


def _as_list(value: object) -> list[Any]:
    """A response field that should be a list, or an empty list. Never ``None``."""
    return value if isinstance(value, list) else []


def list_message_ids(service: Any, query: str, *, limit: int | None = None) -> Iterator[str]:
    """Yield the Gmail id of every message matching ``query``, one page at a time.

    Walks ``users.messages.list`` through ``nextPageToken`` until the API stops returning
    one — Gmail pages at 100 by default, so ANY real month needs this and a single-page
    implementation would silently truncate the window. Ids are de-duplicated, because a
    mailbox mutating between page fetches can return the same id on two pages.

    ``limit`` caps how many ids are yielded (``--limit``, for a cheap first look at a big
    window). Because this is a generator, hitting the cap also stops the pagination: the
    next page is never requested.

    The ids are yielded, not returned as a list, so the caller can fetch-and-write each
    message as it goes. A run over a large window therefore holds one message in memory,
    and an interrupted run has already written everything it got to.
    """
    if limit is not None and limit <= 0:
        return

    messages = service.users().messages()
    seen: set[str] = set()
    page_token: str | None = None

    while True:
        # `partial` binds `page_token` BY VALUE here, so the thunk cannot read the next
        # loop iteration's token (a plain closure would, and ruff B023 flags exactly that).
        response = _execute(
            partial(messages.list, userId="me", q=query, pageToken=page_token),
            "listing message ids",
        )
        for entry in _as_list(response.get("messages")):
            if not isinstance(entry, Mapping):
                continue
            message_id = entry.get("id")
            if not isinstance(message_id, str) or not message_id or message_id in seen:
                continue
            seen.add(message_id)
            yield message_id
            if limit is not None and len(seen) >= limit:
                return

        token = response.get("nextPageToken")
        if not isinstance(token, str) or not token:
            return
        if token == page_token:
            # Forward progress, enforced. A service that keeps handing back the token it was
            # just given spins this loop forever: every page yields zero new ids (they are
            # all in `seen`), `page_token` never changes, and nothing returns or raises.
            # LOUD rather than a silent `return`: a short fetch that looks successful is the
            # one failure this connector cannot afford, because the gap is permanent and
            # invisible — far worse than an error the operator can re-run.
            raise GmailFetchError(
                "Gmail kept returning the same page token, so the message list never "
                "advanced and the window could not be read to the end. No further pages "
                "were requested; the window was NOT fully fetched.",
                "re-run the command — anything already written is skipped. If it repeats, "
                "narrow the window with --until and fetch it in smaller pieces.",
            )
        page_token = token


def fetch_raw(service: Any, message_id: str) -> bytes:
    """Fetch one message as the RFC-822 bytes an ``.eml`` file holds.

    ``format="raw"`` is what makes this connector interchangeable with the ``.mbox``
    archives it supplements: the bytes written are the same bytes a Takeout export or a
    hand-saved ``.eml`` contains, so :mod:`pta_finance.receipt_ingest` cannot tell where a
    message came from.

    Gmail returns that payload base64url-encoded, and MAY omit the ``=`` padding — hence
    the re-padding, without which a perfectly good message raises ``binascii.Error``.

    Neither the id nor any part of the message appears in the errors raised here: an
    exception string is a place message content leaks into logs and issue trackers.
    """
    response = _execute(
        lambda: service.users().messages().get(userId="me", id=message_id, format="raw"),
        "fetching a message",
    )
    encoded = response.get("raw")
    if not isinstance(encoded, str) or not encoded:
        raise GmailFetchError(
            "Gmail returned a message with no `raw` payload, so there is nothing to write.",
            "re-run the command; if it persists for the same window, narrow it with "
            "--until to find the boundary and report the count.",
        )
    padded = encoded + "=" * (-len(encoded) % 4)
    try:
        return base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError) as exc:
        raise GmailFetchError(
            "Gmail returned a message whose `raw` payload is not valid base64url, so it "
            "could not be decoded.",
            "re-run the command; if it persists for the same window, narrow it with "
            "--until to find the boundary and report the count.",
        ) from exc


# ======================================================================================
# The on-disk idempotency key, and the writer that honours it.
#
# ONE source of truth. Every constant the filename rule depends on is named here and
# consumed by exactly ONE function (`eml_filename`); the hash itself is derived by exactly
# ONE function (`_message_digest`), and nothing else in the toolkit may re-derive a
# filename or a digest. Changing any of these values re-downloads every message under a new
# name and breaks skip-if-identical, which is why the plan pins the rule verbatim and why
# these are a shared KEY SHAPE (dev/.claude/rules/code-quality.md), not an implementation
# detail.
#
# **The key has TWO sources, and keeping them apart is the whole design.**
#
#   hash source  The FULL RAW MESSAGE BYTES, verbatim and unconditionally. Not an
#                extraction, not a parse, not a normalisation, not a slice — `sha256` is
#                handed exactly what `fetch_raw` returned. Two byte-distinct messages
#                therefore ALWAYS get two distinct hashes.
#   stem source  LOSSY, human-readable: the parsed + unfolded Message-ID. It only ever
#                names the file for a human; its lossiness cannot cause a collision.
#
# That split is the fix for a whole CLASS of bug, not for any one of its instances. FIVE
# separate collisions shipped while the hash was computed over some derivation of the
# Message-ID: fold truncation, a bare CR/LF treated as a line boundary, whitespace-run
# collapse across a quoted-string local part, two different raw bytes both decoding to
# U+FFFD, and — after the hash had already been moved to the field's raw wire bytes — the
# EXTRACTION of those bytes cutting short at a blank-line scan or a field-name heuristic.
# In every case `sha256` was handed an ALREADY-COLLIDED value, so the suffix — the very
# thing the plan says "makes the rule collision-safe" — faithfully preserved the collision,
# `write_eml` saw differing bytes at one path, reported an ordinary `rewritten`, and a
# message was silently destroyed.
#
# The lesson those five vectors teach is that header framing is itself ambiguous once the
# value contains raw framing bytes, so EVERY bounded extraction has another truncation
# vector. Hashing the whole message removes the extraction, and with it the entire class:
# there is nothing left to get wrong. A sixth vector in the parser cannot reach the hash,
# because the parser is no longer upstream of it.
#
# The cost, accepted deliberately: the name now depends on the whole message, so one
# logical message rendered with different bytes (a different fold, a re-serialised body)
# yields two filenames. That trades a duplicate FILE — which `receipt_map`'s Message-ID +
# content-hash dedup absorbs, exactly as plan DD6 assigns it — for never again destroying a
# message. The failure direction is what makes the trade correct: the old rule failed toward
# SILENT DATA LOSS, this one fails toward a harmless extra file. Gmail's `format="raw"`
# output is byte-stable per message id, so an ordinary re-fetch (the case DD6,
# `build_query`'s overlap rule and Step 11 all quantify over) is byte-identical and still a
# no-op.
# ======================================================================================

#: Every character OUTSIDE this class is replaced. The class deliberately excludes both
#: path separators, ``:``, and everything else a filesystem treats specially, so a
#: sanitised stem can never carry a directory component or a ``..`` traversal out of the
#: inbox — a Message-ID is attacker-influenceable text that becomes a filename.
_FILENAME_UNSAFE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._-]")
_FILENAME_REPLACEMENT: Final[str] = "_"
#: The sanitised stem is truncated to this many characters before the hash is appended.
_FILENAME_STEM_MAX: Final[int] = 80
#: Hex characters of ``sha256(<the full raw message bytes>)`` appended after a ``-``. This
#: suffix is what keeps the rule collision-safe AFTER sanitisation and truncation have
#: collapsed distinct ids onto one stem, and on case-insensitive filesystems where two
#: Message-IDs differing only in case would otherwise land on the same file.
_FILENAME_HASH_CHARS: Final[int] = 8
#: A message with no Message-ID header has no stem to build, so it is named from a longer
#: slice of the SAME digest instead.
_NO_MESSAGE_ID_PREFIX: Final[str] = "nomsgid-"
_NO_MESSAGE_ID_HASH_CHARS: Final[int] = 16
_EML_SUFFIX: Final[str] = ".eml"

# The two patterns that normalise a raw header into the human-readable STEM. They are
# deliberately NOT load-bearing for collision-safety any more — the hash never sees their
# output — but they still decide what a human reads in a directory listing.
#
#: RFC 5322 folding: a CRLF immediately followed by whitespace is layout, not content.
#: Unfolding removes the CRLF and keeps the whitespace (RFC 5322 section 2.2.3).
_HEADER_FOLD: Final[re.Pattern[str]] = re.compile(r"\r?\n(?=[ \t])")
#: Any remaining whitespace run then collapses to ONE space, so the same Message-ID folded
#: at a different column still reads the same in the stem.
_HEADER_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")


def _message_digest(raw: bytes) -> str:
    """THE hash of a message: ``sha256`` of its FULL raw bytes. The only derivation site.

    Both filename branches — the ``-<hash>`` suffix and the ``nomsgid-<hash>`` name — slice
    their hex out of this one value, so there is exactly ONE answer to "what does this
    toolkit hash?" and it is *the message*. A second derivation site is what five shipped
    collisions had in common, so re-introducing one fails
    ``test_exactly_one_function_derives_the_hash_source`` rather than shipping.

    ``raw`` is passed through UNTOUCHED on purpose. Do not add an extraction, a parse, a
    slice, or a normalisation here, however well-bounded it looks: header framing is
    ambiguous once a value contains raw framing bytes, so every bounded extraction has a
    truncation vector, and a truncated hash input is a SILENTLY OVERWRITTEN message.
    """
    return hashlib.sha256(raw).hexdigest()


def _stem_source(raw: bytes, message_id: str | None) -> str | None:
    """The human-readable half of the key. ``None`` means "no usable Message-ID".

    This feeds the STEM and NOTHING else — it never reaches :func:`_message_digest`, so its
    lossiness is harmless by construction. ``message_id`` is an OVERRIDE for a caller that
    knows the id out of band and wants it in the name; production never passes it
    (``write_eml`` omits it), and it cannot affect the hash either way.
    """
    if message_id is not None:
        return message_id.strip() or None
    return message_id_of(raw)


def message_id_of(raw: bytes) -> str | None:
    """The RFC-822 ``Message-ID`` header of a raw message, or ``None`` if it has none.

    This reads ONE header in order to name a file. It is not message parsing and not a
    crack in Design Decision 4: this module still imports nothing from
    :mod:`pta_finance.receipt_ingest` and knows nothing about reimbursement forms. The
    header is the right stem precisely BECAUSE the parser also keys on it — a message
    fetched here and the same message inside an ``.mbox`` archive dedup against each other
    downstream (``receipt_map.map_submissions``).

    **This value is the STEM source only — it must never reach ``sha256``.** It is lossy by
    construction and cannot be made otherwise: ``compat32`` decodes an invalid header byte
    to U+FFFD, the parser treats a bare CR/LF as a line boundary, and the whitespace
    collapse below flattens runs that a quoted-string local part may legitimately contain.
    Each of those has produced a real collision. They are all harmless now, because the
    hash is taken over the whole message and never over anything this function returns.

    Read with ``email.policy.compat32`` rather than the modern ``policy.default``, which
    runs Message-ID through its STRUCTURED parser and discards everything past the first
    fold point when the continuation makes the msg-id token invalid. Unfolding is then done
    explicitly: drop the CRLF of each fold, keep its whitespace, collapse whitespace runs to
    a single space. The result is a readable stem that survives the sanitiser, which is all
    it has to be.
    """
    headers = BytesHeaderParser(policy=email.policy.compat32).parsebytes(raw)
    value = headers.get("Message-ID")
    if value is None:
        return None
    unfolded = _HEADER_FOLD.sub("", str(value))
    return _HEADER_WHITESPACE.sub(" ", unfolded).strip() or None


def eml_filename(raw: bytes, message_id: str | None = None) -> str:
    """The deterministic ``.eml`` filename for one message — the on-disk idempotency key.

    The rule, pinned by the feature plan and implemented here and NOWHERE else:

    1. Strip a surrounding ``<``/``>`` from the Message-ID.
    2. Replace every character outside ``[A-Za-z0-9._-]`` with ``_``.
    3. Truncate to :data:`_FILENAME_STEM_MAX` characters.
    4. Append ``-`` plus the first :data:`_FILENAME_HASH_CHARS` lowercase hex characters of
       ``sha256`` of **the full raw RFC-822 message bytes** (steps 1-3 are lossy; this is
       what keeps the result collision-safe), then ``.eml``.

    Steps 1-3 build the STEM from :func:`_stem_source`; step 4 hashes ``raw`` itself, via
    :func:`_message_digest`. Those are two different values on purpose — see the block
    comment above. The Message-ID supplies the human-readable stem only; it is never the
    hash input, because every bounded extraction of it that this connector shipped had a
    truncation vector and each one silently destroyed a message.

    A message with no Message-ID header is named ``nomsgid-`` plus the first
    :data:`_NO_MESSAGE_ID_HASH_CHARS` hex characters of that SAME digest, so it is still
    stable across runs and still deduplicates against itself.

    ``message_id`` overrides the id read from ``raw`` (see :func:`_stem_source`); production
    omits it. It changes only the STEM: ``eml_filename(raw)`` and
    ``eml_filename(raw, "<anything>")`` carry the same hash suffix, because the suffix is a
    property of the message rather than of the caller.

    The result is always a BARE filename: the character class in step 2 admits no path
    separator, and step 4 always appends, so the name can never be ``.`` or ``..`` and can
    never escape the directory it is joined to. (A degenerate ``<>`` Message-ID sanitises
    to an empty stem and yields ``-<hash>.eml``, which is odd-looking but still a legal,
    stable, bare filename — the rule is pinned, so it is not "fixed" here.)
    """
    digest = _message_digest(raw)
    stem_source = _stem_source(raw, message_id)
    if stem_source is None:
        return f"{_NO_MESSAGE_ID_PREFIX}{digest[:_NO_MESSAGE_ID_HASH_CHARS]}{_EML_SUFFIX}"

    stem = stem_source
    if stem.startswith("<") and stem.endswith(">"):
        stem = stem[1:-1]
    stem = _FILENAME_UNSAFE.sub(_FILENAME_REPLACEMENT, stem)[:_FILENAME_STEM_MAX]
    return f"{stem}-{digest[:_FILENAME_HASH_CHARS]}{_EML_SUFFIX}"


class EmlWrite(NamedTuple):
    """What :func:`write_eml` did with one message.

    ``status`` is what the caller's count summary reports, and it is the only signal an
    operator gets that an overlapping re-fetch was in fact free:

    * ``"new"`` — no file was there; it was written.
    * ``"unchanged"`` — a byte-identical file was already there and was NOT rewritten.
    * ``"rewritten"`` — a file was there with different bytes; it was replaced.
    """

    path: Path
    status: Literal["new", "unchanged", "rewritten"]


def write_eml(raw: bytes, out_dir: Path, message_id: str | None = None) -> EmlWrite:
    """Write one raw message into ``out_dir`` under :func:`eml_filename`; skip if identical.

    ``message_id`` defaults to the message's own ``Message-ID`` header, read from ``raw``
    by :func:`_stem_source`, and it only ever affects the readable STEM — the hash suffix is
    ``sha256`` of ``raw`` either way. That is what makes the name a property of the message
    rather than of the caller: two different callers naming the same message agree by
    construction, even if one of them supplies a different id.

    **Skip-if-identical** is the whole point: re-fetching an overlapping window rewrites
    nothing, so the operating procedure can overlap windows instead of risking a gap
    (plan DD6). Bytes are compared, not mtimes or sizes.

    **``out_dir`` must be the directory that also holds the ``.mbox`` archives**
    (:func:`inbox_dir`, plan DD10) — never a subdirectory of it.
    ``receipt_ingest.iter_source`` globs a directory NON-recursively, so a subdirectory is
    invisible to ``map-receipts --source <dir>`` and would force the archives and the
    fetched mail to be mapped in two separate runs. That is the failure that matters:
    ``receipt_map.map_submissions`` accumulates its Message-ID and content-hash dedup sets
    WITHIN A SINGLE CALL, so two runs each look internally clean while together
    double-counting every message the two sources share.

    The write is atomic — a temp file in the same directory, then ``os.replace`` — so an
    interrupted run can never leave a truncated ``.eml`` for the parser to read, and a
    half-written file can never be mistaken for a complete one by the next run's
    byte-comparison.
    """
    name = eml_filename(raw, message_id)
    path = out_dir / name

    tmp_path: Path | None = None
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() == raw:
                return EmlWrite(path, "unchanged")
            status: Literal["new", "unchanged", "rewritten"] = "rewritten"
        else:
            status = "new"

        handle, tmp_name = tempfile.mkstemp(dir=out_dir, prefix=f"{name}.", suffix=".tmp")
        tmp_path = Path(tmp_name)
        with os.fdopen(handle, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, path)
        tmp_path = None  # ownership transferred; nothing left to clean up
    except OSError as exc:
        raise GmailFetchError(
            f"a fetched message could not be written into {out_dir}.",
            "check that the directory exists and is writable, that there is free disk "
            "space, and that no other process is holding that file open; then re-run — "
            "already-written messages will be skipped.",
        ) from exc
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink()

    return EmlWrite(path, status)
