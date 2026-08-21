"""Composite: leaves and directories answer the same questions; the composite guards the tree."""

from collections.abc import Sequence

import pytest

from common import ConflictError, NotFoundError, ValidationError
from patterns.composite import Directory, File, FileSystemNode, Node, describe, walk

KB = 1024


def build_tree() -> Directory:
    root = Directory("root")
    etc = root.add(Directory("etc"))
    etc.add(File("hosts", 120))
    etc.add(Directory("nginx")).add(File("nginx.conf", 2 * KB))
    log = root.add(Directory("var")).add(Directory("log"))
    log.add(File("app.log", 1024 * KB))
    log.add(File("app.log.1", 512 * KB))
    root.add(File("README", 512))
    return root


def test_size_is_the_same_question_at_every_level() -> None:
    root = build_tree()
    assert root.resolve("README").size() == 512
    assert root.resolve("etc/nginx").size() == 2 * KB
    assert root.resolve("etc").size() == 120 + 2 * KB
    assert root.resolve("var").size() == 1536 * KB
    assert root.size() == 120 + 2 * KB + 1536 * KB + 512
    assert Directory("empty").size() == 0


def test_client_code_never_branches_on_the_node_type() -> None:
    def total(nodes: Sequence[FileSystemNode]) -> int:
        return sum(node.size() for node in nodes)

    root = build_tree()
    mixed: list[FileSystemNode] = [root.resolve("README"), root.resolve("etc"), File("x", 1)]
    assert total(mixed) == 512 + 120 + 2 * KB + 1
    assert total(root.children()) == root.size()  # a directory's children are a mixed sequence too


def test_iteration_is_preorder_and_find_filters_uniformly() -> None:
    root = build_tree()
    names = [node.name for node in root]
    assert names == ["root", "etc", "hosts", "nginx", "nginx.conf", "var", "log", "app.log", "app.log.1", "README"]
    assert [node.name for node in File("solo", 1)] == ["solo"]  # a leaf is a one-node tree
    big = [node.name for node in root.find(lambda n: isinstance(n, File) and n.size() > 100 * KB)]
    assert big == ["app.log", "app.log.1"]
    assert sum(1 for _ in root.find(lambda n: isinstance(n, Directory))) == 5


def test_composite_rejects_duplicates_cycles_and_unknown_names() -> None:
    root = build_tree()
    etc = root.resolve("etc")
    assert isinstance(etc, Directory)
    with pytest.raises(ConflictError):
        etc.add(File("hosts", 1))
    with pytest.raises(ValidationError):
        etc.add(root)  # root contains etc: adding it below etc would close a cycle
    with pytest.raises(ValidationError):
        etc.add(etc)
    with pytest.raises(NotFoundError):
        etc.remove("passwd")
    with pytest.raises(NotFoundError):
        etc.get("passwd")
    assert [node.name for node in etc.children()] == ["hosts", "nginx"]  # nothing half-applied


def test_resolve_walks_paths_and_stops_at_a_leaf() -> None:
    root = build_tree()
    assert root.resolve("var/log/app.log").size() == 1024 * KB
    assert root.resolve("/var//log/") is root.resolve("var/log")
    assert root.resolve("") is root
    with pytest.raises(NotFoundError, match="not a directory"):
        root.resolve("README/anything")
    with pytest.raises(NotFoundError):
        root.resolve("var/cache")


def test_remove_changes_every_ancestor_total_without_a_recount_anywhere_else() -> None:
    root = build_tree()
    log = root.resolve("var/log")
    assert isinstance(log, Directory)
    before = root.size()
    removed = log.remove("app.log.1")
    assert removed.name == "app.log.1"
    assert root.size() == before - 512 * KB
    assert log.size() == 1024 * KB


def test_render_indents_by_depth_and_labels_directories() -> None:
    root = Directory("root")
    root.add(Directory("etc")).add(File("hosts", 120))
    root.add(File("README", 512))
    assert root.render() == "\n".join(
        ["root/ (632 B)", "    etc/ (120 B)", "        hosts (120 B)", "    README (512 B)"]
    )


@pytest.mark.parametrize("bad_name", ["", "a/b"])
def test_names_and_sizes_are_validated_on_construction(bad_name: str) -> None:
    with pytest.raises(ValidationError):
        File(bad_name, 1)
    with pytest.raises(ValidationError):
        Directory(bad_name)
    with pytest.raises(ValidationError):
        File("negative", -1)


def test_single_class_variant_and_match_dispatch_agree_with_the_classes() -> None:
    tree = Node("root", children=[Node("a.txt", 10), Node("docs", children=[Node("b.txt", 20)])])
    assert tree.total() == 30
    assert [(depth, node.name) for depth, node in walk(tree)] == [
        (0, "root"),
        (1, "a.txt"),
        (1, "docs"),
        (2, "b.txt"),
    ]
    root = build_tree()
    assert describe(root.resolve("README")) == "file README: 512 B"
    assert describe(root.resolve("var/log")) == f"dir log/: 2 entries, {1536 * KB} B"
