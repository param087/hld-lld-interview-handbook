"""Minimal HS256 JSON Web Tokens: issue, verify (signature *and* expiry), rotate keys, revoke.

What the module demonstrates, in the order an interviewer asks about it:

* A JWT is three base64url segments, ``header.payload.signature``. The signature is an HMAC
  over the first two, so any service holding the key verifies a token without a database
  call; the payload is only encoded, never encrypted, so it must not carry secrets.
* ``JwtCodec.verify`` checks in this order: algorithm, key id, signature (constant-time),
  ``exp``/``nbf`` with a small leeway, issuer, revocation. Claims are never trusted before
  the signature has been checked.
* Keys rotate by ``kid``: new tokens are signed with the active key, old tokens still verify
  until their key is retired.
* ``RevocationList`` is the state you have to add back to get logout and forced sign-out:
  a denylist of token ids that lives only as long as the tokens would have.

Only ``hmac``, ``hashlib``, ``base64`` and ``json`` do the cryptographic work.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass

from common import (
    Clock,
    ConflictError,
    FakeClock,
    IdGenerator,
    InvalidStateError,
    NotFoundError,
    SequentialIdGenerator,
    SystemClock,
    ValidationError,
)

ALGORITHM = "HS256"
MIN_SECRET_BYTES = 32  # RFC 7518: an HS256 key must be at least as long as the hash output


class InvalidTokenError(ValidationError):
    """The token is malformed, signed by an unknown key, tampered with, or revoked."""


class ExpiredTokenError(InvalidTokenError):
    """The token was valid once; its ``exp`` is in the past."""


# --8<-- [start:encoding]
def b64url_encode(raw: bytes) -> str:
    """base64url without ``=`` padding, as the JWS spec requires."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64url_decode(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise InvalidTokenError("malformed base64url segment") from exc


def encode_json(obj: Mapping[str, object]) -> str:
    """Compact, key-sorted JSON so identical claims always produce identical bytes."""
    return b64url_encode(json.dumps(obj, separators=(",", ":"), sort_keys=True).encode())


def decode_json(segment: str) -> dict[str, object]:
    try:
        value = json.loads(b64url_decode(segment))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidTokenError("segment is not valid JSON") from exc
    if not isinstance(value, dict):
        raise InvalidTokenError("segment must be a JSON object")
    return value


def sign(signing_input: str, secret: bytes) -> str:
    """HMAC-SHA256 over ``header.payload``: the only step that needs the secret."""
    digest = hmac.new(secret, signing_input.encode("ascii"), hashlib.sha256).digest()
    return b64url_encode(digest)


# --8<-- [end:encoding]


# --8<-- [start:codec]
@dataclass(frozen=True, slots=True)
class ParsedToken:
    """The three segments of a token, with the header and claims decoded but not yet trusted."""

    header: dict[str, object]
    claims: dict[str, object]
    signing_input: str
    signature: str

    @staticmethod
    def parse(token: str) -> ParsedToken:
        parts = token.split(".")
        if len(parts) != 3:
            raise InvalidTokenError("a JWT has exactly three dot-separated segments")
        header_b64, payload_b64, signature = parts
        return ParsedToken(
            header=decode_json(header_b64),
            claims=decode_json(payload_b64),
            signing_input=f"{header_b64}.{payload_b64}",
            signature=signature,
        )


class JwtCodec:
    """Issues and verifies HS256 tokens for one issuer.

    ``_keys`` maps key id to secret and ``_active`` names the key new tokens are signed with;
    ``_lock`` guards both, because rotation happens while requests are in flight. Every
    token carries ``sub``, ``iss``, ``iat``, ``exp`` and a unique ``jti`` (for revocation).
    """

    def __init__(
        self,
        keys: Mapping[str, bytes | str],
        active_kid: str,
        *,
        issuer: str = "auth.example",
        leeway_seconds: int = 0,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        if active_kid not in keys:
            raise ValidationError(f"active key {active_kid!r} is not in the key set")
        if leeway_seconds < 0:
            raise ValidationError("leeway must not be negative")
        self._keys = {kid: self._as_secret(secret) for kid, secret in keys.items()}
        self._active = active_kid
        self._issuer = issuer
        self._leeway = leeway_seconds
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("jti")
        self._lock = threading.Lock()

    @staticmethod
    def _as_secret(secret: bytes | str) -> bytes:
        raw = secret.encode() if isinstance(secret, str) else secret
        if len(raw) < MIN_SECRET_BYTES:
            raise ValidationError(f"HS256 secrets must be at least {MIN_SECRET_BYTES} bytes")
        return raw

    @property
    def active_kid(self) -> str:
        with self._lock:
            return self._active

    def issue(
        self, subject: str, ttl_seconds: int, claims: Mapping[str, object] | None = None
    ) -> str:
        """Sign a token for ``subject`` that expires ``ttl_seconds`` from now."""
        if not subject:
            raise ValidationError("subject must be non-empty")
        if ttl_seconds <= 0:
            raise ValidationError("ttl must be positive")
        now = int(self._clock.now())
        with self._lock:
            kid, secret = self._active, self._keys[self._active]
        payload: dict[str, object] = {
            **(claims or {}),
            "sub": subject,
            "iss": self._issuer,
            "iat": now,
            "exp": now + ttl_seconds,
            "jti": self._ids.next_id(),
        }
        header = {"alg": ALGORITHM, "typ": "JWT", "kid": kid}
        signing_input = f"{encode_json(header)}.{encode_json(payload)}"
        return f"{signing_input}.{sign(signing_input, secret)}"

    def verify(self, token: str, revoked: RevocationList | None = None) -> dict[str, object]:
        """Return the claims if the token is authentic, current and not revoked; else raise."""
        parsed = ParsedToken.parse(token)
        if parsed.header.get("alg") != ALGORITHM:
            raise InvalidTokenError(f"unsupported alg {parsed.header.get('alg')!r}")
        kid = str(parsed.header.get("kid"))
        with self._lock:
            secret = self._keys.get(kid)
        if secret is None:
            raise InvalidTokenError(f"unknown key id {kid!r}")
        if not hmac.compare_digest(sign(parsed.signing_input, secret), parsed.signature):
            raise InvalidTokenError("signature mismatch")
        self._check_claims(parsed.claims)
        if revoked is not None and revoked.contains(str(parsed.claims.get("jti"))):
            raise InvalidTokenError("token has been revoked")
        return parsed.claims

    def _check_claims(self, claims: Mapping[str, object]) -> None:
        now = self._clock.now()
        exp = claims.get("exp")
        if not isinstance(exp, int | float):
            raise InvalidTokenError("missing exp claim")
        if now > exp + self._leeway:
            raise ExpiredTokenError(f"token expired {now - exp:.0f} s ago")
        nbf = claims.get("nbf")
        if isinstance(nbf, int | float) and now + self._leeway < nbf:
            raise InvalidTokenError("token is not valid yet")
        if claims.get("iss") != self._issuer:
            raise InvalidTokenError(f"issuer mismatch: {claims.get('iss')!r}")

    def rotate(self, kid: str, secret: bytes | str) -> None:
        """Add a key and sign with it from now on; older keys keep verifying until retired."""
        raw = self._as_secret(secret)
        with self._lock:
            if kid in self._keys:
                raise ConflictError(f"key id {kid!r} already exists")
            self._keys[kid] = raw
            self._active = kid

    def retire(self, kid: str) -> None:
        """Drop a key: every token it signed stops verifying at once."""
        with self._lock:
            if kid not in self._keys:
                raise NotFoundError(f"unknown key id {kid!r}")
            if kid == self._active:
                raise InvalidStateError("cannot retire the active key; rotate first")
            del self._keys[kid]


# --8<-- [end:codec]


# --8<-- [start:revocation]
class RevocationList:
    """Denylist of revoked token ids, each kept only until its token would have expired anyway.

    Stateless tokens cannot be recalled, so logout and forced sign-out need this: one small
    entry per revoked ``jti`` with a TTL equal to the remaining lifetime, checked on every
    request. Short-lived access tokens keep it small. ``_lock`` guards ``_expires_at``.
    """

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._expires_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def revoke(self, jti: str, exp: float) -> None:
        if not jti:
            raise ValidationError("jti must be non-empty")
        with self._lock:
            self._expires_at[jti] = float(exp)

    def contains(self, jti: str) -> bool:
        now = self._clock.now()
        with self._lock:
            exp = self._expires_at.get(jti)
            if exp is None:
                return False
            if exp < now:
                del self._expires_at[jti]  # the token is dead anyway; forget it
                return False
            return True

    def __len__(self) -> int:
        now = self._clock.now()
        with self._lock:
            self._expires_at = {j: exp for j, exp in self._expires_at.items() if exp >= now}
            return len(self._expires_at)


# --8<-- [end:revocation]


def main() -> None:
    clock = FakeClock(start=1_700_000_000)
    codec = JwtCodec({"k1": "k1-" + "x" * 29}, "k1", issuer="auth.example", clock=clock)
    revoked = RevocationList(clock)

    def attempt(token: str) -> str:
        try:
            claims = codec.verify(token, revoked)
        except InvalidTokenError as exc:
            return f"rejected ({exc})"
        return f"ok, sub={claims['sub']} role={claims['role']}"

    token = codec.issue("user:42", ttl_seconds=900, claims={"role": "editor"})
    header_b64, payload_b64, signature = token.split(".")
    print(f"token: {len(token)} chars in 3 segments")
    print(f"header:  {decode_json(header_b64)}")
    print(f"payload: {decode_json(payload_b64)}")
    print(f"fresh token:          {attempt(token)}")

    forged_payload = encode_json({**decode_json(payload_b64), "role": "admin"})
    print(f"payload edited:       {attempt(f'{header_b64}.{forged_payload}.{signature}')}")
    none_header = encode_json({"alg": "none", "typ": "JWT", "kid": "k1"})
    print(f"alg=none, no sig:     {attempt(f'{none_header}.{forged_payload}.')}")

    codec.rotate("k2", "k2-" + "y" * 29)
    fresh = codec.issue("user:42", ttl_seconds=900, claims={"role": "editor"})
    print(f"after rotate to k2:   old {attempt(token)[:2]}, new {attempt(fresh)[:2]}")
    codec.retire("k1")
    print(f"after retire k1:      old {attempt(token)}")

    revoked.revoke(str(decode_json(fresh.split('.')[1])["jti"]), exp=clock.now() + 900)
    print(f"after revoke jti:     new {attempt(fresh)}; denylist size={len(revoked)}")

    clock.advance(901)
    print(f"901 s later (ttl 900): new {attempt(fresh)}; denylist size={len(revoked)}")


if __name__ == "__main__":
    main()
