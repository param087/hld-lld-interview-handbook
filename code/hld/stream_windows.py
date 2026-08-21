"""Event-time windowing: tumbling and sliding windows, watermarks and a late-event policy.

What the module demonstrates, in the order an interviewer asks about it:

* ``WindowAssigner`` maps an event time to the windows that contain it. A tumbling window is
  the special case where the slide equals the size, so one assigner covers both and an event
  in a sliding window lands in ``size / slide`` panes at once.
* The **watermark** is the engine's claim that no event older than ``now - max_lateness`` will
  arrive. It is derived from the data (the largest event time seen minus a bound), never from
  the wall clock, which is what makes a replay produce the same answer as the live run.
* ``WindowedAggregator.ingest`` classifies every event against that watermark: on time, a late
  update inside the allowed lateness, or too late for the side output. ``poll`` fires the
  windows the watermark has passed and evicts their state once lateness expires.
* Allowed lateness is a *state* decision, not a correctness one: keeping a window open for an
  extra hour means keeping every open window's state for an extra hour.

``_lock`` guards the panes, the fired set, the side output and the maximum event time, so
several ingest threads (one per Kafka partition, say) can share one aggregator.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from common import ValidationError


# --8<-- [start:windows]
@dataclass(frozen=True, slots=True)
class Event:
    """One record on the stream. ``event_time`` is when it happened, in epoch seconds."""

    key: str
    event_time: float
    value: int = 1


@dataclass(frozen=True, slots=True)
class Window:
    """A half-open interval ``[start, end)`` in event time."""

    start: float
    end: float

    def __str__(self) -> str:
        return f"[{self.start:.0f}, {self.end:.0f})"


@dataclass(frozen=True, slots=True)
class WindowAssigner:
    """Assigns an event time to windows. ``slide_seconds is None`` means tumbling.

    Tumbling: every event lands in exactly one window, so the counts partition the stream.
    Sliding: an event lands in ``size / slide`` windows, so a 5-minute window sliding every
    minute stores and emits 5x the state and output of the tumbling equivalent. Session
    windows (gap-based, not covered here) are the third family.
    """

    size_seconds: float
    slide_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.size_seconds <= 0:
            raise ValidationError("size_seconds must be positive")
        if self.slide_seconds is not None and not 0 < self.slide_seconds <= self.size_seconds:
            raise ValidationError("slide_seconds must be in (0, size_seconds]")

    @property
    def slide(self) -> float:
        return self.slide_seconds if self.slide_seconds is not None else self.size_seconds

    @property
    def panes_per_event(self) -> int:
        """How many windows one event belongs to: 1 when tumbling."""
        return int(-(-self.size_seconds // self.slide))

    def windows_for(self, event_time: float) -> list[Window]:
        """Every window containing ``event_time``, earliest first."""
        slide = self.slide
        first = ((event_time - self.size_seconds) // slide + 1) * slide
        out: list[Window] = []
        start = first
        while start <= event_time:
            out.append(Window(start=start, end=start + self.size_seconds))
            start += slide
        return out


# --8<-- [end:windows]


# --8<-- [start:aggregator]
class Verdict(StrEnum):
    """What happened to an ingested event."""

    ON_TIME = "on_time"  # its windows had not fired yet
    LATE_UPDATE = "late_update"  # a fired window was reopened inside the allowed lateness
    DROPPED = "dropped"  # past the allowed lateness: side output or nothing


@dataclass(frozen=True, slots=True)
class WindowResult:
    """One emitted window for one key."""

    key: str
    window: Window
    total: int
    events: int
    revision: int  # 0 on the first firing, then 1, 2, ... for each late update
    final: bool  # True when the allowed lateness has expired and the state was evicted


class WindowedAggregator:
    """Event-time windowed sums with a bounded-out-of-orderness watermark.

    ``max_out_of_orderness`` is the lateness you *expect* and subtract from the largest event
    time to form the watermark; ``allowed_lateness`` is the extra grace during which a fired
    window is kept and can be revised. Both cost latency or state: a 5-second bound delays
    every result by 5 seconds, and an hour of lateness holds an hour of windows in memory.
    """

    def __init__(
        self,
        assigner: WindowAssigner,
        max_out_of_orderness: float = 0.0,
        allowed_lateness: float = 0.0,
    ) -> None:
        if max_out_of_orderness < 0 or allowed_lateness < 0:
            raise ValidationError("lateness bounds cannot be negative")
        self._assigner = assigner
        self._bound = max_out_of_orderness
        self._lateness = allowed_lateness
        self._lock = threading.Lock()
        self._panes: dict[tuple[str, Window], list[int]] = {}
        self._revisions: dict[tuple[str, Window], int] = {}
        self._max_event_time: float | None = None
        self._side_output: list[Event] = []
        self._dirty: set[tuple[str, Window]] = set()

    @property
    def watermark(self) -> float:
        """Largest event time seen minus the out-of-orderness bound; -inf before any event."""
        with self._lock:
            return self._watermark_locked()

    @property
    def side_output(self) -> tuple[Event, ...]:
        """Events that arrived past the allowed lateness. Never drop them silently."""
        with self._lock:
            return tuple(self._side_output)

    @property
    def open_windows(self) -> int:
        """Panes still held in memory: the state cost of the lateness settings."""
        with self._lock:
            return len(self._panes)

    def ingest(self, event: Event) -> Verdict:
        """Add one event to every window that contains it, classified against the watermark.

        A sliding-window event can be too late for its oldest window and still on time for the
        newest, so the verdict is the worst outcome across panes and only an event that landed
        nowhere goes to the side output.
        """
        with self._lock:
            if self._max_event_time is None or event.event_time > self._max_event_time:
                self._max_event_time = event.event_time
            watermark = self._watermark_locked()
            accepted = late = 0
            for window in self._assigner.windows_for(event.event_time):
                pane = (event.key, window)
                if watermark >= window.end + self._lateness:
                    continue  # closed: the window fired and its state has been evicted
                self._panes.setdefault(pane, []).append(event.value)
                accepted += 1
                if watermark >= window.end or self._revisions.get(pane, 0) > 0:
                    self._dirty.add(pane)
                    late += 1
            if accepted == 0:
                self._side_output.append(event)
                return Verdict.DROPPED
            return Verdict.LATE_UPDATE if late else Verdict.ON_TIME

    def ingest_all(self, events: Iterable[Event]) -> list[Verdict]:
        return [self.ingest(event) for event in events]

    def poll(self) -> list[WindowResult]:
        """Emit every window the watermark has passed, then evict what lateness has expired.

        Windows fire once when the watermark crosses their end, and again for each late update
        inside the allowed lateness (the ``revision`` counter). A downstream sink therefore has
        to be idempotent on ``(key, window)`` — the same window is written more than once.
        """
        with self._lock:
            watermark = self._watermark_locked()
            results: list[WindowResult] = []
            for pane, values in sorted(self._panes.items(), key=lambda item: (item[0][1].start, item[0][0])):
                key, window = pane
                if watermark < window.end:
                    continue
                revision = self._revisions.get(pane, 0)
                if revision > 0 and pane not in self._dirty:
                    continue
                expired = watermark >= window.end + self._lateness
                results.append(
                    WindowResult(
                        key=key,
                        window=window,
                        total=sum(values),
                        events=len(values),
                        revision=revision,
                        final=expired,
                    )
                )
                self._revisions[pane] = revision + 1
                self._dirty.discard(pane)
            for pane in [p for p in self._panes if watermark >= p[1].end + self._lateness]:
                del self._panes[pane]
            return results

    def _watermark_locked(self) -> float:
        if self._max_event_time is None:
            return float("-inf")
        return self._max_event_time - self._bound


# --8<-- [end:aggregator]


def main() -> None:
    aggregator = WindowedAggregator(
        WindowAssigner(size_seconds=60.0), max_out_of_orderness=5.0, allowed_lateness=30.0
    )
    print("ad clicks, 60 s tumbling windows, 5 s out-of-orderness bound, 30 s allowed lateness")

    def show(label: str) -> None:
        fired = aggregator.poll()
        if not fired:
            print(f"  {label}: nothing ready")
            return
        for result in fired:
            print(
                f"  {label}: {result.key} {result.window} total={result.total} "
                f"events={result.events} revision={result.revision} final={result.final}"
            )

    stream = [
        Event("ad-1", 10.0),
        Event("ad-2", 20.0),
        Event("ad-1", 35.0),
        Event("ad-1", 55.0),
        Event("ad-2", 70.0),  # opens [60, 120) and pushes the watermark to 65
    ]
    for event in stream:
        verdict = aggregator.ingest(event)
        print(f"  t={event.event_time:5.0f} {event.key}  watermark={aggregator.watermark:5.0f}  {verdict.value}")
    show("fire")

    late = Event("ad-1", 50.0)  # its window has fired, but the lateness has not expired
    print(f"late event t=50 ad-1 -> {aggregator.ingest(late).value}")
    show("refire")

    print(f"t=95 ad-1 -> {aggregator.ingest(Event('ad-1', 95.0)).value}, watermark={aggregator.watermark:.0f}")
    show("poll")
    print(f"  window [0, 60) is now closed and evicted; open panes: {aggregator.open_windows}")

    print(f"very late event t=45 ad-1 -> {aggregator.ingest(Event('ad-1', 45.0)).value}")
    print(f"  side output holds {len(aggregator.side_output)} event(s), not silently discarded")

    sliding = WindowAssigner(size_seconds=300.0, slide_seconds=60.0)
    print(
        f"\nsliding 300 s window every 60 s: one event at t=310 lands in "
        f"{sliding.panes_per_event} windows {' '.join(str(w) for w in sliding.windows_for(310.0))}"
    )
    tumbling = WindowAssigner(size_seconds=300.0)
    print(f"the tumbling equivalent stores 1 pane: {tumbling.windows_for(310.0)[0]}")


if __name__ == "__main__":
    main()
