"""Iterator: the walk is separate from the tree, lazy, composable, and the aggregate stays re-iterable."""

from collections.abc import Iterator
from itertools import islice

import pytest

from common import ValidationError
from patterns.iterator import (
    DepthFirstIterator,
    Directory,
    File,
    ListSource,
    Node,
    Page,
    files_only,
    paginate,
    sample_tree,
    walk_breadth_first,
    walk_depth_first,
)


def names(nodes: Iterator[Node]) -> list[str]:
    return [node.name for node in nodes]


def test_depth_first_is_pre_order_left_to_right_in_both_forms() -> None:
    root = sample_tree()
    expected = ["docs", "intro.md", "api.md", "src", "main.py", "util.py", "README"]
    assert names(DepthFirstIterator(root)) == expected
    assert names(walk_depth_first(root)) == expected
    assert names(walk_depth_first(Directory("empty"))) == []
    assert names(DepthFirstIterator(Directory("empty"))) == []


def test_breadth_first_goes_level_by_level() -> None:
    root = sample_tree()
    assert names(walk_breadth_first(root)) == [
        "docs", "src", "README", "intro.md", "api.md", "main.py", "util.py"
    ]
    deep = Directory("a")
    deep.add(Directory("b")).add(Directory("c")).add(File("d", 1))
    assert names(walk_breadth_first(deep)) == ["b", "c", "d"]
    assert names(walk_depth_first(deep)) == ["b", "c", "d"]


def test_the_aggregate_is_re_iterable_but_the_iterator_is_one_shot() -> None:
    root = sample_tree()
    assert names(iter(root)) == ["docs", "src", "README"]
    assert names(iter(root)) == ["docs", "src", "README"]  # a fresh iterator each time
    walker = DepthFirstIterator(root)
    assert iter(walker) is walker
    assert len(list(walker)) == 7
    assert list(walker) == []
    with pytest.raises(StopIteration):
        next(walker)


def test_generators_do_no_work_until_pulled_and_stop_when_the_caller_stops() -> None:
    visited: list[str] = []

    def spy(directory: Directory) -> Iterator[Node]:
        for node in walk_depth_first(directory):
            visited.append(node.name)
            yield node

    root = sample_tree()
    lazy = files_only(spy(root))
    assert visited == []
    first_two = list(islice(lazy, 2))
    assert [node.name for node in first_two] == ["intro.md", "api.md"]
    assert visited == ["docs", "intro.md", "api.md"]  # nothing beyond what was needed
    assert sum(node.size for node in files_only(walk_depth_first(root))) == 5600


def test_removing_a_child_during_a_walk_neither_raises_nor_skips_a_neighbour() -> None:
    root = Directory("root")
    for name in ("a", "b", "c"):
        root.add(File(name, 1))
    seen: list[str] = []
    for node in root:
        seen.append(node.name)
        if node.name == "a":
            root.remove("b")  # a plain list iterator would now skip "c"
    assert seen == ["a", "b", "c"]
    assert names(iter(root)) == ["a", "c"]


def test_a_directory_rejects_duplicate_names_and_add_returns_the_child() -> None:
    root = Directory("root")
    docs = root.add(Directory("docs"))
    assert docs is root.children[0]
    with pytest.raises(ValidationError):
        root.add(File("docs", 1))
    root.remove("missing")  # removing an absent name is a no-op
    assert names(iter(root)) == ["docs"]


def test_paginate_threads_the_cursor_and_fetches_only_what_is_consumed() -> None:
    source = ListSource(["alice", "bob", "carol", "dave", "erin"])
    assert list(islice(paginate(source, limit=2), 3)) == ["alice", "bob", "carol"]
    assert source.calls == [None, "2"]  # the third page was never requested
    source.calls.clear()
    assert list(paginate(source, limit=2)) == ["alice", "bob", "carol", "dave", "erin"]
    assert source.calls == [None, "2", "4"]
    assert list(paginate(ListSource([]), limit=2)) == []
    assert list(paginate(ListSource(["only"]), limit=5)) == ["only"]
    with pytest.raises(ValidationError):
        next(paginate(source, limit=0))


def test_any_object_with_fetch_is_a_page_source() -> None:
    class Pages:
        def __init__(self) -> None:
            self.pages = {None: Page(("x", "y"), "p2"), "p2": Page(("z",), None)}

        def fetch(self, cursor: str | None, limit: int) -> Page:
            return self.pages[cursor]

    assert list(paginate(Pages(), limit=2)) == ["x", "y", "z"]
