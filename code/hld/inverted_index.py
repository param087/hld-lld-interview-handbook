"""A toy inverted index: analyzer, postings lists, boolean AND/OR queries and TF-IDF ranking.

What the module demonstrates, in the order an interviewer asks about it:

* ``Analyzer`` turns text into terms (lowercase, split, drop stop words, light stemming). The
  same analyzer must run at index time and at query time, or queries silently miss documents.
* ``InvertedIndex`` maps every term to a postings list sorted by document id; ``intersect`` and
  ``union`` are the linear merges behind boolean AND and OR.
* ``InvertedIndex.search`` ranks the matches by TF-IDF: a term that is frequent in a document
  and rare in the corpus carries the most weight, and a term in every document carries none.

The search-engine case study reuses this module, so the public surface is deliberately small:
``Analyzer``, ``light_stem``, ``Posting``, ``Hit``, ``intersect``, ``union``, ``InvertedIndex``.
"""

from __future__ import annotations

import bisect
import heapq
import math
import re
import threading
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from common import ConflictError, NotFoundError, ValidationError

# --8<-- [start:analyzer]
DEFAULT_STOP_WORDS: frozenset[str] = frozenset(
    "a an and are as at be by for from has in is it its of on or that the this to was with".split()
)
_WORD = re.compile(r"[a-z0-9]+")
# (suffix, letters that must remain): "indexes" -> "indexe" -> "index"; "string" is left alone.
_SUFFIX_RULES = (("s", 3), ("ing", 4), ("ed", 4), ("e", 4))


def light_stem(word: str) -> str:
    """Strip a plural, a verb suffix and a trailing ``e`` when enough of the word remains.

    ``index``, ``indexes``, ``indexed`` and ``indexing`` all become ``index``; ``store``,
    ``stores`` and ``storing`` all become ``stor``. Like Porter's stemmer it trades a few
    ugly stems for recall; unlike Porter it fits in six lines and makes no claim to cover
    English.
    """
    stem = word
    for suffix, keep in _SUFFIX_RULES:
        if stem.endswith(suffix) and not stem.endswith("ss") and len(stem) - len(suffix) >= keep:
            stem = stem[: -len(suffix)]
    return stem


@dataclass(frozen=True, slots=True)
class Analyzer:
    """Text to terms, applied identically at index time and at query time.

    Lucene's three stages in miniature: a character filter (lowercase), a tokenizer (split on
    anything that is not a letter or digit) and token filters (drop stop words, stem).
    Production analyzers add Snowball stemming, synonyms, ASCII folding and n-grams; the rule
    that matters in the interview is the same: the index and the query must agree.
    """

    stop_words: frozenset[str] = DEFAULT_STOP_WORDS
    stem: bool = True

    def terms(self, text: str) -> list[str]:
        words = (w for w in _WORD.findall(text.lower()) if w not in self.stop_words)
        return [light_stem(w) if self.stem else w for w in words]


# --8<-- [end:analyzer]


# --8<-- [start:postings]
@dataclass(frozen=True, slots=True)
class Posting:
    """One document's entry in a term's postings list: which document, how many times."""

    doc_id: int
    term_freq: int


def intersect(*lists: Sequence[int]) -> list[int]:
    """Ids present in every sorted list: boolean AND as a linear merge, shortest lists first.

    Each merge walks two lists with two cursors, so the cost is the sum of their lengths,
    and starting from the shortest list keeps the running result as small as possible.
    """
    if not lists:
        return []
    ordered = sorted(lists, key=len)
    result = list(ordered[0])
    for other in ordered[1:]:
        merged: list[int] = []
        i = j = 0
        while i < len(result) and j < len(other):
            if result[i] == other[j]:
                merged.append(result[i])
                i += 1
                j += 1
            elif result[i] < other[j]:
                i += 1
            else:
                j += 1
        result = merged
        if not result:
            break
    return result


def union(*lists: Sequence[int]) -> list[int]:
    """Ids present in any sorted list: boolean OR as a k-way merge with duplicates collapsed."""
    result: list[int] = []
    for doc_id in heapq.merge(*lists):
        if not result or result[-1] != doc_id:
            result.append(doc_id)
    return result


# --8<-- [end:postings]


# --8<-- [start:index]
@dataclass(frozen=True, slots=True)
class Hit:
    """A ranked search result."""

    doc_id: int
    score: float


class InvertedIndex:
    """Term to postings (sorted by document id), plus the document lengths TF-IDF needs.

    ``_lock`` guards ``_postings`` and ``_doc_lengths`` on both the write and the read path.
    A real engine writes immutable segments and swaps in a new reader so that searches never
    wait for indexing; this toy takes the lock on both paths for brevity.
    """

    def __init__(self, analyzer: Analyzer | None = None) -> None:
        self._analyzer = analyzer or Analyzer()
        self._postings: dict[str, list[Posting]] = {}
        self._doc_lengths: dict[int, int] = {}
        self._lock = threading.Lock()

    @property
    def analyzer(self) -> Analyzer:
        return self._analyzer

    def __len__(self) -> int:
        """Number of indexed documents."""
        with self._lock:
            return len(self._doc_lengths)

    @property
    def vocabulary_size(self) -> int:
        with self._lock:
            return len(self._postings)

    def add(self, doc_id: int, text: str) -> list[str]:
        """Index one document and return its terms; an id already present is a conflict."""
        terms = self._analyzer.terms(text)
        counts = Counter(terms)
        with self._lock:
            if doc_id in self._doc_lengths:
                raise ConflictError(f"document {doc_id} is already indexed; delete and re-add instead")
            self._doc_lengths[doc_id] = len(terms)
            for term, term_freq in counts.items():
                postings = self._postings.setdefault(term, [])
                bisect.insort(postings, Posting(doc_id, term_freq), key=lambda p: p.doc_id)
        return terms

    def document_length(self, doc_id: int) -> int:
        with self._lock:
            if doc_id not in self._doc_lengths:
                raise NotFoundError(f"document {doc_id} is not indexed")
            return self._doc_lengths[doc_id]

    def postings(self, word: str) -> list[Posting]:
        """The postings list of one word, analyzed first so ``Chunks`` finds ``chunk``."""
        terms = self._analyzer.terms(word)
        if len(terms) > 1:
            raise ValidationError(f"{word!r} analyzes to {len(terms)} terms; pass one word")
        if not terms:
            return []
        with self._lock:
            return list(self._postings.get(terms[0], []))

    def document_frequency(self, word: str) -> int:
        return len(self.postings(word))

    def idf(self, word: str) -> float:
        """Inverse document frequency, ``log(N / df)``: 0 for a term in every document."""
        df = self.document_frequency(word)
        if df == 0:
            raise NotFoundError(f"{word!r} is not in the index")
        return math.log(len(self) / df)

    def match(self, query: str, mode: Literal["and", "or"] = "and") -> list[int]:
        """Boolean retrieval: ids of the documents holding every term (AND) or any term (OR)."""
        if mode not in ("and", "or"):
            raise ValidationError(f"mode must be 'and' or 'or', got {mode!r}")
        terms = sorted(set(self._analyzer.terms(query)))
        if not terms:
            return []
        with self._lock:
            id_lists = [[p.doc_id for p in self._postings.get(t, [])] for t in terms]
        return intersect(*id_lists) if mode == "and" else union(*id_lists)

    def search(self, query: str, mode: Literal["and", "or"] = "or", limit: int = 10) -> list[Hit]:
        """The top ``limit`` matches ranked by TF-IDF.

        ``score(d) = sum over query terms t of tf(t, d) / len(d) * log(N / df(t))``: raw term
        frequency normalised by document length, times the rarity of the term in the corpus.
        Ties are broken by document id so the ranking is deterministic.
        """
        if limit <= 0:
            raise ValidationError("limit must be positive")
        candidates = self.match(query, mode)
        if not candidates:
            return []
        scores = dict.fromkeys(candidates, 0.0)
        with self._lock:
            total_docs = len(self._doc_lengths)
            for term in set(self._analyzer.terms(query)):
                postings = self._postings.get(term, [])
                if not postings:
                    continue
                idf = math.log(total_docs / len(postings))
                for posting in postings:
                    if posting.doc_id in scores:
                        weight = posting.term_freq / self._doc_lengths[posting.doc_id]
                        scores[posting.doc_id] += weight * idf
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        return [Hit(doc_id, score) for doc_id, score in ranked[:limit]]


# --8<-- [end:index]


CORPUS = {
    1: "Object storage keeps immutable blobs in buckets behind a flat namespace.",
    2: "A distributed file system splits files into chunks and replicates each chunk three times.",
    3: "Search engines build an inverted index: every term maps to the documents that contain it.",
    4: "Time-series databases compress timestamps and values, then downsample old points.",
    5: "Graph databases store adjacency lists so a traversal follows edges without joins.",
    6: "Column stores scan one column at a time, so analytics over billions of rows stays fast.",
    7: "Erasure coding stores a file as data and parity chunks: less storage than replication.",
}


def main() -> None:
    index = InvertedIndex()
    for doc_id, text in CORPUS.items():
        index.add(doc_id, text)
    print(f"indexed {len(index)} documents, {index.vocabulary_size} distinct terms")
    print(f"analyze {CORPUS[3]!r}")
    print(f"  -> {index.analyzer.terms(CORPUS[3])}")

    postings = ", ".join(f"doc {p.doc_id} (tf={p.term_freq})" for p in index.postings("Chunks"))
    print(f"postings('Chunks') -> term 'chunk': {postings}")
    idfs = "  ".join(
        f"{term}=log({len(index)}/{index.document_frequency(term)})={index.idf(term):.2f}"
        for term in ("chunk", "storage", "index", "file")
    )
    print(f"idf: {idfs}")

    query = "storage chunks"
    print(f"AND {query!r} -> {index.match(query, 'and')}")
    print(f"OR  {query!r} -> {index.match(query, 'or')}")
    print(f"ranked OR {query!r}:")
    for hit in index.search(query, mode="or"):
        parts = [
            f"{term} x{p.term_freq}"
            for term in index.analyzer.terms(query)
            for p in index.postings(term)
            if p.doc_id == hit.doc_id
        ]
        length = index.document_length(hit.doc_id)
        print(f"  doc {hit.doc_id}  score {hit.score:.3f}  ({', '.join(parts)} in {length} terms)")
    hits = ", ".join(f"doc {h.doc_id} ({h.score:.3f})" for h in index.search("replicated chunks", "and"))
    print(f"ranked AND 'replicated chunks' -> {hits}")
    print(f"match('the') -> {index.match('the')} (every query term was a stop word)")
    try:
        index.add(3, "a duplicate")
    except ConflictError as exc:
        print(f"add doc 3 again -> ConflictError: {exc}")


if __name__ == "__main__":
    main()
