from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock
from lld.in_memory_file_system.models import (
    DirectoryNotEmptyError,
    InvalidPathError,
    IsADirectoryError_,
    NodeStatus,
    NotADirectoryError_,
    PathExistsError,
    PathNotFoundError,
    Permission,
    PermissionDeniedError,
    RecursiveMoveError,
    User,
)
from lld.in_memory_file_system.paths import PathResolver
from lld.in_memory_file_system.services import FileSystem, SecureFileSystem
from lld.in_memory_file_system.visitors import SearchVisitor, SizeVisitor


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_700_000_000)


@pytest.fixture
def fs(clock: FakeClock) -> FileSystem:
    return FileSystem(clock=clock)


# --8<-- [start:happy]
def test_mkdir_write_read_and_ls(fs: FileSystem, clock: FakeClock) -> None:
    fs.mkdir("/srv/app/logs")
    fs.write("/srv/app/main.py", "print('hi')\n")
    clock.advance(30)
    fs.append("/srv/app/logs/app.log", "boot\n")
    fs.append("/srv/app/logs/app.log", "ready\n")

    assert fs.ls("/srv/app") == ["logs", "main.py"]  # sorted, always
    assert fs.read("/srv/app/logs/app.log") == "boot\nready\n"
    assert fs.size("/srv") == 12 + 11  # Composite sums the subtree
    assert fs.stat("/srv/app/logs/app.log").modified == 1_700_000_030
    assert fs.ls("/srv/app/main.py") == ["main.py"]  # LeetCode 588: a file lists itself


# --8<-- [end:happy]


# --8<-- [start:paths]
@pytest.mark.parametrize(
    ("raw", "cwd", "expected"),
    [
        ("/", "/", "/"),
        ("/a/b/", "/", "/a/b"),  # trailing slash
        ("//a///b", "/", "/a/b"),  # repeated separators
        ("/a/./b", "/", "/a/b"),  # "." components
        ("/a/b/..", "/", "/a"),  # parent
        ("/..", "/", "/"),  # ".." past the root is the root, not an error
        ("/a/../../b", "/", "/b"),
        ("b/c", "/a", "/a/b/c"),  # relative to a working directory
        ("../c", "/a/b", "/a/c"),
    ],
)
def test_path_normalisation_edge_cases(raw: str, cwd: str, expected: str) -> None:
    assert PathResolver.normalize(raw, cwd) == expected


def test_is_ancestor_does_not_confuse_a_prefix_with_a_parent() -> None:
    assert PathResolver.is_ancestor("/a", "/a/b") is True
    assert PathResolver.is_ancestor("/a", "/ab") is False
    assert PathResolver.is_ancestor("/a", "/a") is True


# --8<-- [end:paths]


@pytest.mark.parametrize(
    ("action", "error"),
    [
        (lambda f: f.read("/nope.txt"), PathNotFoundError),
        (lambda f: f.read("/srv"), IsADirectoryError_),
        (lambda f: f.rm("/srv"), DirectoryNotEmptyError),
        (lambda f: f.rm("/"), InvalidPathError),
        (lambda f: f.mkdir("/srv/app/main.py/x"), NotADirectoryError_),
        (lambda f: f.mkdir("/srv", parents=False), PathExistsError),
        (lambda f: PathResolver.normalize("  "), InvalidPathError),
    ],
)
def test_invalid_operations_are_rejected(fs: FileSystem, action, error) -> None:
    fs.mkdir("/srv/app")
    fs.write("/srv/app/main.py", "x")
    with pytest.raises(error):
        action(fs)


def test_mkdir_is_idempotent_and_never_clobbers_a_file(fs: FileSystem) -> None:
    first = fs.mkdir("/a/b/c")
    assert fs.mkdir("/a/b/c") is first
    fs.write("/a/b/file.txt", "data")
    with pytest.raises(NotADirectoryError_):
        fs.mkdir("/a/b/file.txt/deeper")


# --8<-- [start:mv]
def test_mv_renames_moves_and_refuses_its_own_subtree(fs: FileSystem) -> None:
    fs.mkdir("/srv/app/logs")
    fs.write("/srv/app/README.md", "# app")

    fs.mv("/srv/app/README.md", "/srv/README.md")  # move up a level
    assert fs.ls("/srv") == ["README.md", "app"] and fs.ls("/srv/app") == ["logs"]

    fs.mv("/srv/README.md", "/srv/app")  # destination is a directory: move into it
    assert fs.ls("/srv/app") == ["README.md", "logs"]

    with pytest.raises(RecursiveMoveError):
        fs.mv("/srv/app", "/srv/app/logs/app")  # would detach the subtree from the root
    with pytest.raises(PathExistsError):
        fs.mv("/srv/app/logs", "/srv/app/README.md")


# --8<-- [end:mv]


def test_rm_recursive_releases_the_whole_subtree(fs: FileSystem) -> None:
    fs.write("/a/b/c.txt", "data")
    node = fs.resolve("/a/b/c.txt")
    with pytest.raises(DirectoryNotEmptyError):
        fs.rm("/a")

    fs.rm("/a", recursive=True)
    assert fs.ls("/") == [] and node.status is NodeStatus.RELEASED
    assert fs.exists("/a/b/c.txt") is False


# --8<-- [start:unlink]
def test_an_open_handle_keeps_an_unlinked_file_alive(fs: FileSystem) -> None:
    fs.write("/var/log/app.log", "boot\n")
    handle = fs.open("/var/log/app.log")
    file = fs.resolve("/var/log/app.log")

    fs.rm("/var/log/app.log")
    assert file.status is NodeStatus.UNLINKED  # gone from the tree, not from memory
    assert handle.read() == "boot\n"
    assert fs.exists("/var/log/app.log") is False

    handle.close()
    assert file.status is NodeStatus.RELEASED  # last handle closed, storage reclaimed


# --8<-- [end:unlink]


def test_cp_makes_an_independent_deep_copy(fs: FileSystem, clock: FakeClock) -> None:
    fs.write("/src/a/one.txt", "one")
    fs.write("/src/a/two.txt", "two")
    clock.advance(10)
    fs.cp("/src/a", "/dst")

    assert fs.ls("/dst") == ["one.txt", "two.txt"] and fs.read("/dst/one.txt") == "one"
    fs.write("/dst/one.txt", "changed")
    assert fs.read("/src/a/one.txt") == "one"  # the original is untouched


def test_visitors_report_and_search_without_touching_the_node_classes(fs: FileSystem) -> None:
    fs.write("/srv/app/main.py", "print('hi')")
    fs.write("/srv/app/logs/app.log", "boot")
    fs.write("/srv/notes.txt", "hello")

    sizes = SizeVisitor()
    fs.accept(sizes, "/srv")
    report = sizes.report()
    assert (report.files, report.directories, report.total_bytes) == (3, 3, 11 + 4 + 5)
    assert report.largest_file == "/srv/app/main.py"

    assert fs.find("/", extension=".log") == ["/srv/app/logs/app.log"]
    assert fs.find("/", name="app") == ["/srv/app"]
    by_size = SearchVisitor.by_min_size(5)
    fs.accept(by_size, "/srv")
    assert sorted(by_size.matches) == ["/srv/app/main.py", "/srv/notes.txt"]


def test_walk_is_a_sorted_depth_first_iterator(fs: FileSystem) -> None:
    fs.write("/a/z.txt", "z")
    fs.write("/a/b/y.txt", "y")
    assert [path for path, _ in fs.walk("/a")] == ["/a", "/a/b", "/a/b/y.txt", "/a/z.txt"]


# --8<-- [start:concurrency]
def test_concurrent_mkdir_and_write_never_lose_a_file(fs: FileSystem) -> None:
    def worker(i: int) -> None:
        fs.mkdir(f"/shared/dir-{i % 4}")
        fs.write(f"/shared/dir-{i % 4}/file-{i:03d}.txt", "x" * (i % 7))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(200)))

    assert fs.ls("/shared") == [f"dir-{i}" for i in range(4)]  # mkdir -p raced 200 times
    report = fs.usage("/shared")
    assert report.files == 200  # every writer got its own file
    assert report.directories == 5  # /shared plus four children, created exactly once
    assert report.total_bytes == sum(i % 7 for i in range(200))


# --8<-- [end:concurrency]


def test_concurrent_appends_to_one_file_keep_every_byte(fs: FileSystem) -> None:
    fs.write("/var/log/app.log", "")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: fs.append("/var/log/app.log", f"line-{i:03d}\n"), range(300)))

    lines = fs.read("/var/log/app.log").splitlines()
    assert len(lines) == 300 and len(set(lines)) == 300  # the file lock serialises appends


def test_permission_proxy_denies_then_the_owner_and_admin_pass(fs: FileSystem) -> None:
    fs.write("/srv/secret.txt", "classified")
    fs.chmod("/srv/secret.txt", Permission.READ_WRITE, Permission.NONE)

    guest = SecureFileSystem(fs, User("guest"))
    with pytest.raises(PermissionDeniedError):
        guest.read("/srv/secret.txt")
    with pytest.raises(PermissionDeniedError):
        guest.write("/srv/secret.txt", "tampered")

    assert SecureFileSystem(fs, User("root")).read("/srv/secret.txt") == "classified"
    assert SecureFileSystem(fs, User("ops", is_admin=True)).read("/srv/secret.txt") == "classified"
    assert fs.stat("/srv/secret.txt").mode() == "-rw----"


def test_leetcode_588_example_sequence(fs: FileSystem) -> None:
    assert fs.ls("/") == []
    fs.mkdir("/a/b/c")
    fs.append("/a/b/c/d", "hello")
    assert fs.ls("/") == ["a"]
    assert fs.read("/a/b/c/d") == "hello"
    fs.append("/a/b/c/d", " world")
    assert fs.read("/a/b/c/d") == "hello world"
    assert fs.ls("/a/b/c") == ["d"]
