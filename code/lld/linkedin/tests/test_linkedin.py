from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import pytest

from common import FakeClock, SequentialIdGenerator
from lld.linkedin.events import EventBus, NotificationService
from lld.linkedin.feeds import FeedService, JobService, MessagingService
from lld.linkedin.graph import ConnectionGraph
from lld.linkedin.models import (
    AlreadyConnectedError,
    ApplicationStatus,
    DuplicateRequestError,
    Member,
    PrivacyError,
    PrivacySettings,
    Profile,
    ReactionType,
    RequestStateError,
    RequestStatus,
    SelfConnectionError,
    Visibility,
)
from lld.linkedin.services import ConnectionService, MemberDirectory, PrivacyGuard, ProfileService
from lld.linkedin.strategies import (
    AtCompany,
    EngagementFeed,
    InLocation,
    MaxExperience,
    RemoteOnly,
    RequiresSkill,
)


@dataclass(slots=True)
class Network:
    """Everything wired together, so each test reads as a scenario."""

    directory: MemberDirectory
    graph: ConnectionGraph
    bus: EventBus
    guard: PrivacyGuard
    connections: ConnectionService
    profiles: ProfileService
    inbox: NotificationService
    clock: FakeClock
    members: list[str] = field(default_factory=list)

    def connect(self, a: str, b: str) -> None:
        request = self.connections.send_request(a, b)
        if request.status is RequestStatus.PENDING:
            self.connections.accept_request(request.id, b)

    def chain(self, *names: str) -> None:
        for a, b in zip(names, names[1:], strict=False):
            self.connect(a, b)


@pytest.fixture
def net() -> Network:
    clock = FakeClock(start=1_000_000)
    directory, graph, bus = MemberDirectory(), ConnectionGraph(), EventBus()
    guard = PrivacyGuard(directory, graph)
    inbox = NotificationService()
    bus.subscribe_all(inbox)
    names = ["ana", "ben", "cara", "dev", "eve", "fin"]
    for name in names:
        directory.add(Member(name, name.title(), Profile(f"{name.title()} the engineer")))
    return Network(
        directory=directory,
        graph=graph,
        bus=bus,
        guard=guard,
        connections=ConnectionService(directory, graph, bus, clock, SequentialIdGenerator("r")),
        profiles=ProfileService(directory, graph, guard),
        inbox=inbox,
        clock=clock,
        members=names,
    )


def test_send_then_accept_creates_one_edge_and_notifies_both_sides(net: Network) -> None:
    request = net.connections.send_request("ana", "ben", "worked together")
    assert request.status is RequestStatus.PENDING
    assert [r.id for r in net.connections.pending_for("ben")] == [request.id]
    assert net.inbox.messages("ben") == ["ana wants to connect: worked together"]

    net.connections.accept_request(request.id, "ben")
    assert request.status is RequestStatus.ACCEPTED and request.resolved_at == net.clock.now()
    assert net.graph.are_connected("ana", "ben") and net.graph.edge_count() == 1
    assert net.inbox.messages("ana") == ["ben is now a connection: worked together"]
    assert net.connections.pending_for("ben") == []


def test_request_validation_and_wrong_actor_transitions(net: Network) -> None:
    with pytest.raises(SelfConnectionError):
        net.connections.send_request("ana", "ana")
    request = net.connections.send_request("ana", "ben")
    with pytest.raises(DuplicateRequestError):
        net.connections.send_request("ana", "ben")
    with pytest.raises(RequestStateError):
        net.connections.accept_request(request.id, "ana")  # the sender cannot accept
    with pytest.raises(RequestStateError):
        net.connections.withdraw_request(request.id, "ben")  # the receiver cannot withdraw

    net.connections.reject_request(request.id, "ben")
    assert request.status is RequestStatus.REJECTED and not net.graph.are_connected("ana", "ben")
    with pytest.raises(RequestStateError):
        net.connections.accept_request(request.id, "ben")  # resolved once, resolved for good


@pytest.mark.parametrize(
    ("target", "expected"),
    [("ana", 0), ("ben", 1), ("cara", 2), ("dev", 3), ("eve", None), ("fin", None)],
)
def test_bfs_reports_degrees_up_to_the_limit(net: Network, target: str, expected: int | None) -> None:
    net.chain("ana", "ben", "cara", "dev", "eve")
    assert net.graph.degree("ana", target) == expected
    assert net.graph.degree("ana", "eve", max_depth=4) == 4  # the limit is policy, not distance


def test_people_you_may_know_ranks_second_degree_by_mutual_connections(net: Network) -> None:
    net.chain("ana", "ben", "cara")
    net.connect("ana", "dev")
    net.connect("dev", "cara")  # cara now has two mutual connections with ana
    net.connect("ben", "eve")  # eve has one
    assert net.profiles.people_you_may_know("ana") == [("cara", 2), ("eve", 1)]
    assert net.graph.mutual("ana", "cara") == {"ben", "dev"}


# --8<-- [start:crossing]
def test_crossing_requests_auto_accept_exactly_once(net: Network) -> None:
    """Twenty threads, ten each way: one edge, one accepted request, no duplicates."""
    def act(i: int) -> str:
        sender, receiver = ("ana", "ben") if i % 2 == 0 else ("ben", "ana")
        try:
            return net.connections.send_request(sender, receiver).status
        except (DuplicateRequestError, AlreadyConnectedError) as exc:
            return type(exc).__name__

    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(act, range(20)))

    assert outcomes.count(RequestStatus.PENDING) == 1  # exactly one request was created
    assert outcomes.count(RequestStatus.ACCEPTED) == 1  # exactly one auto-accept
    assert net.graph.edge_count() == 1 and net.graph.are_connected("ana", "ben")
    assert len(net.connections.pending_for("ana")) == 0
    assert len(net.connections.pending_for("ben")) == 0


# --8<-- [end:crossing]


# --8<-- [start:privacy]
def test_privacy_is_evaluated_on_every_read_not_at_write_time(net: Network) -> None:
    net.chain("ana", "ben", "cara")
    net.directory.get("cara").privacy = PrivacySettings(profile=Visibility.NETWORK)

    view = net.profiles.view("ana", "cara")
    assert view.degree == 2 and not view.restricted and view.headline == "Cara the engineer"

    # Cara tightens her settings; the *same* profile data must now read differently.
    net.directory.get("cara").privacy = PrivacySettings(profile=Visibility.CONNECTIONS)
    tightened = net.profiles.view("ana", "cara")
    assert tightened.restricted and tightened.experiences == ()
    assert not net.profiles.view("cara", "cara").restricted  # owners always see themselves

    with pytest.raises(PrivacyError):
        net.profiles.connections_of("ana", "cara")
    assert net.profiles.connections_of("ben", "cara") == ["ben"]
    with pytest.raises(PrivacyError):
        net.profiles.endorse("ana", "cara", "python")  # endorsing needs first degree
    assert net.profiles.endorse("ben", "cara", "python").endorsements == 1


# --8<-- [end:privacy]


def test_feed_shows_the_audience_intersected_with_each_posts_visibility(net: Network) -> None:
    net.chain("ana", "ben", "cara")
    net.connections.follow("ana", "cara")
    feed = FeedService(
        net.directory, net.graph, net.guard, net.bus, net.clock, SequentialIdGenerator("p")
    )
    net.clock.advance(10)
    mine = feed.publish("ana", "my own update")
    net.clock.advance(10)
    first_degree = feed.publish("ben", "ben ships", Visibility.CONNECTIONS)
    net.clock.advance(10)
    public = feed.publish("cara", "cara hires", Visibility.PUBLIC)
    offsite = feed.publish("cara", "cara offsite", Visibility.CONNECTIONS)  # followed, 2nd degree
    feed.publish("dev", "unreachable", Visibility.PUBLIC)  # not in the audience at all

    assert [p.id for p in feed.feed("ana")] == [public.id, first_degree.id, mine.id]
    feed.react(first_degree.id, "ana", ReactionType.CELEBRATE)
    feed.react(first_degree.id, "ana", ReactionType.LIKE)  # one reaction per member
    assert len(first_degree.reactions) == 1
    assert [p.id for p in feed.feed("ana", EngagementFeed())][0] == first_degree.id
    with pytest.raises(PrivacyError):
        feed.comment(offsite.id, "dev", "nice")  # dev is out of cara's network


def test_messaging_respects_the_recipients_policy(net: Network) -> None:
    net.chain("ana", "ben", "cara")
    messages = MessagingService(net.directory, net.guard, net.bus, net.clock, SequentialIdGenerator("c"))
    messages.send("ana", "ben", "hello")
    assert [m.text for m in messages.conversation("ana", "ben").messages] == ["hello"]
    with pytest.raises(PrivacyError):
        messages.send("ana", "cara", "hello")  # cara accepts connections only

    net.directory.get("cara").privacy = PrivacySettings(messages_from=Visibility.PUBLIC)
    assert messages.send("ana", "cara", "hello").sender_id == "ana"
    with pytest.raises(SelfConnectionError):
        messages.send("ana", "ana", "note to self")


def test_job_search_composes_specifications_and_applications_are_idempotent(net: Network) -> None:
    jobs = JobService(net.directory, net.bus, net.clock, SequentialIdGenerator("j"))
    acme = jobs.add_company("Acme")
    other = jobs.add_company("Globex")
    remote = jobs.post_job(acme.id, "Backend", "Bengaluru", True, 3, frozenset({"python", "sql"}))
    senior = jobs.post_job(acme.id, "Staff", "Bengaluru", False, 8, frozenset({"python"}))
    far = jobs.post_job(other.id, "Backend", "Berlin", True, 2, frozenset({"go"}))

    assert [j.id for j in jobs.search(RequiresSkill("python") & RemoteOnly())] == [remote.id]
    assert [j.id for j in jobs.search(InLocation("berlin") | AtCompany(acme.id))] == [
        remote.id,
        senior.id,
        far.id,
    ]
    assert [j.id for j in jobs.search(~MaxExperience(6))] == [senior.id]

    application = jobs.apply(remote.id, "ana")
    assert jobs.apply(remote.id, "ana").id == application.id  # applying twice is one application
    assert jobs.advance(application.id, ApplicationStatus.INTERVIEW).status is ApplicationStatus.INTERVIEW
