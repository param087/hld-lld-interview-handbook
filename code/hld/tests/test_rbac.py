from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ConflictError, NotFoundError, ValidationError
from hld.rbac import RbacPolicy, permission_matches, validate_permission


def make_policy() -> RbacPolicy:
    policy = RbacPolicy()
    policy.add_role("viewer")
    policy.add_role("editor", inherits=["viewer"])
    policy.add_role("admin", inherits=["editor"])
    policy.grant("viewer", "doc:read")
    policy.grant("editor", "doc:write")
    policy.grant("editor", "doc:delete", condition=lambda c: c.get("owner") == c["user"], label="own")
    policy.grant("admin", "*")
    policy.assign("alice", "admin")
    policy.assign("bob", "editor")
    policy.assign("carol", "viewer")
    return policy


@pytest.mark.parametrize(
    ("granted", "requested", "expected"),
    [
        ("doc:read", "doc:read", True),
        ("doc:read", "doc:write", False),
        ("doc:*", "doc:write", True),
        ("doc:*", "billing:refund", False),
        ("*", "billing:refund", True),
    ],
)
def test_permission_matching(granted: str, requested: str, expected: bool) -> None:
    assert permission_matches(granted, requested) is expected


@pytest.mark.parametrize("bad", ["", "doc", "doc:", ":read", "Doc:Read", "doc:read:all", "*:read"])
def test_malformed_permissions_are_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        validate_permission(bad)


def test_roles_inherit_transitively_and_deny_by_default() -> None:
    policy = make_policy()
    assert policy.roles_of("alice") == {"admin", "editor", "viewer"}
    assert policy.roles_of("carol") == {"viewer"}
    assert policy.permissions_of("bob") == {"doc:read", "doc:write"}  # conditional grant excluded
    assert policy.is_allowed("bob", "doc:read")
    assert policy.is_allowed("bob", "doc:write")
    assert not policy.is_allowed("carol", "doc:write")
    assert not policy.is_allowed("nobody", "doc:read")
    assert policy.check("nobody", "doc:read").reason == "user has no roles"
    assert policy.check("carol", "doc:write").reason == "no role grants it"


def test_decision_carries_the_granting_role_for_audit() -> None:
    policy = make_policy()
    decision = policy.check("bob", "doc:read")
    assert decision.allowed and decision.via_role == "viewer" and decision.via_grant == "doc:read"
    wildcard = policy.check("alice", "billing:refund")
    assert wildcard.allowed and wildcard.via_role == "admin" and wildcard.via_grant == "*"


def test_attribute_condition_turns_rbac_into_abac() -> None:
    policy = make_policy()
    assert policy.is_allowed("bob", "doc:delete", {"owner": "bob"})
    denied = policy.check("bob", "doc:delete", {"owner": "alice"})
    assert not denied.allowed and denied.reason == "condition not met for [own]"
    assert not policy.is_allowed("bob", "doc:delete")  # no owner attribute at all
    assert policy.is_allowed("alice", "doc:delete", {"owner": "bob"})  # admin wildcard ignores it


def test_cycles_unknown_roles_and_duplicates_are_refused() -> None:
    policy = make_policy()
    with pytest.raises(ValidationError, match="cycle"):
        policy.inherit("viewer", "admin")
    with pytest.raises(ValidationError, match="cycle"):
        policy.inherit("viewer", "viewer")
    with pytest.raises(ConflictError):
        policy.add_role("viewer")
    with pytest.raises(NotFoundError):
        policy.add_role("auditor", inherits=["ghost"])
    with pytest.raises(NotFoundError):
        policy.grant("ghost", "doc:read")
    with pytest.raises(NotFoundError):
        policy.assign("dave", "ghost")
    with pytest.raises(NotFoundError):
        policy.unassign("dave", "viewer")
    with pytest.raises(ValidationError):
        policy.check("alice", "doc:*")
    with pytest.raises(ValidationError):
        policy.add_role("")


def test_unassign_and_late_inheritance_take_effect_immediately() -> None:
    policy = make_policy()
    policy.add_role("auditor")
    policy.grant("auditor", "audit:read")
    policy.assign("dave", "auditor")
    assert not policy.is_allowed("dave", "doc:read")
    policy.inherit("auditor", "viewer")
    assert policy.is_allowed("dave", "doc:read")
    policy.unassign("dave", "auditor")
    assert policy.roles_of("dave") == set()
    assert not policy.is_allowed("dave", "audit:read")


def test_concurrent_assignments_and_checks() -> None:
    policy = make_policy()

    def worker(i: int) -> bool:
        user = f"u{i}"
        policy.assign(user, "editor")
        allowed = policy.is_allowed(user, "doc:write") and not policy.is_allowed(user, "billing:refund")
        policy.unassign(user, "editor")
        return allowed and not policy.is_allowed(user, "doc:write")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(worker, range(300)))
    assert all(results)
    assert policy.roles_of("u7") == set()
    assert policy.is_allowed("bob", "doc:write")
