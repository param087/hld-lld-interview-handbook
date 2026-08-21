"""Members, profiles, connection requests, posts, jobs and messages.

The only behaviour here is per-object invariants and the connection request's
state machine; the graph lives in ``graph.py`` and every rule that needs two
objects at once lives in ``services.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from common import ConflictError, InvalidStateError, NotFoundError, ValidationError


# --8<-- [start:enums]
class RequestStatus(StrEnum):
    PENDING = "pending"  # sent, waiting for the receiver
    ACCEPTED = "accepted"  # the edge exists
    REJECTED = "rejected"  # the receiver said no
    WITHDRAWN = "withdrawn"  # the sender took it back


class Visibility(StrEnum):
    """Who may see a profile section, a connection list or a post."""

    PUBLIC = "public"
    CONNECTIONS = "connections"  # first degree only
    NETWORK = "network"  # up to third degree
    PRIVATE = "private"  # nobody but the owner


class ReactionType(StrEnum):
    LIKE = "like"
    CELEBRATE = "celebrate"
    INSIGHTFUL = "insightful"
    SUPPORT = "support"


class ApplicationStatus(StrEnum):
    SUBMITTED = "submitted"
    SCREENING = "screening"
    INTERVIEW = "interview"
    OFFERED = "offered"
    DECLINED = "declined"


class EventType(StrEnum):
    REQUEST_SENT = "request_sent"
    REQUEST_ACCEPTED = "request_accepted"
    POST_PUBLISHED = "post_published"
    POST_REACTED = "post_reacted"
    MESSAGE_SENT = "message_sent"
    APPLICATION_SUBMITTED = "application_submitted"


# --8<-- [end:enums]


# --8<-- [start:errors]
class MemberNotFoundError(NotFoundError):
    """Unknown member, post, job or conversation id."""


class SelfConnectionError(ValidationError):
    """You cannot connect to, follow or message yourself."""


class AlreadyConnectedError(ConflictError):
    """An edge already exists between these two members."""


class DuplicateRequestError(ConflictError):
    """A pending request already exists for this pair."""


class RequestStateError(InvalidStateError):
    """The request is no longer pending, or the wrong member is acting on it."""


class PrivacyError(ValidationError):
    """The viewer is not allowed to see this, or not allowed to message this member."""


# --8<-- [end:errors]


# --8<-- [start:profile]
@dataclass(frozen=True, slots=True)
class Experience:
    company: str
    title: str
    start_year: int
    end_year: int | None = None

    def years(self, current_year: int) -> int:
        return max(0, (self.end_year or current_year) - self.start_year)


@dataclass(frozen=True, slots=True)
class Education:
    school: str
    degree: str
    end_year: int


@dataclass(slots=True)
class Skill:
    name: str
    endorsements: int = 0


@dataclass(frozen=True, slots=True)
class PrivacySettings:
    """Read-time policy. Nothing is filtered when it is written, only when it is read."""

    profile: Visibility = Visibility.NETWORK
    connections: Visibility = Visibility.CONNECTIONS
    messages_from: Visibility = Visibility.CONNECTIONS

    def with_profile(self, visibility: Visibility) -> PrivacySettings:
        return PrivacySettings(visibility, self.connections, self.messages_from)


@dataclass(slots=True)
class Profile:
    headline: str = ""
    summary: str = ""
    experiences: list[Experience] = field(default_factory=list)
    educations: list[Education] = field(default_factory=list)
    skills: list[Skill] = field(default_factory=list)

    def total_experience(self, current_year: int) -> int:
        return sum(e.years(current_year) for e in self.experiences)

    def skill(self, name: str) -> Skill | None:
        return next((s for s in self.skills if s.name.lower() == name.lower()), None)


@dataclass(slots=True)
class Member:
    id: str
    name: str
    profile: Profile = field(default_factory=Profile)
    privacy: PrivacySettings = field(default_factory=PrivacySettings)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValidationError("member name must be non-empty")


@dataclass(frozen=True, slots=True)
class ProfileView:
    """What a *particular viewer* is allowed to see. Built per read, never cached."""

    member_id: str
    name: str
    headline: str
    degree: int | None  # 1, 2, 3, or None when further away
    experiences: tuple[Experience, ...] = ()
    educations: tuple[Education, ...] = ()
    skills: tuple[str, ...] = ()
    restricted: bool = False

    DEGREE_LABELS = {0: "you", 1: "1st", 2: "2nd", 3: "3rd"}

    def line(self) -> str:
        distance = self.DEGREE_LABELS.get(self.degree, "out of network")
        tail = " [restricted]" if self.restricted else ""
        return f"{self.name} ({distance}) - {self.headline}{tail}"


# --8<-- [end:profile]


# --8<-- [start:entities]
@dataclass(slots=True)
class ConnectionRequest:
    """The State pattern in its lightweight form: a status plus guarded transitions."""

    id: str
    sender_id: str
    receiver_id: str
    created_at: float
    message: str = ""
    status: RequestStatus = RequestStatus.PENDING
    resolved_at: float | None = None

    def pair(self) -> tuple[str, str]:
        """The unordered key. Two crossing requests hash to the same slot."""
        return (self.sender_id, self.receiver_id) if self.sender_id < self.receiver_id else (
            self.receiver_id,
            self.sender_id,
        )

    def _resolve(self, status: RequestStatus, actor_id: str, expected: str, at: float) -> None:
        if self.status is not RequestStatus.PENDING:
            raise RequestStateError(f"request {self.id} is {self.status}, not pending")
        if actor_id != expected:
            raise RequestStateError(f"{actor_id} may not {status} request {self.id}")
        self.status = status
        self.resolved_at = at

    def accept(self, actor_id: str, at: float) -> None:
        self._resolve(RequestStatus.ACCEPTED, actor_id, self.receiver_id, at)

    def reject(self, actor_id: str, at: float) -> None:
        self._resolve(RequestStatus.REJECTED, actor_id, self.receiver_id, at)

    def withdraw(self, actor_id: str, at: float) -> None:
        self._resolve(RequestStatus.WITHDRAWN, actor_id, self.sender_id, at)


@dataclass(frozen=True, slots=True)
class Reaction:
    member_id: str
    type: ReactionType
    at: float


@dataclass(frozen=True, slots=True)
class Comment:
    id: str
    post_id: str
    author_id: str
    text: str
    at: float


@dataclass(slots=True)
class Post:
    id: str
    author_id: str
    text: str
    created_at: float
    visibility: Visibility = Visibility.CONNECTIONS
    reactions: dict[str, Reaction] = field(default_factory=dict)  # one per member
    comment_ids: list[str] = field(default_factory=list)

    def engagement(self) -> int:
        return len(self.reactions) + 2 * len(self.comment_ids)


@dataclass(frozen=True, slots=True)
class Company:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    company_id: str
    title: str
    location: str
    remote: bool
    min_experience: int
    skills: frozenset[str] = frozenset()
    posted_at: float = 0.0


@dataclass(slots=True)
class JobApplication:
    id: str
    job_id: str
    member_id: str
    submitted_at: float
    status: ApplicationStatus = ApplicationStatus.SUBMITTED


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    conversation_id: str
    sender_id: str
    text: str
    at: float


@dataclass(slots=True)
class Conversation:
    id: str
    participants: frozenset[str]
    messages: list[Message] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class NetworkEvent:
    """What travels on the event bus. Frozen, so a handler cannot corrupt it."""

    type: EventType
    at: float
    actor_id: str
    recipient_id: str
    subject_id: str = ""
    detail: str = ""


# --8<-- [end:entities]
