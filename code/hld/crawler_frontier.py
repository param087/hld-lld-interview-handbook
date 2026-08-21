"""The URL frontier of a web crawler: priority in, politeness out, duplicates never.

What the module demonstrates, in the order an interviewer asks about it:

* ``UrlFrontier`` is the Mercator two-level queue. **Front queues** hold URLs by priority;
  **back queues** hold URLs for exactly one host each, so a worker that pops a back queue is
  physically unable to hammer a host. A min-heap of ``(ready_at, host)`` decides who is next.
* Politeness is two rules, not one: at most one in-flight request per host (``next_url`` takes
  the host off the heap, ``complete`` puts it back) and a minimum gap between requests, taken
  from ``robots.txt`` ``Crawl-delay`` when the host publishes one.
* Dedup is also two rules: a ``BloomFilter`` over normalised URLs answers "have I ever queued
  this?" in ~10 bits per URL, and ``ContentDeduper`` answers "is this page a near-duplicate of
  one I already have?" with a 64-bit SimHash and a banded index.
* Traps are bounded by ``max_depth`` and ``max_urls_per_host``, because an infinite calendar
  will happily generate URLs until you run out of disk.

The clock is injected, so the demo and the tests exercise real politeness delays without
sleeping. Reuses ``hld.bloom_filter``; nothing here re-implements it.
"""

from __future__ import annotations

import hashlib
import heapq
import re
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from common import Clock, SystemClock, ValidationError
from hld.bloom_filter import BloomFilter

TOKEN = re.compile(r"[a-z0-9]+")
DEFAULT_PORTS = {"http": 80, "https": 443}


# --8<-- [start:normalize]
def normalize(url: str) -> tuple[str, str]:
    """``(canonical_url, host)``. Two spellings of one page must produce one key.

    Lowercases the scheme and host, drops the default port and the fragment, sorts query
    parameters and forces an empty path to ``/`` -- without this the Bloom filter would
    happily queue ``Example.com/a?b=1&c=2`` and ``example.com/a?c=2&b=1`` as two pages.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        raise ValidationError(f"unsupported scheme in {url!r}")
    host = parts.hostname or ""
    if not host:
        raise ValidationError(f"no host in {url!r}")
    netloc = host if parts.port in (None, DEFAULT_PORTS[scheme]) else f"{host}:{parts.port}"
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((scheme, netloc, parts.path or "/", query, "")), host


@dataclass(frozen=True, slots=True)
class RobotsPolicy:
    """The part of ``robots.txt`` a frontier cares about, already parsed."""

    disallowed: tuple[str, ...] = ()
    crawl_delay: float | None = None

    def allows(self, path: str) -> bool:
        return not any(path.startswith(prefix) for prefix in self.disallowed)


@dataclass(frozen=True, slots=True)
class CrawlRequest:
    url: str
    host: str
    depth: int
    priority: int


@dataclass(frozen=True, slots=True)
class FrontierStats:
    queued: int  # URLs accepted and still waiting
    rejected_seen: int  # the Bloom filter had already seen the URL
    rejected_robots: int
    rejected_depth: int
    rejected_host_cap: int
    hosts: int


# --8<-- [end:normalize]


# --8<-- [start:frontier]
class UrlFrontier:
    """Priority front queues, per-host back queues, one ready-heap. Thread-safe.

    ``_lock`` guards every field below: the front queues, the back queues, the ready heap, the
    busy-host set and the counters. Fetching happens outside the lock -- a worker calls
    ``next_url``, does its HTTP request, then calls ``complete``.
    """

    def __init__(
        self,
        default_delay: float = 1.0,
        max_back_queues: int = 8,
        max_depth: int = 4,
        max_urls_per_host: int = 10_000,
        capacity: int = 1_000_000,
        error_rate: float = 0.01,
        clock: Clock | None = None,
    ) -> None:
        if default_delay < 0:
            raise ValidationError("default_delay cannot be negative")
        if max_back_queues <= 0:
            raise ValidationError("max_back_queues must be positive")
        self._default_delay = default_delay
        self._max_back_queues = max_back_queues
        self._max_depth = max_depth
        self._max_urls_per_host = max_urls_per_host
        self._clock = clock or SystemClock()
        self._seen = BloomFilter(capacity, error_rate)
        self._robots: dict[str, RobotsPolicy] = {}
        self._front: dict[int, deque[CrawlRequest]] = defaultdict(deque)
        self._back: dict[str, deque[CrawlRequest]] = {}
        # heap of (earliest next fetch, -priority of this host's next URL, host)
        self._ready: list[tuple[float, int, str]] = []
        self._busy: set[str] = set()
        self._per_host_count: dict[str, int] = defaultdict(int)
        self._counts = {"seen": 0, "robots": 0, "depth": 0, "host_cap": 0}
        self._lock = threading.RLock()

    def set_robots(self, host: str, policy: RobotsPolicy) -> None:
        with self._lock:
            self._robots[host] = policy

    def delay_for(self, host: str) -> float:
        """``robots.txt`` wins when it is stricter; you never crawl faster than asked."""
        policy = self._robots.get(host)
        if policy is None or policy.crawl_delay is None:
            return self._default_delay
        return max(self._default_delay, policy.crawl_delay)

    def add(self, url: str, priority: int = 1, depth: int = 0) -> bool:
        """Enqueue one discovered link. ``False`` means it was filtered, and why is counted."""
        canonical, host = normalize(url)
        path = urlsplit(canonical).path
        with self._lock:
            if depth > self._max_depth:  # a crawler trap generates depth, not content
                self._counts["depth"] += 1
                return False
            policy = self._robots.get(host)
            if policy is not None and not policy.allows(path):
                self._counts["robots"] += 1
                return False
            if canonical in self._seen:  # false positives drop a page; false negatives cannot happen
                self._counts["seen"] += 1
                return False
            if self._per_host_count[host] >= self._max_urls_per_host:
                self._counts["host_cap"] += 1
                return False
            self._seen.add(canonical)
            self._per_host_count[host] += 1
            self._front[priority].append(CrawlRequest(canonical, host, depth, priority))
            return True

    def next_url(self) -> CrawlRequest | None:
        """The highest-priority URL of a host that is neither busy nor inside its delay."""
        with self._lock:
            self._refill()
            now = self._clock.now()
            if not self._ready or self._ready[0][0] > now:
                return None  # every host is either busy or still cooling down
            _, _, host = heapq.heappop(self._ready)
            queue = self._back[host]
            request = queue.popleft()
            self._busy.add(host)  # one in-flight request per host, always
            if not queue:
                del self._back[host]
            return request

    def complete(self, request: CrawlRequest) -> None:
        """Call after the fetch. The host cools down for its delay before it is eligible again.

        Production adds a lease timeout here so a worker that dies mid-fetch cannot park a host
        forever; the sweeper re-arms any host busy for longer than the lease.
        """
        with self._lock:
            self._busy.discard(request.host)
            if request.host in self._back:
                self._arm(request.host, self._clock.now() + self.delay_for(request.host))

    def next_ready_at(self) -> float | None:
        """When the earliest host becomes eligible, so a worker sleeps instead of spinning."""
        with self._lock:
            self._refill()
            return self._ready[0][0] if self._ready else None

    def _refill(self) -> None:
        """Move URLs from the priority front queues into per-host back queues.

        Strict priority order: queue 3 drains before queue 1. That can starve low-priority
        URLs, which is why real crawlers pick the front queue by a weighted lottery instead.
        """
        while len(self._back) < self._max_back_queues:
            request = self._take_highest_priority()
            if request is None:
                return
            queue = self._back.get(request.host)
            if queue is None:
                self._back[request.host] = deque([request])
                if request.host not in self._busy:
                    self._arm(request.host, self._clock.now())
            else:
                queue.append(request)

    def _arm(self, host: str, ready_at: float) -> None:
        """Make ``host`` eligible at ``ready_at``. Ties on the clock go to the higher priority."""
        heapq.heappush(self._ready, (ready_at, -self._back[host][0].priority, host))

    def _take_highest_priority(self) -> CrawlRequest | None:
        for priority in sorted(self._front, reverse=True):
            queue = self._front[priority]
            if queue:
                return queue.popleft()
        return None

    def stats(self) -> FrontierStats:
        with self._lock:
            queued = sum(len(q) for q in self._front.values()) + sum(
                len(q) for q in self._back.values()
            )
            return FrontierStats(
                queued=queued,
                rejected_seen=self._counts["seen"],
                rejected_robots=self._counts["robots"],
                rejected_depth=self._counts["depth"],
                rejected_host_cap=self._counts["host_cap"],
                hosts=len(self._per_host_count),
            )


# --8<-- [end:frontier]


# --8<-- [start:simhash]
def simhash(text: str, shingle: int = 3) -> int:
    """64-bit SimHash over word shingles: similar documents get similar bit patterns.

    Each shingle votes +1 or -1 on all 64 bits according to its own hash; the sign of each
    column becomes the output bit. Change a few words and only a few columns flip sign, which
    is exactly the property a cryptographic hash does not have.
    """
    tokens = TOKEN.findall(text.lower())
    if not tokens:
        return 0
    shingles = [" ".join(tokens[i : i + shingle]) for i in range(max(1, len(tokens) - shingle + 1))]
    columns = [0] * 64
    for gram in shingles:
        digest = int.from_bytes(hashlib.blake2b(gram.encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            columns[bit] += 1 if digest >> bit & 1 else -1
    value = 0
    for bit, total in enumerate(columns):
        if total > 0:
            value |= 1 << bit
    return value


def hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


class ContentDeduper:
    """Near-duplicate detection over SimHashes, indexed by 16-bit bands.

    Two hashes within ``max_distance <= 3`` must agree on at least one of the four 16-bit
    bands (pigeonhole), so a candidate lookup touches four buckets instead of the whole corpus.
    ``_lock`` guards the band index and the stored hashes.
    """

    BANDS = 4
    BAND_BITS = 16

    def __init__(self, max_distance: int = 3) -> None:
        if not 0 <= max_distance < self.BANDS:
            raise ValidationError(f"max_distance must be in [0, {self.BANDS})")
        self._max_distance = max_distance
        self._bands: dict[tuple[int, int], list[str]] = defaultdict(list)
        self._hashes: dict[str, int] = {}
        self._lock = threading.Lock()

    def _keys(self, value: int) -> list[tuple[int, int]]:
        mask = (1 << self.BAND_BITS) - 1
        return [(i, value >> (i * self.BAND_BITS) & mask) for i in range(self.BANDS)]

    def find_duplicate(self, text: str) -> str | None:
        """The URL of a stored near-duplicate, or ``None``."""
        value = simhash(text)
        with self._lock:
            for key in self._keys(value):
                for url in self._bands[key]:
                    if hamming_distance(value, self._hashes[url]) <= self._max_distance:
                        return url
        return None

    def register(self, url: str, text: str) -> int:
        value = simhash(text)
        with self._lock:
            self._hashes[url] = value
            for key in self._keys(value):
                self._bands[key].append(url)
        return value


# --8<-- [end:simhash]


ARTICLE = (
    "distributed web crawlers keep a frontier of urls they still intend to fetch. "
    "the frontier decides which page a worker downloads next, and it must balance two goals "
    "that pull against each other: crawl the important pages first, and never overload a "
    "single host. mercator solved this with two levels of queues, priority in front and one "
    "queue per host behind them, so that politeness is a property of the data structure "
    "rather than a rule every worker has to remember to follow. "
    "each back queue holds urls for exactly one host, and a heap of ready times decides which "
    "host a worker may touch next. a crawler that ignores this becomes an accidental denial of "
    "service against small sites, and gets its address range blocked within a day. politeness "
    "is cheap to implement and expensive to retrofit, so it belongs in the frontier itself. "
    "the second half of the problem is duplication. the same document is reachable through many "
    "urls, and many documents differ only in a navigation bar or a tracking parameter. a bloom "
    "filter over canonical urls answers the first question in about ten bits per url, and a "
    "simhash over the extracted text answers the second one without storing the text at all. "
    "freshness is the third axis. a news home page changes every few minutes while an archived "
    "page from a decade ago never changes again, so a single recrawl interval either wastes "
    "bandwidth or serves stale results. estimate a change rate per url from its own history and "
    "schedule the next visit from that estimate, with a cap so nothing is forgotten forever."
)
FOOTER = " related stories: politics, sport, weather. copyright example ltd. terms and privacy."


def main() -> None:
    from common import FakeClock

    clock = FakeClock(start=0.0)
    frontier = UrlFrontier(default_delay=1.0, max_depth=2, clock=clock)
    frontier.set_robots("shop.example", RobotsPolicy(disallowed=("/cart",), crawl_delay=2.0))
    seeds = [
        ("https://news.example/?a=1&b=2", 3, 0),
        ("https://NEWS.example:443/?b=2&a=1", 3, 0),  # the same page after normalisation
        ("https://news.example/politics", 3, 1),
        ("https://shop.example/deals", 1, 0),
        ("https://shop.example/cart/checkout", 1, 0),  # robots.txt disallows /cart
        ("https://blog.example/calendar/2031/07/04", 1, 5),  # calendar trap: too deep
    ]
    for url, priority, depth in seeds:
        frontier.add(url, priority=priority, depth=depth)
    stats = frontier.stats()
    print(
        f"seeded {len(seeds)}: queued={stats.queued} hosts={stats.hosts} "
        f"rejected seen={stats.rejected_seen} robots={stats.rejected_robots} depth={stats.rejected_depth}"
    )

    for _ in range(8):
        request = frontier.next_url()
        if request is None:
            ready = frontier.next_ready_at()
            if ready is None:
                print(f"t={clock.now():4.1f}  frontier empty")
                break
            clock.set(ready)
            continue
        delay = frontier.delay_for(request.host)
        print(f"t={clock.now():4.1f}  GET {request.url}  (p{request.priority}, next fetch of this host in {delay}s)")
        frontier.complete(request)

    deduper = ContentDeduper(max_distance=3)
    deduper.register("https://news.example/story", ARTICLE)
    unrelated = "kubernetes operators reconcile desired state against observed cluster state and retry with backoff until they agree."
    for label, text in (("same article, new footer", ARTICLE + FOOTER), ("unrelated page", unrelated)):
        distance = hamming_distance(simhash(ARTICLE), simhash(text))
        print(f"simhash {label:24s} hamming={distance:2d} duplicate_of={deduper.find_duplicate(text)}")


if __name__ == "__main__":
    main()
