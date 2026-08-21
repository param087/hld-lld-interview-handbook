import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, ValidationError
from hld.crawler_frontier import (
    ARTICLE,
    FOOTER,
    ContentDeduper,
    RobotsPolicy,
    UrlFrontier,
    hamming_distance,
    normalize,
    simhash,
)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=0.0)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/a?x=1&y=2",
        "https://EXAMPLE.com/a?y=2&x=1",
        "https://example.com:443/a?x=1&y=2#section",
    ],
)
def test_normalize_collapses_equivalent_spellings(url: str) -> None:
    assert normalize(url) == ("https://example.com/a?x=1&y=2", "example.com")


@pytest.mark.parametrize("url", ["ftp://example.com/x", "not-a-url", "https:///nohost"])
def test_normalize_rejects_what_a_crawler_cannot_fetch(url: str) -> None:
    with pytest.raises(ValidationError):
        normalize(url)


def test_filters_report_why_a_url_was_dropped(clock: FakeClock) -> None:
    frontier = UrlFrontier(max_depth=2, max_urls_per_host=2, clock=clock)
    frontier.set_robots("shop.test", RobotsPolicy(disallowed=("/cart",)))
    assert frontier.add("https://shop.test/a") is True
    assert frontier.add("https://SHOP.test/a") is False  # normalised to the same URL
    assert frontier.add("https://shop.test/cart/pay") is False  # robots.txt
    assert frontier.add("https://shop.test/deep", depth=9) is False  # trap depth
    assert frontier.add("https://shop.test/b") is True
    assert frontier.add("https://shop.test/c") is False  # per-host cap of 2
    stats = frontier.stats()
    assert (stats.rejected_seen, stats.rejected_robots, stats.rejected_depth) == (1, 1, 1)
    assert stats.rejected_host_cap == 1
    assert stats.queued == 2


def test_high_priority_urls_are_dequeued_first(clock: FakeClock) -> None:
    frontier = UrlFrontier(default_delay=0.0, clock=clock)
    frontier.add("https://low.test/1", priority=1)
    frontier.add("https://high.test/1", priority=5)
    frontier.add("https://mid.test/1", priority=3)
    order = []
    for _ in range(3):
        request = frontier.next_url()
        assert request is not None
        order.append(request.host)
        frontier.complete(request)
    assert order == ["high.test", "mid.test", "low.test"]


def test_only_max_back_queues_hosts_are_promoted_and_priority_decides_which(
    clock: FakeClock,
) -> None:
    """Back queues are a bounded resource; the front queues decide who gets one."""
    frontier = UrlFrontier(default_delay=99.0, max_back_queues=2, clock=clock)
    for priority, host in enumerate(["d.test", "c.test", "b.test", "a.test"], start=1):
        for i in range(2):
            frontier.add(f"https://{host}/{i}", priority=priority)
    promoted = []
    for _ in range(2):
        request = frontier.next_url()
        assert request is not None
        promoted.append(request.host)
    assert promoted == ["a.test", "b.test"]  # priorities 4 and 3 won the two slots
    assert frontier.next_url() is None  # both hosts busy, no slot for a third back queue


def test_politeness_keeps_one_host_at_arms_length(clock: FakeClock) -> None:
    frontier = UrlFrontier(default_delay=1.5, clock=clock)
    for i in range(3):
        frontier.add(f"https://one.test/{i}")
    first = frontier.next_url()
    assert first is not None
    frontier.complete(first)
    assert frontier.next_url() is None  # still cooling down
    assert frontier.next_ready_at() == pytest.approx(1.5)
    clock.set(1.5)
    second = frontier.next_url()
    assert second is not None and second.url != first.url


def test_robots_crawl_delay_overrides_a_faster_default(clock: FakeClock) -> None:
    frontier = UrlFrontier(default_delay=0.5, clock=clock)
    frontier.set_robots("slow.test", RobotsPolicy(crawl_delay=4.0))
    frontier.set_robots("fast.test", RobotsPolicy(crawl_delay=0.1))
    assert frontier.delay_for("slow.test") == 4.0
    assert frontier.delay_for("fast.test") == 0.5  # never faster than your own floor
    assert frontier.delay_for("unknown.test") == 0.5


def test_simhash_finds_a_reskinned_page_and_ignores_a_different_one() -> None:
    deduper = ContentDeduper(max_distance=3)
    deduper.register("https://news.test/story", ARTICLE)
    unrelated = "kubernetes operators reconcile desired state against observed cluster state."
    assert hamming_distance(simhash(ARTICLE), simhash(ARTICLE + FOOTER)) <= 3
    assert deduper.find_duplicate(ARTICLE + FOOTER) == "https://news.test/story"
    assert deduper.find_duplicate(unrelated) is None
    assert simhash("") == 0
    with pytest.raises(ValidationError):
        ContentDeduper(max_distance=4)


def test_concurrent_workers_never_hit_one_host_twice_at_once(clock: FakeClock) -> None:
    hosts = [f"h{i}.test" for i in range(4)]
    frontier = UrlFrontier(default_delay=0.0, max_back_queues=8, clock=clock)
    for host in hosts:
        for i in range(50):
            frontier.add(f"https://{host}/{i}")
    guard = threading.Lock()
    in_flight: set[str] = set()
    violations: list[str] = []
    fetched: list[str] = []

    def worker() -> None:
        for _ in range(2_000):
            request = frontier.next_url()
            if request is None:
                if frontier.stats().queued == 0:
                    return
                continue
            with guard:
                if request.host in in_flight:
                    violations.append(request.host)
                in_flight.add(request.host)
                fetched.append(request.url)
            with guard:
                in_flight.discard(request.host)
            frontier.complete(request)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: worker(), range(8)))
    assert violations == []
    assert len(fetched) == len(set(fetched)) == 200
    assert frontier.stats().queued == 0
