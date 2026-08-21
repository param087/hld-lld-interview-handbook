"""Proxy: the same interface as the subject, and the proxy decides whether, when and how the real call happens."""

import gc
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import NotFoundError, ValidationError
from patterns.proxy import (
    AccessControlledDocument,
    AccessPolicy,
    CachedImageProxy,
    Document,
    DocumentServer,
    Image,
    ImageStore,
    LazyProxy,
    Permission,
    PermissionDeniedError,
    RealImage,
    RemoteDocumentStub,
    RemoteError,
    TextDocument,
    Thumbnail,
    TreeNode,
)

FILES = {"photos/a.jpg": (3000, 2000), "photos/b.jpg": (1200, 1200)}


def grants_policy(grants: dict[str, set[Permission]]) -> AccessPolicy:
    """A policy the test can revoke from, to prove the proxy consults it on every call."""
    return lambda user, permission: permission in grants.get(user, set())


def test_virtual_proxy_answers_path_without_loading_and_loads_on_first_render() -> None:
    store = ImageStore(FILES)
    proxy = CachedImageProxy("photos/a.jpg", store)
    assert proxy.path == "photos/a.jpg"
    assert not proxy.is_loaded and store.load_count == 0  # listing never touches the store
    assert proxy.render(300) == "photos/a.jpg 300x200"
    assert proxy.is_loaded and store.load_count == 1


def test_proxy_and_real_image_are_interchangeable_for_the_client() -> None:
    store = ImageStore(FILES)
    real: Image = RealImage("photos/b.jpg", store)
    proxy: Image = CachedImageProxy("photos/b.jpg", store)
    assert isinstance(real, Image) and isinstance(proxy, Image)
    for width in (50, 600, 1200):
        assert proxy.render(width) == real.render(width)


def test_caching_proxy_renders_each_width_once_and_caches_no_failure() -> None:
    store = ImageStore(FILES)
    proxy = CachedImageProxy("photos/a.jpg", store)
    assert proxy.render(200) == proxy.render(200) == "photos/a.jpg 200x133"
    assert proxy.render(400) == "photos/a.jpg 400x267"
    assert store.load_count == 1  # three renders, one load
    with pytest.raises(ValidationError):
        proxy.render(0)
    with pytest.raises(ValidationError):
        proxy.render(0)  # still raises: the failure was not cached
    missing = CachedImageProxy("photos/zzz.jpg", store)
    with pytest.raises(NotFoundError):
        missing.render(100)
    assert not missing.is_loaded  # a failed load leaves the proxy cold, so the next call retries


def test_concurrent_renders_of_a_cold_proxy_load_the_real_image_exactly_once() -> None:
    store = ImageStore(FILES)
    proxy = CachedImageProxy("photos/a.jpg", store)
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(proxy.render, [200] * 200))
    assert results == ["photos/a.jpg 200x133"] * 200
    assert store.load_count == 1


@pytest.mark.parametrize(
    ("user", "can_read", "can_write"),
    [("alice", True, True), ("bob", True, False), ("mallory", False, False)],
)
def test_protection_proxy_checks_the_bound_user_on_every_call(
    user: str, can_read: bool, can_write: bool
) -> None:
    grants = {"alice": {Permission.READ, Permission.WRITE}, "bob": {Permission.READ}}
    document = TextDocument("doc-1", "v1")
    view: Document = AccessControlledDocument(document, user, grants_policy(grants))
    assert isinstance(view, Document) and view.doc_id == "doc-1"
    if can_read:
        assert view.read() == "v1"
    else:
        with pytest.raises(PermissionDeniedError, match=f"{user} may not read doc-1"):
            view.read()
    if can_write:
        view.write("v2")
        assert document.read() == "v2"  # forwarded unchanged to the real document
    else:
        with pytest.raises(PermissionDeniedError, match=f"{user} may not write doc-1"):
            view.write("v2")
        assert document.read() == "v1"  # the real document never saw the call


def test_revoking_a_permission_applies_on_the_next_call_not_the_next_open() -> None:
    grants = {"alice": {Permission.READ, Permission.WRITE}}
    view = AccessControlledDocument(TextDocument("doc-1"), "alice", grants_policy(grants))
    view.write("first")
    grants["alice"].discard(Permission.WRITE)
    with pytest.raises(PermissionDeniedError):
        view.write("second")
    assert view.read() == "first"


def test_remote_stub_sends_one_message_per_call_and_the_server_calls_the_real_document() -> None:
    document = TextDocument("doc-1", "hello")
    server = DocumentServer({"doc-1": document})
    wire: list[dict[str, str]] = []

    def transport(request: dict[str, str]) -> dict[str, str]:
        wire.append(request)
        return server.handle(request)

    stub: Document = RemoteDocumentStub("doc-1", transport)
    assert isinstance(stub, Document) and stub.doc_id == "doc-1"
    assert wire == []  # doc_id is local state; nothing crossed the wire yet
    assert stub.read() == "hello"
    stub.write("bye")
    assert document.read() == "bye"
    assert wire == [
        {"method": "read", "doc_id": "doc-1"},
        {"method": "write", "doc_id": "doc-1", "text": "bye"},
    ]


def test_remote_failures_surface_as_remote_error_from_either_side_of_the_wire() -> None:
    server = DocumentServer({"doc-1": TextDocument("doc-1")})
    with pytest.raises(RemoteError, match="no document 'doc-9'"):
        RemoteDocumentStub("doc-9", server.handle).read()
    assert server.handle({"method": "delete", "doc_id": "doc-1"}) == {"error": "unknown method 'delete'"}

    def broken(_request: dict[str, str]) -> dict[str, str]:
        raise ConnectionResetError("connection reset")

    with pytest.raises(RemoteError, match="transport failed: connection reset"):
        RemoteDocumentStub("doc-1", broken).read()


def test_lazy_proxy_builds_the_target_once_under_concurrency_and_forwards_attributes() -> None:
    builds: list[int] = []

    def build() -> TextDocument:
        builds.append(1)
        return TextDocument("doc-2", "lazy")

    proxy = LazyProxy(build)
    assert builds == []
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: proxy.read(), range(100)))
    assert results == ["lazy"] * 100
    assert len(builds) == 1
    assert proxy.doc_id == "doc-2"


def test_getattr_delegation_does_not_cover_dunder_lookups_or_private_names() -> None:
    proxy = LazyProxy(lambda: [1, 2, 3])
    assert proxy.count(2) == 1  # an ordinary attribute is forwarded
    with pytest.raises(TypeError):
        len(proxy)  # len() looks up __len__ on type(proxy), which __getattr__ never sees
    assert not isinstance(proxy, list)
    with pytest.raises(AttributeError):
        proxy._private  # noqa: B018 - the lookup itself is the assertion


def test_cached_property_loads_once_and_weakref_proxy_does_not_keep_its_subject_alive() -> None:
    store = ImageStore(FILES)
    thumbnail = Thumbnail("photos/a.jpg", store)
    assert store.load_count == 0
    assert thumbnail.image is thumbnail.image  # computed once, then a plain instance attribute
    assert store.load_count == 1

    root = TreeNode("root")
    docs = TreeNode("docs", parent=root)
    assert root.children == [docs] and docs.path() == "root/docs"
    del root
    gc.collect()
    with pytest.raises(ReferenceError):
        docs.path()
