import dataclasses
import math

import pytest

from common import ValidationError
from hld.estimation import (
    APP_SERVER_QPS,
    SECONDS_PER_DAY,
    Estimator,
    downtime,
    fmt_bytes,
    fmt_count,
    fmt_duration,
    parallel_availability,
    serial_availability,
)


def says(value: float, cheatsheet: float, tolerance: float = 0.05) -> bool:
    """The cheatsheet rounds to what you can say out loud ("~1.7k/s"); accept within 5%."""
    return abs(value - cheatsheet) <= tolerance * cheatsheet


def test_url_shortener_one_liners_match_the_cheatsheet() -> None:
    writes = Estimator(100e6, payload_bytes=500)  # 100M new URLs/day, ~500 B per record
    assert says(writes.qps, 1_200)
    assert says(writes.peak_qps, 3_500)
    reads = writes.with_ratio(100)
    assert says(reads.qps, 120_000)
    assert says(writes.storage_per_year(years=10), 180e12)
    assert says(writes.storage_per_year(years=10, replication=3), 550e12)
    assert says(reads.cache_size(), 1e12)  # 20% of 10B reads x 500 B ~ 1 TB of hot data


def test_twitter_one_liners_match_the_cheatsheet() -> None:
    posts = Estimator.from_dau(300e6, 0.5, payload_bytes=1_000)
    assert posts.requests_per_day == 150e6
    assert says(posts.qps, 1_700)
    assert says(posts.peak_qps, 5_000)
    reads = Estimator.from_dau(300e6, 50)
    assert reads.requests_per_day == 15e9
    assert says(reads.qps, 175_000)
    assert says(reads.peak_qps, 500_000)
    assert says(posts.storage_per_day, 150e9)
    assert says(posts.storage_per_year(), 55e12)
    media = Estimator(posts.requests_per_day * 0.10, payload_bytes=1e6)
    assert says(media.storage_per_day, 15e12)


def test_youtube_one_liner_matches_the_cheatsheet() -> None:
    uploads = Estimator.from_dau(5e6, 0.10, payload_bytes=300e6)
    assert uploads.requests_per_day == 500_000
    assert says(uploads.storage_per_day, 150e12)


@pytest.mark.parametrize(
    ("estimator", "qps", "peak", "storage_per_day"),
    [
        # chat: 50M DAU x 40 messages/day = 2B/day ~ 23k/s, peak ~70k, 2B x 100 B = 200 GB/day
        (Estimator.from_dau(50e6, 40, payload_bytes=100), 23_000, 70_000, 200e9),
        # metrics: 100k hosts x 100 metrics / 10 s = 1M points/s; 16 B raw = 16 MB/s, ~1.4 TB/day
        (Estimator(100_000 * 100 / 10 * SECONDS_PER_DAY, payload_bytes=16), 1e6, 3e6, 1.4e12),
    ],
)
def test_chat_and_metrics_one_liners(
    estimator: Estimator, qps: float, peak: float, storage_per_day: float
) -> None:
    assert says(estimator.qps, qps)
    assert says(estimator.peak_qps, peak)
    assert says(estimator.storage_per_day, storage_per_day)


def test_metrics_compress_to_about_120_gb_per_day() -> None:
    points = Estimator(1e6 * SECONDS_PER_DAY, payload_bytes=16)
    assert says(points.bandwidth, 16e6)  # 16 MB/s raw
    compressed = Estimator(points.requests_per_day, payload_bytes=1.4)  # Gorilla: ~1.4 B/point
    assert says(compressed.storage_per_day, 120e9)


def test_bandwidth_formula() -> None:
    traffic = Estimator(10_000 * SECONDS_PER_DAY, payload_bytes=10_000)  # 10k QPS x 10 KB
    assert traffic.bandwidth == pytest.approx(100e6)  # 100 MB/s
    assert traffic.bandwidth * 8 / 1e9 == pytest.approx(0.8)  # 0.8 Gbps
    assert traffic.peak_bandwidth == pytest.approx(300e6)


def test_servers_needed_rounds_up_and_applies_headroom() -> None:
    posts = Estimator(150e6)  # ~5.2k/s peak
    assert posts.servers_needed() == math.ceil(posts.peak_qps / APP_SERVER_QPS * 1.5) == 8
    assert posts.servers_needed(headroom=1.0) == 6
    assert posts.servers_needed(qps_per_server=10_000) == 1
    assert Estimator(0).servers_needed() == 0


def test_with_ratio_keeps_peak_factor_and_can_change_payload() -> None:
    writes = Estimator(1e6, payload_bytes=1_000, peak_factor=5)
    reads = writes.with_ratio(10, payload_bytes=20_000)
    assert reads.requests_per_day == 10e6
    assert reads.payload_bytes == 20_000
    assert reads.peak_factor == 5
    assert writes.with_ratio(10).payload_bytes == 1_000


def test_nines_to_downtime_table() -> None:
    assert downtime(0.99) / SECONDS_PER_DAY == pytest.approx(3.65, rel=0.01)
    assert downtime(0.999) / 3_600 == pytest.approx(8.76, rel=0.01)
    assert downtime(0.9999) / 60 == pytest.approx(52.6, rel=0.01)
    assert downtime(0.99999) / 60 == pytest.approx(5.26, rel=0.01)
    assert downtime(0.9999, period_seconds=SECONDS_PER_DAY) == pytest.approx(8.64)
    assert serial_availability(0.999, 0.999) == pytest.approx(0.998, rel=1e-4)
    assert parallel_availability(0.999, 0.999) == pytest.approx(0.999999)


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (150e9, "150 GB"),
        (54.75e12, "54.8 TB"),
        (999.9e9, "1 TB"),  # rounds up into the next unit instead of printing 1e+03 GB
        (500, "500 B"),
        (0, "0 B"),
    ],
)
def test_fmt_bytes(value: float, text: str) -> None:
    assert fmt_bytes(value) == text


def test_fmt_count_and_duration() -> None:
    assert fmt_count(150e6) == "150M"
    assert fmt_count(15e9) == "15B"
    assert fmt_count(500_000) == "500k"
    assert fmt_count(12) == "12"
    assert fmt_duration(downtime(0.999)) == "8.76 hours"
    assert fmt_duration(downtime(0.9999)) == "52.6 minutes"
    assert fmt_duration(downtime(0.99)) == "3.65 days"
    assert fmt_duration(45) == "45 seconds"


def test_validation_errors() -> None:
    with pytest.raises(ValidationError):
        Estimator(-1)
    with pytest.raises(ValidationError):
        Estimator(1, payload_bytes=-1)
    with pytest.raises(ValidationError):
        Estimator(1, peak_factor=0.5)
    with pytest.raises(ValidationError):
        Estimator.from_dau(-1, 1)
    est = Estimator(1_000, payload_bytes=10)
    with pytest.raises(ValidationError):
        est.with_ratio(0)
    with pytest.raises(ValidationError):
        est.cache_size(hot_fraction=1.5)
    with pytest.raises(ValidationError):
        est.servers_needed(qps_per_server=0)
    with pytest.raises(ValidationError):
        est.servers_needed(headroom=0.5)
    with pytest.raises(ValidationError):
        est.storage_per_year(replication=0)
    with pytest.raises(ValidationError):
        downtime(1.5)
    with pytest.raises(ValidationError):
        serial_availability()
    with pytest.raises(ValidationError):
        fmt_bytes(-1)


def test_estimator_is_an_immutable_value_object() -> None:
    est = Estimator(1_000)
    with pytest.raises(dataclasses.FrozenInstanceError):
        est.requests_per_day = 5  # type: ignore[misc]
    assert est == Estimator(1_000)
