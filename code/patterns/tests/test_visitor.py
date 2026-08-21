"""Visitor: operations live in visitors, the element classes never change."""

from __future__ import annotations

import pytest

from common import ConflictError, ValidationError
from patterns.visitor import (
    Directory,
    File,
    Node,
    SearchVisitor,
    SizeVisitor,
    Visitor,
    build_tree,
    find,
    size_of,
)


def test_size_visitor_sums_files_recursively_and_an_empty_directory_is_zero() -> None:
    home = build_tree()
    sizes = SizeVisitor()
    assert home.accept(sizes) == 6_700
    by_name = {child.name: child.accept(sizes) for child in home.children}
    assert by_name == {"docs": 1_500, "src": 5_200, "empty": 0}
    assert File("one.bin", 7).accept(sizes) == 7


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("*.md", ["home/docs/guide.md", "home/src/README.md"]),
        ("*.py", ["home/src/main.py", "home/src/pkg/__init__.py", "home/src/pkg/core.py"]),
        ("core.*", ["home/src/pkg/core.py"]),
        ("*.jpg", []),
    ],
)
def test_search_visitor_returns_full_paths_in_traversal_order(pattern: str, expected: list[str]) -> None:
    assert build_tree().accept(SearchVisitor(pattern)) == expected


def test_search_visitor_restores_its_path_stack_so_it_can_be_reused() -> None:
    home = build_tree()
    visitor = SearchVisitor("*.md")
    first = home.accept(visitor)
    second = home.accept(visitor)
    assert first == second == ["home/docs/guide.md", "home/src/README.md"]
    docs = next(child for child in home.children if child.name == "docs")
    assert docs.accept(visitor) == ["docs/guide.md"]  # paths are relative to the node visited


def test_a_new_operation_is_a_new_visitor_not_a_change_to_the_elements() -> None:
    class CountVisitor:
        """Files and directories under a node, as a pair: a third operation, zero edits to Node."""

        def visit_file(self, file: File) -> tuple[int, int]:
            return (1, 0)

        def visit_directory(self, directory: Directory) -> tuple[int, int]:
            files, dirs = 0, 1
            for child in directory.children:
                child_files, child_dirs = child.accept(self)
                files, dirs = files + child_files, dirs + child_dirs
            return (files, dirs)

    assert build_tree().accept(CountVisitor()) == (6, 5)
    assert not any(hasattr(cls, "count") for cls in (Node, File, Directory))


def test_singledispatch_and_match_agree_with_the_class_visitors() -> None:
    home = build_tree()
    assert size_of(home) == home.accept(SizeVisitor()) == 6_700
    for node in home.children:
        assert size_of(node) == node.accept(SizeVisitor())
    for pattern in ("*.md", "*.py", "*.jpg"):
        assert find(home, pattern) == home.accept(SearchVisitor(pattern))


def test_the_functional_forms_reject_an_element_type_they_have_no_rule_for() -> None:
    class Symlink(Node):
        def accept[R](self, visitor: Visitor[R]) -> R:
            raise NotImplementedError("a symlink is not a file")

    link = Symlink("latest")
    with pytest.raises(TypeError, match="no size rule for Symlink"):
        size_of(link)
    with pytest.raises(TypeError, match="no search rule for Symlink"):
        find(link, "*")


def test_elements_validate_their_own_invariants() -> None:
    docs = Directory("docs")
    docs.add(File("guide.md", 1))
    with pytest.raises(ConflictError, match="already exists"):
        docs.add(File("guide.md", 2))
    with pytest.raises(ValidationError):
        File("", 1)
    with pytest.raises(ValidationError):
        Directory("a/b")
    with pytest.raises(ValidationError):
        File("neg.bin", -1)
    assert [child.name for child in docs.children] == ["guide.md"]
