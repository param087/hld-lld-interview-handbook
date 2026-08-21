import random
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, ValidationError
from hld.comment_fanout import CommentBus, CommentService, EdgeServer


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_000.0)


@pytest.fixture
def bus() -> CommentBus:
    return CommentBus()


@pytest.fixture
def service(bus: CommentBus, clock: FakeClock) -> CommentService:
    return CommentService(
        bus,
        clock,
        random.Random(42),
        max_per_batch=5,
        window_s=0.2,
        hot_window_s=1.0,
        hot_viewers=100,
    )


def test_batching_turns_many_comments_into_one_frame_per_viewer(service, bus, clock) -> None:
    edge = EdgeServer("edge-1", bus, service)
    edge.join("s1", "v1")
    edge.join("s1", "v2")
    for i in range(4):
        service.publish("s1", f"u{i}", f"hello {i}")
    assert edge.socket_writes() == 0  # nothing leaves until the window closes
    clock.advance(0.2)
    batches = service.tick()
    assert len(batches) == 1 and batches[0].window_size == 4
    assert [c.seq for c in batches[0].comments] == [1, 2, 3, 4]
    assert len(edge.outbox("v1")) == 1  # four comments, one frame
    assert edge.socket_writes() == 2  # one write per local viewer, not per comment
    assert service.tick() == []  # the buffer is empty again


def test_sampling_keeps_priority_comments_and_reports_the_drop(service, bus, clock) -> None:
    edge = EdgeServer("edge-1", bus, service)
    edge.join("s1", "v1")
    for i in range(40):
        service.publish("s1", f"u{i}", f"spam {i}")
    moderator = service.publish("s1", "mod", "keep it civil", priority=True)
    broadcaster = service.publish("s1", "host", "thanks for watching", priority=True)
    clock.advance(0.2)
    batch = service.tick()[0]
    assert batch.window_size == 42
    assert len(batch.comments) == 5  # the cap
    assert batch.dropped == 37 and batch.sampled is True
    seqs = [c.seq for c in batch.comments]
    assert seqs == sorted(seqs)  # sampling never reorders the conversation
    assert {moderator.seq, broadcaster.seq} <= set(seqs)  # priority messages always survive


def test_one_bus_delivery_per_edge_server_not_per_viewer(service, bus, clock) -> None:
    edges = [EdgeServer(f"edge-{i}", bus, service) for i in range(3)]
    for i in range(300):
        edges[i % 3].join("s1", f"v{i}")
    assert bus.subscriber_count("s1") == 3  # three subscriptions for three hundred viewers
    assert service.viewer_count("s1") == 300
    service.publish("s1", "u1", "hi")
    clock.advance(1.0)
    batch = service.tick()[0]
    assert bus.publish("s1", batch) == 3  # a republish reaches three servers
    assert sum(edge.socket_writes() for edge in edges) == 600  # two deliveries x 300 sockets


def test_the_window_widens_once_a_stream_is_hot(service, bus, clock) -> None:
    edge = EdgeServer("edge-1", bus, service)
    edge.join("s1", "v0")
    assert service.window_s("s1") == 0.2
    for i in range(1, 150):
        edge.join("s1", f"v{i}")
    assert service.viewer_count("s1") == 150
    assert service.window_s("s1") == 1.0  # over the 100-viewer hot threshold
    service.publish("s1", "u1", "hello")
    clock.advance(0.5)
    assert service.tick() == []  # 0.5 s is not yet a hot window
    clock.advance(0.5)
    assert len(service.tick()) == 1


def test_tick_flushes_only_the_streams_whose_window_elapsed(service, bus, clock) -> None:
    edge = EdgeServer("edge-1", bus, service)
    edge.join("early", "v1")
    edge.join("late", "v2")
    service.publish("early", "u1", "first")
    clock.advance(0.2)
    service.publish("late", "u2", "second")
    flushed = service.tick()
    assert [b.stream_id for b in flushed] == ["early"]  # "late" opened its window just now
    clock.advance(0.2)
    assert [b.stream_id for b in service.tick()] == ["late"]


def test_viewer_count_is_a_sum_of_server_reports(service, bus) -> None:
    left, right = EdgeServer("edge-1", bus, service), EdgeServer("edge-2", bus, service)
    left.join("s1", "v1")
    left.join("s1", "v2")
    right.join("s1", "v3")
    assert service.viewer_count("s1") == 3
    left.leave("s1", "v1")
    assert service.viewer_count("s1") == 2
    left.leave("s1", "v2")
    assert bus.subscriber_count("s1") == 1  # edge-1 has nobody left, so it unsubscribes
    right.leave("s1", "v3")
    assert (service.viewer_count("s1"), bus.subscriber_count("s1")) == (0, 0)
    right.leave("s1", "ghost")  # leaving a stream you never joined is a no-op


def test_validation_errors(service, bus, clock) -> None:
    edge = EdgeServer("edge-1", bus, service)
    edge.join("s1", "v1")
    with pytest.raises(ValidationError):
        service.publish("s1", "u1", "   ")
    with pytest.raises(ValidationError):
        service.report_viewers("edge-1", "s1", -1)
    with pytest.raises(ValidationError):
        CommentService(bus, clock, max_per_batch=0)
    with pytest.raises(ValidationError):
        CommentService(bus, clock, window_s=2.0, hot_window_s=1.0)
    assert service.flush("no-such-stream") is None
    assert service.viewer_count("no-such-stream") == 0


def test_concurrent_publishes_get_unique_contiguous_seqs(bus, clock) -> None:
    service = CommentService(bus, clock, random.Random(7), max_per_batch=1_000, window_s=0.2)
    edge = EdgeServer("edge-1", bus, service)
    edge.join("s1", "v1")
    with ThreadPoolExecutor(max_workers=8) as pool:
        comments = list(pool.map(lambda i: service.publish("s1", f"u{i}", f"m{i}"), range(400)))
    assert sorted(c.seq for c in comments) == list(range(1, 401))
    clock.advance(0.2)
    batch = service.tick()[0]
    assert batch.dropped == 0
    assert [c.seq for c in batch.comments] == list(range(1, 401))  # nothing lost, nothing doubled
    assert edge.socket_writes() == 1
