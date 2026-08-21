"""Back-of-envelope estimation: the four numbers of a system design round, as code.

What the module demonstrates, in the order an interviewer asks for it:

* ``Estimator`` turns "N daily users doing K things a day with P-byte payloads" into
  average and peak QPS, storage per day and per year, bandwidth, cache size and a server
  count, using exactly the formulas on the latency-and-estimation cheatsheet.
* ``downtime``, ``serial_availability`` and ``parallel_availability`` turn nines into
  minutes and show why chained dependencies lose a nine.
* ``fmt_count``, ``fmt_bytes`` and ``fmt_duration`` print numbers the way you say them
  ("150M", "55 TB", "52.6 minutes"), three significant figures at most.

The tests assert that the cheatsheet's worked one-liners (URL shortener, Twitter-like feed,
YouTube uploads, chat, metrics) come out of this code within the cheatsheet's rounding.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from common import ValidationError

# --8<-- [start:constants]
SECONDS_PER_DAY = 86_400  # say "10^5" in the room; the code keeps the exact value
DAYS_PER_YEAR = 365
SECONDS_PER_YEAR = SECONDS_PER_DAY * DAYS_PER_YEAR  # ~31.5M
DEFAULT_PEAK_FACTOR = 3.0  # peak is 2-5x average; 3x unless told otherwise, 10x for events
DEFAULT_HOT_FRACTION = 0.2  # 80/20 rule: the hottest 20% of objects take ~80% of the reads
APP_SERVER_QPS = 1_000  # stateless app server doing real work; 10k+ for trivial work
DEFAULT_HEADROOM = 1.5  # provision 1.5-2x the peak so one failure or a spike does not hurt
# --8<-- [end:constants]


# --8<-- [start:estimator]
@dataclass(frozen=True, slots=True)
class Estimator:
    """One traffic class (the writes, or the reads) described by daily volume and payload.

    ``requests_per_day`` alone gives QPS; add ``payload_bytes`` for storage, bandwidth and
    cache size. Peak is a multiple of average because a day is not flat.
    """

    requests_per_day: float
    payload_bytes: float = 0.0
    peak_factor: float = DEFAULT_PEAK_FACTOR

    def __post_init__(self) -> None:
        if self.requests_per_day < 0 or self.payload_bytes < 0:
            raise ValidationError("volumes and sizes cannot be negative")
        if self.peak_factor < 1:
            raise ValidationError("peak factor must be >= 1 (peak is never below average)")

    @classmethod
    def from_dau(
        cls,
        dau: float,
        actions_per_user_per_day: float,
        payload_bytes: float = 0.0,
        peak_factor: float = DEFAULT_PEAK_FACTOR,
    ) -> Estimator:
        """``DAU x actions per user per day`` is the first line of every estimation."""
        if dau < 0 or actions_per_user_per_day < 0:
            raise ValidationError("DAU and actions per user cannot be negative")
        return cls(dau * actions_per_user_per_day, payload_bytes, peak_factor)

    def with_ratio(self, ratio: float, payload_bytes: float | None = None) -> Estimator:
        """The other side of a read/write ratio: ``writes.with_ratio(100)`` is the read load."""
        if ratio <= 0:
            raise ValidationError("ratio must be positive")
        payload = self.payload_bytes if payload_bytes is None else payload_bytes
        return Estimator(self.requests_per_day * ratio, payload, self.peak_factor)

    @property
    def qps(self) -> float:
        """Average requests per second: daily volume / 86,400 (1M/day is ~12/s)."""
        return self.requests_per_day / SECONDS_PER_DAY

    @property
    def peak_qps(self) -> float:
        """Average x peak factor: the number you size the serving tier for."""
        return self.qps * self.peak_factor

    @property
    def storage_per_day(self) -> float:
        """Bytes written per day: daily volume x object size."""
        return self.requests_per_day * self.payload_bytes

    def storage_per_year(self, years: float = 1.0, replication: int = 1) -> float:
        """Bytes after ``years``; ``replication=3`` is the raw disk for three copies."""
        if years < 0 or replication < 1:
            raise ValidationError("years must be >= 0 and replication >= 1")
        return self.storage_per_day * DAYS_PER_YEAR * years * replication

    @property
    def bandwidth(self) -> float:
        """Average bytes per second on the wire: QPS x payload (x8 for bits)."""
        return self.qps * self.payload_bytes

    @property
    def peak_bandwidth(self) -> float:
        return self.peak_qps * self.payload_bytes

    def cache_size(self, hot_fraction: float = DEFAULT_HOT_FRACTION) -> float:
        """80/20 rule: bytes to hold the hot ``hot_fraction`` of a day's objects."""
        if not 0 <= hot_fraction <= 1:
            raise ValidationError("hot fraction must be between 0 and 1")
        return self.requests_per_day * hot_fraction * self.payload_bytes

    def servers_needed(
        self, qps_per_server: float = APP_SERVER_QPS, headroom: float = DEFAULT_HEADROOM
    ) -> int:
        """Peak QPS / capacity per node, times headroom, rounded up to whole machines."""
        if qps_per_server <= 0:
            raise ValidationError("capacity per server must be positive")
        if headroom < 1:
            raise ValidationError("headroom must be >= 1")
        return math.ceil(self.peak_qps / qps_per_server * headroom)


# --8<-- [end:estimator]


# --8<-- [start:availability]
def downtime(availability: float, period_seconds: float = SECONDS_PER_YEAR) -> float:
    """Seconds of allowed downtime per period: (1 - availability) x period.

    0.999 over a year is 31,536 s = 8.76 hours; 0.9999 is 52.6 minutes.
    """
    if not 0 <= availability <= 1:
        raise ValidationError("availability is a fraction between 0 and 1")
    if period_seconds < 0:
        raise ValidationError("period cannot be negative")
    return (1 - availability) * period_seconds


def serial_availability(*parts: float) -> float:
    """A request that needs every part: availabilities multiply (0.999 x 0.999 = 0.998)."""
    return math.prod(_checked(parts))


def parallel_availability(*parts: float) -> float:
    """A request that needs any one part: 1 - product of the failure probabilities."""
    return 1 - math.prod(1 - a for a in _checked(parts))


def _checked(parts: tuple[float, ...]) -> tuple[float, ...]:
    if not parts or any(not 0 <= a <= 1 for a in parts):
        raise ValidationError("give at least one availability between 0 and 1")
    return parts


# --8<-- [end:availability]


# --8<-- [start:formatting]
_COUNT_UNITS = ("", "k", "M", "B", "T")
_BYTE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB", "EB")


def _scale(n: float, units: tuple[str, ...], step: float = 1000.0) -> tuple[str, str]:
    """Pick the unit that leaves a mantissa below ``step`` after rounding to 3 figures."""
    if n < 0:
        raise ValidationError("cannot format a negative quantity")
    value, idx = float(n), 0
    while idx < len(units) - 1 and float(f"{value:.3g}") >= step:
        value /= step
        idx += 1
    return f"{value:.3g}", units[idx]


def fmt_count(n: float) -> str:
    """150_000_000 -> '150M', 15e9 -> '15B': decimal units, three significant figures."""
    mantissa, unit = _scale(n, _COUNT_UNITS)
    return f"{mantissa}{unit}"


def fmt_bytes(n: float) -> str:
    """Decimal (SI) units as the cheatsheet uses for storage: 150e9 -> '150 GB'."""
    mantissa, unit = _scale(n, _BYTE_UNITS)
    return f"{mantissa} {unit}"


def fmt_duration(seconds: float) -> str:
    """Largest unit that keeps the number readable: 31,536 s -> '8.76 hours'."""
    if seconds < 0:
        raise ValidationError("cannot format a negative duration")
    for size, name in ((SECONDS_PER_DAY, "days"), (3_600, "hours"), (60, "minutes")):
        if seconds >= 2 * size:
            return f"{seconds / size:.3g} {name}"
    return f"{seconds:.3g} seconds"


# --8<-- [end:formatting]


def main() -> None:
    print(f"a day is {SECONDS_PER_DAY:,} s (say 10^5); peak = {DEFAULT_PEAK_FACTOR:.0f}x average")

    posts = Estimator.from_dau(300e6, 0.5, payload_bytes=1_000)
    reads = Estimator.from_dau(300e6, 50, payload_bytes=20 * 1_000)  # a page is 20 posts x 1 KB
    media = Estimator(posts.requests_per_day * 0.10, payload_bytes=1e6)
    print(f"\nTwitter-like feed: 300M DAU x 0.5 posts = {fmt_count(posts.requests_per_day)} posts/day,"
          f" x 50 reads = {fmt_count(reads.requests_per_day)} reads/day")
    print(f"  writes   {posts.qps:,.0f}/s avg, {posts.peak_qps:,.0f}/s peak")
    print(f"  reads    {reads.qps:,.0f}/s avg, {reads.peak_qps:,.0f}/s peak,"
          f" {fmt_bytes(reads.bandwidth)}/s of feed pages")
    print(f"  text     {fmt_bytes(posts.storage_per_day)}/day, {fmt_bytes(posts.storage_per_year())}/year,"
          f" {fmt_bytes(posts.storage_per_year(replication=3))}/year with 3 replicas")
    print(f"  media    10% x 1 MB = {fmt_bytes(media.storage_per_day)}/day")
    print(f"  servers  {reads.servers_needed():,} app servers at {APP_SERVER_QPS:,} QPS and 1.5x headroom;"
          f" {reads.servers_needed(qps_per_server=10_000)} if a cache makes reads trivial")

    uploads = Estimator.from_dau(5e6, 0.10, payload_bytes=300e6)
    print(f"\nYouTube uploads: 5M DAU x 10% x 1 video = {fmt_count(uploads.requests_per_day)} videos/day x 300 MB")
    print(f"  ingest   {uploads.qps:.1f} uploads/s avg, {fmt_bytes(uploads.bandwidth)}/s in,"
          f" {fmt_bytes(uploads.peak_bandwidth)}/s at peak")
    print(f"  storage  {fmt_bytes(uploads.storage_per_day)}/day raw, {fmt_bytes(uploads.storage_per_year())}/year"
          f" before transcoding multiplies it")

    writes = Estimator(100e6, payload_bytes=500)
    lookups = writes.with_ratio(100)
    print(f"\nURL shortener: {fmt_count(writes.requests_per_day)} new URLs/day, 100:1 reads, 500 B per record")
    print(f"  writes   {writes.qps:,.0f}/s avg, {writes.peak_qps:,.0f}/s peak")
    print(f"  reads    {lookups.qps:,.0f}/s avg, {lookups.peak_qps:,.0f}/s peak")
    print(f"  storage  {fmt_bytes(writes.storage_per_year(years=10))} over 10 years,"
          f" {fmt_bytes(writes.storage_per_year(years=10, replication=3))} with 3 replicas")
    print(f"  cache    20% of {fmt_count(lookups.requests_per_day)} reads x 500 B = {fmt_bytes(lookups.cache_size())}"
          f" of hot data per day (hold the hottest few GB)")

    print("\navailability: " + "; ".join(
        f"{a:.{d}%} = {fmt_duration(downtime(a))}/year" for a, d in ((0.999, 1), (0.9999, 2), (0.99999, 3))
    ))
    print(f"two 99.9% services in series = {serial_availability(0.999, 0.999):.1%};"
          f" in parallel = {parallel_availability(0.999, 0.999):.4%}")


if __name__ == "__main__":
    main()
