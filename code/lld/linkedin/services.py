"""The member directory, the connection service and the read-time privacy guard.

``ConnectionService`` is the only writer of the graph, and it is where the two
races live: two members sending each other a request at the same instant, and
two tabs accepting the same request. ``PrivacyGuard`` is the only place a
visibility rule is evaluated, and it runs on every read.
"""

from __future__ import annotations

import threading

from common import Clock, IdGenerator, SequentialIdGenerator, SystemClock
from lld.linkedin.events import EventBus
from lld.linkedin.graph import ConnectionGraph
from lld.linkedin.models import (
    AlreadyConnectedError,
    ConnectionRequest,
    DuplicateRequestError,
    EventType,
    Member,
    MemberNotFoundError,
    NetworkEvent,
    PrivacyError,
    ProfileView,
    RequestStatus,
    SelfConnectionError,
    Skill,
    Visibility,
)


# --8<-- [start:directory]
class MemberDirectory:
    """Members by id. The persistence seam: one dict today, one table tomorrow."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._members: dict[str, Member] = {}

    def add(self, member: Member) -> Member:
        with self._lock:
            self._members[member.id] = member
        return member

    def get(self, member_id: str) -> Member:
        with self._lock:
            try:
                return self._members[member_id]
            except KeyError:
                raise MemberNotFoundError(f"unknown member {member_id!r}") from None

    def all(self) -> list[Member]:
        with self._lock:
            return list(self._members.values())


# --8<-- [end:directory]


# --8<-- [start:connections]
class ConnectionService:
    """Requests in, edges out. The only writer of ``ConnectionGraph``.

    ``_lock`` guards the request table and the pending-pair index. It is always
    acquired *before* the graph's lock and never the other way round, so the two
    cannot deadlock. Handlers on the bus run after both are released.
    """

    def __init__(
        self,
        directory: MemberDirectory,
        graph: ConnectionGraph,
        bus: EventBus,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        self._directory = directory
        self._graph = graph
        self._bus = bus
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("r")
        self._lock = threading.Lock()
        self._requests: dict[str, ConnectionRequest] = {}
        self._pending: dict[tuple[str, str], ConnectionRequest] = {}

    def send_request(self, sender_id: str, receiver_id: str, message: str = "") -> ConnectionRequest:
        """Send, or auto-accept when the other member has already asked.

        The pending index is keyed by the *unordered* pair, so two crossing
        requests contend for one slot. Exactly one of them creates it and
        exactly one of them accepts it — never two edges, never two rows.
        """
        if sender_id == receiver_id:
            raise SelfConnectionError("a member cannot connect to themselves")
        self._directory.get(sender_id)
        self._directory.get(receiver_id)
        key = self._pair(sender_id, receiver_id)
        with self._lock:
            if self._graph.are_connected(sender_id, receiver_id):
                raise AlreadyConnectedError(f"{sender_id} and {receiver_id} are already connected")
            existing = self._pending.get(key)
            if existing is not None and existing.sender_id == sender_id:
                raise DuplicateRequestError(f"{sender_id} already has a pending request to {receiver_id}")
            if existing is not None:
                # Crossing requests: the sender is answering a question already asked.
                existing.accept(sender_id, self._clock.now())
                del self._pending[key]
                self._graph.add_edge(sender_id, receiver_id)
                resolved = existing
            else:
                resolved = ConnectionRequest(
                    id=self._ids.next_id(),
                    sender_id=sender_id,
                    receiver_id=receiver_id,
                    created_at=self._clock.now(),
                    message=message,
                )
                self._requests[resolved.id] = resolved
                self._pending[key] = resolved
        self._announce(resolved, sender_id)
        return resolved

    def accept_request(self, request_id: str, actor_id: str) -> ConnectionRequest:
        request = self._transition(request_id, actor_id, RequestStatus.ACCEPTED)
        self._announce(request, actor_id)
        return request

    def reject_request(self, request_id: str, actor_id: str) -> ConnectionRequest:
        return self._transition(request_id, actor_id, RequestStatus.REJECTED)

    def withdraw_request(self, request_id: str, actor_id: str) -> ConnectionRequest:
        return self._transition(request_id, actor_id, RequestStatus.WITHDRAWN)

    def disconnect(self, member_id: str, other_id: str) -> bool:
        with self._lock:
            return self._graph.remove_edge(member_id, other_id)

    def follow(self, follower_id: str, target_id: str) -> None:
        self._directory.get(target_id)
        self._graph.follow(follower_id, target_id)

    def request(self, request_id: str) -> ConnectionRequest:
        with self._lock:
            return self._get(request_id)

    def _get(self, request_id: str) -> ConnectionRequest:
        """Caller already holds ``_lock``; a plain Lock is not reentrant."""
        try:
            return self._requests[request_id]
        except KeyError:
            raise MemberNotFoundError(f"unknown request {request_id!r}") from None

    def pending_for(self, member_id: str) -> list[ConnectionRequest]:
        with self._lock:
            return [r for r in self._pending.values() if r.receiver_id == member_id]

    def _transition(self, request_id: str, actor_id: str, status: RequestStatus) -> ConnectionRequest:
        with self._lock:
            request = self._get(request_id)
            now = self._clock.now()
            if status is RequestStatus.ACCEPTED:
                request.accept(actor_id, now)
                self._pending.pop(request.pair(), None)
                self._graph.add_edge(request.sender_id, request.receiver_id)
            elif status is RequestStatus.REJECTED:
                request.reject(actor_id, now)
                self._pending.pop(request.pair(), None)
            else:
                request.withdraw(actor_id, now)
                self._pending.pop(request.pair(), None)
            return request

    def _announce(self, request: ConnectionRequest, actor_id: str) -> None:
        """Published outside every lock."""
        accepted = request.status is RequestStatus.ACCEPTED
        other = request.sender_id if actor_id == request.receiver_id else request.receiver_id
        self._bus.publish(
            NetworkEvent(
                type=EventType.REQUEST_ACCEPTED if accepted else EventType.REQUEST_SENT,
                at=self._clock.now(),
                actor_id=actor_id,
                recipient_id=other,
                subject_id=request.id,
                detail=request.message,
            )
        )

    @staticmethod
    def _pair(a: str, b: str) -> tuple[str, str]:
        return (a, b) if a < b else (b, a)


# --8<-- [end:connections]


# --8<-- [start:privacy]
class PrivacyGuard:
    """The one place a visibility rule is evaluated, and it runs on every read.

    Filtering at write time would be faster and wrong: a member who changes
    their settings must change what every future read returns, including reads
    of data written years earlier.
    """

    def __init__(self, directory: MemberDirectory, graph: ConnectionGraph) -> None:
        self._directory = directory
        self._graph = graph

    def degree(self, viewer_id: str, member_id: str) -> int | None:
        return self._graph.degree(viewer_id, member_id)

    def may_see(self, viewer_id: str, owner_id: str, visibility: Visibility) -> bool:
        if viewer_id == owner_id or visibility is Visibility.PUBLIC:
            return True
        if visibility is Visibility.PRIVATE:
            return False
        degree = self.degree(viewer_id, owner_id)
        if visibility is Visibility.CONNECTIONS:
            return degree == 1
        return degree is not None  # NETWORK: anyone within three hops

    def require(self, viewer_id: str, owner_id: str, visibility: Visibility, what: str) -> None:
        if not self.may_see(viewer_id, owner_id, visibility):
            raise PrivacyError(f"{viewer_id} may not see the {what} of {owner_id}")


class ProfileService:
    """Profile reads, always through the guard, always rebuilt for this viewer."""

    def __init__(self, directory: MemberDirectory, graph: ConnectionGraph, guard: PrivacyGuard) -> None:
        self._directory = directory
        self._graph = graph
        self._guard = guard

    def view(self, viewer_id: str, member_id: str) -> ProfileView:
        member = self._directory.get(member_id)
        degree = 0 if viewer_id == member_id else self._guard.degree(viewer_id, member_id)
        if not self._guard.may_see(viewer_id, member_id, member.privacy.profile):
            return ProfileView(member.id, member.name, member.profile.headline, degree, restricted=True)
        return ProfileView(
            member_id=member.id,
            name=member.name,
            headline=member.profile.headline,
            degree=degree,
            experiences=tuple(member.profile.experiences),
            educations=tuple(member.profile.educations),
            skills=tuple(s.name for s in member.profile.skills),
        )

    def connections_of(self, viewer_id: str, member_id: str) -> list[str]:
        member = self._directory.get(member_id)
        self._guard.require(viewer_id, member_id, member.privacy.connections, "connection list")
        return sorted(self._graph.connections(member_id))

    def endorse(self, endorser_id: str, member_id: str, skill_name: str) -> Skill:
        """Endorsing needs a first-degree connection — a rule, not a suggestion."""
        if self._guard.degree(endorser_id, member_id) != 1:
            raise PrivacyError(f"{endorser_id} must be connected to {member_id} to endorse")
        member = self._directory.get(member_id)
        skill = member.profile.skill(skill_name)
        if skill is None:
            skill = Skill(skill_name)
            member.profile.skills.append(skill)
        skill.endorsements += 1
        return skill

    def people_you_may_know(self, member_id: str, limit: int = 5) -> list[tuple[str, int]]:
        return self._graph.people_you_may_know(member_id, limit)


# --8<-- [end:privacy]
