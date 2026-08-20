from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ConflictError, FakeClock, InvalidStateError, NotFoundError, ValidationError
from hld.jwt_minimal import (
    ExpiredTokenError,
    InvalidTokenError,
    JwtCodec,
    RevocationList,
    b64url_decode,
    b64url_encode,
    decode_json,
    encode_json,
)

SECRET_A = b"a" * 32
SECRET_B = b"b" * 32


def make_codec(clock: FakeClock | None = None, **kwargs: object) -> JwtCodec:
    return JwtCodec({"k1": SECRET_A}, "k1", clock=clock or FakeClock(1_000_000), **kwargs)  # type: ignore[arg-type]


def test_issue_then_verify_roundtrip_carries_standard_claims() -> None:
    clock = FakeClock(1_000_000)
    codec = make_codec(clock)
    token = codec.issue("user:1", ttl_seconds=60, claims={"role": "editor"})
    header_b64, payload_b64, signature = token.split(".")
    assert decode_json(header_b64) == {"alg": "HS256", "typ": "JWT", "kid": "k1"}
    claims = codec.verify(token)
    assert claims == decode_json(payload_b64)
    assert claims["sub"] == "user:1" and claims["role"] == "editor"
    assert claims["iat"] == 1_000_000 and claims["exp"] == 1_000_060
    assert claims["iss"] == "auth.example" and claims["jti"] == "jti-1"
    assert "=" not in signature and len(signature) == 43  # 32-byte HMAC, unpadded base64url


def test_base64url_helpers_roundtrip_without_padding() -> None:
    for raw in (b"", b"a", b"ab", b"abc", bytes(range(256))):
        encoded = b64url_encode(raw)
        assert "=" not in encoded and "+" not in encoded and "/" not in encoded
        assert b64url_decode(encoded) == raw
    assert decode_json(encode_json({"b": 1, "a": [1, 2]})) == {"a": [1, 2], "b": 1}
    with pytest.raises(InvalidTokenError):
        decode_json(b64url_encode(b"[1, 2]"))  # a JSON array, not an object


def test_edited_payload_fails_signature_check() -> None:
    codec = make_codec()
    header_b64, payload_b64, signature = codec.issue("user:1", 60, {"role": "viewer"}).split(".")
    forged = encode_json({**decode_json(payload_b64), "role": "admin"})
    with pytest.raises(InvalidTokenError, match="signature mismatch"):
        codec.verify(f"{header_b64}.{forged}.{signature}")


def test_alg_none_and_foreign_algorithms_are_rejected() -> None:
    codec = make_codec()
    _, payload_b64, signature = codec.issue("user:1", 60).split(".")
    for alg in ("none", "None", "RS256", "HS512"):
        header = encode_json({"alg": alg, "typ": "JWT", "kid": "k1"})
        with pytest.raises(InvalidTokenError, match="unsupported alg"):
            codec.verify(f"{header}.{payload_b64}.{signature}")
        with pytest.raises(InvalidTokenError):
            codec.verify(f"{header}.{payload_b64}.")


def test_token_signed_with_another_secret_is_rejected() -> None:
    clock = FakeClock(1_000_000)
    other = JwtCodec({"k1": SECRET_B}, "k1", clock=clock)
    with pytest.raises(InvalidTokenError, match="signature mismatch"):
        make_codec(clock).verify(other.issue("user:1", 60))
    stranger = JwtCodec({"k9": SECRET_A}, "k9", clock=clock)
    with pytest.raises(InvalidTokenError, match="unknown key id"):
        make_codec(clock).verify(stranger.issue("user:1", 60))


def test_expiry_is_enforced_with_leeway() -> None:
    clock = FakeClock(1_000_000)
    codec = make_codec(clock, leeway_seconds=5)
    token = codec.issue("user:1", ttl_seconds=60)
    clock.advance(64)  # 4 s past exp, inside the leeway
    assert codec.verify(token)["sub"] == "user:1"
    clock.advance(2)  # 6 s past exp
    with pytest.raises(ExpiredTokenError, match="expired 6 s ago"):
        codec.verify(token)


def test_nbf_and_issuer_are_checked_after_the_signature() -> None:
    clock = FakeClock(1_000_000)
    codec = make_codec(clock)
    early = codec.issue("user:1", 600, claims={"nbf": 1_000_100})
    with pytest.raises(InvalidTokenError, match="not valid yet"):
        codec.verify(early)
    clock.advance(100)
    assert codec.verify(early)["nbf"] == 1_000_100
    foreign = JwtCodec({"k1": SECRET_A}, "k1", issuer="other.example", clock=clock)
    with pytest.raises(InvalidTokenError, match="issuer mismatch"):
        codec.verify(foreign.issue("user:1", 60))


def test_rotation_keeps_old_tokens_valid_until_the_key_is_retired() -> None:
    codec = make_codec()
    old = codec.issue("user:1", 60)
    codec.rotate("k2", SECRET_B)
    new = codec.issue("user:1", 60)
    assert codec.active_kid == "k2"
    assert decode_json(new.split(".")[0])["kid"] == "k2"
    assert codec.verify(old)["sub"] == codec.verify(new)["sub"] == "user:1"
    with pytest.raises(InvalidStateError):
        codec.retire("k2")
    codec.retire("k1")
    with pytest.raises(InvalidTokenError, match="unknown key id"):
        codec.verify(old)
    assert codec.verify(new)["jti"] == "jti-2"
    with pytest.raises(ConflictError):
        codec.rotate("k2", SECRET_A)
    with pytest.raises(NotFoundError):
        codec.retire("k1")


def test_revocation_list_rejects_until_the_token_would_expire() -> None:
    clock = FakeClock(1_000_000)
    codec = make_codec(clock)
    revoked = RevocationList(clock)
    token = codec.issue("user:1", 60)
    claims = codec.verify(token, revoked)
    revoked.revoke(str(claims["jti"]), exp=float(claims["exp"]))  # type: ignore[arg-type]
    assert len(revoked) == 1
    with pytest.raises(InvalidTokenError, match="revoked"):
        codec.verify(token, revoked)
    assert codec.verify(token) == claims  # a service that skips the list still accepts it
    clock.advance(61)
    assert len(revoked) == 0
    with pytest.raises(ExpiredTokenError):
        codec.verify(token, revoked)
    with pytest.raises(ValidationError):
        revoked.revoke("", exp=1.0)


@pytest.mark.parametrize(
    "token",
    ["", "abc", "a.b", "a.b.c.d", "!!.x.y", f"{encode_json({'alg': 'HS256'})}.bm90anNvbg.sig"],
)
def test_malformed_tokens_raise_invalid_token_error(token: str) -> None:
    with pytest.raises(InvalidTokenError):
        make_codec().verify(token)


def test_constructor_and_issue_validation() -> None:
    with pytest.raises(ValidationError):
        JwtCodec({"k1": b"short"}, "k1")
    with pytest.raises(ValidationError):
        JwtCodec({"k1": SECRET_A}, "missing")
    with pytest.raises(ValidationError):
        JwtCodec({"k1": SECRET_A}, "k1", leeway_seconds=-1)
    codec = make_codec()
    with pytest.raises(ValidationError):
        codec.issue("", 60)
    with pytest.raises(ValidationError):
        codec.issue("user:1", 0)


def test_concurrent_rotation_issue_and_verify() -> None:
    clock = FakeClock(1_000_000)
    codec = make_codec(clock)
    revoked = RevocationList(clock)

    def worker(i: int) -> bool:
        if i % 10 == 0:
            codec.rotate(f"r{i}", f"secret-{i:02d}-".encode() * 4)
        token = codec.issue(f"user:{i}", 60)
        claims = codec.verify(token, revoked)
        if i % 3 == 0:
            revoked.revoke(str(claims["jti"]), float(claims["exp"]))  # type: ignore[arg-type]
            return revoked.contains(str(claims["jti"]))
        return claims["sub"] == f"user:{i}"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(200)))
    assert all(results)
    assert codec.active_kid.startswith("r")
    assert len(revoked) == len([i for i in range(200) if i % 3 == 0])
