"""An in-memory time-series database: label index, rollup tiers and alert-rule evaluation.

The metrics-monitoring case study compressed into one module:

* ``LabelIndex`` resolves a matcher set by intersecting postings lists smallest-first, and
  reports the cardinality that one runaway label produces.
* ``TimeSeriesDB.write`` appends to the head block and refuses a *new* series once the
  cardinality budget is spent; ``compact`` folds old raw samples into mergeable buckets and
  drops them (downsampling and retention in one move); ``range_query`` plans each query onto
  the cheapest tier that can answer it.
* ``RuleEvaluator`` runs the inactive -> pending -> firing state machine that a ``for:``
  duration needs; ``AlertRouter`` deduplicates, groups, routes and repeats the notifications.
"""

from __future__ import annotations

import threading
from bisect import bisect_left
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from common import Clock, SystemClock, ValidationError

Labels = tuple[tuple[str, str], ...]


# --8<-- [start:model]
def canonical(labels: Mapping[str, str]) -> Labels:
    """Sort the label pairs so one label set always produces one series identity."""
    return tuple(sorted(labels.items()))


def series_id(metric: str, labels: Labels) -> str:
    """The series identity: metric name plus the sorted label set, Prometheus-style."""
    return f"{metric}{{{','.join(f'{k}={v}' for k, v in labels)}}}"


@dataclass(frozen=True, slots=True)
class Series:
    """One time series. The labels are the identity, not metadata attached to it."""

    id: str
    metric: str
    labels: Labels


@dataclass(frozen=True, slots=True)
class Point:
    """A timestamp and a value: 16 bytes raw, ~1.4 bytes once Gorilla-compressed."""

    timestamp: float
    value: float


@dataclass(frozen=True, slots=True)
class Bucket:
    """A downsampled bucket.

    Keeping count and total rather than only the mean is what makes rollups *mergeable*:
    twelve 5-minute buckets fold into one 1-hour bucket and the hourly mean stays exact. A
    stored mean cannot be merged without its weight, which is why downsampled dashboards
    drift away from the raw ones.
    """

    start: float
    count: int
    total: float
    minimum: float
    maximum: float
    last: float

    @property
    def mean(self) -> float:
        return self.total / self.count

    def merge(self, other: Bucket) -> Bucket:
        later = other if other.start >= self.start else self
        return Bucket(
            start=min(self.start, other.start),
            count=self.count + other.count,
            total=self.total + other.total,
            minimum=min(self.minimum, other.minimum),
            maximum=max(self.maximum, other.maximum),
            last=later.last,
        )

    @staticmethod
    def of(start: float, values: Sequence[float]) -> Bucket:
        if not values:
            raise ValidationError("a bucket needs at least one sample")
        return Bucket(start, len(values), sum(values), min(values), max(values), values[-1])


class Aggregation(StrEnum):
    """The folds a dashboard panel or an alert rule asks for."""

    AVG = "avg"
    SUM = "sum"
    MIN = "min"
    MAX = "max"
    COUNT = "count"
    LAST = "last"

    def apply(self, bucket: Bucket) -> float:
        match self:
            case Aggregation.AVG:
                return bucket.mean
            case Aggregation.SUM:
                return bucket.total
            case Aggregation.MIN:
                return bucket.minimum
            case Aggregation.MAX:
                return bucket.maximum
            case Aggregation.COUNT:
                return float(bucket.count)
            case _:
                return bucket.last


# --8<-- [end:model]


# --8<-- [start:index]
class LabelIndex:
    """Inverted index from one label pair to the series that carry it.

    ``{service="checkout", status="500"}`` is answered by intersecting two postings lists,
    smallest first - the linear merge a search engine uses for a boolean AND. That is why a
    matcher query stays fast over 10M series, and why one unbounded label (a user id, a
    request id, a full URL) destroys the database: every distinct value mints a new series,
    a new postings entry and a new head chunk.
    """

    def __init__(self) -> None:
        self._series: dict[str, Series] = {}
        self._by_metric: dict[str, set[str]] = {}
        self._postings: dict[tuple[str, str], set[str]] = {}

    def __len__(self) -> int:
        return len(self._series)

    def __contains__(self, sid: object) -> bool:
        return sid in self._series

    def add(self, series: Series) -> None:
        self._series[series.id] = series
        self._by_metric.setdefault(series.metric, set()).add(series.id)
        for pair in series.labels:
            self._postings.setdefault(pair, set()).add(series.id)

    def select(self, metric: str, matchers: Mapping[str, str]) -> list[str]:
        """Series ids matching the metric name and every label pair, sorted for determinism."""
        candidates = self._by_metric.get(metric)
        if not candidates:
            return []
        lists = [candidates, *(self._postings.get(pair, set()) for pair in canonical(matchers))]
        lists.sort(key=len)
        result = set(lists[0])
        for other in lists[1:]:
            result &= other
            if not result:
                break
        return sorted(result)

    def label_cardinality(self, key: str) -> int:
        """How many distinct values one label key has taken: the number to alert on."""
        return sum(1 for k, _ in self._postings if k == key)


# --8<-- [end:index]


# --8<-- [start:tsdb]
class TimeSeriesDB:
    """A head block of raw samples plus coarser rollup tiers, behind one lock.

    ``_lock`` guards ``_index``, ``_raw``, ``_rollups`` and ``_raw_floor``. A production TSDB
    keeps each series' head block in memory, seals it into an immutable compressed chunk every
    two hours, and answers each query from whichever tier carries the resolution asked for.
    That planning step is ``_plan``.
    """

    def __init__(self, max_series: int = 100_000, clock: Clock | None = None) -> None:
        self._max_series = max_series
        self._clock = clock or SystemClock()
        self._index = LabelIndex()
        self._raw: dict[str, list[Point]] = {}
        self._rollups: dict[int, dict[str, dict[int, Bucket]]] = {}
        self._raw_floor = float("-inf")  # oldest timestamp the head block still holds
        self._lock = threading.Lock()

    @property
    def cardinality(self) -> int:
        with self._lock:
            return len(self._index)

    def label_cardinality(self, key: str) -> int:
        with self._lock:
            return self._index.label_cardinality(key)

    def sample_count(self) -> int:
        with self._lock:
            return sum(len(samples) for samples in self._raw.values())

    def write(
        self,
        metric: str,
        labels: Mapping[str, str],
        value: float,
        timestamp: float | None = None,
    ) -> str:
        """Append one sample. A *new* series past the cap is rejected, not silently accepted.

        The alternative is that one bad deploy adds a ``request_id`` label and the database
        dies for every other tenant on the shard.
        """
        pairs = canonical(labels)
        sid = series_id(metric, pairs)
        ts = self._clock.now() if timestamp is None else timestamp
        with self._lock:
            if sid not in self._index:
                if len(self._index) >= self._max_series:
                    raise ValidationError(
                        f"series cardinality limit {self._max_series} reached; refusing {sid}"
                    )
                self._index.add(Series(sid, metric, pairs))
                self._raw[sid] = []
            samples = self._raw[sid]
            if samples and ts <= samples[-1].timestamp:
                raise ValidationError(f"out-of-order sample for {sid} at {ts}")
            samples.append(Point(ts, value))
        return sid

    def select(self, metric: str, matchers: Mapping[str, str] | None = None) -> list[str]:
        with self._lock:
            return self._index.select(metric, matchers or {})

    def compact(self, step: int, before: float) -> tuple[int, int]:
        """Fold raw samples older than ``before`` into ``step``-second buckets and drop them.

        Returns ``(samples dropped, buckets written)``. Re-running it is safe: an existing
        bucket is merged with the new one rather than overwritten.
        """
        if step <= 0:
            raise ValidationError("step must be positive")
        dropped = written = 0
        with self._lock:
            tier = self._rollups.setdefault(step, {})
            for sid, samples in self._raw.items():
                cut = bisect_left(samples, before, key=lambda p: p.timestamp)
                if cut == 0:
                    continue
                grouped: dict[int, list[float]] = {}
                for point in samples[:cut]:
                    grouped.setdefault(int(point.timestamp // step), []).append(point.value)
                series_tier = tier.setdefault(sid, {})
                for idx, values in grouped.items():
                    fresh = Bucket.of(float(idx * step), values)
                    previous = series_tier.get(idx)
                    series_tier[idx] = previous.merge(fresh) if previous else fresh
                    written += 1
                del samples[:cut]
                dropped += cut
            self._raw_floor = max(self._raw_floor, before)
        return dropped, written

    def range_query(
        self,
        metric: str,
        matchers: Mapping[str, str] | None = None,
        *,
        start: float,
        end: float,
        step: int,
        agg: Aggregation = Aggregation.AVG,
    ) -> list[Point]:
        """One line of a dashboard panel: fold every matching series into ``step`` buckets.

        Buckets are anchored to the epoch, never to ``start``, so a stored rollup bucket and
        a query bucket always line up; otherwise a 5-minute rollup read at a 5-minute step
        would smear two adjacent buckets together and the panel would disagree with raw.
        The fold is over samples, so ``avg`` is the count-weighted mean across series and
        time. A real engine separates the time-fold (``rate``, ``increase``) from the
        series-fold (``sum by (service)``); the shape of the plan is the same.
        """
        if step <= 0 or end <= start:
            raise ValidationError("need a positive step and end > start")
        ids = self.select(metric, matchers)
        folded: dict[int, Bucket] = {}
        with self._lock:
            source = self._plan(start, step)
            for sid in ids:
                for bucket in self._buckets_for(sid, source):
                    if not start <= bucket.start < end:
                        continue
                    idx = int(bucket.start // step)
                    folded[idx] = folded[idx].merge(bucket) if idx in folded else bucket
        return [Point(float(i * step), agg.apply(folded[i])) for i in sorted(folded)]

    def _plan(self, start: float, step: int) -> int | None:
        """Which tier answers this query: ``None`` for raw, else the rollup step to read.

        A tier is usable only when its resolution divides the requested step, which is the
        rule that keeps downsampled panels exact instead of merely plausible.
        """
        if start >= self._raw_floor:
            return None
        usable = [s for s in self._rollups if s <= step and step % s == 0]
        if not usable:
            raise ValidationError(f"no tier stores t={start:.0f} at a resolution of {step}s")
        return max(usable)

    def _buckets_for(self, sid: str, source: int | None) -> Iterable[Bucket]:
        if source is None:
            return (Bucket.of(p.timestamp, [p.value]) for p in self._raw.get(sid, ()))
        return iter(self._rollups.get(source, {}).get(sid, {}).values())


# --8<-- [end:tsdb]


# --8<-- [start:rules]
class AlertState(StrEnum):
    INACTIVE = "inactive"
    PENDING = "pending"
    FIRING = "firing"


@dataclass(frozen=True, slots=True)
class AlertRule:
    """A query, a threshold and a ``for`` duration.

    ``for_s`` is the field candidates forget. Without it every single-scrape blip pages
    somebody at 03:00, and the on-call engineer learns to ignore the channel.
    """

    name: str
    metric: str
    matchers: Labels
    threshold: float
    window_s: int = 300
    for_s: float = 0.0
    agg: Aggregation = Aggregation.MAX
    severity: str = "warning"
    group_by: tuple[str, ...] = ()

    def group_key(self) -> str:
        chosen = [f"{k}={v}" for k, v in self.matchers if k in self.group_by]
        return ",".join(chosen) if chosen else "-"


@dataclass(frozen=True, slots=True)
class Alert:
    fingerprint: str
    rule: str
    severity: str
    state: AlertState
    value: float
    since: float
    group_key: str


class RuleEvaluator:
    """Runs every rule on a fixed interval and owns the per-rule state machine.

    ``inactive -> pending`` on the first breach, ``pending -> firing`` once it has held for
    ``for_s``, straight back to ``inactive`` when it clears. The evaluator is the only
    component that knows *how long* a condition has held, which is why a restart re-arms
    every ``for:`` timer and why you never page straight from a query result.
    """

    def __init__(self, db: TimeSeriesDB) -> None:
        self._db = db
        self._state: dict[str, tuple[AlertState, float]] = {}
        self._lock = threading.Lock()

    def evaluate(self, rules: Iterable[AlertRule], now: float) -> list[Alert]:
        out: list[Alert] = []
        for rule in rules:
            points = self._db.range_query(
                rule.metric,
                dict(rule.matchers),
                start=now - rule.window_s,
                end=now,
                step=rule.window_s,
                agg=rule.agg,
            )
            value = points[-1].value if points else 0.0
            breached = value > rule.threshold
            with self._lock:
                state, since = self._state.get(rule.name, (AlertState.INACTIVE, now))
                if not breached:
                    state, since = AlertState.INACTIVE, now
                else:
                    if state is AlertState.INACTIVE:
                        since = now
                    held = now - since >= rule.for_s
                    state = AlertState.FIRING if held else AlertState.PENDING
                self._state[rule.name] = (state, since)
            out.append(
                Alert(
                    fingerprint=series_id(rule.name, rule.matchers),
                    rule=rule.name,
                    severity=rule.severity,
                    state=state,
                    value=value,
                    since=since,
                    group_key=rule.group_key(),
                )
            )
        return out


@dataclass(frozen=True, slots=True)
class Notification:
    receiver: str
    group_key: str
    fingerprints: tuple[str, ...]
    reason: str  # new | repeat | resolved


class AlertRouter:
    """Deduplicate, group, route and repeat: the notification half of the problem.

    Dedup is by fingerprint, so N evaluator replicas producing the same alert send one page.
    Grouping collapses "40 hosts in one rack are down" into a single notification. The repeat
    interval stops a firing alert from paging every evaluation cycle forever, and a group
    whose members have all cleared sends exactly one resolved message.
    """

    def __init__(self, routes: Mapping[str, str], repeat_interval_s: float = 3600.0) -> None:
        self._routes = dict(routes)
        self._repeat = repeat_interval_s
        self._sent: dict[str, float] = {}
        self._members: dict[str, tuple[str, ...]] = {}
        self._lock = threading.Lock()

    def dispatch(self, alerts: Iterable[Alert], now: float) -> list[Notification]:
        groups: dict[tuple[str, str], list[Alert]] = {}
        for alert in alerts:
            if alert.state is not AlertState.FIRING:
                continue
            receiver = self._routes.get(alert.severity, self._routes.get("default", "ops"))
            groups.setdefault((receiver, alert.group_key), []).append(alert)
        out: list[Notification] = []
        with self._lock:
            for (receiver, gkey), members in sorted(groups.items()):
                fingerprints = tuple(sorted(a.fingerprint for a in members))
                key = f"{receiver}|{gkey}"
                last = self._sent.get(key)
                if last is None or fingerprints != self._members.get(key):
                    reason = "new"
                elif now - last >= self._repeat:
                    reason = "repeat"
                else:
                    continue  # deduplicated: same members, still inside the repeat interval
                self._sent[key] = now
                self._members[key] = fingerprints
                out.append(Notification(receiver, gkey, fingerprints, reason))
            live = {f"{receiver}|{gkey}" for receiver, gkey in groups}
            for key in sorted(set(self._members) - live):
                receiver, gkey = key.split("|", 1)
                out.append(Notification(receiver, gkey, self._members.pop(key), "resolved"))
                self._sent.pop(key, None)
        return out


# --8<-- [end:rules]


DEMO_START = 1_700_000_100.0  # a multiple of 60 and of 300, so every bucket grid lines up


def _rounded(points: Sequence[Point]) -> list[tuple[float, float]]:
    """Compare two query results without tripping over float summation order."""
    return [(p.timestamp, round(p.value, 9)) for p in points]


def _checkout_latency(offset: int) -> float:
    """i-2 is healthy, spikes from t+1150s to t+1330s, then recovers."""
    if 1150 <= offset <= 1330:
        return 0.90
    return 0.10 if offset >= 1340 else 0.30


def main() -> None:
    db = TimeSeriesDB(max_series=4)
    for offset in range(0, 1401, 10):
        ts = DEMO_START + offset
        db.write("latency_seconds", {"service": "checkout", "instance": "i-1"}, 0.20 + 0.05 * (offset // 300 % 3), ts)
        db.write("latency_seconds", {"service": "checkout", "instance": "i-2"}, _checkout_latency(offset), ts)
        db.write("latency_seconds", {"service": "cart", "instance": "i-3"}, 0.05, ts)
    db.write("latency_seconds", {"service": "search", "user_id": "u-1"}, 0.4, DEMO_START + 1400)
    print(
        f"series={db.cardinality}  distinct instance values={db.label_cardinality('instance')}"
        f"  distinct user_id values={db.label_cardinality('user_id')}"
    )
    try:
        db.write("latency_seconds", {"service": "search", "user_id": "u-2"}, 0.4, DEMO_START + 1400)
    except ValidationError as exc:
        print(f"one more user_id label -> ValidationError: {exc}")

    checkout = {"service": "checkout"}
    query = dict(start=DEMO_START, end=DEMO_START + 900, step=300, agg=Aggregation.AVG)
    raw = db.range_query("latency_seconds", checkout, **query)
    print(f"raw samples in the head block: {db.sample_count()}")
    print("avg checkout latency per 5-minute bucket, from raw samples:")
    for point in raw:
        print(f"  t+{point.timestamp - DEMO_START:>5.0f}s  {point.value:.3f}")

    dropped, written = db.compact(step=300, before=DEMO_START + 900)
    print(f"compact(step=300s, before=t+900s): dropped {dropped} raw samples, wrote {written} buckets")
    rolled = db.range_query("latency_seconds", checkout, **query)
    print(f"head block now holds {db.sample_count()} raw samples")
    print(f"same query, now planned onto the 5-minute tier: identical={_rounded(rolled) == _rounded(raw)}")

    rule = AlertRule(
        name="CheckoutLatencyHigh",
        metric="latency_seconds",
        matchers=canonical({"service": "checkout"}),
        threshold=0.5,
        window_s=60,
        for_s=120.0,
        agg=Aggregation.MAX,
        severity="page",
        group_by=("service",),
    )
    evaluator = RuleEvaluator(db)
    router = AlertRouter({"page": "oncall-payments", "warning": "slack-alerts"})
    for offset in (1200, 1260, 1320, 1350, 1410):
        now = DEMO_START + offset
        alert = evaluator.evaluate([rule], now)[0]
        sent = router.dispatch([alert], now)
        note = sent[0] if sent else None
        detail = f"-> {note.reason} page to {note.receiver}" if note else "-> nothing sent"
        print(f"t+{offset}s  max={alert.value:.2f}  state={alert.state:<8} {detail}")


if __name__ == "__main__":
    main()
