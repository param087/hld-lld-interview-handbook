import math
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ConflictError, NotFoundError, ValidationError
from hld.inverted_index import Analyzer, InvertedIndex, Posting, intersect, light_stem, union

DOCS = {
    10: "Object storage keeps blobs in buckets",
    20: "A file system splits files into chunks and replicates each chunk",
    30: "Erasure coding stores a file as data and parity chunks",
}


def build(docs: dict[int, str] = DOCS) -> InvertedIndex:
    index = InvertedIndex()
    for doc_id, text in docs.items():
        index.add(doc_id, text)
    return index


@pytest.mark.parametrize(
    ("word", "stem"),
    [
        ("index", "index"),
        ("indexes", "index"),
        ("indexed", "index"),
        ("indexing", "index"),
        ("store", "stor"),
        ("stores", "stor"),
        ("storing", "stor"),
        ("classes", "class"),
        ("class", "class"),
        ("keys", "key"),
        ("bus", "bus"),
        ("string", "string"),
        ("time", "time"),
    ],
)
def test_light_stem_conflates_inflections_and_leaves_short_words(word: str, stem: str) -> None:
    assert light_stem(word) == stem


def test_analyzer_lowercases_splits_drops_stop_words_and_stems() -> None:
    analyzer = Analyzer()
    assert analyzer.terms("The Indexes, indexed by Lucene-style analyzers!") == [
        "index",
        "index",
        "lucen",  # the trailing-e rule, as Porter would do it
        "styl",
        "analyzer",
    ]
    assert analyzer.terms("the a of") == []
    plain = Analyzer(stop_words=frozenset(), stem=False)
    assert plain.terms("The Indexes") == ["the", "indexes"]


def test_postings_are_sorted_by_doc_id_and_count_term_frequency() -> None:
    index = build()
    assert len(index) == 3
    assert index.postings("Chunks") == [Posting(20, 2), Posting(30, 1)]
    assert index.postings("unknown") == []
    assert index.postings("the") == []  # a stop word has no postings
    assert index.document_frequency("file") == 2
    assert index.document_length(10) == 5
    late = InvertedIndex()
    late.add(30, "chunk")
    late.add(10, "chunk chunk")
    late.add(20, "chunk")
    assert late.postings("chunk") == [Posting(10, 2), Posting(20, 1), Posting(30, 1)]


def test_intersect_and_union_are_merges_of_sorted_lists() -> None:
    assert intersect([1, 3, 5, 7], [3, 4, 5], [3, 5]) == [3, 5]
    assert intersect([1, 2, 3], [4, 5]) == []
    assert intersect([2, 4], [1, 2, 3, 4, 5]) == [2, 4]
    assert intersect([1, 2]) == [1, 2]
    assert intersect() == []
    assert union([1, 3], [2, 3, 4], [4, 9]) == [1, 2, 3, 4, 9]
    assert union([], [7]) == [7]
    assert union() == []


def test_and_or_queries_go_through_the_same_analyzer() -> None:
    index = build()
    assert index.match("STORAGE chunk") == []  # AND: no document has both
    assert index.match("storage chunk", mode="or") == [10, 20, 30]
    assert index.match("Files Chunks", mode="and") == [20, 30]
    assert index.match("the of") == []
    assert index.match("never indexed") == []


def test_tfidf_scores_match_the_formula_and_rank_rare_dense_terms_first() -> None:
    index = build()
    n = len(index)
    idf_chunk = math.log(n / 2)
    idf_storage = math.log(n / 1)
    hits = index.search("storage chunks", mode="or")
    assert [hit.doc_id for hit in hits] == [10, 20, 30]
    assert hits[0].score == pytest.approx(1 / index.document_length(10) * idf_storage)
    assert hits[1].score == pytest.approx(2 / index.document_length(20) * idf_chunk)
    assert hits[2].score == pytest.approx(1 / index.document_length(30) * idf_chunk)
    assert index.search("storage chunks", mode="and") == []
    assert index.search("storage chunks", mode="or", limit=1) == hits[:1]
    # a term in every document has idf log(1) = 0 and cannot separate the matches
    assert index.idf("file") == pytest.approx(math.log(3 / 2))
    everywhere = build({1: "data data", 2: "data", 3: "data"})
    assert everywhere.idf("data") == 0.0
    assert all(hit.score == 0.0 for hit in everywhere.search("data"))
    assert [hit.doc_id for hit in everywhere.search("data")] == [1, 2, 3]  # ties by id


def test_validation_conflict_and_not_found_errors() -> None:
    index = build()
    with pytest.raises(ConflictError):
        index.add(10, "again")
    with pytest.raises(ValidationError):
        index.match("chunk", mode="xor")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        index.search("chunk", limit=0)
    with pytest.raises(ValidationError):
        index.postings("two words")
    with pytest.raises(NotFoundError):
        index.idf("missing")
    with pytest.raises(NotFoundError):
        index.document_length(99)


def test_concurrent_adds_keep_postings_and_counts_consistent() -> None:
    index = InvertedIndex()
    workers, per_worker = 8, 50

    def add_batch(worker: int) -> None:
        for i in range(per_worker):
            doc_id = worker * per_worker + i
            index.add(doc_id, f"shared term plus unique token w{worker}d{i}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(add_batch, range(workers)))

    total = workers * per_worker
    assert len(index) == total
    shared = index.postings("shared")
    assert [p.doc_id for p in shared] == list(range(total))
    assert all(p.term_freq == 1 for p in shared)
    assert index.idf("shared") == 0.0
    assert index.match("w3d7 unique") == [3 * per_worker + 7]
    (hit,) = index.search("w0d0 shared", mode="and")
    assert hit.doc_id == 0
    assert hit.score == pytest.approx(1 / index.document_length(0) * math.log(total))
