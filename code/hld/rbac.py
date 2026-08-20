"""Role-based access control with inherited roles, wildcard permissions and attribute conditions.

What the module demonstrates, in the order an interviewer asks about it:

* Permissions are ``resource:action`` strings; a grant may use ``*`` as the action or as the
  whole permission, so ``doc:*`` covers every document action and ``*`` is superuser.
* Roles inherit: ``admin`` includes ``editor`` includes ``viewer``. ``roles_of`` computes the
  transitive closure and ``add_role`` refuses cycles.
* ``check`` answers allow/deny with the role that granted it, so audit logs can explain
  decisions. Unknown users, roles and permissions deny by default.
* A grant may carry a *condition* on request attributes (owner-only delete, business hours).
  That single hook is the step from RBAC to ABAC: roles answer *who*, conditions answer
  *under which circumstances*.
"""

from __future__ import annotations

import re
import threading
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from common import ConflictError, NotFoundError, ValidationError

Context = Mapping[str, object]
Condition = Callable[[Context], bool]

PERMISSION = re.compile(r"^(\*|[a-z][a-z0-9_-]*:(\*|[a-z][a-z0-9_-]*))$")


# --8<-- [start:grants]
def validate_permission(permission: str) -> str:
    """``resource:action`` in lower snake/kebab case; ``resource:*`` or ``*`` only in grants."""
    if not PERMISSION.match(permission):
        raise ValidationError(f"malformed permission {permission!r}; expected resource:action")
    return permission


def permission_matches(granted: str, requested: str) -> bool:
    """Does a granted pattern cover a concrete ``resource:action`` request?"""
    if granted == "*":
        return True
    g_resource, g_action = granted.split(":")
    r_resource, r_action = requested.split(":")
    return g_resource == r_resource and g_action in ("*", r_action)


@dataclass(frozen=True, slots=True)
class Grant:
    """A permission pattern attached to a role, optionally guarded by an attribute condition."""

    permission: str
    condition: Condition | None = None
    label: str = ""

    def covers(self, requested: str, context: Context) -> bool:
        if not permission_matches(self.permission, requested):
            return False
        return self.condition is None or bool(self.condition(context))


@dataclass(frozen=True, slots=True)
class Decision:
    """The answer plus the evidence an audit log needs."""

    allowed: bool
    user: str
    permission: str
    via_role: str | None = None
    via_grant: str | None = None
    reason: str = ""


# --8<-- [end:grants]


# --8<-- [start:policy]
@dataclass(slots=True)
class Role:
    name: str
    parents: set[str] = field(default_factory=set)
    grants: list[Grant] = field(default_factory=list)


class RbacPolicy:
    """Roles, their grants, their parents and the user-to-role assignments.

    ``_lock`` guards ``_roles`` and ``_assignments``. Policy changes are rare and checks are
    hot, so ``check`` snapshots what it needs under the lock and evaluates outside it.
    """

    def __init__(self) -> None:
        self._roles: dict[str, Role] = {}
        self._assignments: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    def add_role(self, name: str, inherits: Iterable[str] = ()) -> None:
        """Create ``name``; it inherits every grant of the roles in ``inherits``."""
        if not name:
            raise ValidationError("role name must be non-empty")
        parents = set(inherits)
        with self._lock:
            if name in self._roles:
                raise ConflictError(f"role {name!r} already exists")
            for parent in parents:
                self._role(parent)
            self._roles[name] = Role(name, parents)

    def inherit(self, child: str, parent: str) -> None:
        """Make ``child`` inherit ``parent``; refuses anything that would close a cycle."""
        with self._lock:
            self._role(child)
            if child in self._closure(parent):
                raise ValidationError(f"{parent!r} already inherits {child!r}; that would be a cycle")
            self._roles[child].parents.add(parent)

    def grant(
        self, role: str, permission: str, condition: Condition | None = None, label: str = ""
    ) -> None:
        validate_permission(permission)
        with self._lock:
            self._role(role).grants.append(Grant(permission, condition, label))

    def assign(self, user: str, role: str) -> None:
        if not user:
            raise ValidationError("user must be non-empty")
        with self._lock:
            self._role(role)
            self._assignments.setdefault(user, set()).add(role)

    def unassign(self, user: str, role: str) -> None:
        with self._lock:
            roles = self._assignments.get(user, set())
            if role not in roles:
                raise NotFoundError(f"user {user!r} does not have role {role!r}")
            roles.discard(role)

    def roles_of(self, user: str) -> set[str]:
        """Direct roles plus everything they inherit (transitive closure)."""
        with self._lock:
            direct = set(self._assignments.get(user, set()))
            closure: set[str] = set()
            for role in direct:
                closure |= self._closure(role)
            return closure

    def permissions_of(self, user: str) -> set[str]:
        """Unconditional permission patterns; conditional grants need a context to evaluate."""
        with self._lock:
            roles = [self._roles[r] for r in self._user_closure(user)]
        return {g.permission for role in roles for g in role.grants if g.condition is None}

    def check(self, user: str, permission: str, context: Context | None = None) -> Decision:
        """Allow iff some role of ``user`` holds a grant covering ``permission`` whose condition passes."""
        validate_permission(permission)
        if "*" in permission:
            raise ValidationError("check a concrete resource:action, not a pattern")
        ctx: Context = {"user": user, **(context or {})}
        with self._lock:
            roles = [self._roles[r] for r in sorted(self._user_closure(user))]
        blocked: str | None = None
        for role in roles:
            for grant in role.grants:
                if not permission_matches(grant.permission, permission):
                    continue
                if grant.covers(permission, ctx):
                    label = grant.label or grant.permission
                    return Decision(True, user, permission, role.name, label, "granted")
                blocked = blocked or f"condition not met for [{grant.label or grant.permission}]"
        if not roles:
            return Decision(False, user, permission, reason="user has no roles")
        return Decision(False, user, permission, reason=blocked or "no role grants it")

    def is_allowed(self, user: str, permission: str, context: Context | None = None) -> bool:
        return self.check(user, permission, context).allowed

    def _role(self, name: str) -> Role:
        if name not in self._roles:
            raise NotFoundError(f"unknown role {name!r}")
        return self._roles[name]

    def _closure(self, role: str) -> set[str]:
        """``role`` and every ancestor, breadth-first; caller holds the lock."""
        seen = {role}
        queue = deque([role])
        while queue:
            for parent in self._roles[queue.popleft()].parents:
                if parent not in seen:
                    seen.add(parent)
                    queue.append(parent)
        return seen

    def _user_closure(self, user: str) -> set[str]:
        closure: set[str] = set()
        for role in self._assignments.get(user, set()):
            closure |= self._closure(role)
        return closure


# --8<-- [end:policy]


def main() -> None:
    policy = RbacPolicy()
    policy.add_role("viewer")
    policy.add_role("editor", inherits=["viewer"])
    policy.add_role("admin", inherits=["editor"])
    policy.grant("viewer", "doc:read")
    policy.grant("editor", "doc:write")
    policy.grant(
        "editor",
        "doc:delete",
        condition=lambda c: c.get("owner") == c["user"],
        label="doc:delete if owner",
    )
    policy.grant("admin", "*")
    for user, role in [("alice", "admin"), ("bob", "editor"), ("carol", "viewer")]:
        policy.assign(user, role)
    print("roles: viewer <- editor <- admin (arrow = inherits)")
    print(f"bob's effective roles: {sorted(policy.roles_of('bob'))}")
    print(f"bob's unconditional permissions: {sorted(policy.permissions_of('bob'))}")

    def show(user: str, permission: str, **context: object) -> None:
        d = policy.check(user, permission, context)
        verdict = "ALLOW" if d.allowed else "DENY "
        evidence = f"via {d.via_role} [{d.via_grant}]" if d.allowed else d.reason
        ctx = f" ctx={context}" if context else ""
        print(f"  {verdict} {user:<6} {permission:<15}{ctx:<22} {evidence}")

    print("checks:")
    show("carol", "doc:read")
    show("carol", "doc:write")
    show("bob", "doc:write")
    show("bob", "doc:delete", owner="bob")
    show("bob", "doc:delete", owner="alice")
    show("alice", "doc:delete", owner="bob")
    show("alice", "billing:refund")
    show("dave", "doc:read")
    try:
        policy.inherit("viewer", "admin")
    except ValidationError as exc:
        print(f"cycle refused: {exc}")
    policy.unassign("bob", "editor")
    show("bob", "doc:read")


if __name__ == "__main__":
    main()
