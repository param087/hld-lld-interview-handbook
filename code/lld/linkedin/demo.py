"""A small network: requests that cross, degrees, privacy at read time, feed and jobs."""

from common import FakeClock, SequentialIdGenerator
from lld.linkedin.events import EventBus, NotificationService
from lld.linkedin.feeds import FeedService, JobService, MessagingService
from lld.linkedin.graph import ConnectionGraph
from lld.linkedin.models import (
    Experience,
    Member,
    PrivacyError,
    PrivacySettings,
    Profile,
    ReactionType,
    Visibility,
)
from lld.linkedin.services import ConnectionService, MemberDirectory, PrivacyGuard, ProfileService
from lld.linkedin.strategies import MaxExperience, RemoteOnly, RequiresSkill


def main() -> None:
    clock = FakeClock(start=1_700_000_000)
    directory, graph, bus = MemberDirectory(), ConnectionGraph(), EventBus()
    guard = PrivacyGuard(directory, graph)
    inbox = NotificationService()
    bus.subscribe_all(inbox)
    connections = ConnectionService(directory, graph, bus, clock, SequentialIdGenerator("r"))
    profiles = ProfileService(directory, graph, guard)
    feed = FeedService(directory, graph, guard, bus, clock, SequentialIdGenerator("p"))
    jobs = JobService(directory, bus, clock, SequentialIdGenerator("j"))
    messages = MessagingService(directory, guard, bus, clock, SequentialIdGenerator("c"))

    names = ["ana", "ben", "cara", "dev", "eve"]
    for name in names:
        directory.add(
            Member(
                name,
                name.title(),
                Profile(f"{name.title()} the engineer", experiences=[Experience("Acme", "SDE", 2018, 2024)]),
            )
        )
    directory.get("eve").privacy = PrivacySettings(profile=Visibility.CONNECTIONS)

    crossing = connections.send_request("ana", "ben", "worked together")
    crossed = connections.send_request("ben", "ana", "same here")
    print(f"ana->ben is {crossing.status}; ben->ana returned request {crossed.id} ({crossed.status})")
    print(f"edges after the crossing: {graph.edge_count()}")

    pending = connections.send_request("ben", "cara")
    connections.accept_request(pending.id, "cara")
    connections.accept_request(connections.send_request("cara", "dev").id, "dev")
    connections.accept_request(connections.send_request("dev", "eve").id, "eve")
    degrees = [(other, graph.degree("ana", other)) for other in names[1:]]
    print(f"degrees from ana: {degrees}")
    print(f"people ana may know: {profiles.people_you_may_know('ana', 3)}")

    print(f"ana sees cara: {profiles.view('ana', 'cara').line()}")
    print(f"ana sees eve:  {profiles.view('ana', 'eve').line()}")
    try:
        profiles.connections_of("ana", "eve")
    except PrivacyError as exc:
        print(f"connection list blocked: {exc}")

    withdrawn = connections.send_request("ben", "eve", "hello")
    connections.withdraw_request(withdrawn.id, "ben")
    print(f"request {withdrawn.id} is now {withdrawn.status}; eve has {len(connections.pending_for('eve'))} pending")

    connections.follow("ana", "cara")
    clock.advance(60)
    post = feed.publish("ben", "Shipped the scheduler", Visibility.CONNECTIONS)
    clock.advance(60)
    feed.publish("cara", "Hiring backend engineers", Visibility.PUBLIC)
    clock.advance(60)
    feed.publish("cara", "Team offsite photos", Visibility.CONNECTIONS)
    feed.react(post.id, "ana", ReactionType.CELEBRATE)
    print(f"ana feed: {[p.text for p in feed.feed('ana')]}")

    acme = jobs.add_company("Acme")
    jobs.post_job(acme.id, "Backend engineer", "Bengaluru", True, 3, frozenset({"python", "sql"}))
    jobs.post_job(acme.id, "Staff engineer", "Bengaluru", False, 8, frozenset({"python"}))
    matches = jobs.search(RequiresSkill("python") & RemoteOnly() & MaxExperience(6))
    print(f"jobs for a 6-year engineer: {[j.title for j in matches]}")
    application = jobs.apply(matches[0].id, "ana")
    print(f"application {application.id} is {application.status}")

    messages.send("ben", "ana", "congrats")
    print(f"ana inbox ({inbox.unread('ana')}): {inbox.messages('ana')[:2]}")


if __name__ == "__main__":
    main()
