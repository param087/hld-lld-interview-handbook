"""Live comments for a stream: per-stream pub/sub, batching windows and sampling under load.

The crux of the live-streaming design in one module:

* ``CommentService`` sequences comments per stream into a buffer and flushes that buffer once per
  batching window. One frame carrying twenty comments costs a socket write; twenty frames cost
  twenty. The window widens automatically once a stream crosses the "hot" viewer threshold.
* When a window holds more comments than a viewer can read, the flush **samples**: every priority
  comment (broadcaster, moderator, subscriber) is kept and the rest are sampled down, with the
  discarded count reported so the client can render "+340 more".
* ``CommentBus`` is per-stream pub/sub whose subscribers are *edge servers*, not viewers, so one
  flush costs one delivery per server holding viewers rather than one per viewer.
* ``EdgeServer`` owns the sockets and writes each batch to its local viewers of that stream.
"""

from __future__ import annotations

import random
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from common import Clock, SystemClock, ValidationError

DEFAULT_WINDOW_S = 0.2  # 5 flushes/s: below the threshold where a chat column becomes unreadable
DEFAULT_HOT_WINDOW_S = 1.0  # a stream with a huge audience trades latency for socket writes
DEFAULT_HOT_VIEWERS = 100_000
DEFAULT_MAX_PER_BATCH = 20


# --8<-- [start:models]
@dataclass(frozen=True, slots=True)
class Comment:
    """One chat message. ``priority`` marks broadcasters, moderators and subscribers, whose
    messages are never sampled out -- the rule that keeps a hot chat useful rather than random."""

    stream_id: str
    seq: int
    user_id: str
    body: str
    at: float
    priority: bool = False


@dataclass(frozen=True, slots=True)
class CommentBatch:
    """What one flush delivers: the surviving comments plus how many were dropped."""

    stream_id: str
    comments: tuple[Comment, ...]
    dropped: int
    window_size: int

    @property
    def sampled(self) -> bool:
        return self.dropped > 0


# --8<-- [end:models]


# --8<-- [start:bus]
class CommentBus:
    """Per-stream pub/sub. Subscribers are edge servers, so a flush costs one delivery per server.

    A stream with 500k viewers on 5 edge servers costs 5 deliveries per flush. Subscribing per
    viewer would cost 500k, and broadcasting to the whole fleet would send every stream's chat to
    every server. ``_lock`` protects ``_subscribers``.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, dict[str, Callable[[CommentBatch], None]]] = {}
        self._lock = threading.Lock()

    def subscribe(self, stream_id: str, server_id: str, handler: Callable[[CommentBatch], None]) -> None:
        with self._lock:
            self._subscribers.setdefault(stream_id, {})[server_id] = handler

    def unsubscribe(self, stream_id: str, server_id: str) -> None:
        with self._lock:
            handlers = self._subscribers.get(stream_id)
            if handlers is None:
                return
            handlers.pop(server_id, None)
            if not handlers:
                del self._subscribers[stream_id]

    def subscriber_count(self, stream_id: str) -> int:
        with self._lock:
            return len(self._subscribers.get(stream_id, {}))

    def publish(self, stream_id: str, batch: CommentBatch) -> int:
        with self._lock:
            handlers = list(self._subscribers.get(stream_id, {}).values())
        for handler in handlers:
            handler(batch)
        return len(handlers)


# --8<-- [end:bus]


# --8<-- [start:service]
@dataclass(slots=True)
class _Stream:
    """Per-stream state held by the service: sequencer, window buffer and viewer reports."""

    next_seq: int = 1
    buffer: list[Comment] = field(default_factory=list)
    window_opened_at: float = 0.0  # set by the first comment of a batch, not by the last flush
    viewers: dict[str, int] = field(default_factory=dict)  # edge server -> its local viewer count


class CommentService:
    """Sequencer, batching windows, sampling and the viewer-count rollup.

    Every comment gets a ``seq`` per stream, so a client that holds 41 and sees 43 knows it was
    sampled rather than disconnected. ``_lock`` protects every stream's buffer, counters and
    viewer reports; the publish to the bus happens outside it, so a slow edge server can never
    delay the next comment's sequence number.
    """

    def __init__(
        self,
        bus: CommentBus,
        clock: Clock | None = None,
        rng: random.Random | None = None,
        max_per_batch: int = DEFAULT_MAX_PER_BATCH,
        window_s: float = DEFAULT_WINDOW_S,
        hot_window_s: float = DEFAULT_HOT_WINDOW_S,
        hot_viewers: int = DEFAULT_HOT_VIEWERS,
    ) -> None:
        if max_per_batch <= 0 or window_s <= 0 or hot_window_s < window_s:
            raise ValidationError("max_per_batch and the windows must be positive and ordered")
        self._bus = bus
        self._clock = clock or SystemClock()
        self._rng = rng or random.Random()
        self._max_per_batch = max_per_batch
        self._window = window_s
        self._hot_window = hot_window_s
        self._hot_viewers = hot_viewers
        self._streams: dict[str, _Stream] = {}
        self._lock = threading.Lock()

    # -- write path ------------------------------------------------------------------
    def publish(self, stream_id: str, user_id: str, body: str, priority: bool = False) -> Comment:
        """Sequence and buffer. Nothing is delivered until the window closes."""
        if not body.strip():
            raise ValidationError("empty comment")
        with self._lock:
            stream = self._streams.setdefault(stream_id, _Stream())
            now = self._clock.now()
            if not stream.buffer:
                stream.window_opened_at = now  # the window starts with the first comment in it
            comment = Comment(stream_id, stream.next_seq, user_id, body.strip(), now, priority)
            stream.next_seq += 1
            stream.buffer.append(comment)
            return comment

    def report_viewers(self, server_id: str, stream_id: str, count: int) -> None:
        """Edge servers report their local viewer count every few seconds; the total is a sum."""
        if count < 0:
            raise ValidationError("viewer count cannot be negative")
        with self._lock:
            stream = self._streams.setdefault(stream_id, _Stream())
            if count:
                stream.viewers[server_id] = count
            else:
                stream.viewers.pop(server_id, None)

    def viewer_count(self, stream_id: str) -> int:
        """Approximate by construction: a sum of per-server reports a few seconds old."""
        with self._lock:
            stream = self._streams.get(stream_id)
            return sum(stream.viewers.values()) if stream else 0

    def window_s(self, stream_id: str) -> float:
        with self._lock:
            return self._window_locked(self._streams.get(stream_id))

    def _window_locked(self, stream: _Stream | None) -> float:
        viewers = sum(stream.viewers.values()) if stream else 0
        return self._hot_window if viewers >= self._hot_viewers else self._window

    # -- fan-out ---------------------------------------------------------------------
    def flush(self, stream_id: str) -> CommentBatch | None:
        """Close the window: sample it down to ``max_per_batch`` and publish it once."""
        with self._lock:
            stream = self._streams.get(stream_id)
            if stream is None or not stream.buffer:
                return None
            window, stream.buffer = stream.buffer, []
            kept, dropped = self._sample_locked(window)
        batch = CommentBatch(stream_id, tuple(kept), dropped, len(window))
        self._bus.publish(stream_id, batch)
        return batch

    def _sample_locked(self, window: list[Comment]) -> tuple[list[Comment], int]:
        """Keep every priority comment, sample the rest. Caller holds the lock (the RNG is shared).

        Sampling beats truncation: taking the first N would silence everyone whose message landed
        late in the window, which in practice means everyone with a slow connection.
        """
        priority = [c for c in window if c.priority]
        ordinary = [c for c in window if not c.priority]
        room = max(0, self._max_per_batch - len(priority))
        if len(ordinary) <= room:
            return sorted(window, key=lambda c: c.seq), 0
        keep = self._rng.sample(ordinary, room)
        return sorted(priority + keep, key=lambda c: c.seq), len(ordinary) - room

    def tick(self) -> list[CommentBatch]:
        """Flush every stream whose window has elapsed: one timer for the fleet, not one per viewer."""
        now = self._clock.now()
        with self._lock:
            due = sorted(
                stream_id
                for stream_id, stream in self._streams.items()
                if stream.buffer and now - stream.window_opened_at >= self._window_locked(stream)
            )
        return [batch for stream_id in due if (batch := self.flush(stream_id)) is not None]


# --8<-- [end:service]


# --8<-- [start:edge]
class EdgeServer:
    """A WebSocket edge: it owns viewer sockets and turns one batch into local socket writes.

    ``_writes`` is the number that matters in the estimation -- it is what a batching window and a
    sampling cap actually buy. ``_lock`` protects ``_rooms``, ``_outboxes`` and ``_writes``.
    """

    def __init__(self, server_id: str, bus: CommentBus, service: CommentService) -> None:
        self.server_id = server_id
        self._bus = bus
        self._service = service
        self._rooms: dict[str, set[str]] = {}  # stream -> local viewer ids
        self._outboxes: dict[str, list[CommentBatch]] = {}
        self._writes = 0
        self._lock = threading.Lock()

    def join(self, stream_id: str, viewer_id: str) -> None:
        with self._lock:
            first = stream_id not in self._rooms
            self._rooms.setdefault(stream_id, set()).add(viewer_id)
            self._outboxes.setdefault(viewer_id, [])
            local = len(self._rooms[stream_id])
        if first:
            self._bus.subscribe(stream_id, self.server_id, self._on_batch)
        self._service.report_viewers(self.server_id, stream_id, local)

    def leave(self, stream_id: str, viewer_id: str) -> None:
        with self._lock:
            room = self._rooms.get(stream_id)
            if room is None:
                return
            room.discard(viewer_id)
            self._outboxes.pop(viewer_id, None)
            empty = not room
            if empty:
                del self._rooms[stream_id]
            local = len(room)
        if empty:
            self._bus.unsubscribe(stream_id, self.server_id)
        self._service.report_viewers(self.server_id, stream_id, local)

    def _on_batch(self, batch: CommentBatch) -> None:
        with self._lock:
            for viewer in self._rooms.get(batch.stream_id, ()):
                self._outboxes[viewer].append(batch)
                self._writes += 1

    def outbox(self, viewer_id: str) -> list[CommentBatch]:
        with self._lock:
            return list(self._outboxes.get(viewer_id, ()))

    def socket_writes(self) -> int:
        with self._lock:
            return self._writes


# --8<-- [end:edge]


def main() -> None:
    from common import FakeClock

    clock = FakeClock(start=1_700_000_000)
    bus = CommentBus()
    service = CommentService(
        bus, clock, random.Random(42), max_per_batch=8, window_s=0.2, hot_window_s=1.0, hot_viewers=500
    )
    edges = [EdgeServer(f"edge-{i}", bus, service) for i in range(3)]
    for i in range(600):
        edges[i % 3].join("hot", f"viewer-{i}")
    edges[0].join("quiet", "viewer-x")
    print(f"stream hot: {service.viewer_count('hot')} viewers on {bus.subscriber_count('hot')} edge servers")
    print(f"batch window: hot={service.window_s('hot')} s, quiet={service.window_s('quiet')} s")

    for i in range(60):
        service.publish("hot", f"viewer-{i}", f"message {i}")
    service.publish("hot", "mod-1", "keep it civil", priority=True)
    service.publish("quiet", "viewer-x", "anyone here?")

    clock.advance(1.0)
    batches = {batch.stream_id: batch for batch in service.tick()}
    hot = batches["hot"]
    print(f"hot window held {hot.window_size} comments -> batch of {len(hot.comments)}, {hot.dropped} sampled out")
    print("moderator survived sampling:", any(c.priority for c in hot.comments))
    print("seqs delivered:", [c.seq for c in hot.comments])

    writes = sum(edge.socket_writes() for edge in edges)
    naive = hot.window_size * service.viewer_count("hot")
    print(f"socket writes: {writes} batched vs {naive} unbatched ({naive // writes}x cheaper)")
    print("one viewer received:", len(edges[0].outbox("viewer-0")), "frame(s) in that second")

    for i in range(600):
        edges[i % 3].leave("hot", f"viewer-{i}")
    print("everyone left:", bus.subscriber_count("hot"), "subscribers,", service.viewer_count("hot"), "viewers")


if __name__ == "__main__":
    main()
