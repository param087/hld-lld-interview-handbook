"""Event-time tumbling windows with a watermark, dedup and top-N: the ad-click aggregator's core.

The crux of the ad click aggregation design in one module. Everything hard about counting a
stream is here, and none of it is the counting:

* **Event time, not processing time.** A click is bucketed by when it happened on the device, so
  a phone that was offline for a minute still lands in the right minute.
* **A watermark** ``max_event_time - lag`` decides when a window is complete. Windows close only
  when the watermark passes their end, which is what makes the emitted counts stable.
* **Dedup by event id**, because the transport is at-least-once. Effectively-once = at-least-once
  plus an idempotent consumer, and this dictionary is that consumer.
* **Late events go to a side output**, never silently into the wrong bucket. ``reconcile`` folds
  them back in, which is the batch half of a lambda architecture in six lines.
"""

from __future__ import annotations

import heapq
import threading
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum

from common import ValidationError


# --8<-- [start:models]
class Outcome(StrEnum):
    """What happened to an ingested event. Every one of these is a metric worth alerting on."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    LATE = "late"


@dataclass(frozen=True, slots=True)
class ClickEvent:
    """One impression or click. ``event_id`` is minted at the edge and is the dedup key."""

    event_id: str
    ad_id: str
    event_time: float  # seconds since the epoch, taken on the device

    def __post_init__(self) -> None:
        if not self.event_id or not self.ad_id:
            raise ValidationError("event_id and ad_id must be non-empty")


@dataclass(frozen=True, slots=True)
class Window:
    """A closed tumbling window and the counts it emitted."""

    start: float
    end: float
    counts: dict[str, int]
    late_folded: int = 0

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def top(self, n: int) -> list[tuple[str, int]]:
        """Top-N by count, ties broken by ad id so the output is deterministic."""
        return heapq.nsmallest(n, self.counts.items(), key=lambda kv: (-kv[1], kv[0]))


@dataclass(slots=True)
class Stats:
    accepted: int = 0
    duplicates: int = 0
    late: int = 0
    windows_closed: int = 0
    dedup_entries: int = 0


# --8<-- [end:models]


# --8<-- [start:aggregator]
class WindowedAggregator:
    """Tumbling event-time windows with a watermark, dedup and a late-event side output.

    ``_open`` maps a window index to its running counts, ``_closed`` holds windows the watermark
    has passed, and ``_late`` holds events that arrived after their window closed. ``_seen`` is
    the dedup set, expired against the watermark so it cannot grow without bound. ``_lock`` guards
    all of them: a real deployment runs one instance per Kafka partition, several per process.
    """

    def __init__(
        self,
        window_s: float = 60.0,
        watermark_lag_s: float = 30.0,
        dedup_ttl_s: float = 300.0,
    ) -> None:
        if window_s <= 0 or watermark_lag_s < 0 or dedup_ttl_s < 0:
            raise ValidationError("window_s must be positive; lag and TTL must not be negative")
        self.window_s = window_s
        self.watermark_lag_s = watermark_lag_s
        self.dedup_ttl_s = dedup_ttl_s
        self._max_event_time = float("-inf")
        self._open: dict[int, Counter[str]] = {}
        self._closed: dict[int, Window] = {}
        self._late: dict[int, Counter[str]] = {}
        self._seen: dict[str, float] = {}  # event_id -> event_time
        self._stats = Stats()
        self._lock = threading.Lock()

    # -- windows and watermark ---------------------------------------------------------
    def window_index(self, event_time: float) -> int:
        """Which tumbling window a timestamp falls in. Windows never overlap and never gap."""
        return int(event_time // self.window_s)

    def window_bounds(self, index: int) -> tuple[float, float]:
        return index * self.window_s, (index + 1) * self.window_s

    @property
    def watermark(self) -> float:
        """The event time before which the stream is assumed complete.

        A pure function of the highest timestamp seen minus a fixed lag. The lag is the whole
        trade-off: raise it to catch more stragglers, lower it to publish counts sooner.
        """
        with self._lock:
            return self._max_event_time - self.watermark_lag_s

    # -- ingest ------------------------------------------------------------------------
    def ingest(self, event: ClickEvent) -> Outcome:
        """Count one event, or reject it as a duplicate or as late."""
        with self._lock:
            if event.event_id in self._seen:
                self._stats.duplicates += 1
                return Outcome.DUPLICATE
            self._seen[event.event_id] = event.event_time
            self._max_event_time = max(self._max_event_time, event.event_time)
            index = self.window_index(event.event_time)
            if index in self._closed:
                self._late.setdefault(index, Counter())[event.ad_id] += 1
                self._stats.late += 1
                return Outcome.LATE
            self._open.setdefault(index, Counter())[event.ad_id] += 1
            self._stats.accepted += 1
            return Outcome.ACCEPTED

    def poll_closed(self) -> list[Window]:
        """Emit every open window the watermark has passed, oldest first.

        Emission is an explicit step rather than a side effect of ``ingest`` so that tests and
        demos are deterministic; a stream engine fires it from a timer on the same condition.
        """
        with self._lock:
            mark = self._max_event_time - self.watermark_lag_s
            ready = sorted(i for i in self._open if self.window_bounds(i)[1] <= mark)
            emitted: list[Window] = []
            for index in ready:
                start, end = self.window_bounds(index)
                window = Window(start, end, dict(self._open.pop(index)))
                self._closed[index] = window
                emitted.append(window)
            self._stats.windows_closed += len(emitted)
            self._expire_dedup(mark)
            return emitted

    def _expire_dedup(self, mark: float) -> None:
        """Drop dedup keys older than the TTL; caller holds the lock.

        Bounded memory is the point: at 10k events/s a 5-minute TTL is 3M keys, which is a Redis
        shard, while an unbounded set is an outage waiting for a traffic spike.
        """
        cutoff = mark - self.dedup_ttl_s
        stale = [key for key, seen_at in self._seen.items() if seen_at < cutoff]
        for key in stale:
            del self._seen[key]

    # -- queries -----------------------------------------------------------------------
    def top_n(self, n: int, window_start: float | None = None) -> list[tuple[str, int]]:
        """Top-N ads in one closed window, or in the most recent closed window."""
        if n <= 0:
            raise ValidationError("n must be positive")
        with self._lock:
            if not self._closed:
                return []
            index = self.window_index(window_start) if window_start is not None else max(self._closed)
            window = self._closed.get(index)
        return window.top(n) if window is not None else []

    def closed_windows(self) -> list[Window]:
        with self._lock:
            return [self._closed[i] for i in sorted(self._closed)]

    def stats(self) -> Stats:
        with self._lock:
            return Stats(
                self._stats.accepted,
                self._stats.duplicates,
                self._stats.late,
                self._stats.windows_closed,
                len(self._seen),
            )

    # -- the batch half ----------------------------------------------------------------
    def reconcile(self) -> list[Window]:
        """Fold the late side output back into the closed windows and return the corrected ones.

        This is the batch path of a lambda architecture in miniature: the stream publishes fast
        and slightly wrong, a slower job replays the raw events and republishes the truth. The
        serving layer always reads the newest version of a window, so the correction is invisible.
        """
        with self._lock:
            corrected: list[Window] = []
            for index, extra in sorted(self._late.items()):
                window = self._closed[index]
                counts = Counter(window.counts)
                counts.update(extra)
                fixed = Window(window.start, window.end, dict(counts), window.late_folded + sum(extra.values()))
                self._closed[index] = fixed
                corrected.append(fixed)
            self._late.clear()
            return corrected


# --8<-- [end:aggregator]


@dataclass(slots=True)
class _Scenario:
    """A hand-built stream: readable, deterministic, and containing every case that matters."""

    base: float = 1_700_000_040.0  # exactly on a minute boundary, so offsets read as window times
    events: list[ClickEvent] = field(default_factory=list)

    def at(self, offset: float, ad_id: str, event_id: str) -> None:
        self.events.append(ClickEvent(event_id, ad_id, self.base + offset))


def _scenario() -> _Scenario:
    s = _Scenario()
    for offset, ad, eid in [
        (0, "ad_shoes", "e1"),
        (5, "ad_shoes", "e2"),
        (9, "ad_phone", "e3"),
        (12, "ad_shoes", "e4"),
        (5, "ad_shoes", "e2"),  # retry of e2: at-least-once delivery
        (40, "ad_phone", "e5"),
        (48, "ad_car", "e6"),
        (61, "ad_phone", "e7"),  # second window opens
        (70, "ad_shoes", "e8"),
        (95, "ad_phone", "e9"),
        (130, "ad_car", "e10"),  # pushes the watermark past window 0
        (20, "ad_shoes", "e11"),  # straggler for window 0, arrives after it closed
        (140, "ad_shoes", "e12"),
    ]:
        s.at(offset, ad, eid)
    return s


def main() -> None:
    agg = WindowedAggregator(window_s=60.0, watermark_lag_s=30.0, dedup_ttl_s=300.0)
    scenario = _scenario()
    print(f"tumbling windows of {agg.window_s:g} s, watermark lag {agg.watermark_lag_s:g} s")

    def label(window: Window) -> str:
        return f"window {int((window.start - scenario.base) // 60)}"

    def counts_of(window: Window) -> str:
        return " ".join(f"{ad}={n}" for ad, n in sorted(window.counts.items()))

    outcomes: Counter[str] = Counter()
    for event in scenario.events:
        outcome = agg.ingest(event)
        outcomes[outcome.value] += 1
        if outcome is not Outcome.ACCEPTED:
            offset = event.event_time - scenario.base
            print(f"  {event.event_id} ({event.ad_id} at t+{offset:g}s) -> {outcome.value}")
        for window in agg.poll_closed():
            print(f"  closed {label(window)}: {counts_of(window)} (total {window.total})")
    print(f"outcomes: {dict(sorted(outcomes.items()))}")
    print(f"watermark is t+{agg.watermark - scenario.base:g}s, so window 1 is still open")
    print(f"top-2 in the newest closed window: {agg.top_n(2)}")

    agg.ingest(ClickEvent("e13", "ad_phone", scenario.base + 200))
    for window in agg.poll_closed():
        print(f"  one t+200s event lifts the watermark to t+170s and closes {label(window)}: {counts_of(window)}")

    for window in agg.reconcile():
        print(f"  reconciled {label(window)}: {counts_of(window)} (+{window.late_folded} late folded in)")
    stats = agg.stats()
    print(
        f"stats: {stats.accepted} accepted, {stats.duplicates} duplicates, {stats.late} late, "
        f"{stats.windows_closed} windows closed, {stats.dedup_entries} dedup keys held"
    )


if __name__ == "__main__":
    main()
