"""BM25 ranking over immutable segments, with near-real-time refresh and scatter-gather merge.

The serving half of the search-engine case study. The analyzer and the postings-list
primitives come from :mod:`hld.inverted_index`; this module adds the four things a real
serving tier needs on top of a term-to-postings map:

* ``BM25`` - term frequency with saturation and length normalisation, the scorer that
  replaced raw TF-IDF everywhere.
* ``Segment`` - an immutable inverted index over one batch of documents. Never updated: a
  delete is a tombstone, an edit is a delete plus a new document in a newer segment.
* ``SearchIndex`` - one serving shard: sealed segments plus a writable buffer, made visible
  by ``refresh()`` and compacted by ``merge()``. ``recent()`` is the Earlybird path, which
  walks reverse-chronological postings and never scores anything.
* ``ScatterGatherSearcher`` - documents partitioned across shards, one query broadcast to
  all of them, local top-K lists merged into a global top K.
"""

from __future__ import annotations

import heapq
import itertools
import math
import threading
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from common import Clock, NotFoundError, SystemClock, ValidationError
from hld.inverted_index import Analyzer, Posting, intersect


# --8<-- [start:bm25]
@dataclass(frozen=True, slots=True)
class BM25:
    """Okapi BM25: saturating term frequency, normalised by document length.

    Raw TF-IDF has one fatal flaw for the web: term frequency grows without limit, so a page
    repeating "cheap flights" two hundred times outranks the airline. BM25 saturates term
    frequency towards ``k1 + 1`` and divides by how much longer the document is than the
    corpus average, so the two-hundredth repeat is worth almost nothing and a long page gets
    no free credit for containing more words.
    """

    k1: float = 1.2
    b: float = 0.75

    def idf(self, doc_freq: int, doc_count: int) -> float:
        """``log(1 + (N - df + 0.5) / (df + 0.5))`` - always positive, unlike the textbook form."""
        if doc_freq <= 0 or doc_count <= 0:
            return 0.0
        return math.log(1.0 + (doc_count - doc_freq + 0.5) / (doc_freq + 0.5))

    def term_score(
        self,
        term_freq: int,
        doc_length: int,
        avg_length: float,
        doc_freq: int,
        doc_count: int,
    ) -> float:
        if term_freq <= 0:
            return 0.0
        norm = 1.0 - self.b + self.b * (doc_length / avg_length if avg_length > 0 else 1.0)
        saturated = term_freq * (self.k1 + 1.0) / (term_freq + self.k1 * norm)
        return self.idf(doc_freq, doc_count) * saturated


@dataclass(frozen=True, slots=True)
class Ranking:
    """How the text score, a query-independent quality score and freshness combine.

    Production engines learn this blend from click data; the shape is what matters in the
    room. Quality (PageRank) multiplies, because a trustworthy page should lift every query
    it matches. Freshness adds, because recency must be able to surface a page that barely
    matches when the query is about something that happened an hour ago.
    """

    page_rank_weight: float = 1.0
    freshness_weight: float = 0.0
    half_life_s: float = 86_400.0

    def combine(self, text_score: float, page_rank: float, age_s: float) -> float:
        quality = 1.0 + self.page_rank_weight * page_rank
        if self.freshness_weight <= 0.0:
            return text_score * quality
        decay = 0.5 ** (max(0.0, age_s) / self.half_life_s)
        return text_score * quality + self.freshness_weight * decay


# --8<-- [end:bm25]


# --8<-- [start:segment]
@dataclass(frozen=True, slots=True)
class Document:
    """A crawled document plus the two query-independent signals the ranker uses."""

    doc_id: int
    text: str
    page_rank: float = 0.0
    created_at: float = 0.0


@dataclass(frozen=True, slots=True)
class Result:
    """One row of a result page, carrying the text score separately so ranking is debuggable."""

    doc_id: int
    score: float
    text_score: float
    segment: str

    @property
    def sort_key(self) -> tuple[float, int]:
        return (-self.score, self.doc_id)


class Segment:
    """An immutable inverted index over one batch of documents.

    Immutability is the whole design of Lucene and of Earlybird. A segment is written once
    and never updated, so readers need no lock, a merge is a pure function of its inputs, and
    a crash can only lose the batch that was still being written. Postings are stored sorted
    by document id, which makes a boolean AND a linear merge and - because ids are
    time-sortable - makes "newest first" a walk from the end.
    """

    def __init__(self, name: str, docs: Sequence[Document], analyzer: Analyzer) -> None:
        postings: dict[str, list[Posting]] = {}
        lengths: dict[int, int] = {}
        meta: dict[int, Document] = {}
        for doc in sorted(docs, key=lambda d: d.doc_id):
            terms = analyzer.terms(doc.text)
            lengths[doc.doc_id] = len(terms)
            meta[doc.doc_id] = doc
            for term, term_freq in Counter(terms).items():
                postings.setdefault(term, []).append(Posting(doc.doc_id, term_freq))
        self.name = name
        self._postings = postings
        self._lengths = lengths
        self._meta = meta
        self._total_length = sum(lengths.values())

    def __len__(self) -> int:
        return len(self._lengths)

    @property
    def total_length(self) -> int:
        return self._total_length

    @property
    def documents(self) -> list[Document]:
        return [self._meta[doc_id] for doc_id in sorted(self._meta)]

    def postings(self, term: str) -> list[Posting]:
        return self._postings.get(term, [])

    def doc_frequency(self, term: str) -> int:
        return len(self._postings.get(term, ()))

    def doc_length(self, doc_id: int) -> int:
        return self._lengths[doc_id]

    def document(self, doc_id: int) -> Document:
        return self._meta[doc_id]

    def matching(self, terms: Sequence[str], require_all: bool) -> list[int]:
        """Document ids matching the terms, ascending: boolean AND or OR over postings."""
        id_lists = [[p.doc_id for p in self.postings(term)] for term in terms]
        if require_all:
            return intersect(*id_lists) if all(id_lists) else []
        return sorted({doc_id for ids in id_lists for doc_id in ids})


# --8<-- [end:segment]


# --8<-- [start:shard]
class SearchIndex:
    """One serving shard: sealed segments, a writable buffer, tombstones and a scorer.

    ``_lock`` guards ``_buffer``, ``_segments`` and ``_deleted``. A document lands in the
    buffer and becomes searchable only at ``refresh()``, which seals the buffer into a new
    segment. That interval is the "near" in near-real-time search: one second is a product
    decision (how stale may results be) paid for in segment count, because every open
    segment is another postings list every query has to walk.
    """

    def __init__(
        self,
        name: str = "shard",
        analyzer: Analyzer | None = None,
        bm25: BM25 | None = None,
        ranking: Ranking | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.name = name
        self._analyzer = analyzer or Analyzer()
        self._bm25 = bm25 or BM25()
        self._ranking = ranking or Ranking()
        self._clock = clock or SystemClock()
        self._segments: list[Segment] = []
        self._buffer: list[Document] = []
        self._deleted: set[int] = set()
        self._generation = 0
        self._lock = threading.Lock()

    @property
    def segment_count(self) -> int:
        with self._lock:
            return len(self._segments)

    @property
    def doc_count(self) -> int:
        with self._lock:
            return sum(len(s) for s in self._segments) - len(self._deleted)

    def add(self, doc: Document) -> None:
        """Buffer a document. It is invisible to queries until the next ``refresh()``."""
        if not doc.text.strip():
            raise ValidationError(f"document {doc.doc_id} has no indexable text")
        with self._lock:
            self._buffer.append(doc)

    def delete(self, doc_id: int) -> None:
        """Tombstone, never an in-place edit: segments are immutable."""
        with self._lock:
            self._deleted.add(doc_id)

    def refresh(self) -> Segment | None:
        """Seal the buffer into a new immutable segment and make it visible."""
        with self._lock:
            if not self._buffer:
                return None
            self._generation += 1
            segment = Segment(f"{self.name}-s{self._generation}", self._buffer, self._analyzer)
            self._segments.append(segment)
            self._buffer = []
            return segment

    def merge(self) -> int:
        """Compact every segment into one, dropping tombstoned documents. Returns docs kept.

        Merging is what keeps query cost bounded: N small segments means N postings walks per
        term. It also physically removes deleted documents, which is the only time a delete
        stops costing anything.
        """
        with self._lock:
            live = [
                doc
                for segment in self._segments
                for doc in segment.documents
                if doc.doc_id not in self._deleted
            ]
            self._generation += 1
            self._segments = [Segment(f"{self.name}-m{self._generation}", live, self._analyzer)] if live else []
            self._deleted.clear()
            return len(live)

    def _snapshot(self) -> tuple[list[Segment], set[int]]:
        with self._lock:
            return list(self._segments), set(self._deleted)

    def search(self, query: str, limit: int = 10, require_all: bool = False) -> list[Result]:
        """Score every matching document with BM25, then blend quality and freshness.

        Corpus statistics (``N``, average document length, document frequency per term) are
        summed across this shard's segments. Across *shards* they differ, which is why a
        distributed engine either broadcasts global term statistics before scoring or accepts
        that shards score slightly differently - tolerable once each shard holds millions of
        documents.
        """
        if limit <= 0:
            raise ValidationError("limit must be positive")
        terms = sorted(set(self._analyzer.terms(query)))
        segments, deleted = self._snapshot()
        if not terms or not segments:
            return []
        doc_count = sum(len(s) for s in segments) - len(deleted)
        if doc_count <= 0:
            return []
        avg_length = sum(s.total_length for s in segments) / doc_count
        doc_freq = {term: sum(s.doc_frequency(term) for s in segments) for term in terms}
        now = self._clock.now()

        scored: list[Result] = []
        for segment in segments:
            candidates = set(segment.matching(terms, require_all))
            for doc_id in sorted(candidates - deleted):
                text_score = sum(
                    self._bm25.term_score(
                        posting.term_freq,
                        segment.doc_length(doc_id),
                        avg_length,
                        doc_freq[term],
                        doc_count,
                    )
                    for term in terms
                    for posting in segment.postings(term)
                    if posting.doc_id == doc_id
                )
                doc = segment.document(doc_id)
                final = self._ranking.combine(text_score, doc.page_rank, now - doc.created_at)
                scored.append(Result(doc_id, final, text_score, segment.name))
        scored.sort(key=lambda r: r.sort_key)
        return scored[:limit]

    def recent(self, query: str, limit: int = 10) -> list[Result]:
        """The Earlybird path: newest matching documents, with no scoring at all.

        Real-time search does not rank a corpus; it walks reverse-chronological postings and
        stops as soon as it has enough. That early exit is only legal because document ids
        are time-sortable, so "newest" is "highest id" and the newest segment is the last one.
        """
        if limit <= 0:
            raise ValidationError("limit must be positive")
        terms = sorted(set(self._analyzer.terms(query)))
        if not terms:
            return []
        segments, deleted = self._snapshot()
        out: list[Result] = []
        for segment in reversed(segments):
            for doc_id in reversed(segment.matching(terms, require_all=True)):
                if doc_id in deleted:
                    continue
                out.append(Result(doc_id, 0.0, 0.0, segment.name))
                if len(out) == limit:
                    return out
        return out

    def document(self, doc_id: int) -> Document:
        segments, deleted = self._snapshot()
        if doc_id not in deleted:
            for segment in reversed(segments):
                try:
                    return segment.document(doc_id)
                except KeyError:
                    continue
        raise NotFoundError(f"document {doc_id} is not in {self.name}")


# --8<-- [end:shard]


# --8<-- [start:scatter]
class ScatterGatherSearcher:
    """Documents partitioned across shards; every query is broadcast to every shard.

    Partitioning **by document** gives each shard a slice of the corpus and the complete
    postings for its own documents, so a query fans out and the local top-K lists merge into
    the global top K. Partitioning **by term** would send each query only to the shards
    owning its terms, but a two-term query then has to ship one shard's entire postings list
    across the network to intersect it with another's. That is why web search partitions by
    document and pays the fan-out.
    """

    def __init__(self, shards: Sequence[SearchIndex]) -> None:
        if not shards:
            raise ValidationError("a searcher needs at least one shard")
        self._shards = tuple(shards)

    @property
    def shard_count(self) -> int:
        return len(self._shards)

    @property
    def doc_count(self) -> int:
        return sum(shard.doc_count for shard in self._shards)

    def shard_for(self, doc_id: int) -> SearchIndex:
        return self._shards[doc_id % len(self._shards)]

    def add(self, doc: Document) -> None:
        self.shard_for(doc.doc_id).add(doc)

    def delete(self, doc_id: int) -> None:
        """Broadcast: the shard that owns the id is known, but a re-shard must not lose it."""
        for shard in self._shards:
            shard.delete(doc_id)

    def refresh(self) -> int:
        return sum(1 for shard in self._shards if shard.refresh() is not None)

    def search(self, query: str, limit: int = 10, require_all: bool = False) -> list[Result]:
        """Ask every shard for its local top ``limit``, then merge. Correct because the global
        top K cannot contain a document that is below rank K inside its own shard."""
        pages = [shard.search(query, limit, require_all) for shard in self._shards]
        merged = heapq.merge(*pages, key=lambda r: r.sort_key)
        return list(itertools.islice(merged, limit))


# --8<-- [end:scatter]


DEMO_START = 1_700_000_000.0
CORPUS: tuple[tuple[int, str, float], ...] = (
    (101, "An inverted index maps every term to the documents that contain it.", 0.9),
    (102, "Search engines build an inverted index with MapReduce over a crawled corpus.", 0.6),
    (103, "BM25 ranks documents by saturated term frequency and document length.", 0.8),
    (104, "PageRank scores a page by the pages that link to it.", 0.95),
    (105, "Cheap index cheap index cheap index cheap index cheap index cheap index.", 0.05),
    (106, "Sharding a search index by document lets every shard serve any query.", 0.5),
    (107, "A crawler fetches pages, respects robots rules and dedupes near-identical text.", 0.4),
    (108, "Real time search keeps recent documents in memory and walks them newest first.", 0.3),
)


def main() -> None:
    from common import FakeClock

    clock = FakeClock(start=DEMO_START)
    shard = SearchIndex("solo", clock=clock)
    for offset, (doc_id, text, page_rank) in enumerate(CORPUS):
        shard.add(Document(doc_id, text, page_rank, DEMO_START - 3600 * (len(CORPUS) - offset)))
    print(f"buffered {len(CORPUS)} documents; searchable before refresh: {shard.doc_count}")
    shard.refresh()
    print(f"after refresh: {shard.doc_count} documents in {shard.segment_count} segment")

    print("search 'index' (doc 101 uses the term once, doc 105 repeats it six times):")
    hits = {hit.doc_id: hit for hit in shard.search("index", limit=4)}
    for hit in shard.search("index", limit=4):
        print(f"  doc {hit.doc_id}  final {hit.score:.3f}  text {hit.text_score:.3f}  ({hit.segment})")
    ratio = hits[105].text_score / hits[101].text_score
    print(f"saturation caps stuffing at {ratio:.1f}x, not 6x; PageRank 0.9 vs 0.05 then puts 101 first")

    quality = SearchIndex("quality", ranking=Ranking(page_rank_weight=2.0), clock=clock)
    fresh = SearchIndex("fresh", ranking=Ranking(freshness_weight=1.5, half_life_s=7200), clock=clock)
    for target in (quality, fresh):
        for offset, (doc_id, text, page_rank) in enumerate(CORPUS):
            target.add(Document(doc_id, text, page_rank, DEMO_START - 3600 * (len(CORPUS) - offset)))
        target.refresh()
    print(f"with PageRank weight 2.0 -> {[h.doc_id for h in quality.search('index search', limit=3)]}")
    print(f"with freshness weight 1.5 -> {[h.doc_id for h in fresh.search('index search', limit=3)]}")

    searcher = ScatterGatherSearcher([SearchIndex(f"shard-{i}", clock=clock) for i in range(3)])
    for doc_id, text, page_rank in CORPUS:
        searcher.add(Document(doc_id, text, page_rank, DEMO_START))
    print(f"refreshed {searcher.refresh()} of {searcher.shard_count} shards, {searcher.doc_count} documents")
    merged = [h.doc_id for h in searcher.search("index", limit=4)]
    single = [h.doc_id for h in shard.search("index", limit=4)]
    print(f"scatter-gather top 4: {merged}   one shard: {single}")
    print("the orders differ because each shard scores with its own df and average length")

    shard.add(Document(999, "A late inverted index arrives after the last refresh.", 0.1, DEMO_START))
    print(f"new document visible before refresh: {999 in [h.doc_id for h in shard.search('index', limit=9)]}")
    shard.refresh()
    print(f"visible after refresh: {999 in [h.doc_id for h in shard.search('index', limit=9)]}, segments={shard.segment_count}")
    print(f"recent('index') newest first: {[h.doc_id for h in shard.recent('index', limit=3)]}")
    shard.delete(105)
    print(f"tombstoned doc 105 of {shard.doc_count + 1}; merge keeps {shard.merge()} in {shard.segment_count} segment")


if __name__ == "__main__":
    main()
