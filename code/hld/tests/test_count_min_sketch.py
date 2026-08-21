from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ValidationError
from hld.count_min_sketch import CountMinSketch, TopK, zipf_stream


def test_sizing_from_epsilon_and_delta() -> None:
    sketch = CountMinSketch(epsilon=0.01, delta=0.01)
    assert (sketch.width, sketch.depth) == (272, 5)
    assert sketch.memory_bytes() == 4 * 272 * 5
    tight = CountMinSketch(epsilon=0.001, delta=0.001)
    assert (tight.width, tight.depth) == (2_719, 7)
    assert sketch.error_bound == 0
    sketch.add("x", 1_000)
    assert sketch.error_bound == 10


def test_never_underestimates_and_respects_the_error_bound() -> None:
    stream = zipf_stream(keys=5_000, events=30_000, seed=11)
    exact = Counter(stream)
    sketch = CountMinSketch(epsilon=0.001, delta=0.01)
    for item in stream:
        sketch.add(item)
    assert sketch.total == 30_000
    overs = {item: sketch.estimate(item) - count for item, count in exact.items()}
    assert min(overs.values()) >= 0
    beyond = sum(over > sketch.error_bound for over in overs.values())
    assert beyond <= sketch.delta * len(exact)  # the guarantee: P[over > eps N] <= delta
    assert sketch.estimate("never-seen") <= sketch.error_bound


def test_heavy_hitters_are_estimated_almost_exactly() -> None:
    stream = zipf_stream(keys=5_000, events=30_000, seed=12)
    exact = Counter(stream)
    sketch = CountMinSketch(epsilon=0.001, delta=0.01)
    for item in stream:
        sketch.add(item)
    for item, count in exact.most_common(10):
        assert count <= sketch.estimate(item) <= count + sketch.error_bound


def test_merge_equals_one_sketch_over_both_streams() -> None:
    left, right = zipf_stream(2_000, 10_000, seed=13), zipf_stream(2_000, 10_000, seed=14)
    a, b, whole = (CountMinSketch(0.001, 0.01) for _ in range(3))
    for item in left:
        a.add(item)
        whole.add(item)
    for item in right:
        b.add(item)
        whole.add(item)
    a.merge(b)
    assert a.total == whole.total == 20_000
    assert all(a.estimate(f"k{i}") == whole.estimate(f"k{i}") for i in range(2_000))
    with pytest.raises(ValidationError):
        a.merge(CountMinSketch(0.01, 0.01))
    with pytest.raises(ValidationError):
        a.merge(a)


def test_topk_finds_the_true_heavy_hitters_in_order() -> None:
    stream = zipf_stream(keys=5_000, events=30_000, seed=15)
    exact = Counter(stream)
    topk = TopK(k=5, epsilon=0.001, delta=0.01)
    for item in stream:
        topk.add(item)
    expected = sorted(exact.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    assert [item for item, _ in topk.top()] == [item for item, _ in expected]
    for (_item, estimate), (_, count) in zip(topk.top(), expected, strict=True):
        assert count <= estimate <= count + topk.sketch.error_bound
    assert len(topk.top(3)) == 3
    assert len(topk._heap) <= 4 * topk.k


def test_topk_evicts_the_smallest_candidate() -> None:
    topk = TopK(k=2, epsilon=0.01, delta=0.01)
    topk.add("a", 5)
    topk.add("b", 3)
    assert [item for item, _ in topk.top()] == ["a", "b"]
    topk.add("c", 4)  # beats b
    assert [item for item, _ in topk.top()] == ["a", "c"]
    topk.add("b", 1)  # b is now 4 in the sketch, not above the heap minimum of 4
    assert [item for item, _ in topk.top()] == ["a", "c"]
    topk.add("b", 1)  # b is 5: ties a, beats c
    assert [item for item, _ in topk.top()] == ["a", "b"]


@pytest.mark.parametrize("action", [
    lambda: CountMinSketch(epsilon=0.0),
    lambda: CountMinSketch(delta=1.0),
    lambda: CountMinSketch().add("x", 0),
    lambda: CountMinSketch().add("x", -1),
    lambda: TopK(k=0),
])
def test_validation(action) -> None:
    with pytest.raises(ValidationError):
        action()


def test_concurrent_adds_keep_totals_and_lower_bounds() -> None:
    stream = zipf_stream(keys=1_000, events=16_000, seed=16)
    exact = Counter(stream)
    topk = TopK(k=10, epsilon=0.001, delta=0.01)
    chunks = [stream[i::8] for i in range(8)]

    def add_all(chunk: list[str]) -> None:
        for item in chunk:
            topk.add(item)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(add_all, chunks))
    assert topk.sketch.total == 16_000
    assert all(topk.sketch.estimate(item) >= count for item, count in exact.items())
    top_items = [item for item, _ in topk.top()]
    assert len(top_items) == 10
    assert exact.most_common(1)[0][0] in top_items
