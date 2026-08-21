"""The surfaces built on top of the graph: the feed, messaging and jobs.

None of them owns an edge and none of them evaluates a visibility rule itself —
they call ``PrivacyGuard``. That is what keeps the privacy story auditable:
there is exactly one function to read when someone asks "who can see this?".
"""

from __future__ import annotations

import threading

from common import Clock, IdGenerator, SequentialIdGenerator, SystemClock
from lld.linkedin.events import EventBus
from lld.linkedin.graph import ConnectionGraph
from lld.linkedin.models import (
    ApplicationStatus,
    Comment,
    Company,
    Conversation,
    EventType,
    Job,
    JobApplication,
    MemberNotFoundError,
    Message,
    NetworkEvent,
    Post,
    PrivacyError,
    Reaction,
    ReactionType,
    SelfConnectionError,
    Visibility,
)
from lld.linkedin.services import MemberDirectory, PrivacyGuard
from lld.linkedin.strategies import ChronologicalFeed, FeedRanking, JobSpec


# --8<-- [start:feed]
class FeedService:
    """Posts, reactions and the chronological feed. Audience is resolved per read."""

    def __init__(
        self,
        directory: MemberDirectory,
        graph: ConnectionGraph,
        guard: PrivacyGuard,
        bus: EventBus,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
        ranking: FeedRanking | None = None,
    ) -> None:
        self._directory = directory
        self._graph = graph
        self._guard = guard
        self._bus = bus
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("p")
        self._ranking = ranking or ChronologicalFeed()
        self._lock = threading.Lock()
        self._posts: dict[str, Post] = {}
        self._comments: dict[str, Comment] = {}

    def publish(self, author_id: str, text: str, visibility: Visibility = Visibility.CONNECTIONS) -> Post:
        self._directory.get(author_id)
        post = Post(
            id=self._ids.next_id(),
            author_id=author_id,
            text=text,
            created_at=self._clock.now(),
            visibility=visibility,
        )
        with self._lock:
            self._posts[post.id] = post
        for peer in sorted(self._graph.connections(author_id)):
            self._bus.publish(
                NetworkEvent(EventType.POST_PUBLISHED, post.created_at, author_id, peer, post.id)
            )
        return post

    def post(self, post_id: str) -> Post:
        with self._lock:
            try:
                return self._posts[post_id]
            except KeyError:
                raise MemberNotFoundError(f"unknown post {post_id!r}") from None

    def react(self, post_id: str, member_id: str, reaction: ReactionType) -> Post:
        """One reaction per member: the dict key *is* the uniqueness constraint."""
        post = self.post(post_id)
        self._guard.require(member_id, post.author_id, post.visibility, "post")
        with self._lock:
            post.reactions[member_id] = Reaction(member_id, reaction, self._clock.now())
        self._bus.publish(
            NetworkEvent(EventType.POST_REACTED, self._clock.now(), member_id, post.author_id, post_id)
        )
        return post

    def comment(self, post_id: str, author_id: str, text: str) -> Comment:
        post = self.post(post_id)
        self._guard.require(author_id, post.author_id, post.visibility, "post")
        entry = Comment(self._ids.next_id(), post_id, author_id, text, self._clock.now())
        with self._lock:
            self._comments[entry.id] = entry
            post.comment_ids.append(entry.id)
        return entry

    def feed(self, viewer_id: str, ranking: FeedRanking | None = None, limit: int = 20) -> list[Post]:
        """Connections plus follows plus yourself, filtered by each post's visibility."""
        audience = self._graph.connections(viewer_id) | self._graph.following(viewer_id) | {viewer_id}
        with self._lock:
            candidates = [p for p in self._posts.values() if p.author_id in audience]
        visible = [
            p for p in candidates if self._guard.may_see(viewer_id, p.author_id, p.visibility)
        ]
        return (ranking or self._ranking).rank(visible)[:limit]


# --8<-- [end:feed]


# --8<-- [start:messaging]
class MessagingService:
    """Direct messages, with the recipient's ``messages_from`` rule checked on send."""

    def __init__(
        self,
        directory: MemberDirectory,
        guard: PrivacyGuard,
        bus: EventBus,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        self._directory = directory
        self._guard = guard
        self._bus = bus
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("c")
        self._lock = threading.Lock()
        self._conversations: dict[frozenset[str], Conversation] = {}

    def send(self, sender_id: str, recipient_id: str, text: str) -> Message:
        if sender_id == recipient_id:
            raise SelfConnectionError("a member cannot message themselves")
        recipient = self._directory.get(recipient_id)
        if not self._guard.may_see(sender_id, recipient_id, recipient.privacy.messages_from):
            raise PrivacyError(f"{recipient_id} does not accept messages from {sender_id}")
        key = frozenset({sender_id, recipient_id})
        with self._lock:
            conversation = self._conversations.get(key)
            if conversation is None:
                conversation = Conversation(self._ids.next_id(), key)
                self._conversations[key] = conversation
            message = Message(
                self._ids.next_id(), conversation.id, sender_id, text, self._clock.now()
            )
            conversation.messages.append(message)
        self._bus.publish(
            NetworkEvent(EventType.MESSAGE_SENT, message.at, sender_id, recipient_id, conversation.id)
        )
        return message

    def conversation(self, viewer_id: str, other_id: str) -> Conversation:
        key = frozenset({viewer_id, other_id})
        with self._lock:
            conversation = self._conversations.get(key)
        if conversation is None:
            raise MemberNotFoundError(f"no conversation between {viewer_id} and {other_id}")
        return conversation


# --8<-- [end:messaging]


# --8<-- [start:jobs]
class JobService:
    """Job postings, Specification-driven search, and applications."""

    def __init__(
        self,
        directory: MemberDirectory,
        bus: EventBus,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        self._directory = directory
        self._bus = bus
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("j")
        self._lock = threading.Lock()
        self._companies: dict[str, Company] = {}
        self._jobs: dict[str, Job] = {}
        self._applications: dict[str, JobApplication] = {}

    def add_company(self, name: str) -> Company:
        company = Company(self._ids.next_id(), name)
        with self._lock:
            self._companies[company.id] = company
        return company

    def post_job(
        self,
        company_id: str,
        title: str,
        location: str,
        remote: bool = False,
        min_experience: int = 0,
        skills: frozenset[str] = frozenset(),
    ) -> Job:
        job = Job(
            id=self._ids.next_id(),
            company_id=company_id,
            title=title,
            location=location,
            remote=remote,
            min_experience=min_experience,
            skills=frozenset(s.lower() for s in skills),
            posted_at=self._clock.now(),
        )
        with self._lock:
            self._jobs[job.id] = job
        return job

    def search(self, spec: JobSpec, limit: int = 10) -> list[Job]:
        with self._lock:
            jobs = list(self._jobs.values())
        return [j for j in jobs if spec.is_satisfied_by(j)][:limit]

    def apply(self, job_id: str, member_id: str) -> JobApplication:
        self._directory.get(member_id)
        with self._lock:
            if job_id not in self._jobs:
                raise MemberNotFoundError(f"unknown job {job_id!r}")
            existing = next(
                (a for a in self._applications.values() if a.job_id == job_id and a.member_id == member_id),
                None,
            )
            if existing is not None:
                return existing  # applying twice is idempotent, not an error
            application = JobApplication(
                self._ids.next_id(), job_id, member_id, self._clock.now()
            )
            self._applications[application.id] = application
        self._bus.publish(
            NetworkEvent(
                EventType.APPLICATION_SUBMITTED,
                application.submitted_at,
                member_id,
                self._jobs[job_id].company_id,
                job_id,
            )
        )
        return application

    def advance(self, application_id: str, status: ApplicationStatus) -> JobApplication:
        with self._lock:
            try:
                application = self._applications[application_id]
            except KeyError:
                raise MemberNotFoundError(f"unknown application {application_id!r}") from None
            application.status = status
            return application


# --8<-- [end:jobs]
