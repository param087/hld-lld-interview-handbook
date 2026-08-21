"""mkdir -p, write, ls, find, mv, rm -r, an open handle and a permission check."""

from common import FakeClock
from lld.in_memory_file_system.models import (
    NodeStatus,
    Permission,
    PermissionDeniedError,
    RecursiveMoveError,
    User,
)
from lld.in_memory_file_system.services import FileSystem, SecureFileSystem


def main() -> None:
    clock = FakeClock(start=1_700_000_000)
    fs = FileSystem(clock=clock)

    fs.mkdir("/srv/app/logs")
    fs.write("/srv/app/main.py", "print('hi')\n")
    fs.write("/srv/app/README.md", "# app\n")
    fs.append("/srv/app/logs/app.log", "boot\n")
    clock.advance(60)
    fs.append("/srv/app/logs/app.log", "ready\n")
    print(f"ls /srv/app -> {fs.ls('/srv/app')}")
    print(f"read log    -> {fs.read('/srv/app/logs/app.log')!r}")

    print(f"path edge cases: {fs.ls('/srv/app/logs/../')} == {fs.ls('//srv//app/')}")
    report = fs.usage("/srv")
    print(f"du /srv: {report.total_bytes}B in {report.files} files, {report.directories} dirs, largest {report.largest_file}")
    print(f"find *.log  -> {fs.find('/', extension='.log')}")

    fs.mv("/srv/app/README.md", "/srv/README.md")
    print(f"after mv: /srv -> {fs.ls('/srv')}, /srv/app -> {fs.ls('/srv/app')}")
    try:
        fs.mv("/srv/app", "/srv/app/logs/app")
    except RecursiveMoveError as exc:
        print(f"rejected: {exc}")

    handle = fs.open("/srv/app/logs/app.log")
    log = fs.rm("/srv/app/logs/app.log")
    print(f"rm with a handle open -> status={log.status}, still readable: {handle.read()!r}")
    handle.close()
    print(f"after close -> status={log.status}")

    fs.chmod("/srv/app/main.py", Permission.READ_WRITE, Permission.NONE)
    guest = SecureFileSystem(fs, User("guest"))
    try:
        guest.read("/srv/app/main.py")
    except PermissionDeniedError as exc:
        print(f"guest denied: {exc}")
    print(f"root allowed: {SecureFileSystem(fs, User('root', is_admin=True)).read('/srv/app/main.py')!r}")

    fs.cp("/srv/app", "/backup")
    print(f"cp -r /srv/app /backup -> {fs.ls('/backup')}, {fs.size('/backup')}B")
    removed = fs.rm("/srv", recursive=True)
    print(f"rm -r /srv -> status={removed.status}, released={removed.status is NodeStatus.RELEASED}, root now {fs.ls('/')}")
    print(fs.tree("/backup"))


if __name__ == "__main__":
    main()
