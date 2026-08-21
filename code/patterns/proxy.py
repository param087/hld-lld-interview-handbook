"""Proxy: an object that stands in for another one and controls access to it.

The proxy has the same interface as the real object, so the client cannot tell them
apart; what it adds is a decision about the call. ``CachedImageProxy`` is a virtual
proxy with a cache: the expensive ``RealImage`` is created on the first ``render`` and
every rendered width is remembered. ``AccessControlledDocument`` is a protection proxy:
a permission check in front of every ``TextDocument`` call. ``RemoteDocumentStub`` is a
remote proxy, one message per call. The last section shows the Pythonic forms:
``__getattr__`` delegation, ``cached_property`` and ``weakref.proxy``.
"""

from __future__ import annotations

import threading
import weakref
from collections.abc import Callable, Mapping
from enum import StrEnum
from functools import cached_property
from typing import Any, Protocol, runtime_checkable

from common import HandbookError, NotFoundError, ValidationError


# --8<-- [start:subject]
@runtime_checkable
class Image(Protocol):
    """The Subject: what the client calls, satisfied by the real image and by its proxy."""

    @property
    def path(self) -> str: ...

    def render(self, width: int) -> str: ...


class ImageStore:
    """The disk or blob store: ``load`` is the expensive call a proxy postpones.

    ``_lock`` guards ``_loaded``; ``load_count`` proves how often the store was hit.
    """

    def __init__(self, files: Mapping[str, tuple[int, int]]) -> None:
        self._files = dict(files)
        self._loaded: list[str] = []
        self._lock = threading.Lock()

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted(self._files))  # listing is cheap; it is the pixels that cost

    def load(self, path: str) -> tuple[int, int]:
        """Returns (width, height); a real store would return megabytes of pixels."""
        dimensions = self._files.get(path)
        if dimensions is None:
            raise NotFoundError(f"no image at {path!r}")
        with self._lock:
            self._loaded.append(path)
        return dimensions

    @property
    def load_count(self) -> int:
        with self._lock:
            return len(self._loaded)


class RealImage:
    """The RealSubject: the load happens in the constructor, which is exactly what a proxy delays."""

    def __init__(self, path: str, store: ImageStore) -> None:
        self._path = path
        self._width, self._height = store.load(path)

    @property
    def path(self) -> str:
        return self._path

    def render(self, width: int) -> str:
        if width <= 0:
            raise ValidationError("width must be positive")
        height = max(1, round(self._height * width / self._width))
        return f"{self._path} {width}x{height}"


# --8<-- [end:subject]


# --8<-- [start:virtual_proxy]
class CachedImageProxy:
    """A virtual proxy with a cache: the ``Image`` interface, the ``RealImage`` made on first ``render``.

    ``path`` is answered from the proxy's own field, so listing a gallery of a thousand
    images touches the store zero times. ``_lock`` guards ``_real`` and ``_renders``,
    without which two threads rendering a cold image would both load it. A render that
    raises caches nothing, and a failed load leaves ``_real`` empty so the next retries.
    """

    def __init__(self, path: str, store: ImageStore) -> None:
        self._path = path
        self._store = store
        self._real: RealImage | None = None
        self._renders: dict[int, str] = {}
        self._lock = threading.Lock()

    @property
    def path(self) -> str:
        return self._path

    @property
    def is_loaded(self) -> bool:
        with self._lock:
            return self._real is not None

    def render(self, width: int) -> str:
        with self._lock:
            cached = self._renders.get(width)
            if cached is None:
                if self._real is None:
                    self._real = RealImage(self._path, self._store)
                cached = self._renders[width] = self._real.render(width)
            return cached


# --8<-- [end:virtual_proxy]


# --8<-- [start:protection_proxy]
class Permission(StrEnum):
    READ = "read"
    WRITE = "write"


class PermissionDeniedError(HandbookError):
    """The proxy refused to forward the call; the real document never saw it."""


@runtime_checkable
class Document(Protocol):
    """The Subject shared by the real document, the protection proxy and the remote stub."""

    @property
    def doc_id(self) -> str: ...

    def read(self) -> str: ...

    def write(self, text: str) -> None: ...


class TextDocument:
    """The RealSubject: it knows nothing about users, roles or networks."""

    def __init__(self, doc_id: str, text: str = "") -> None:
        self._doc_id = doc_id
        self._text = text

    @property
    def doc_id(self) -> str:
        return self._doc_id

    def read(self) -> str:
        return self._text

    def write(self, text: str) -> None:
        self._text = text


type AccessPolicy = Callable[[str, Permission], bool]


class AccessControlledDocument:
    """A protection proxy: the same ``read`` and ``write``, each preceded by a policy check.

    Bound to one user at construction, so the signatures stay identical to the subject's:
    nobody passes a user into ``read``. The policy is consulted on every call, so a
    revocation lands on the next call rather than the next login.
    """

    def __init__(self, inner: Document, user: str, policy: AccessPolicy) -> None:
        self._inner = inner
        self._user = user
        self._policy = policy

    @property
    def doc_id(self) -> str:
        return self._inner.doc_id

    def read(self) -> str:
        self._check(Permission.READ)
        return self._inner.read()

    def write(self, text: str) -> None:
        self._check(Permission.WRITE)
        self._inner.write(text)

    def _check(self, permission: Permission) -> None:
        if not self._policy(self._user, permission):
            raise PermissionDeniedError(f"{self._user} may not {permission} {self._inner.doc_id}")


# --8<-- [end:protection_proxy]


# --8<-- [start:remote_proxy]
class RemoteError(HandbookError):
    """The failure a local object never has: the call may not have happened at all."""


type Message = dict[str, str]
type Transport = Callable[[Message], Message]


class DocumentServer:
    """The far side of the wire: unpack, call the real document, pack the reply; a gRPC service in production."""

    def __init__(self, documents: Mapping[str, Document]) -> None:
        self._documents = dict(documents)

    def handle(self, request: Message) -> Message:
        document = self._documents.get(request.get("doc_id", ""))
        if document is None:
            return {"error": f"no document {request.get('doc_id')!r}"}
        match request.get("method"):
            case "read":
                return {"result": document.read()}
            case "write":
                document.write(request.get("text", ""))
                return {"result": ""}
            case other:
                return {"error": f"unknown method {other!r}"}


class RemoteDocumentStub:
    """A remote proxy: the ``Document`` interface, one message per call.

    ``doc_id`` is local state; ``read`` and ``write`` cross the transport. The interface
    hides the network but not its cost or its failure modes: every call is a round trip,
    and either side's failure arrives as ``RemoteError``.
    """

    def __init__(self, doc_id: str, transport: Transport) -> None:
        self._doc_id = doc_id
        self._transport = transport

    @property
    def doc_id(self) -> str:
        return self._doc_id

    def read(self) -> str:
        return self._call("read")

    def write(self, text: str) -> None:
        self._call("write", text=text)

    def _call(self, method: str, **fields: str) -> str:
        request: Message = {"method": method, "doc_id": self._doc_id, **fields}
        try:
            reply = self._transport(request)
        except OSError as exc:
            raise RemoteError(f"transport failed: {exc}") from exc
        if "error" in reply:
            raise RemoteError(reply["error"])
        return reply.get("result", "")


# --8<-- [end:remote_proxy]


# --8<-- [start:pythonic]
class LazyProxy[T]:
    """``__getattr__`` delegation: build the target on first use, forward every attribute after that.

    ``__getattr__`` runs only when normal lookup fails, so the proxy's own fields never
    recurse into it. It never sees dunder lookups either: ``len(proxy)``, ``proxy == other``
    and ``with proxy:`` go to ``type(proxy)``, and ``isinstance`` reports the proxy.
    """

    def __init__(self, factory: Callable[[], T]) -> None:
        self._factory = factory
        self._target: T | None = None
        self._lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):  # never forward private names; also ends recursion before __init__
            raise AttributeError(name)
        with self._lock:
            if self._target is None:
                self._target = self._factory()
        return getattr(self._target, name)


class Thumbnail:
    """``cached_property`` is the virtual proxy for one attribute: computed on first access, then stored.

    Since 3.12 it holds no lock: two threads racing on a cold attribute may both run it.
    """

    def __init__(self, path: str, store: ImageStore) -> None:
        self.path = path
        self._store = store

    @cached_property
    def image(self) -> RealImage:
        return RealImage(self.path, self._store)


class TreeNode:
    """A tree whose back-reference to the parent is a ``weakref.proxy``.

    The parent owns its children; a child must not own its parent, or no subtree is ever
    collected. The proxy forwards ``path`` and raises ``ReferenceError`` once it is gone.
    """

    def __init__(self, name: str, parent: TreeNode | None = None) -> None:
        self.name = name
        self.children: list[TreeNode] = []
        self.parent: TreeNode | None = None
        if parent is not None:
            self.parent = weakref.proxy(parent)
            parent.children.append(self)

    def path(self) -> str:
        if self.parent is None:
            return self.name
        return f"{self.parent.path()}/{self.name}"


# --8<-- [end:pythonic]


def main() -> None:
    store = ImageStore({"photos/a.jpg": (3000, 2000), "photos/b.jpg": (1200, 1200),
                        "photos/c.jpg": (4000, 3000), "photos/d.jpg": (800, 600)})
    print("--- virtual proxy: list four images and load none; render one and load one ---")
    gallery = [CachedImageProxy(path, store) for path in store.paths]
    print(f"listed {len(gallery)} paths, loads: {store.load_count}")
    first = gallery[0]
    print(f"render 200: {first.render(200)}  loads: {store.load_count}")
    print(f"render 200: {first.render(200)}  loads: {store.load_count} (cache hit)")
    print(f"render 400: {first.render(400)}  loads: {store.load_count} (new width, same image)")
    print(f"loaded {sum(1 for image in gallery if image.is_loaded)} of {len(gallery)} images")

    print("--- protection proxy: one document, two users, the policy outside the document ---")
    document = TextDocument("doc-1", "Loan policy: 5 items, 10 days")
    grants = {"alice": {Permission.READ, Permission.WRITE}, "bob": {Permission.READ}}

    def policy(user: str, permission: Permission) -> bool:
        return permission in grants.get(user, set())

    alice_view: Document = AccessControlledDocument(document, "alice", policy)
    bob_view: Document = AccessControlledDocument(document, "bob", policy)
    alice_view.write("Loan policy: 5 items, 14 days")
    print(f"alice writes, bob reads: {bob_view.read()!r}")
    try:
        bob_view.write("Loan policy: unlimited")
    except PermissionDeniedError as exc:
        print(f"bob writes: PermissionDeniedError: {exc}")
    grants["alice"].discard(Permission.WRITE)
    try:
        alice_view.write("Loan policy: unlimited")
    except PermissionDeniedError as exc:
        print(f"alice writes after revocation: PermissionDeniedError: {exc}")

    print("--- remote proxy: the same interface, one message per call ---")
    server = DocumentServer({"doc-1": document})
    wire: list[Message] = []

    def transport(request: Message) -> Message:
        wire.append(request)
        return server.handle(request)

    stub: Document = RemoteDocumentStub("doc-1", transport)
    print(f"stub.read() -> {stub.read()!r} via {wire[-1]}")
    try:
        RemoteDocumentStub("doc-9", transport).read()
    except RemoteError as exc:
        print(f"unknown document: RemoteError: {exc}")

    print("--- pythonic forms: __getattr__ delegation, cached_property, weakref.proxy ---")
    builds: list[str] = []

    def build() -> TextDocument:
        builds.append("doc-2")
        return TextDocument("doc-2", "built on first attribute access")

    lazy = LazyProxy(build)
    print(f"LazyProxy created; builds so far: {len(builds)}")
    print(f"lazy.read() -> {lazy.read()!r}; builds: {len(builds)}")
    thumbnail, before = Thumbnail("photos/d.jpg", store), store.load_count
    renders = f"{thumbnail.image.render(80)}, {thumbnail.image.render(40)}"
    print(f"thumbnail.image twice: {renders}; loads: {store.load_count - before}")
    root = TreeNode("root")
    docs = TreeNode("docs", parent=root)
    print(f"weakref.proxy parent: {docs.path()}")
    del root
    try:
        docs.path()
    except ReferenceError as exc:
        print(f"after the parent is collected: ReferenceError: {exc}")


if __name__ == "__main__":
    main()
