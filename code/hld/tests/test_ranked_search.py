"""Tests for BM25 ranking, segment refresh and merge, and the scatter-gather top-K merge."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, NotFoundError, ValidationError
from hld.inverted_index import Analyzer
from hld.ranked_search import (
    BM25,
    Document,
    Ranking,
    ScatterGatherSearcher,
    SearchIndex,
    Segment,
)

T0 = 1_700_000_000.0
DOCS = (
    Document(101, "An inverted index maps every term to the documents that contain it.", 0.9, T0),
    Document(102, "Search engines build an inverted index with MapReduce over a corpus.", 0.6, T0),
    Document(103, "BM25 ranks documents by saturated term frequency and document length.", 0.8, T0),
    Document(104, "PageRank scores a page by the pages that link to it.", 0.95, T0),
    Document(105, "Cheap index cheap index cheap index cheap index cheap index cheap index.", 0.05, T0),
    Document(106, "Sharding a search index by document lets every shard serve any query.", 0.5, T0),
)


def _shard(name: str = "t", **kwargs: object) -> SearchIndex:
    index = SearchIndex(name, clock=FakeClock(start=T0), **kwargs)  # type: ignore[arg-type]
    for doc in DOCS:
        index.add(doc)
    index.refresh()
    return index


def test_bm25_saturates_term_frequency_and_penalises_length() -> None:
    bm25 = BM25()
    one = bm25.term_score(1, 10, 10.0, 2, 100)
    ten = bm25.term_score(10, 10, 10.0, 2, 100)
    hundred = bm25.term_score(100, 10, 10.0, 2, 100)
    assert one < ten < hundred
    assert ten / one < 10  # saturation: ten occurrences are not worth ten times one
    assert hundred - ten < ten - one  # each extra occurrence is worth less than the last
    # A longer document with the same term frequency scores lower.
    assert bm25.term_score(3, 40, 10.0, 2, 100) < bm25.term_score(3, 10, 10.0, 2, 100)
    # A term in every document carries almost no weight.
    assert bm25.idf(100, 100) < 0.02 < bm25.idf(2, 100)
    assert bm25.term_score(0, 10, 10.0, 2, 100) == 0.0


def test_saturation_bounds_what_keyword_stuffing_buys() -> None:
    index = _shard()
    hits = {hit.doc_id: hit for hit in index.search("index", limit=10)}
    # Doc 105 repeats "index" six times, doc 101 uses it once in a shorter document.
    assert 1.0 < hits[105].text_score / hits[101].text_score < 2.0
    # And the quality signal is what actually settles the order.
    assert hits[101].score > hits[105].score


def test_documents_are_invisible_until_refresh_and_merge_drops_tombstones() -> None:
    index = SearchIndex("t", clock=FakeClock(start=T0))
    for doc in DOCS:
        index.add(doc)
    assert index.doc_count == 0
    assert index.search("index") == []

    index.refresh()
    assert index.doc_count == len(DOCS)
    assert index.segment_count == 1
    assert index.refresh() is None  # empty buffer, no new segment

    index.add(Document(107, "Another inverted index arrives later.", 0.1, T0))
    index.refresh()
    assert index.segment_count == 2
    assert 107 in [hit.doc_id for hit in index.search("index", limit=10)]

    index.delete(105)
    assert 105 not in [hit.doc_id for hit in index.search("index", limit=10)]
    assert index.merge() == len(DOCS)  # 6 original + 1 new - 1 tombstoned
    assert index.segment_count == 1
    with pytest.raises(NotFoundError):
        index.document(105)


@pytest.mark.parametrize(
    ("weights", "expected_first"),
    [
        (Ranking(page_rank_weight=0.0), 105),  # pure BM25 rewards the keyword-stuffed page
        (Ranking(page_rank_weight=8.0), 101),  # a query-independent quality score flips it
    ],
)
def test_static_signals_reorder_the_text_ranking(weights: Ranking, expected_first: int) -> None:
    index = _shard(ranking=weights)
    assert index.search("index", limit=3)[0].doc_id == expected_first


def test_freshness_lifts_a_recent_document() -> None:
    clock = FakeClock(start=T0)
    index = SearchIndex("t", ranking=Ranking(freshness_weight=5.0, half_life_s=3600), clock=clock)
    index.add(Document(1, "an index of old news", 0.5, T0 - 10 * 3600))
    index.add(Document(2, "an index of news", 0.0, T0))
    index.refresh()
    assert [hit.doc_id for hit in index.search("index news", limit=2)] == [2, 1]


def test_boolean_and_narrows_the_candidate_set() -> None:
    index = _shard()
    any_of = {hit.doc_id for hit in index.search("index pagerank", limit=10)}
    all_of = {hit.doc_id for hit in index.search("index sharding", limit=10, require_all=True)}
    assert 104 in any_of  # matched by "pagerank" alone
    assert all_of == {106}  # the only document holding both terms
    assert index.search("the and of") == []  # every query term is a stop word


def test_scatter_gather_merges_local_top_k_into_a_global_top_k() -> None:
    shards = [SearchIndex(f"s{i}", clock=FakeClock(start=T0)) for i in range(3)]
    searcher = ScatterGatherSearcher(shards)
    for doc in DOCS:
        searcher.add(doc)
    assert searcher.refresh() == 3
    assert searcher.doc_count == len(DOCS)

    hits = searcher.search("index", limit=4)
    assert len(hits) == 4
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)
    # Every returned document really does contain the term.
    assert set(h.doc_id for h in hits) <= {101, 102, 105, 106}
    searcher.delete(101)
    assert 101 not in [h.doc_id for h in searcher.search("index", limit=6)]


def test_recent_walks_newest_first_and_never_scores() -> None:
    index = SearchIndex("t", clock=FakeClock(start=T0))
    for doc_id in (10, 11, 12):
        index.add(Document(doc_id, "breaking news about an index", 0.0, T0))
    index.refresh()
    index.add(Document(20, "later news about an index", 0.0, T0))
    index.refresh()
    hits = index.recent("news index", limit=3)
    assert [h.doc_id for h in hits] == [20, 12, 11]
    assert all(h.score == 0.0 for h in hits)
    assert index.recent("the") == []


def test_validation_rejects_empty_documents_and_bad_limits() -> None:
    index = SearchIndex("t", clock=FakeClock(start=T0))
    with pytest.raises(ValidationError):
        index.add(Document(1, "   ", 0.0, T0))
    with pytest.raises(ValidationError):
        index.search("index", limit=0)
    with pytest.raises(ValidationError):
        index.recent("index", limit=-1)
    with pytest.raises(ValidationError):
        ScatterGatherSearcher([])


def test_segment_is_a_pure_function_of_its_documents() -> None:
    analyzer = Analyzer()
    left = Segment("a", DOCS, analyzer)
    right = Segment("b", tuple(reversed(DOCS)), analyzer)
    assert len(left) == len(right) == len(DOCS)
    assert left.total_length == right.total_length
    assert left.doc_frequency("index") == right.doc_frequency("index")
    assert [d.doc_id for d in left.documents] == [d.doc_id for d in right.documents]
    assert left.matching(["index"], require_all=True) == [101, 102, 105, 106]


def test_concurrent_writers_and_readers_see_a_consistent_index() -> None:
    index = SearchIndex("t", clock=FakeClock(start=T0))
    writers, per_writer = 8, 40

    def crawl(worker: int) -> int:
        for i in range(per_writer):
            index.add(Document(worker * 1000 + i, f"inverted index shard {worker} page {i}", 0.1, T0))
        index.refresh()
        return len(index.search("index", limit=5))

    with ThreadPoolExecutor(max_workers=writers) as pool:
        counts = list(pool.map(crawl, range(writers)))

    index.refresh()
    assert index.doc_count == writers * per_writer
    assert all(count <= 5 for count in counts)
    assert len(index.search("index", limit=10)) == 10
