"""Application services for the student-facing Autograde MVP.

The HTTP layer is intentionally kept outside this module.  This service owns
credential issuance, authorization, object projections, and browser pairing.
Raw activation/device/access/refresh credentials exist only long enough to be
returned to the authorized client; SQLite receives domain-separated HMAC
verifiers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import html
import ipaddress
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, BinaryIO, Callable, Mapping, MutableMapping, Optional, Protocol
from urllib.parse import quote, urlencode, urlsplit

from .domain import git_oid, utc_iso
from .platform_auth import (
    hash_student_password,
    InvalidSignedValue,
    new_activation_code,
    new_api_token,
    new_assignment_claim_code,
    new_device_code,
    new_public_id,
    new_user_code,
    secret_digest,
    sign_browser_value,
    verify_student_password,
    verify_browser_value,
)
from .platform_events import emit_operator_event
from .platform_bundle import (
    BundleLimitError,
    BundleStorageError,
    BundleStore,
    UnsafeBundleError,
)
from .platform_github import GitHubOAuthClient, GitHubOAuthError
from .platform_pinner import (
    SubmissionPinner,
    SubmissionPinInfrastructureError,
    SubmissionSourceInvalid,
    SubmissionSourceUnavailable,
)
from .platform_qr import assignment_claim_qr_svg
from .platform_state import (
    DeviceAuthorization,
    DeviceAuthorizationExpired,
    DeviceAuthorizationState,
    BundleAssignmentRelease,
    BundleSubmissionRequest,
    BundleSubmissionResult,
    PlatformAccessDenied,
    PlatformAssignment,
    PlatformConflict,
    PlatformIdempotencyConflict,
    PlatformInvalidTransition,
    PlatformNotFound,
    PlatformSession,
    PlatformSessionLimitExceeded,
    PlatformStateStore,
    PlatformSubmissionLimitExceeded,
    RefreshTokenReuseDetected,
    RefreshRotationLimitExceeded,
    ResultPolicy,
    StudentIdentityKind,
    SubmissionMode,
    SubmissionRequest,
    SubmissionResult,
)


class PlatformAPIError(RuntimeError):
    """Safe error which may cross the public HTTP boundary."""

    def __init__(
        self,
        status: int,
        code: str,
        safe_message: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        super().__init__(safe_message)
        self.status = status
        self.code = code
        self.safe_message = safe_message
        self.headers = dict(headers or {})


@dataclass(frozen=True)
class PlatformResponse:
    """Framework-neutral response used by the browser activation endpoints."""

    status: int
    body: str | Mapping[str, Any] | None
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PlatformFileResponse:
    """Framework-neutral trusted file response for immutable bundle downloads."""

    status: int
    path: Path
    content_type: str = "application/octet-stream"
    headers: Mapping[str, str] = field(default_factory=dict)


class SubmissionNotifier(Protocol):
    def __call__(self, submission_id: str) -> Optional[bool]: ...


@dataclass
class _RefreshFlight:
    """One in-process refresh rotation shared only by overlapping callers.

    The completed flight is removed before waiters are released.  A later use of
    the old refresh token therefore still reaches the durable reuse detector and
    revokes the family; only requests which overlapped the original rotation can
    receive the same response.
    """

    completed: threading.Event = field(default_factory=threading.Event)
    response: Optional[Mapping[str, Any]] = None
    error: Optional[BaseException] = None
    followers: int = 0


@dataclass(frozen=True)
class TokenPolicy:
    device_lifetime_seconds: int = 300
    poll_interval_seconds: int = 5
    access_lifetime_seconds: int = 900
    # Shared lab seats must not retain a month-long credential.  Refresh
    # rotation is bounded by this absolute four-hour login deadline.
    refresh_lifetime_seconds: int = 4 * 60 * 60
    max_active_sessions: int = 5
    max_daily_session_issuances: int = 20
    max_retained_sessions: int = 1_000
    max_refresh_rotations: int = 2_048
    max_activation_attempts: int = 5
    session_list_limit: int = 50
    auth_history_retention_seconds: int = 30 * 24 * 60 * 60
    assignment_claim_lifetime_seconds: int = 10 * 60
    max_password_attempts: int = 5
    password_lockout_seconds: int = 5 * 60
    max_concurrent_password_verifications: int = 4

    def __post_init__(self) -> None:
        values = (
            self.device_lifetime_seconds,
            self.poll_interval_seconds,
            self.access_lifetime_seconds,
            self.refresh_lifetime_seconds,
            self.max_active_sessions,
            self.max_daily_session_issuances,
            self.max_retained_sessions,
            self.max_refresh_rotations,
            self.max_activation_attempts,
            self.session_list_limit,
            self.auth_history_retention_seconds,
            self.assignment_claim_lifetime_seconds,
            self.max_password_attempts,
            self.password_lockout_seconds,
            self.max_concurrent_password_verifications,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
            raise ValueError("token policy durations must be positive integers")
        if self.refresh_lifetime_seconds <= self.access_lifetime_seconds:
            raise ValueError("refresh token lifetime must exceed access token lifetime")
        if self.max_retained_sessions < self.max_active_sessions:
            raise ValueError(
                "max_retained_sessions must be at least max_active_sessions"
            )
        if self.session_list_limit > 100:
            raise ValueError("session_list_limit must not exceed 100")
        if self.max_activation_attempts > 20:
            raise ValueError("max_activation_attempts must not exceed 20")
        if self.max_password_attempts > 20:
            raise ValueError("max_password_attempts must not exceed 20")
        if self.max_concurrent_password_verifications > 32:
            raise ValueError(
                "max_concurrent_password_verifications must not exceed 32"
            )
        if self.session_list_limit < self.max_active_sessions:
            raise ValueError(
                "session_list_limit must be at least max_active_sessions"
            )


@dataclass(frozen=True)
class SubmissionPolicy:
    max_outstanding_per_student: int = 3
    max_daily_per_student: int = 50

    def __post_init__(self) -> None:
        values = (
            self.max_outstanding_per_student,
            self.max_daily_per_student,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in values
        ):
            raise ValueError("submission limits must be positive integers")


_USER_CODE = re.compile(r"^[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{4}-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{4}$")
_ACTIVATION_CODE = re.compile(
    r"^AG1[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{26}$"
)
_ASSIGNMENT_CLAIM_CODE = re.compile(
    r"^AK1[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{12}$"
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~-]{0,255}$")
_MAX_DEVICE_POLL_TRACKERS = 2_000
_DEFAULT_ACTIVATION_LIFETIME_SECONDS = 7 * 24 * 60 * 60
_MAX_ACTIVATION_LIFETIME_SECONDS = 30 * 24 * 60 * 60
_DUMMY_PASSWORD_HASH = (
    "scrypt$v2$16384$8$1$AAAAAAAAAAAAAAAAAAAAAA$"
    "KtFLbYYMHawcT6bz3UL3EQWOYqrGjouUFpkpVOI3qcI"
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _validated_public_base_url(
    value: str, *, external_access_mode: str = "disabled"
) -> str:
    parsed = urlsplit(value.strip())
    local = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if external_access_mode not in {"disabled", "insecure-http"}:
        raise ValueError("external_access_mode must be disabled or insecure-http")
    if external_access_mode == "insecure-http":
        if parsed.scheme != "http" or not _is_rfc1918_ipv4_literal(parsed.hostname):
            raise ValueError(
                "insecure-http public_base_url must use an RFC1918 HTTP origin"
            )
    elif parsed.scheme != "https" and not (parsed.scheme == "http" and local):
        raise ValueError("public_base_url must use HTTPS or loopback HTTP")
    if (
        not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "public_base_url must be an origin without credentials, path, query, or fragment"
        )
    return f"{parsed.scheme}://{parsed.netloc}"


def _is_rfc1918_ipv4_literal(value: str | None) -> bool:
    if value is None:
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if not isinstance(address, ipaddress.IPv4Address):
        return False
    return any(
        address in network
        for network in (
            ipaddress.IPv4Network("10.0.0.0/8"),
            ipaddress.IPv4Network("172.16.0.0/12"),
            ipaddress.IPv4Network("192.168.0.0/16"),
        )
    )


def _required_string(
    value: Any,
    field_name: str,
    *,
    maximum: int = 255,
) -> str:
    if not isinstance(value, str):
        raise PlatformAPIError(400, "invalid_request", f"{field_name} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or any(ord(char) < 32 for char in normalized):
        raise PlatformAPIError(400, "invalid_request", f"{field_name} is invalid")
    return normalized


def _strict_object(
    payload: Mapping[str, Any],
    *,
    allowed: set[str],
    required: set[str],
) -> None:
    unexpected = set(payload) - allowed
    missing = required - set(payload)
    if unexpected or missing:
        raise PlatformAPIError(400, "invalid_request", "request fields are invalid")


class StudentPlatformService:
    """Course-scoped facade consumed by HTTP and operator CLI adapters."""

    def __init__(
        self,
        *,
        state: PlatformStateStore,
        server_secret: bytes,
        course_key: str,
        public_base_url: str,
        external_access_mode: str = "disabled",
        token_policy: TokenPolicy | None = None,
        submission_policy: SubmissionPolicy | None = None,
        github_oauth: GitHubOAuthClient | None = None,
        submission_pinner: SubmissionPinner | None = None,
        notify_submission: SubmissionNotifier | None = None,
        bundle_store: BundleStore | None = None,
        notify_bundle_submission: SubmissionNotifier | None = None,
        instructor_token: str | None = None,
        now: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if len(server_secret) < 32:
            raise ValueError("server_secret must contain at least 32 bytes")
        self.state = state
        self._secret = server_secret
        self.course_key = _required_string(course_key, "course_key")
        self.external_access_mode = external_access_mode
        self.public_base_url = _validated_public_base_url(
            public_base_url,
            external_access_mode=external_access_mode,
        )
        self.policy = token_policy or TokenPolicy()
        self.submission_policy = submission_policy or SubmissionPolicy()
        self.github_oauth = github_oauth
        self.submission_pinner = submission_pinner
        self.notify_submission = notify_submission
        self.bundle_store = bundle_store
        self.notify_bundle_submission = notify_bundle_submission
        self._instructor_token = (
            _required_string(instructor_token, "instructor_token", maximum=512)
            if instructor_token is not None
            else None
        )
        self._now = now
        self._monotonic = monotonic
        self._poll_lock = threading.Lock()
        # verifier -> (next allowed poll, authorization expiry), both on the
        # monotonic clock.  The table is deliberately bounded independently of
        # SQLite so unauthenticated device polling cannot grow process memory.
        self._next_poll: MutableMapping[str, tuple[float, float]] = {}
        self._refresh_flights_lock = threading.Lock()
        self._refresh_flights: MutableMapping[str, _RefreshFlight] = {}
        self._submission_locks = tuple(threading.Lock() for _ in range(64))
        self._password_verification_slots = threading.BoundedSemaphore(
            self.policy.max_concurrent_password_verifications
        )

    # Public JSON API -------------------------------------------------

    def create_device_authorization(
        self, payload: Mapping[str, Any], base_url: str | None = None
    ) -> Mapping[str, Any]:
        del base_url  # Never trust Host-derived URLs; use the configured public origin.
        _strict_object(
            payload,
            allowed={"client", "extension_version", "device_name", "claim_code"},
            required={"device_name"},
        )
        device_label = _required_string(payload.get("device_name"), "device_name", maximum=128)
        if "client" in payload:
            _required_string(payload["client"], "client", maximum=64)
        if "extension_version" in payload:
            _required_string(payload["extension_version"], "extension_version", maximum=64)

        now = self._aware_now()
        device_code = new_device_code()
        user_code = new_user_code()
        authorization_id = new_public_id("dev")
        self._purge_expired_poll_entries()
        try:
            self.state.create_device_authorization(
                authorization_id=authorization_id,
                device_code_hash=self._digest("device", device_code),
                user_code_hmac=self._digest("user-code", user_code),
                course_key=self.course_key,
                device_label=device_label,
                expires_at=now + timedelta(seconds=self.policy.device_lifetime_seconds),
                poll_interval_seconds=self.policy.poll_interval_seconds,
                at=now,
            )
        except PlatformConflict as exc:
            raise PlatformAPIError(
                429,
                "temporarily_unavailable",
                "too many connection requests are pending; try again later",
                headers={"Retry-After": str(self.policy.poll_interval_seconds)},
            ) from exc
        verification_uri = f"{self.public_base_url}/activate"
        return {
            "device_code": device_code,
            "user_code": user_code,
            "verification_uri": verification_uri,
            "verification_uri_complete": f"{verification_uri}?{urlencode({'user_code': user_code})}",
            "expires_in": self.policy.device_lifetime_seconds,
            "interval": self.policy.poll_interval_seconds,
            "poll_interval": self.policy.poll_interval_seconds,
        }

    def exchange_device_authorization(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        _strict_object(payload, allowed={"device_code"}, required={"device_code"})
        device_code = _required_string(payload.get("device_code"), "device_code", maximum=256)
        verifier = self._digest("device", device_code)
        try:
            authorization = self.state.get_device_authorization_by_device_code_hash(
                verifier, course_key=self.course_key
            )
        except PlatformNotFound as exc:
            raise PlatformAPIError(400, "invalid_grant", "device code is invalid") from exc

        now = self._aware_now()
        if authorization.expires_at <= utc_iso(now):
            self._forget_poll(verifier)
            raise PlatformAPIError(400, "expired_token", "device code has expired")
        if authorization.state == DeviceAuthorizationState.PENDING:
            expires_at = datetime.fromisoformat(
                authorization.expires_at.replace("Z", "+00:00")
            )
            remaining_seconds = max(0.0, (expires_at - now).total_seconds())
            if self._poll_is_too_fast(
                verifier,
                authorization.poll_interval_seconds,
                remaining_seconds,
            ):
                raise PlatformAPIError(400, "slow_down", "polling too quickly")
            raise PlatformAPIError(400, "authorization_pending", "authorization is pending")
        if authorization.state == DeviceAuthorizationState.DENIED:
            self._forget_poll(verifier)
            raise PlatformAPIError(400, "access_denied", "authorization was denied")
        if authorization.state == DeviceAuthorizationState.EXPIRED:
            self._forget_poll(verifier)
            raise PlatformAPIError(400, "expired_token", "device code has expired")
        if authorization.state == DeviceAuthorizationState.CONSUMED:
            self._forget_poll(verifier)
            raise PlatformAPIError(400, "invalid_grant", "device code was already used")

        access_token = new_api_token()
        refresh_token = new_api_token()
        access_expiry = now + timedelta(seconds=self.policy.access_lifetime_seconds)
        refresh_expiry = now + timedelta(seconds=self.policy.refresh_lifetime_seconds)
        try:
            self.state.consume_device_authorization(
                device_code_hash=verifier,
                course_key=self.course_key,
                session_id=new_public_id("ses"),
                token_family_id=new_public_id("fam"),
                access_token_hash=self._digest("access", access_token),
                access_token_expires_at=access_expiry,
                refresh_token_hash=self._digest("refresh", refresh_token),
                refresh_token_expires_at=refresh_expiry,
                max_active_sessions=self.policy.max_active_sessions,
                max_daily_session_issuances=(
                    self.policy.max_daily_session_issuances
                ),
                max_retained_sessions=self.policy.max_retained_sessions,
                history_retention_seconds=(
                    self.policy.auth_history_retention_seconds
                ),
                at=now,
            )
        except DeviceAuthorizationExpired as exc:
            raise PlatformAPIError(400, "expired_token", "device code has expired") from exc
        except (PlatformAccessDenied, PlatformInvalidTransition) as exc:
            raise PlatformAPIError(400, "invalid_grant", "device code cannot be used") from exc
        except PlatformSessionLimitExceeded as exc:
            raise PlatformAPIError(
                429,
                "session_limit_exceeded",
                "session issuance limit reached; revoke an old device or try again later",
                headers={"Retry-After": "60"},
            ) from exc
        finally:
            with self._poll_lock:
                self._next_poll.pop(verifier, None)
        return self._token_response(access_token, refresh_token)

    def refresh_tokens(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        _strict_object(payload, allowed={"refresh_token"}, required={"refresh_token"})
        presented = _required_string(payload.get("refresh_token"), "refresh_token", maximum=256)
        verifier = self._digest("refresh", presented)
        with self._refresh_flights_lock:
            flight = self._refresh_flights.get(verifier)
            if flight is None:
                flight = _RefreshFlight()
                self._refresh_flights[verifier] = flight
                leader = True
            else:
                flight.followers += 1
                leader = False

        if not leader:
            flight.completed.wait()
            if flight.error is not None:
                if isinstance(flight.error, PlatformAPIError):
                    raise PlatformAPIError(
                        flight.error.status,
                        flight.error.code,
                        flight.error.safe_message,
                        headers=flight.error.headers,
                    ) from flight.error
                raise RuntimeError("concurrent token refresh failed") from flight.error
            if flight.response is None:
                raise RuntimeError("concurrent token refresh completed without a response")
            return dict(flight.response)

        try:
            response = self._rotate_refresh_token(verifier)
            flight.response = dict(response)
            return response
        except BaseException as exc:
            flight.error = exc
            raise
        finally:
            # Remove the entry before waking waiters.  A request which starts
            # after this durable rotation completed is not coalesced and remains
            # subject to strict refresh-token reuse detection.
            with self._refresh_flights_lock:
                if self._refresh_flights.get(verifier) is flight:
                    del self._refresh_flights[verifier]
            flight.completed.set()

    def _rotate_refresh_token(
        self,
        presented_verifier: str,
    ) -> Mapping[str, Any]:
        replacement = new_api_token()
        access_token = new_api_token()
        now = self._aware_now()
        try:
            session = self.state.rotate_refresh_token(
                presented_refresh_token_hash=presented_verifier,
                course_key=self.course_key,
                replacement_refresh_token_hash=self._digest("refresh", replacement),
                replacement_refresh_token_expires_at=now
                + timedelta(seconds=self.policy.refresh_lifetime_seconds),
                access_token_hash=self._digest("access", access_token),
                access_token_expires_at=now
                + timedelta(seconds=self.policy.access_lifetime_seconds),
                max_refresh_rotations=self.policy.max_refresh_rotations,
                at=now,
            )
        except RefreshTokenReuseDetected as exc:
            raise PlatformAPIError(401, "invalid_grant", "refresh token reuse detected") from exc
        except RefreshRotationLimitExceeded as exc:
            raise PlatformAPIError(
                401,
                "reauthentication_required",
                "session refresh limit reached; connect this device again",
            ) from exc
        except PlatformAccessDenied as exc:
            raise PlatformAPIError(401, "invalid_grant", "refresh token is invalid") from exc
        access_expiry = datetime.fromisoformat(
            session.access_token_expires_at.replace("Z", "+00:00")
        )
        expires_in = max(1, int((access_expiry - now).total_seconds()))
        return self._token_response(
            access_token,
            replacement,
            expires_in=expires_in,
        )

    def list_student_sessions(
        self,
        *,
        student_key: str,
        limit: int = 100,
    ) -> Mapping[str, Any]:
        """Return a bounded, credential-free session list for the local operator CLI."""

        student = self.state.get_student_by_key(
            _required_string(student_key, "student_key", maximum=255)
        )
        sessions = self.state.list_student_sessions(
            student_id=student.id,
            course_key=self.course_key,
            limit=limit,
        )
        now = utc_iso(self._aware_now())
        return {
            "student_key": student.student_key,
            "course_key": self.course_key,
            "count": len(sessions),
            "sessions": [
                self._operator_session_projection(session, now) for session in sessions
            ],
        }

    def revoke_student_session(
        self,
        *,
        student_key: str,
        session_id: str,
    ) -> Mapping[str, Any]:
        """Revoke one course session for a student through the local operator CLI."""

        student = self.state.get_student_by_key(
            _required_string(student_key, "student_key", maximum=255)
        )
        now = self._aware_now()
        session = self.state.revoke_session(
            session_id=self._safe_identifier(session_id, "session_id"),
            course_key=self.course_key,
            owner_student_id=student.id,
            at=now,
        )
        return {
            "student_key": student.student_key,
            "course_key": self.course_key,
            "session": self._operator_session_projection(session, utc_iso(now)),
        }

    def reset_student_sessions(self, *, student_key: str) -> Mapping[str, Any]:
        """Revoke every retained course session after a student loses credentials."""

        student = self.state.get_student_by_key(
            _required_string(student_key, "student_key", maximum=255)
        )
        revoked = self.state.revoke_student_sessions(
            student_id=student.id,
            course_key=self.course_key,
            at=self._aware_now(),
        )
        return {
            "student_key": student.student_key,
            "course_key": self.course_key,
            "revoked": revoked,
        }

    def revoke_current(self, access_token: str) -> None:
        session, _student = self._authorize(access_token)
        self.state.revoke_session(
            session_id=session.session_id,
            course_key=self.course_key,
            owner_student_id=session.student_id,
            at=self._aware_now(),
        )
        return None

    def get_me(self, access_token: str) -> Mapping[str, Any]:
        session, student = self._authorize(access_token)
        response: MutableMapping[str, Any] = {
            "student_key": student.student_key,
            "identity": {"kind": student.identity_kind.value},
            "course_key": self.course_key,
            "session": {
                "id": session.session_id,
                "device_label": session.device_label,
                "expires_at": session.access_token_expires_at,
            },
        }
        if student.identity_kind == StudentIdentityKind.GITHUB:
            response["github"] = {
                "id": student.github_user_id,
                "login": student.github_login,
            }
        return response

    def list_sessions(self, access_token: str) -> Mapping[str, Any]:
        current, _student = self._authorize(access_token)
        try:
            sessions = self.state.list_owned_sessions(
                access_token_hash=self._access_verifier(access_token),
                course_key=self.course_key,
                limit=self.policy.session_list_limit,
                at=self._aware_now(),
            )
        except PlatformAccessDenied as exc:
            raise PlatformAPIError(401, "invalid_token", "access token is invalid or expired") from exc
        return {
            "sessions": [
                self._session_projection(session, current.session_id)
                for session in sessions
            ]
        }

    def revoke_session(self, access_token: str, session_id: str) -> None:
        self._authorize(access_token)
        try:
            self.state.revoke_owned_session(
                access_token_hash=self._access_verifier(access_token),
                course_key=self.course_key,
                session_id=self._safe_identifier(session_id, "session_id"),
                at=self._aware_now(),
            )
        except (PlatformNotFound, PlatformAccessDenied) as exc:
            # Do not reveal whether a session belongs to another student.
            raise PlatformAPIError(404, "not_found", "session was not found") from exc
        return None

    def require_assignment_acceptance(self, access_token: str) -> tuple[str, str]:
        """Student portal credentials must come from an accepted assignment."""
        session, _student = self._authorize(access_token)
        scope = self.state.get_session_assignment_scope(
            session_id=session.session_id, course_key=self.course_key
        )
        if scope is None:
            raise PlatformAPIError(403, "assignment_acceptance_required",
                                   "학생 웹에서 수령 코드를 발급받아 과제를 수락해 주세요.")
        return scope

    def list_accepted_assignments(self, access_token: str) -> Mapping[str, Any]:
        """Only the current claim session's accepted assignment, never a catalog."""
        try:
            self.require_assignment_acceptance(access_token)
        except PlatformAPIError as exc:
            if exc.code != "assignment_acceptance_required":
                raise
            return {"assignments": []}
        return self.list_assignments(access_token)

    def list_assignments(self, access_token: str) -> Mapping[str, Any]:
        session, _student = self._authorize(access_token)
        assignment_scope = self.state.get_session_assignment_scope(
            session_id=session.session_id, course_key=self.course_key
        )
        verifier = self._access_verifier(access_token)
        try:
            assignments = self.state.list_owned_assignments(
                access_token_hash=verifier,
                course_key=self.course_key,
                available_only=True,
                at=self._aware_now(),
            )
        except PlatformAccessDenied as exc:
            raise PlatformAPIError(403, "access_denied", "course enrollment is inactive") from exc
        projected = []
        for assignment in assignments:
            if assignment_scope is not None and assignment_scope != (
                "git",
                assignment.assignment_id,
            ):
                continue
            item = dict(self._assignment_projection(assignment))
            latest = self.state.get_latest_owned_submission(
                access_token_hash=verifier,
                course_key=self.course_key,
                assignment_id=assignment.assignment_id,
                at=self._aware_now(),
            )
            if latest is not None:
                item["latest_submission"] = self._submission_projection(latest)
            projected.append(item)

        try:
            bundle_assignments = self.state.list_owned_bundle_assignments(
                access_token_hash=verifier,
                course_key=self.course_key,
                available_only=True,
                at=self._aware_now(),
            )
        except PlatformAccessDenied as exc:
            raise PlatformAPIError(
                403, "access_denied", "course enrollment is inactive"
            ) from exc
        for assignment in bundle_assignments:
            if assignment_scope is not None and assignment_scope != (
                "bundle",
                assignment.assignment_id,
            ):
                continue
            item = dict(self._bundle_assignment_projection(assignment))
            latest = self.state.get_latest_owned_bundle_submission(
                access_token_hash=verifier,
                course_key=self.course_key,
                assignment_id=assignment.assignment_id,
                at=self._aware_now(),
            )
            if latest is not None:
                item["latest_submission"] = self._bundle_submission_projection(latest)
            projected.append(item)
        projected.sort(
            key=lambda item: (
                str(item["assignment_key"]),
                str(item["release_id"]),
                str(item["assignment_id"]),
            )
        )
        return {"assignments": projected}

    def get_bundle_starter(
        self, access_token: str, assignment_id: str
    ) -> PlatformFileResponse:
        """Authorize and return one immutable starter archive."""

        assignment = self._owned_bundle_assignment(access_token, assignment_id)
        if self.bundle_store is None:
            raise PlatformAPIError(
                503,
                "bundle_delivery_unavailable",
                "assignment download is temporarily unavailable",
            )
        try:
            artifact = self.bundle_store.get(assignment.starter_digest)
        except (BundleStorageError, OSError) as exc:
            raise PlatformAPIError(
                503,
                "bundle_delivery_unavailable",
                "assignment download is temporarily unavailable",
            ) from exc
        try:
            self.state.record_bundle_download(
                download_id=new_public_id("bdl"),
                access_token_hash=self._access_verifier(access_token),
                course_key=self.course_key,
                assignment_id=assignment.assignment_id,
                at=self._aware_now(),
            )
        except PlatformAccessDenied as exc:
            raise PlatformAPIError(
                403, "access_denied", "assignment is not available"
            ) from exc
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", assignment.assignment_key).strip(".-")
        if not safe_name:
            safe_name = "assignment"
        return PlatformFileResponse(
            200,
            artifact.path,
            "application/gzip",
            {
                "Content-Disposition": f'attachment; filename="{safe_name}.tar.gz"',
                "ETag": f'"{artifact.archive_sha256}"',
                "X-Autograde-SHA256": artifact.archive_sha256,
            },
        )

    def submit_bundle(
        self,
        access_token: str,
        idempotency_key: str,
        assignment_id: str,
        upload: BinaryIO,
        content_length: int,
    ) -> Mapping[str, Any]:
        """Persist one bounded source bundle before acknowledging admission."""

        safe_assignment_id = self._safe_identifier(assignment_id, "assignment_id")
        safe_idempotency_key = _required_string(
            idempotency_key, "Idempotency-Key", maximum=128
        )
        if (
            isinstance(content_length, bool)
            or not isinstance(content_length, int)
            or content_length <= 0
        ):
            raise PlatformAPIError(
                400, "invalid_request", "submission bundle size is invalid"
            )
        assignment = self._owned_bundle_assignment(access_token, safe_assignment_id)
        if self.bundle_store is None:
            raise PlatformAPIError(
                503,
                "bundle_submission_unavailable",
                "submission upload is temporarily unavailable",
            )
        # A bundle assignment is course-wide, so locking only by assignment
        # would serialize every student's upload.  Stripe by the authenticated
        # session verifier as well; SQLite/CAS constraints remain the
        # cross-session source of truth for semantic/idempotent duplicates.
        lock_material = (
            self._access_verifier(access_token)
            + "\0"
            + safe_assignment_id
        ).encode("ascii", "strict")
        lock_index = int.from_bytes(
            hashlib.sha256(lock_material).digest()[:8], "big"
        ) % len(self._submission_locks)
        with self._submission_locks[lock_index]:
            response = self._submit_bundle_unlocked(
                access_token=access_token,
                idempotency_key=safe_idempotency_key,
                assignment=assignment,
                upload=upload,
                content_length=content_length,
            )
        if self.notify_bundle_submission is not None and not bool(response["replayed"]):
            submission_id = response["submission"]["submission_id"]
            try:
                notified = self.notify_bundle_submission(str(submission_id))
                if notified is False:
                    emit_operator_event(
                        "bundle_submission_notify_rejected",
                        component="worker",
                        submission_id=submission_id,
                    )
            except Exception as exc:
                emit_operator_event(
                    "bundle_submission_notify_failed",
                    component="worker",
                    exception=exc,
                    submission_id=submission_id,
                )
        return response

    def _submit_bundle_unlocked(
        self,
        *,
        access_token: str,
        idempotency_key: str,
        assignment: BundleAssignmentRelease,
        upload: BinaryIO,
        content_length: int,
    ) -> Mapping[str, Any]:
        assert self.bundle_store is not None
        try:
            artifact = self.bundle_store.ingest(upload, expected_kind="submission")
        except BundleLimitError as exc:
            raise PlatformAPIError(
                413, "payload_too_large", "submission bundle exceeds service limits"
            ) from exc
        except UnsafeBundleError as exc:
            raise PlatformAPIError(
                422, "invalid_bundle", "submission bundle is invalid or unsafe"
            ) from exc
        except (BundleStorageError, OSError) as exc:
            raise PlatformAPIError(
                503,
                "bundle_submission_unavailable",
                "submission upload is temporarily unavailable",
            ) from exc
        if artifact.compressed_bytes != content_length:
            raise PlatformAPIError(
                400, "invalid_request", "submission bundle size changed during upload"
            )
        canonical = {
            "assignment_id": assignment.assignment_id,
            "release_id": assignment.release_id,
            "source_digest": artifact.archive_sha256,
            "source_size_bytes": artifact.compressed_bytes,
        }
        request_hash = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        try:
            outcome = self.state.create_accepted_bundle_submission(
                submission_id=new_public_id("bsub"),
                receipt_id=new_public_id("brcp"),
                access_token_hash=self._access_verifier(access_token),
                course_key=self.course_key,
                assignment_id=assignment.assignment_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                source_path=str(artifact.path),
                source_digest=artifact.archive_sha256,
                source_size_bytes=artifact.compressed_bytes,
                endpoint=f"/v1/assignments/{assignment.assignment_id}/submissions",
                max_outstanding_per_student=(
                    self.submission_policy.max_outstanding_per_student
                ),
                max_daily_per_student=self.submission_policy.max_daily_per_student,
                at=self._aware_now(),
            )
        except PlatformIdempotencyConflict as exc:
            raise PlatformAPIError(
                409, "idempotency_conflict", "idempotency key was reused"
            ) from exc
        except PlatformSubmissionLimitExceeded as exc:
            raise PlatformAPIError(
                429,
                "submission_limit_exceeded",
                "submission limit reached; wait for grading or try again later",
                headers={"Retry-After": "60"},
            ) from exc
        except PlatformAccessDenied as exc:
            now = utc_iso(self._aware_now())
            if assignment.due_at is not None and now >= assignment.due_at:
                raise PlatformAPIError(
                    403, "assignment_closed", "assignment deadline has passed"
                ) from exc
            raise PlatformAPIError(
                403, "access_denied", "submission is not allowed"
            ) from exc
        return {
            "submission": self._bundle_submission_projection(outcome.request),
            "replayed": outcome.replayed,
        }

    def get_assignment_repository(
        self, access_token: str, assignment_id: str
    ) -> Mapping[str, Any]:
        assignment = self._owned_assignment(access_token, assignment_id)
        return {"repository": self._repository_projection(assignment)}

    def submit(
        self,
        access_token: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        repository_id = payload.get("github_repository_id")
        stripe_key = (
            repository_id
            if isinstance(repository_id, int) and not isinstance(repository_id, bool)
            else 0
        )
        lock = self._submission_locks[stripe_key % len(self._submission_locks)]
        with lock:
            response = self._submit_unlocked(access_token, idempotency_key, payload)
        if self.notify_submission is not None and not bool(response["replayed"]):
            submission_id = response["submission"]["submission_id"]
            try:
                notification_result = self.notify_submission(submission_id)
                if notification_result is False:
                    emit_operator_event(
                        "submission_notify_rejected",
                        component="worker",
                        submission_id=submission_id,
                    )
            except Exception as exc:
                emit_operator_event(
                    "submission_notify_failed",
                    component="worker",
                    exception=exc,
                    submission_id=submission_id,
                )
        return response

    def _submit_unlocked(
        self,
        access_token: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        _strict_object(
            payload,
            allowed={
                "assignment_id",
                "github_repository_id",
                "head_sha",
                "pull_request_number",
            },
            required={"assignment_id", "github_repository_id", "head_sha"},
        )
        assignment_id = _required_string(payload.get("assignment_id"), "assignment_id")
        idempotency_key = _required_string(idempotency_key, "Idempotency-Key", maximum=128)
        repository_id = payload.get("github_repository_id")
        if isinstance(repository_id, bool) or not isinstance(repository_id, int) or repository_id <= 0:
            raise PlatformAPIError(400, "invalid_request", "github_repository_id is invalid")
        try:
            head_sha = git_oid(_required_string(payload.get("head_sha"), "head_sha", maximum=64))
        except ValueError as exc:
            raise PlatformAPIError(400, "invalid_request", "head_sha must be a full Git object id") from exc
        pr_number = payload.get("pull_request_number")
        if pr_number is not None and (
            isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0
        ):
            raise PlatformAPIError(400, "invalid_request", "pull_request_number is invalid")

        assignment = self._owned_assignment(access_token, assignment_id)
        canonical = {
            "assignment_id": assignment_id,
            "github_repository_id": repository_id,
            "head_sha": head_sha,
        }
        if pr_number is not None:
            canonical["pull_request_number"] = pr_number
        request_hash = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        access_verifier = self._access_verifier(access_token)

        # Exact idempotency and already-admitted semantic duplicates are
        # resolved before deadline checks or network work.  This preserves the
        # original response after a deadline or later branch movement.
        preflight_at = self._aware_now()
        try:
            replay = self.state.find_submission_replay(
                access_token_hash=access_verifier,
                course_key=self.course_key,
                assignment_id=assignment_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                github_repository_id=repository_id,
                requested_sha=head_sha,
                pull_request_number=pr_number,
                max_outstanding_per_student=(
                    self.submission_policy.max_outstanding_per_student
                ),
                max_daily_per_student=self.submission_policy.max_daily_per_student,
                at=preflight_at,
            )
        except PlatformIdempotencyConflict as exc:
            raise PlatformAPIError(409, "idempotency_conflict", "idempotency key was reused") from exc
        except PlatformAccessDenied as exc:
            raise PlatformAPIError(403, "access_denied", "submission is not allowed") from exc
        except PlatformSubmissionLimitExceeded as exc:
            raise PlatformAPIError(
                429,
                "submission_limit_exceeded",
                "submission limit reached; wait for grading or try again later",
                headers={"Retry-After": "60"},
            ) from exc
        if replay is not None:
            return {
                "submission": self._submission_projection(replay.request),
                "replayed": True,
            }

        if assignment.github_repository_id != repository_id:
            raise PlatformAPIError(
                403,
                "access_denied",
                "repository is not assigned to this student assignment",
            )
        if assignment.submission_mode == SubmissionMode.PULL_REQUEST:
            if pr_number is None:
                raise PlatformAPIError(
                    400, "invalid_request", "pull_request_number is required"
                )
        elif pr_number is not None:
            raise PlatformAPIError(
                400, "invalid_request", "pull_request_number is not allowed"
            )

        started_at = self._aware_now()
        if assignment.opens_at is not None and utc_iso(started_at) < assignment.opens_at:
            raise PlatformAPIError(403, "assignment_not_open", "assignment is not open")
        if assignment.due_at is not None and utc_iso(started_at) >= assignment.due_at:
            raise PlatformAPIError(403, "assignment_closed", "assignment deadline has passed")
        if self.submission_pinner is None:
            raise PlatformAPIError(
                503,
                "submission_pinning_unavailable",
                "submission collection is temporarily unavailable",
            )

        submission_id = new_public_id("sub")
        try:
            pinned = self.submission_pinner.pin(
                assignment=assignment,
                requested_sha=head_sha,
                submission_id=submission_id,
            )
        except SubmissionSourceUnavailable as exc:
            raise PlatformAPIError(
                409,
                "source_changed_or_unavailable",
                "push the submitted commit to the configured branch and try again",
            ) from exc
        except SubmissionSourceInvalid as exc:
            raise PlatformAPIError(
                422,
                exc.code,
                exc.public_message,
            ) from exc
        except SubmissionPinInfrastructureError as exc:
            raise PlatformAPIError(
                503,
                "repository_collection_failed",
                "the service could not preserve the submitted commit",
            ) from exc
        if pinned.commit_sha != head_sha:
            raise PlatformAPIError(
                503,
                "repository_collection_failed",
                "the service could not preserve the submitted commit",
            )

        # This timestamp is deliberately taken after remote observation and
        # snapshot completion.  The state layer checks the deadline again in
        # the same transaction that writes request + immutable receipt.
        accepted_at = self._aware_now()
        try:
            outcome = self.state.create_accepted_submission(
                submission_id=submission_id,
                receipt_id=new_public_id("rcp"),
                access_token_hash=access_verifier,
                course_key=self.course_key,
                assignment_id=assignment_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                github_repository_id=repository_id,
                requested_sha=pinned.commit_sha,
                pull_request_number=pr_number,
                source_path=pinned.source_path,
                source_digest=pinned.source_digest,
                snapshot_key=pinned.snapshot_key,
                max_outstanding_per_student=(
                    self.submission_policy.max_outstanding_per_student
                ),
                max_daily_per_student=self.submission_policy.max_daily_per_student,
                reserved_attempt_day=utc_iso(preflight_at)[:10],
                at=accepted_at,
            )
        except PlatformIdempotencyConflict as exc:
            raise PlatformAPIError(409, "idempotency_conflict", "idempotency key was reused") from exc
        except PlatformSubmissionLimitExceeded as exc:
            raise PlatformAPIError(
                429,
                "submission_limit_exceeded",
                "submission limit reached; wait for grading or try again later",
                headers={"Retry-After": "60"},
            ) from exc
        except PlatformAccessDenied as exc:
            if assignment.due_at is not None and utc_iso(accepted_at) >= assignment.due_at:
                raise PlatformAPIError(
                    403, "assignment_closed", "assignment deadline has passed"
                ) from exc
            raise PlatformAPIError(403, "access_denied", "submission is not allowed") from exc

        return {"submission": self._submission_projection(outcome.request), "replayed": outcome.replayed}

    def get_bundle_history(self, access_token: str, assignment_id: str) -> Mapping[str, Any]:
        assignment = self._owned_bundle_assignment(access_token, assignment_id)
        requests = self.state.list_owned_bundle_submissions(
            access_token_hash=self._access_verifier(access_token), course_key=self.course_key,
            assignment_id=assignment.assignment_id, at=self._aware_now(),
        )
        # Grades remain behind get_result's disclosure policy.
        return {"submissions": [self._bundle_submission_projection(row) for row in requests[:100]],
                "has_more": len(requests) > 100, "limit": 100}

    def get_bundle_source(self, access_token: str, submission_id: str) -> PlatformFileResponse:
        submission = self.get_submission(access_token, submission_id)["submission"]
        if submission.get("delivery_mode") != "bundle":
            raise PlatformAPIError(404, "not_found", "submission source was not found")
        try:
            self.state.get_owned_bundle_receipt(
                access_token_hash=self._access_verifier(access_token), course_key=self.course_key,
                submission_id=submission["submission_id"], at=self._aware_now(),
            )
        except PlatformNotFound as exc:
            raise PlatformAPIError(404, "not_found", "accepted source was not found") from exc
        if self.bundle_store is None:
            raise PlatformAPIError(503, "bundle_delivery_unavailable", "submission source is unavailable")
        try:
            artifact = self.bundle_store.get(submission["source_sha256"])
        except (BundleStorageError, OSError) as exc:
            raise PlatformAPIError(503, "bundle_delivery_unavailable", "submission source is unavailable") from exc
        return PlatformFileResponse(200, artifact.path, "application/gzip", {
            "Content-Disposition": f'attachment; filename="{submission["submission_id"]}.tar.gz"',
            "ETag": f'"{artifact.archive_sha256}"',
            "X-Autograde-SHA256": artifact.archive_sha256,
        })

    def get_submission(self, access_token: str, submission_id: str) -> Mapping[str, Any]:
        session, _student = self._authorize(access_token)
        safe_id = self._safe_identifier(submission_id, "submission_id")
        if safe_id.startswith("bsub_"):
            try:
                request = self.state.get_owned_bundle_submission(
                    access_token_hash=self._access_verifier(access_token),
                    course_key=self.course_key,
                    submission_id=safe_id,
                    at=self._aware_now(),
                )
            except PlatformNotFound as exc:
                raise PlatformAPIError(
                    404, "not_found", "submission was not found"
                ) from exc
            self._enforce_session_assignment_scope(
                session, "bundle", request.assignment_id
            )
            return {"submission": self._bundle_submission_projection(request)}
        try:
            request = self.state.get_owned_submission(
                access_token_hash=self._access_verifier(access_token),
                course_key=self.course_key,
                submission_id=safe_id,
                at=self._aware_now(),
            )
        except PlatformNotFound as exc:
            raise PlatformAPIError(404, "not_found", "submission was not found") from exc
        self._enforce_session_assignment_scope(session, "git", request.assignment_id)
        return {"submission": self._submission_projection(request)}

    def get_result(self, access_token: str, submission_id: str) -> Mapping[str, Any]:
        session, _student = self._authorize(access_token)
        safe_id = self._safe_identifier(submission_id, "submission_id")
        verifier = self._access_verifier(access_token)
        if safe_id.startswith("bsub_"):
            try:
                result = self.state.get_owned_bundle_result(
                    access_token_hash=verifier,
                    course_key=self.course_key,
                    submission_id=safe_id,
                    at=self._aware_now(),
                )
                receipt = self.state.get_owned_bundle_receipt(
                    access_token_hash=verifier,
                    course_key=self.course_key,
                    submission_id=safe_id,
                    at=self._aware_now(),
                )
            except PlatformNotFound as exc:
                raise PlatformAPIError(
                    404,
                    "result_not_available",
                    "published result is not available",
                ) from exc
            self._enforce_session_assignment_scope(
                session, "bundle", receipt.assignment_id
            )
            projection = self._bundle_result_projection(result)
            if receipt.result_policy == ResultPolicy.SCORE_ONLY:
                projection["rubric"] = {}
                projection["diagnostics"] = []
            return {"result": projection}
        try:
            result = self.state.get_owned_result(
                access_token_hash=verifier,
                course_key=self.course_key,
                submission_id=safe_id,
                at=self._aware_now(),
            )
            request = self.state.get_owned_submission(
                access_token_hash=verifier,
                course_key=self.course_key,
                submission_id=safe_id,
                at=self._aware_now(),
            )
            assignment = self.state.get_owned_assignment(
                access_token_hash=verifier,
                course_key=self.course_key,
                assignment_id=request.assignment_id,
                require_available=False,
                at=self._aware_now(),
            )
        except PlatformNotFound as exc:
            raise PlatformAPIError(404, "result_not_available", "published result is not available") from exc
        self._enforce_session_assignment_scope(
            session, "git", request.assignment_id
        )
        projection = self._result_projection(result)
        if assignment.result_policy == ResultPolicy.SCORE_ONLY:
            projection["rubric"] = {}
            projection["diagnostics"] = []
        return {"result": projection}

    # Instructor dashboard -------------------------------------------

    def instructor_dashboard(self, authorization: str) -> Mapping[str, Any]:
        """Return a credential-free operational roster/assignment matrix."""

        self._authorize_instructor(authorization)
        rows = self.state.list_bundle_dashboard_rows(course_key=self.course_key)
        try:
            course_summary = self.state.get_course_summary(course_key=self.course_key)
        except PlatformNotFound:
            course_summary = {
                "course_key": self.course_key,
                "enrolled_students": 0,
                "active_students": 0,
                "assignments": 0,
                "acceptances": 0,
                "submissions": 0,
            }
        student_summaries = self.state.list_course_student_summaries(
            course_key=self.course_key
        )
        projected_rows = [self._dashboard_projection(row) for row in rows]
        claim_assignments = self.state.list_operator_bundle_assignments(
            course_key=self.course_key,
            ready_only=True,
            active_only=True,
        )
        now = utc_iso(self._aware_now())
        assignments = [
            {
                "assignment_id": assignment.assignment_id,
                "assignment_key": assignment.assignment_key,
                "release_id": assignment.release_id,
                "title": assignment.title,
                "claim_url": self._assignment_claim_url(assignment.assignment_id),
            }
            for assignment in claim_assignments
            if (
                assignment.opens_at is None or assignment.opens_at <= now
            )
            and (assignment.due_at is None or assignment.due_at > now)
        ]
        return {
            "course_key": self.course_key,
            "generated_at": utc_iso(self._aware_now()),
            "course": course_summary,
            "students": student_summaries,
            "assignments": assignments,
            "rows": projected_rows,
        }

    def instructor_dashboard_page(self, authorization: str) -> PlatformResponse:
        dashboard = self.instructor_dashboard(authorization)
        rows = dashboard["rows"]
        course = dashboard["course"]
        student_management_rows = []
        for student in dashboard["students"]:
            password_state = "미설정"
            if student["password_reset_required"]:
                password_state = "재설정 필요 (숫자 6자리)"
            elif student["password_configured"]:
                locked_until = student["password_locked_until"]
                password_state = (
                    f"잠김 ({locked_until})"
                    if locked_until and locked_until > dashboard["generated_at"]
                    else "설정됨"
                )
            student_management_rows.append(
                "<tr>"
                f"<td>{html.escape(str(student['student_key']))}</td>"
                f"<td>{'활성' if student['active'] else '비활성'}</td>"
                f"<td>{html.escape(password_state)}</td>"
                f"<td>{int(student['acceptances'])}</td>"
                f"<td>{int(student['downloads'])}</td>"
                f"<td>{int(student['submissions'])}</td>"
                "</tr>"
            )
        assignment_cards = []
        for assignment in dashboard["assignments"]:
            claim_url = str(assignment["claim_url"])
            title = str(assignment["title"])
            assignment_cards.append(
                "<section>"
                f"<h2>{html.escape(title)}</h2>"
                f"<p>{html.escape(str(assignment['assignment_key']))} · "
                f"{html.escape(str(assignment['release_id']))}</p>"
                f"<figure role=\"img\" aria-label=\"{html.escape(title, quote=True)} "
                "과제 수령 페이지 QR 코드\">"
                f"{assignment_claim_qr_svg(claim_url)}"
                "</figure>"
                f"<p><a href=\"{html.escape(claim_url, quote=True)}\">"
                f"{html.escape(claim_url)}</a></p>"
                "<p>QR에는 학생 정보나 수령 코드가 포함되지 않습니다.</p>"
                "</section>"
            )
        table_rows = []
        for row in rows:
            score = "—"
            if row["score"] is not None:
                score = f"{row['score']:g} / {row['max_score']:g}"
            table_rows.append(
                "<tr>"
                f"<td>{html.escape(str(row['student_key']))}</td>"
                f"<td>{html.escape(str(row['assignment_key']))} · "
                f"{html.escape(str(row['title']))}</td>"
                f"<td>{html.escape(str(row['release_id']))}</td>"
                f"<td>{int(row['acceptance_count'])}</td>"
                f"<td>{int(row['download_count'])}</td>"
                f"<td>{int(row['submission_count'])}</td>"
                f"<td>{html.escape(str(row['state'] or '미제출'))}</td>"
                f"<td>{html.escape(score)}</td>"
                f"<td>{html.escape(str(row['latest_received_at'] or '—'))}</td>"
                "</tr>"
            )
        body = (
            "<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{html.escape(self.course_key)} 교수자 관리</title>"
            "<style>body{margin:0;background:#f3f5f8;color:#18283a;font:15px/1.6 system-ui}"
            "body>nav{padding:14px 24px;background:#16384c;color:white}body>nav a{color:white;margin-right:18px}"
            "main{max-width:1180px;margin:24px auto;padding:24px;background:white;border-radius:12px}"
            ".audience{color:#17615b;font-weight:600}h1{margin:6px 0 12px}h2{margin-top:28px;font-size:18px}"
            "a{color:#185b94}table{display:block;max-width:100%;overflow-x:auto;border-collapse:collapse}"
            "th,td{text-align:left;padding:10px 14px;border-bottom:1px solid #dce4ec;white-space:nowrap}"
            "th{background:#eef3f7;font-weight:500}"
            "@media(max-width:600px){main{padding:16px;margin:12px}body>nav{padding:12px}}"
            "</style></head><body><main data-audience=\"instructor\">"
            "<div class=\"audience\">교수자 관리 · 교과목 전체 현황</div>"
            f"<h1>{html.escape(self.course_key)} 채점 현황</h1>"
            f"<p>갱신 시각: {html.escape(str(dashboard['generated_at']))} · "
            "<a href=\"/instructor\">새로 고침</a></p>"
            "<h2>교과목 요약</h2>"
            f"<p>등록 학생 {int(course['enrolled_students'])}명 · "
            f"활성 학생 {int(course['active_students'])}명 · "
            f"과제 {int(course['assignments'])}개 · "
            f"수락 {int(course['acceptances'])}건 · "
            f"제출 {int(course['submissions'])}건</p>"
            "<h2>학생 관리</h2>"
            "<table><thead><tr><th>학생</th><th>상태</th><th>전용 비밀번호</th>"
            "<th>수락</th><th>다운로드</th><th>제출</th></tr></thead><tbody>"
            + "".join(student_management_rows)
            + "</tbody></table>"
            + (
                "<p>등록된 학생이 없습니다.</p>"
                if not student_management_rows
                else ""
            )
            + "<h2>과제 수령 QR</h2>"
            + "".join(assignment_cards)
            + ("<p>공개된 bundle 과제가 없습니다.</p>" if not assignment_cards else "")
            + "<table><thead><tr><th>학생</th><th>과제</th><th>릴리스</th>"
            "<th>수락</th><th>다운로드</th><th>제출</th><th>최신 상태</th><th>점수</th>"
            "<th>최근 제출</th></tr></thead><tbody>"
            + "".join(table_rows)
            + "</tbody></table>"
            + ("<p>등록된 학생 또는 과제가 없습니다.</p>" if not table_rows else "")
            + "</main></body></html>"
        )
        return PlatformResponse(
            200, body, {"Content-Type": "text/html; charset=utf-8"}
        )

    def _authorize_instructor(self, authorization: str) -> None:
        challenge = {"WWW-Authenticate": 'Basic realm="Autograde instructor", charset="UTF-8"'}
        if self._instructor_token is None:
            raise PlatformAPIError(
                503,
                "dashboard_unavailable",
                "instructor dashboard is not configured",
            )
        candidate = ""
        try:
            if not isinstance(authorization, str):
                raise ValueError("invalid basic authorization")
            scheme, encoded = authorization.split(" ", 1)
            if scheme != "Basic" or not encoded or len(encoded) > 1024:
                raise ValueError("invalid basic authorization")
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8", "strict")
            username, separator, candidate = decoded.partition(":")
            if separator != ":" or username != "instructor":
                candidate = ""
        except (ValueError, UnicodeDecodeError):
            candidate = ""
        if not hmac.compare_digest(
            candidate.encode("utf-8"), self._instructor_token.encode("ascii")
        ):
            raise PlatformAPIError(
                401,
                "instructor_auth_required",
                "instructor credentials are required",
                headers=challenge,
            )

    def _dashboard_projection(self, row: Any) -> Mapping[str, Any]:
        return {
            "student_key": row.student_key,
            "assignment_id": row.assignment_id,
            "assignment_key": row.assignment_key,
            "release_id": row.release_id,
            "title": row.title,
            "download_count": row.download_count,
            "first_downloaded_at": row.first_downloaded_at,
            "last_downloaded_at": row.last_downloaded_at,
            "acceptance_count": row.acceptance_count,
            "latest_claim_accepted_at": row.latest_claim_accepted_at,
            "submission_count": row.submission_count,
            "latest_submission_id": row.latest_submission_id,
            "state": row.latest_state.value if row.latest_state is not None else None,
            "latest_received_at": row.latest_received_at,
            "latest_accepted_at": row.latest_accepted_at,
            "score": row.latest_score,
            "max_score": row.max_score,
            "published_at": row.latest_published_at,
            "claim_url": self._assignment_claim_url(row.assignment_id),
        }

    def _assignment_claim_url(self, assignment_id: str) -> str:
        return (
            f"{self.public_base_url}/assignment-claim/"
            f"{quote(assignment_id, safe='')}"
        )

    # Password-backed assignment claims ------------------------------

    def set_student_password(
        self, *, student_key: str, password: str
    ) -> Mapping[str, Any]:
        """Set one course-scoped Autograde password from a trusted CLI."""

        student = self.state.get_student_by_key(
            _required_string(student_key, "student_key", maximum=255)
        )
        credential = self.state.set_student_password_hash(
            student_id=student.id,
            course_key=self.course_key,
            password_hash=hash_student_password(password),
            at=self._aware_now(),
        )
        return {
            "student_key": credential.student_key,
            "course_key": credential.course_key,
            "password_configured": True,
            "password_reset_required": False,
            "updated_at": credential.updated_at,
        }

    def assignment_claim_page(
        self, query: Mapping[str, str]
    ) -> PlatformResponse:
        """Render a CSRF-bound password form; the URL contains no secret."""

        self._require_secure_password_claim_transport()
        _strict_object(
            query,
            allowed={"assignment_id"},
            required={"assignment_id"},
        )
        assignment_id = self._safe_identifier(
            _required_string(
                query.get("assignment_id"), "assignment_id", maximum=256
            ),
            "assignment_id",
        )
        assignment_label = self._assignment_claim_display_name(assignment_id)
        csrf = new_api_token()
        signed_entry = sign_browser_value(
            self._secret,
            "assignment-claim-entry",
            {
                "assignment_id": assignment_id,
                "course_key": self.course_key,
                "csrf": csrf,
            },
            lifetime_seconds=self.policy.assignment_claim_lifetime_seconds,
            now=int(self._aware_now().timestamp()),
        )
        body = self._html_page(
            "과제 수령 코드 발급",
            "<h1>과제 수령 코드(과제 키) 발급</h1>"
            f"<p>교과목: <strong>{html.escape(self.course_key)}</strong><br>"
            f"과제: <strong>{html.escape(assignment_label)}</strong></p>"
            "<p>학교 SSO 비밀번호가 아니라 이 서비스에 별도로 등록한 "
            "<strong>Autograde 전용 비밀번호</strong>를 입력하세요.</p>"
            "<form method=\"post\" action=\"/assignment-claim/issue\">"
            f"<input type=\"hidden\" name=\"assignment_id\" "
            f"value=\"{html.escape(assignment_id, quote=True)}\">"
            f"<input type=\"hidden\" name=\"csrf\" "
            f"value=\"{html.escape(csrf, quote=True)}\">"
            "<label>학번 <input name=\"student_key\" autocomplete=\"username\" "
            "maxlength=\"255\" required></label>"
            "<label>Autograde 전용 비밀번호 <input type=\"password\" "
            "name=\"password\" autocomplete=\"current-password\" "
            "inputmode=\"numeric\" pattern=\"[0-9]{6}\" "
            "minlength=\"6\" maxlength=\"6\" required></label>"
            "<p><small>Autograde 전용 비밀번호는 숫자 6자리입니다. "
            "학교 포털 비밀번호를 입력하지 마세요.</small></p>"
            "<button type=\"submit\">수령 코드 발급</button></form>",
        )
        return PlatformResponse(
            200,
            body,
            {
                "Content-Type": "text/html; charset=utf-8",
                "Set-Cookie": self._assignment_claim_cookie(signed_entry),
            },
        )

    def issue_assignment_claim(
        self, form: Mapping[str, str], cookies: Mapping[str, str]
    ) -> PlatformResponse:
        """Authenticate a student and display one raw claim code exactly once."""

        self._require_secure_password_claim_transport()
        _strict_object(
            form,
            allowed={"student_key", "password", "assignment_id", "csrf"},
            required={"student_key", "password", "assignment_id", "csrf"},
        )
        assignment_id = self._safe_identifier(
            _required_string(
                form.get("assignment_id"), "assignment_id", maximum=256
            ),
            "assignment_id",
        )
        student_key = _required_string(
            form.get("student_key"), "student_key", maximum=255
        )
        password = form.get("password")
        if not isinstance(password, str):
            password = ""
        try:
            entry = verify_browser_value(
                self._secret,
                "assignment-claim-entry",
                cookies.get("autograde_assignment_claim", ""),
                now=int(self._aware_now().timestamp()),
            )
            entry_course = entry.get("course_key")
            entry_assignment = entry.get("assignment_id")
            expected_csrf = entry.get("csrf")
            supplied_csrf = form.get("csrf", "")
            if (
                not isinstance(entry_course, str)
                or not hmac.compare_digest(entry_course, self.course_key)
                or not isinstance(entry_assignment, str)
                or not hmac.compare_digest(entry_assignment, assignment_id)
                or not isinstance(expected_csrf, str)
                or not hmac.compare_digest(expected_csrf, supplied_csrf)
            ):
                raise InvalidSignedValue("assignment claim entry binding is invalid")
        except InvalidSignedValue as exc:
            raise PlatformAPIError(
                403,
                "assignment_claim_denied",
                "assignment claim could not be issued",
            ) from exc

        credential = self.authenticate_student_password(student_key, password)
        return self.issue_authenticated_assignment_claim(credential, assignment_id)

    def authenticate_student_password(self, student_key: str, password: str):
        """Shared password verification for the direct page and course portal."""
        self._require_secure_password_claim_transport()
        credential = None
        try:
            credential = self.state.get_student_password_credential(
                student_key=student_key,
                course_key=self.course_key,
            )
        except PlatformNotFound:
            pass
        stored_hash = (
            _DUMMY_PASSWORD_HASH
            if credential is None
            else credential.password_hash
        )
        if not self._password_verification_slots.acquire(timeout=5):
            raise PlatformAPIError(
                503,
                "assignment_claim_unavailable",
                "assignment claim is temporarily unavailable",
                headers={"Retry-After": "1"},
            )
        try:
            password_matches = verify_student_password(password, stored_hash)
        finally:
            self._password_verification_slots.release()
        now = self._aware_now()
        locked = bool(
            credential is not None
            and credential.locked_until is not None
            and credential.locked_until > utc_iso(now)
        )
        if not password_matches or credential is None or locked:
            if credential is not None and not password_matches:
                try:
                    self.state.record_student_password_failure(
                        enrollment_id=credential.enrollment_id,
                        expected_password_hash=credential.password_hash,
                        max_failed_attempts=self.policy.max_password_attempts,
                        lockout_seconds=self.policy.password_lockout_seconds,
                        at=now,
                    )
                except PlatformAccessDenied:
                    pass
            raise PlatformAPIError(
                403,
                "assignment_claim_denied",
                "assignment claim could not be issued",
            )

        return credential

    def create_authenticated_assignment_claim(self, credential, assignment_id: str):
        """Issue after a verified web session; the store rechecks the credential."""
        self._require_secure_password_claim_transport()
        now = self._aware_now()
        grant = None
        claim_code = ""
        for _attempt in range(5):
            claim_code = new_assignment_claim_code()
            try:
                grant = self.state.issue_assignment_grant(
                    assignment_grant_id=new_public_id("agr"),
                    student_id=credential.student_id,
                    course_key=self.course_key,
                    expected_password_hash=credential.password_hash,
                    assignment_id=assignment_id,
                    claim_tag=claim_code.split("-", 2)[1],
                    claim_code_hmac=self._digest(
                        "assignment-claim-code", claim_code
                    ),
                    expires_at=now
                    + timedelta(
                        seconds=self.policy.assignment_claim_lifetime_seconds
                    ),
                    at=now,
                )
                break
            except PlatformConflict:
                continue
            except (PlatformAccessDenied, PlatformNotFound) as exc:
                raise PlatformAPIError(
                    403,
                    "assignment_claim_denied",
                    "assignment claim could not be issued",
                ) from exc
        if grant is None:
            raise PlatformAPIError(
                503,
                "assignment_claim_unavailable",
                "assignment claim is temporarily unavailable",
            )

        return grant, claim_code

    def issue_authenticated_assignment_claim(self, credential, assignment_id: str):
        grant, claim_code = self.create_authenticated_assignment_claim(credential, assignment_id)

        body = self._html_page(
            "과제 수령 코드",
            "<h1>수령 코드가 발급되었습니다</h1>"
            "<p>과제: <strong>"
            f"{html.escape(self._assignment_claim_display_name(grant.assignment_id))}"
            "</strong></p>"
            f"<p>수령 코드: <strong><code>{html.escape(claim_code)}</code></strong></p>"
            "<p>이 코드는 10분 동안 유효하고 한 번만 사용할 수 있습니다. "
            "VS Code의 Autograde 입력창에 옮긴 뒤 이 페이지를 닫으세요. "
            "코드는 다시 표시되지 않습니다.</p>",
        )
        return PlatformResponse(
            200,
            body,
            {
                "Content-Type": "text/html; charset=utf-8",
                "Set-Cookie": self._assignment_claim_cookie("", max_age=0),
            },
        )

    def redeem_assignment_claim(
        self, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        """Consume one claim while atomically approving its pending device."""

        self._require_secure_password_claim_transport()
        _strict_object(
            payload,
            allowed={"claim_code", "device_code"},
            required={"claim_code", "device_code"},
        )
        claim_code = self._normalize_assignment_claim_code(
            payload.get("claim_code")
        )
        device_code = _required_string(
            payload.get("device_code"), "device_code", maximum=256
        )
        now = self._aware_now()
        try:
            grant, _device, acceptance = self.state.redeem_assignment_claim(
                claim_tag=claim_code.split("-", 2)[1],
                claim_code_hmac=self._digest(
                    "assignment-claim-code", claim_code
                ),
                course_key=self.course_key,
                device_code_hash=self._digest("device", device_code),
                acceptance_id=new_public_id("aac"),
                at=now,
            )
        except (
            PlatformAccessDenied,
            PlatformConflict,
            PlatformInvalidTransition,
            PlatformNotFound,
        ) as exc:
            raise PlatformAPIError(
                403,
                "assignment_claim_denied",
                "assignment claim could not be completed",
            ) from exc

        return {
            "course_key": grant.course_key,
            "assignment_id": grant.assignment_id,
            "delivery_mode": grant.delivery_mode,
            "acceptance_id": acceptance.acceptance_id,
        }

    # Browser pairing -------------------------------------------------

    def activate_page(self, query: Mapping[str, str]) -> PlatformResponse:
        user_code = query.get("user_code", "")
        normalized = ""
        authorization: DeviceAuthorization | None = None
        if user_code:
            try:
                normalized = self._normalize_user_code(user_code)
                candidate = self.state.get_device_authorization_by_user_code_hmac(
                    self._digest("user-code", normalized),
                    course_key=self.course_key,
                )
                if (
                    candidate.state == DeviceAuthorizationState.PENDING
                    and candidate.expires_at > utc_iso(self._aware_now())
                ):
                    authorization = candidate
            except (PlatformAPIError, PlatformNotFound):
                normalized = ""
        escaped = html.escape(normalized, quote=True)
        ready = authorization is not None
        disabled = "" if ready else " disabled"
        csrf = new_api_token() if ready else ""
        headers = {"Content-Type": "text/html; charset=utf-8"}
        if authorization is not None:
            signed_entry = sign_browser_value(
                self._secret,
                "activation-entry",
                {
                    "authorization_id": authorization.authorization_id,
                    "user_code_hmac": self._digest("user-code", normalized),
                    "course_key": self.course_key,
                    "csrf": csrf,
                },
                lifetime_seconds=self.policy.device_lifetime_seconds,
                now=int(self._aware_now().timestamp()),
            )
            headers["Set-Cookie"] = self._activation_cookie(signed_entry)
            device_summary = (
                "<p>연결할 장치: <strong>"
                f"{html.escape(authorization.device_label)}</strong></p>"
                "<p>VS Code에 표시된 연결 코드와 아래 코드가 같은지 확인하세요.</p>"
            )
        else:
            headers["Set-Cookie"] = self._activation_cookie("", max_age=0)
            device_summary = "<p>유효하고 대기 중인 연결 코드를 입력하세요.</p>"
        oauth_link = ""
        if self.github_oauth is not None:
            action = (
                f"/auth/github/start?{urlencode({'user_code': normalized})}"
                if ready
                else "#"
            )
            oauth_link = (
                "<p>또는</p>"
                f"<a class=\"button{disabled}\" "
                f"href=\"{html.escape(action, quote=True)}\">GitHub로 로그인</a>"
            )
        body = self._html_page(
            "VS Code 연결",
            f"<h1>Autograde 연결</h1><p>VS Code에 표시된 코드를 입력하세요.</p>"
            f"<form method=\"get\" action=\"/activate\">"
            f"<label>연결 코드 <input name=\"user_code\" value=\"{escaped}\" "
            f"autocomplete=\"one-time-code\" maxlength=\"9\"></label>"
            f"<button type=\"submit\">코드 확인</button></form>"
            f"<p><strong>{escaped or '연결 코드 없음'}</strong></p>"
            f"{device_summary}"
            f"<form method=\"post\" action=\"/activate/approve\">"
            f"<input type=\"hidden\" name=\"user_code\" value=\"{escaped}\">"
            f"<input type=\"hidden\" name=\"csrf\" "
            f"value=\"{html.escape(csrf, quote=True)}\">"
            f"<label>학생 활성화 코드 <input type=\"password\" "
            f"name=\"activation_code\" autocomplete=\"one-time-code\" "
            f"maxlength=\"48\" required></label>"
            f"<button type=\"submit\"{disabled}>이 장치 연결</button></form>"
            f"{oauth_link}",
        )
        return PlatformResponse(200, body, headers)

    def github_start(self, query: Mapping[str, str]) -> PlatformResponse:
        if self.github_oauth is None:
            raise PlatformAPIError(503, "oauth_unavailable", "GitHub login is not configured")
        user_code = self._normalize_user_code(query.get("user_code", ""))
        verifier = self._digest("user-code", user_code)
        try:
            authorization = self.state.get_device_authorization_by_user_code_hmac(
                verifier, course_key=self.course_key
            )
        except PlatformNotFound as exc:
            raise PlatformAPIError(404, "invalid_user_code", "connection code was not found") from exc
        if authorization.state != DeviceAuthorizationState.PENDING:
            raise PlatformAPIError(409, "invalid_user_code", "connection code cannot be approved")
        if authorization.expires_at <= utc_iso(self._aware_now()):
            raise PlatformAPIError(410, "expired_token", "connection code has expired")
        state = sign_browser_value(
            self._secret,
            "oauth-state",
            {
                "authorization_id": authorization.authorization_id,
                "user_code_hmac": verifier,
                "course_key": self.course_key,
            },
            lifetime_seconds=self.policy.device_lifetime_seconds,
            now=int(self._aware_now().timestamp()),
        )
        return PlatformResponse(
            302,
            None,
            {"Location": self.github_oauth.authorization_url(state=state)},
        )

    def github_callback(self, query: Mapping[str, str]) -> PlatformResponse:
        if self.github_oauth is None:
            raise PlatformAPIError(503, "oauth_unavailable", "GitHub login is not configured")
        code = _required_string(query.get("code"), "code", maximum=1024)
        signed_state = _required_string(query.get("state"), "state", maximum=4096)
        try:
            oauth_state = verify_browser_value(
                self._secret,
                "oauth-state",
                signed_state,
                now=int(self._aware_now().timestamp()),
            )
            authorization_id = self._safe_identifier(
                str(oauth_state.get("authorization_id", "")), "authorization_id"
            )
            user_code_hmac = _required_string(
                oauth_state.get("user_code_hmac"), "user_code_hmac", maximum=64
            )
            oauth_course = _required_string(
                oauth_state.get("course_key"), "course_key", maximum=255
            )
            if not hmac.compare_digest(oauth_course, self.course_key):
                raise InvalidSignedValue("OAuth state belongs to another course")
            identity = self.github_oauth.exchange_identity(code=code)
            subject = f"github:{identity.user_id}"
            student = self.state.get_student_by_subject(subject)
            self.state.require_active_enrollment(student_id=student.id, course_key=self.course_key)
            authorization = self.state.get_device_authorization_by_user_code_hmac(
                user_code_hmac, course_key=self.course_key
            )
        except (InvalidSignedValue, GitHubOAuthError, PlatformNotFound, PlatformAccessDenied) as exc:
            raise PlatformAPIError(403, "login_denied", "GitHub account is not enrolled") from exc
        if authorization.authorization_id != authorization_id or authorization.state != DeviceAuthorizationState.PENDING:
            raise PlatformAPIError(409, "invalid_user_code", "connection code cannot be approved")

        csrf = new_api_token()
        web_session = sign_browser_value(
            self._secret,
            "web-session",
            {
                "authorization_id": authorization_id,
                "user_code_hmac": user_code_hmac,
                "auth_subject": subject,
                "course_key": self.course_key,
                "csrf": csrf,
            },
            lifetime_seconds=self.policy.device_lifetime_seconds,
            now=int(self._aware_now().timestamp()),
        )
        cookie = self._activation_cookie(web_session)
        body = self._html_page(
            "연결 승인",
            f"<h1>연결 승인</h1><p><strong>{html.escape(student.github_login)}</strong> 계정으로 "
            f"{html.escape(authorization.device_label)} 장치를 연결합니다.</p>"
            f"<form method=\"post\" action=\"/activate/approve\">"
            f"<input type=\"hidden\" name=\"csrf\" value=\"{html.escape(csrf, quote=True)}\">"
            f"<button type=\"submit\">연결 승인</button></form>",
        )
        return PlatformResponse(
            200,
            body,
            {"Content-Type": "text/html; charset=utf-8", "Set-Cookie": cookie},
        )

    def approve_activation(
        self, form: Mapping[str, str], cookies: Mapping[str, str]
    ) -> PlatformResponse:
        if {"user_code", "activation_code"} & set(form):
            try:
                _strict_object(
                    form,
                    allowed={"user_code", "activation_code", "csrf"},
                    required={"user_code", "activation_code", "csrf"},
                )
                user_code = self._normalize_user_code(form.get("user_code", ""))
                activation_code = self._normalize_activation_code(
                    form.get("activation_code", "")
                )
                signed_entry = cookies.get("autograde_activation", "")
                entry = verify_browser_value(
                    self._secret,
                    "activation-entry",
                    signed_entry,
                    now=int(self._aware_now().timestamp()),
                )
                expected_csrf = entry.get("csrf")
                supplied_csrf = form.get("csrf", "")
                entry_course = entry.get("course_key")
                user_code_hmac = self._digest("user-code", user_code)
                entry_user_code_hmac = entry.get("user_code_hmac")
                if (
                    not isinstance(expected_csrf, str)
                    or not hmac.compare_digest(expected_csrf, supplied_csrf)
                    or not isinstance(entry_course, str)
                    or not hmac.compare_digest(entry_course, self.course_key)
                    or not isinstance(entry_user_code_hmac, str)
                    or not hmac.compare_digest(entry_user_code_hmac, user_code_hmac)
                ):
                    raise InvalidSignedValue("activation entry binding is invalid")
                authorization = (
                    self.state.get_device_authorization_by_user_code_hmac(
                        user_code_hmac,
                        course_key=self.course_key,
                    )
                )
                entry_authorization_id = entry.get("authorization_id")
                if (
                    not isinstance(entry_authorization_id, str)
                    or not hmac.compare_digest(
                        entry_authorization_id,
                        authorization.authorization_id,
                    )
                ):
                    raise InvalidSignedValue("activation entry binding is invalid")
                self.state.redeem_student_activation(
                    user_code_hmac=user_code_hmac,
                    activation_code_hmac=self._digest(
                        "activation-code", activation_code
                    ),
                    course_key=self.course_key,
                    max_failed_attempts=self.policy.max_activation_attempts,
                    at=self._aware_now(),
                )
            except (
                PlatformAPIError,
                InvalidSignedValue,
                PlatformAccessDenied,
                PlatformConflict,
                PlatformInvalidTransition,
                PlatformNotFound,
            ) as exc:
                raise PlatformAPIError(
                    403,
                    "activation_denied",
                    "student activation could not be completed",
                ) from exc
            body = self._html_page(
                "연결 완료",
                "<h1>연결 완료</h1><p>이 창을 닫고 VS Code로 돌아가세요.</p>",
            )
            return PlatformResponse(
                200,
                body,
                {
                    "Content-Type": "text/html; charset=utf-8",
                    "Set-Cookie": self._activation_cookie("", max_age=0),
                },
            )

        _strict_object(form, allowed={"csrf"}, required={"csrf"})
        signed_session = cookies.get("autograde_activation", "")
        csrf = form.get("csrf", "")
        try:
            session = verify_browser_value(
                self._secret,
                "web-session",
                signed_session,
                now=int(self._aware_now().timestamp()),
            )
        except InvalidSignedValue as exc:
            raise PlatformAPIError(403, "invalid_session", "activation session is invalid") from exc
        expected_csrf = session.get("csrf")
        if not isinstance(expected_csrf, str) or not hmac.compare_digest(expected_csrf, csrf):
            raise PlatformAPIError(403, "invalid_csrf", "activation confirmation is invalid")
        session_course = session.get("course_key")
        if not isinstance(session_course, str) or not hmac.compare_digest(
            session_course, self.course_key
        ):
            raise PlatformAPIError(
                403, "invalid_session", "activation session is invalid"
            )
        try:
            self.state.approve_device_authorization(
                user_code_hmac=_required_string(
                    session.get("user_code_hmac"), "user_code_hmac", maximum=64
                ),
                auth_subject=_required_string(
                    session.get("auth_subject"), "auth_subject", maximum=255
                ),
                course_key=self.course_key,
                at=self._aware_now(),
            )
        except (PlatformNotFound, PlatformAccessDenied, PlatformInvalidTransition) as exc:
            raise PlatformAPIError(409, "approval_failed", "connection code cannot be approved") from exc
        body = self._html_page(
            "연결 완료",
            "<h1>연결 완료</h1><p>이 창을 닫고 VS Code로 돌아가세요.</p>",
        )
        return PlatformResponse(
            200,
            body,
            {
                "Content-Type": "text/html; charset=utf-8",
                "Set-Cookie": self._activation_cookie("", max_age=0),
            },
        )

    # Operator-only pairing helper -----------------------------------

    def issue_student_activation(
        self,
        *,
        student_key: str,
        lifetime_seconds: int = _DEFAULT_ACTIVATION_LIFETIME_SECONDS,
    ) -> Mapping[str, Any]:
        """Issue a high-entropy one-time code for one active course enrollment."""

        student_key = _required_string(student_key, "student_key", maximum=255)
        if (
            isinstance(lifetime_seconds, bool)
            or not isinstance(lifetime_seconds, int)
            or lifetime_seconds <= 0
            or lifetime_seconds > _MAX_ACTIVATION_LIFETIME_SECONDS
        ):
            raise ValueError("activation lifetime must be between 1 second and 30 days")
        student = self.state.get_student_by_key(student_key)
        code = new_activation_code()
        now = self._aware_now()
        activation = self.state.issue_student_activation(
            activation_id=new_public_id("act"),
            student_id=student.id,
            course_key=self.course_key,
            code_hmac=self._digest("activation-code", code),
            expires_at=now + timedelta(seconds=lifetime_seconds),
            at=now,
        )
        return {
            "activation_id": activation.activation_id,
            "student_key": student.student_key,
            "course_key": activation.course_key,
            "activation_code": code,
            "expires_at": activation.expires_at,
        }

    def revoke_student_activation(self, *, student_key: str) -> int:
        """Revoke the currently issued code without changing existing sessions."""

        student = self.state.get_student_by_key(
            _required_string(student_key, "student_key", maximum=255)
        )
        return self.state.revoke_student_activations(
            student_id=student.id,
            course_key=self.course_key,
            at=self._aware_now(),
        )

    def revoke_issued_student_activation(self, *, activation_id: str) -> bool:
        """Revoke one just-issued code after an operator delivery failure."""

        return self.state.revoke_student_activation_by_id(
            self._safe_identifier(activation_id, "activation_id"),
            course_key=self.course_key,
            at=self._aware_now(),
        )

    def approve_user_code(self, *, user_code: str, github_user_id: int) -> None:
        """Approve a code from a trusted local operator CLI (development fallback)."""

        if isinstance(github_user_id, bool) or not isinstance(github_user_id, int) or github_user_id <= 0:
            raise ValueError("github_user_id must be a positive integer")
        verifier = self._digest("user-code", self._normalize_user_code(user_code))
        self.state.approve_device_authorization(
            user_code_hmac=verifier,
            auth_subject=f"github:{github_user_id}",
            course_key=self.course_key,
            at=self._aware_now(),
        )

    # Internal helpers ------------------------------------------------

    def _aware_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise RuntimeError("platform clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)

    def _digest(self, purpose: str, value: str) -> str:
        return secret_digest(self._secret, purpose, value)

    def _require_secure_password_claim_transport(self) -> None:
        parsed = urlsplit(self.public_base_url)
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if self.external_access_mode == "insecure-http" or not (
            parsed.scheme == "https" or (parsed.scheme == "http" and loopback)
        ):
            raise PlatformAPIError(
                403,
                "assignment_claim_unavailable",
                "password-based assignment claims require HTTPS",
            )

    def _access_verifier(self, access_token: str) -> str:
        return self._digest("access", _required_string(access_token, "access token", maximum=256))

    def _authorize(self, access_token: str) -> tuple[PlatformSession, Any]:
        try:
            verifier = self._access_verifier(access_token)
        except PlatformAPIError as exc:
            raise PlatformAPIError(
                401,
                "invalid_token",
                "access token is invalid or expired",
                headers={
                    "WWW-Authenticate": (
                        'Bearer realm="autograde", error="invalid_token"'
                    )
                },
            ) from exc
        try:
            return self.state.authorize_access_token(
                access_token_hash=verifier,
                course_key=self.course_key,
                at=self._aware_now(),
            )
        except PlatformAccessDenied as exc:
            raise PlatformAPIError(
                401,
                "invalid_token",
                "access token is invalid or expired",
                headers={"WWW-Authenticate": 'Bearer realm="autograde", error="invalid_token"'},
            ) from exc

    def _owned_assignment(self, access_token: str, assignment_id: str) -> PlatformAssignment:
        session, _student = self._authorize(access_token)
        safe_assignment_id = self._safe_identifier(assignment_id, "assignment_id")
        self._enforce_session_assignment_scope(session, "git", safe_assignment_id)
        try:
            return self.state.get_owned_assignment(
                access_token_hash=self._access_verifier(access_token),
                course_key=self.course_key,
                assignment_id=safe_assignment_id,
                require_available=True,
                at=self._aware_now(),
            )
        except PlatformNotFound as exc:
            raise PlatformAPIError(404, "not_found", "assignment was not found") from exc
        except PlatformAccessDenied as exc:
            raise PlatformAPIError(403, "access_denied", "assignment is not available") from exc

    def _owned_bundle_assignment(
        self, access_token: str, assignment_id: str
    ) -> BundleAssignmentRelease:
        session, _student = self._authorize(access_token)
        safe_assignment_id = self._safe_identifier(assignment_id, "assignment_id")
        self._enforce_session_assignment_scope(
            session, "bundle", safe_assignment_id
        )
        try:
            return self.state.get_owned_bundle_assignment(
                access_token_hash=self._access_verifier(access_token),
                course_key=self.course_key,
                assignment_id=safe_assignment_id,
                require_available=True,
                at=self._aware_now(),
            )
        except PlatformNotFound as exc:
            raise PlatformAPIError(
                404, "not_found", "assignment was not found"
            ) from exc
        except PlatformAccessDenied as exc:
            raise PlatformAPIError(
                403, "access_denied", "assignment is not available"
            ) from exc

    def _enforce_session_assignment_scope(
        self, session: PlatformSession, delivery_mode: str, assignment_id: str
    ) -> None:
        try:
            scope = self.state.get_session_assignment_scope(
                session_id=session.session_id, course_key=self.course_key
            )
        except PlatformNotFound as exc:
            raise PlatformAPIError(
                401, "invalid_token", "access token is invalid or expired"
            ) from exc
        if scope is not None and scope != (delivery_mode, assignment_id):
            raise PlatformAPIError(
                403,
                "access_denied",
                "this session is restricted to another assignment",
            )

    def _poll_is_too_fast(
        self,
        verifier: str,
        interval: int,
        remaining_lifetime_seconds: float,
    ) -> bool:
        now = self._monotonic()
        with self._poll_lock:
            self._purge_expired_poll_entries_locked(now)
            previous = self._next_poll.get(verifier)
            if previous is None and len(self._next_poll) >= _MAX_DEVICE_POLL_TRACKERS:
                # This is a defensive process-memory bound.  SQLite applies the
                # matching outstanding-authorization quota, but evicting the
                # soonest-expiring cadence entry keeps this layer safe even if a
                # custom state adapter does not.
                soonest = min(
                    self._next_poll,
                    key=lambda key: self._next_poll[key][1],
                )
                del self._next_poll[soonest]
            self._next_poll[verifier] = (
                now + interval,
                now + remaining_lifetime_seconds,
            )
        return previous is not None and now < previous[0]

    def _purge_expired_poll_entries(self) -> None:
        now = self._monotonic()
        with self._poll_lock:
            self._purge_expired_poll_entries_locked(now)

    def _purge_expired_poll_entries_locked(self, now: float) -> None:
        expired = [
            verifier
            for verifier, (_next_allowed, expires_at) in self._next_poll.items()
            if expires_at <= now
        ]
        for verifier in expired:
            del self._next_poll[verifier]

    def _forget_poll(self, verifier: str) -> None:
        with self._poll_lock:
            self._next_poll.pop(verifier, None)

    def _token_response(
        self,
        access_token: str,
        refresh_token: str,
        *,
        expires_in: int | None = None,
    ) -> Mapping[str, Any]:
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "expires_in": (
                self.policy.access_lifetime_seconds
                if expires_in is None
                else expires_in
            ),
        }

    @staticmethod
    def _normalize_user_code(value: Any) -> str:
        if not isinstance(value, str):
            raise PlatformAPIError(400, "invalid_user_code", "connection code is invalid")
        compact = value.strip().upper().replace(" ", "")
        if len(compact) == 8:
            compact = f"{compact[:4]}-{compact[4:]}"
        if not _USER_CODE.fullmatch(compact):
            raise PlatformAPIError(400, "invalid_user_code", "connection code is invalid")
        return compact

    @staticmethod
    def _normalize_activation_code(value: Any) -> str:
        if not isinstance(value, str) or len(value) > 64:
            raise PlatformAPIError(
                403, "activation_denied", "student activation could not be completed"
            )
        text = value.strip().upper()
        if not text or any(ord(character) < 32 for character in text):
            raise PlatformAPIError(
                403, "activation_denied", "student activation could not be completed"
            )
        compact = text.replace("-", "").replace(" ", "")
        if not _ACTIVATION_CODE.fullmatch(compact):
            raise PlatformAPIError(
                403, "activation_denied", "student activation could not be completed"
            )
        raw = compact[3:]
        groups = [raw[index : index + 4] for index in range(0, len(raw), 4)]
        return "AG1-" + "-".join(groups)

    @staticmethod
    def _normalize_assignment_claim_code(value: Any) -> str:
        if not isinstance(value, str) or len(value) > 32:
            raise PlatformAPIError(
                403,
                "assignment_claim_denied",
                "assignment claim could not be completed",
            )
        text = value.strip().upper()
        if not text or any(ord(character) < 32 for character in text):
            raise PlatformAPIError(
                403,
                "assignment_claim_denied",
                "assignment claim could not be completed",
            )
        compact = text.replace("-", "").replace(" ", "")
        if not _ASSIGNMENT_CLAIM_CODE.fullmatch(compact):
            raise PlatformAPIError(
                403,
                "assignment_claim_denied",
                "assignment claim could not be completed",
            )
        raw = compact[3:]
        return "AK1-" + "-".join(
            raw[index : index + 4] for index in range(0, len(raw), 4)
        )

    @staticmethod
    def _safe_identifier(value: str, field_name: str) -> str:
        if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
            raise PlatformAPIError(400, "invalid_request", f"{field_name} is invalid")
        return value

    @staticmethod
    def _session_projection(
        session: PlatformSession,
        current_session_id: str,
    ) -> Mapping[str, Any]:
        return {
            "session_id": session.session_id,
            "device_label": session.device_label,
            "created_at": session.created_at,
            "last_seen_at": session.last_seen_at,
            "access_expires_at": session.access_token_expires_at,
            "refresh_expires_at": session.refresh_token_expires_at,
            "revoked_at": session.revoked_at,
            "current": session.session_id == current_session_id,
        }

    @staticmethod
    def _operator_session_projection(
        session: PlatformSession,
        now: str,
    ) -> Mapping[str, Any]:
        return {
            "session_id": session.session_id,
            "device_label": session.device_label,
            "created_at": session.created_at,
            "last_seen_at": session.last_seen_at,
            "access_expires_at": session.access_token_expires_at,
            "refresh_expires_at": session.refresh_token_expires_at,
            "revoked_at": session.revoked_at,
            "active": (
                session.revoked_at is None
                and session.refresh_token_expires_at > now
            ),
        }

    @staticmethod
    def _repository_projection(assignment: PlatformAssignment) -> MutableMapping[str, Any]:
        full_name = f"{assignment.repository_owner}/{assignment.repository_name}"
        return {
            "github_repository_id": assignment.github_repository_id,
            "full_name": full_name,
            "clone_url": assignment.clone_url,
            "html_url": assignment.clone_url[:-4]
            if assignment.clone_url.endswith(".git")
            else assignment.clone_url,
            "state": "ready" if assignment.ready and assignment.active else "unavailable",
            "target_ref": assignment.target_ref,
        }

    @classmethod
    def _assignment_projection(cls, assignment: PlatformAssignment) -> Mapping[str, Any]:
        return {
            "assignment_id": assignment.assignment_id,
            "assignment_key": assignment.assignment_key,
            "title": assignment.assignment_key,
            "course_key": assignment.course_key,
            "release_id": assignment.release_id,
            "delivery_mode": "git",
            "status": "ready" if assignment.ready and assignment.active else "unavailable",
            "submission_mode": assignment.submission_mode.value,
            "assignment_path": assignment.assignment_path,
            "opens_at": assignment.opens_at,
            "due_at": assignment.due_at,
            "max_score": assignment.max_score,
            "repository": cls._repository_projection(assignment),
        }

    def _bundle_assignment_projection(
        self, assignment: BundleAssignmentRelease
    ) -> Mapping[str, Any]:
        encoded_id = quote(assignment.assignment_id, safe="")
        starter_url = f"{self.public_base_url}/v1/assignments/{encoded_id}/starter"
        submission_url = (
            f"{self.public_base_url}/v1/assignments/{encoded_id}/submissions"
        )
        return {
            "assignment_id": assignment.assignment_id,
            "assignment_key": assignment.assignment_key,
            "title": assignment.title,
            "course_key": assignment.course_key,
            "release_id": assignment.release_id,
            "delivery_mode": "bundle",
            "status": (
                "ready" if assignment.ready and assignment.active else "unavailable"
            ),
            "submission_mode": "bundle",
            "opens_at": assignment.opens_at,
            "due_at": assignment.due_at,
            "max_score": assignment.max_score,
            "starter_url": starter_url,
            "submission_url": submission_url,
            "starter": {
                "url": starter_url,
                "sha256": assignment.starter_digest,
                "size_bytes": assignment.starter_size_bytes,
            },
        }

    @staticmethod
    def _submission_projection(request: SubmissionRequest) -> Mapping[str, Any]:
        value: MutableMapping[str, Any] = {
            "submission_id": request.submission_id,
            "assignment_id": request.assignment_id,
            "state": request.state.value,
            "head_sha": request.requested_sha,
            "received_at": request.received_at,
            "updated_at": request.updated_at,
        }
        if request.pull_request_number is not None:
            value["pull_request_number"] = request.pull_request_number
        if request.failure_code is not None:
            value["failure"] = {
                "code": request.failure_code,
                "message": request.failure_message or "submission failed",
            }
        return value

    @staticmethod
    def _bundle_submission_projection(
        request: BundleSubmissionRequest,
    ) -> Mapping[str, Any]:
        value: MutableMapping[str, Any] = {
            "submission_id": request.submission_id,
            "assignment_id": request.assignment_id,
            "delivery_mode": "bundle",
            "state": request.state.value,
            "source_sha256": request.source_digest,
            "source_size_bytes": request.source_size_bytes,
            "received_at": request.received_at,
            "updated_at": request.updated_at,
        }
        if request.failure_code is not None:
            value["failure"] = {
                "code": request.failure_code,
                "message": request.failure_message or "submission failed",
            }
        return value

    @staticmethod
    def _result_projection(result: SubmissionResult) -> MutableMapping[str, Any]:
        return {
            "result_id": result.result_id,
            "submission_id": result.submission_id,
            "state": "published",
            "head_sha": result.commit_sha,
            "score": result.score,
            "max_score": result.max_score,
            "rubric": dict(result.rubric),
            "diagnostics": list(result.diagnostics),
            "published_at": result.published_at,
        }

    @staticmethod
    def _bundle_result_projection(
        result: BundleSubmissionResult,
    ) -> MutableMapping[str, Any]:
        return {
            "result_id": result.result_id,
            "submission_id": result.submission_id,
            "delivery_mode": "bundle",
            "state": "published",
            "source_sha256": result.source_digest,
            "score": result.score,
            "max_score": result.max_score,
            "rubric": dict(result.rubric),
            "diagnostics": list(result.diagnostics),
            "published_at": result.published_at,
        }

    def _activation_cookie(self, value: str, *, max_age: int | None = None) -> str:
        secure = "; Secure" if self.public_base_url.startswith("https://") else ""
        age = f"; Max-Age={max_age}" if max_age is not None else ""
        return (
            f"autograde_activation={value}; Path=/activate; HttpOnly; SameSite=Lax"
            f"{secure}{age}"
        )

    def _assignment_claim_cookie(
        self, value: str, *, max_age: int | None = None
    ) -> str:
        secure = "; Secure" if self.public_base_url.startswith("https://") else ""
        age = f"; Max-Age={max_age}" if max_age is not None else ""
        return (
            "autograde_assignment_claim="
            f"{value}; Path=/assignment-claim; HttpOnly; SameSite=Strict"
            f"{secure}{age}"
        )

    def _assignment_claim_display_name(self, assignment_id: str) -> str:
        """Prefer a human title without exposing another course's metadata."""

        try:
            assignment = self.state.get_bundle_assignment(assignment_id)
        except PlatformNotFound:
            return assignment_id
        if assignment.course_key != self.course_key:
            return assignment_id
        return f"{assignment.title} ({assignment.assignment_key})"

    @staticmethod
    def _html_page(title: str, content: str) -> str:
        return (
            "<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{html.escape(title)}</title>"
            "<style>body{font-family:system-ui,sans-serif;max-width:42rem;margin:4rem auto;"
            "padding:0 1rem;line-height:1.5}.button,button{display:inline-block;background:#0969da;"
            "color:white;padding:.7rem 1rem;border:0;border-radius:.4rem;text-decoration:none;"
            "font-size:1rem}.disabled{pointer-events:none;opacity:.5}</style></head>"
            f"<body>{content}</body></html>"
        )
