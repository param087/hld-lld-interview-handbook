import re
from concurrent.futures import ThreadPoolExecutor

import pytest

from common import ConflictError, ValidationError
from hld.cursor_pagination import (
    Cursor,
    CursorCodec,
    Order,
    OrderTable,
    query_fingerprint,
    walk_keyset,
    walk_offset,
)

SECRET = b"test-secret"
CUSTOMERS = ("ann", "bob", "cat")
BASE_MS = 1_700_000_000_000
URL_SAFE = re.compile(r"^[A-Za-z0-9_.-]+$")


def order(i: int, per_second: int = 3) -> Order:
    """``ord-00001`` upwards; ``per_second`` orders share each timestamp so ties are everywhere."""
    return Order(f"ord-{i:05d}", CUSTOMERS[i % 3], BASE_MS + ((i - 1) // per_second) * 1_000)


def make_table(n: int, max_limit: int = 100) -> OrderTable:
    table = OrderTable(CursorCodec(SECRET), max_limit=max_limit)
    for i in range(1, n + 1):
        table.insert(order(i))
    return table


def newest_first(n: int) -> tuple[str, ...]:
    return tuple(f"ord-{i:05d}" for i in range(n, 0, -1))


def test_keyset_walk_returns_every_row_once_newest_first() -> None:
    table = make_table(100)
    stats = walk_keyset(table, limit=7)  # 7 is not a multiple of 3: ties straddle page boundaries
    assert stats.ids == newest_first(100)
    assert stats.duplicates == 0
    assert stats.pages == 15  # 14 full pages + 2 rows
    assert table.page_by_keyset(limit=100).next_cursor is None


def test_keyset_pages_are_stable_under_inserts_but_offset_pages_are_not() -> None:
    keyset_table, offset_table = make_table(60), make_table(60)
    newer = Order("ord-00061", "ann", BASE_MS + 10_000_000)

    by_keyset = walk_keyset(
        keyset_table, 10, between_pages=lambda n: keyset_table.insert(newer) if n == 1 else None
    )
    assert by_keyset.duplicates == 0
    assert by_keyset.ids == newest_first(
        60
    )  # the newer row sits before the boundary: never shown, never repeated

    by_offset = walk_offset(
        offset_table, 10, between_pages=lambda n: offset_table.insert(newer) if n == 1 else None
    )
    assert by_offset.duplicates == 1
    assert (
        by_offset.ids[9] == by_offset.ids[10] == "ord-00051"
    )  # page 2 starts with page 1's last row


def test_cursor_round_trip_is_opaque_and_url_safe() -> None:
    codec = CursorCodec(SECRET)
    cursor = Cursor(BASE_MS + 42, "ord-00042", query_fingerprint("ann"))
    token = codec.encode(cursor)
    assert URL_SAFE.match(token)
    assert token == codec.encode(cursor)  # deterministic, so clients can compare and cache
    assert codec.decode(token) == cursor
    assert "ord-00042" not in token and str(BASE_MS + 42) not in token


@pytest.mark.parametrize("bad", ["", "2", "abc", "a.b", "!!!.???", ".", "eyJ4IjoxfQ"])
def test_malformed_cursor_is_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        CursorCodec(SECRET).decode(bad)


def test_tampered_cursor_is_rejected() -> None:
    codec = CursorCodec(SECRET)
    token = codec.encode(Cursor(BASE_MS, "ord-00010", query_fingerprint(None)))
    body, tag = token.split(".")
    flipped_tag = tag[:-1] + ("A" if tag[-1] != "A" else "B")
    forged_body = (
        CursorCodec(b"other-secret")
        .encode(Cursor(BASE_MS, "ord-00001", query_fingerprint(None)))
        .split(".")[0]
    )
    for forged in (f"{body}.{flipped_tag}", f"{forged_body}.{tag}"):
        with pytest.raises(ValidationError, match="signature"):
            codec.decode(forged)
    with pytest.raises(ValidationError, match="signature"):
        CursorCodec(b"other-secret").decode(token)


def test_cursor_from_a_different_query_is_rejected() -> None:
    table = make_table(30)
    ann = table.page_by_keyset(limit=5, customer_id="ann")
    assert ann.next_cursor is not None
    assert query_fingerprint("ann") != query_fingerprint(None) != query_fingerprint("bob")
    for other in (None, "bob"):
        with pytest.raises(ValidationError, match="different query"):
            table.page_by_keyset(limit=5, cursor=ann.next_cursor, customer_id=other)
    again = table.page_by_keyset(limit=5, cursor=ann.next_cursor, customer_id="ann")
    assert all(o.customer_id == "ann" for o in again.items)


def test_filtered_pages_use_the_customer_index() -> None:
    table = make_table(90)
    page = table.page_by_keyset(limit=10, customer_id="bob")
    assert [o.customer_id for o in page.items] == ["bob"] * 10
    assert page.rows_examined == 11  # limit + 1 through the composite index, not a scan of 90
    stats = walk_keyset(table, limit=10, customer_id="bob")
    assert stats.ids == tuple(i for i in newest_first(90) if int(i[4:]) % 3 == 1)
    assert table.page_by_keyset(limit=10, customer_id="nobody").items == ()


def test_offset_cost_grows_with_depth_and_keyset_cost_does_not() -> None:
    table = make_table(1_000)
    deep = table.page_by_offset(limit=10, offset=900)
    assert deep.rows_examined == 910 and len(deep.items) == 10
    by_keyset, by_offset = walk_keyset(table, limit=10), walk_offset(table, limit=10)
    assert by_keyset.ids == by_offset.ids == newest_first(1_000)
    assert by_keyset.rows_examined == 99 * 11 + 10
    assert by_offset.rows_examined == sum(10 * k + 10 for k in range(100)) + 1_000
    assert by_offset.rows_examined > 40 * by_keyset.rows_examined


def test_limit_and_offset_validation() -> None:
    table = make_table(80, max_limit=50)
    for bad in (0, -1):
        with pytest.raises(ValidationError):
            table.page_by_keyset(limit=bad)
        with pytest.raises(ValidationError):
            table.page_by_offset(limit=bad)
    assert len(table.page_by_keyset(limit=500).items) == 50  # clamped, not rejected
    assert len(table.page_by_offset(limit=500).items) == 50
    with pytest.raises(ValidationError):
        table.page_by_offset(limit=10, offset=-1)
    with pytest.raises(ValidationError):
        OrderTable(CursorCodec(SECRET), max_limit=0)
    with pytest.raises(ConflictError):
        table.insert(order(1))


def test_concurrent_inserts_never_corrupt_a_keyset_walk() -> None:
    table = make_table(300)
    existing = set(newest_first(300))
    arriving = {f"new-{i:04d}" for i in range(350)}

    def writer(chunk: int) -> None:
        for i in range(chunk * 50, chunk * 50 + 50):
            table.insert(Order(f"new-{i:04d}", "ann", BASE_MS + 1_000_000 + i))

    with ThreadPoolExecutor(max_workers=8) as pool:
        reader = pool.submit(walk_keyset, table, 7)
        list(pool.map(writer, range(7)))
        seen = reader.result()

    assert seen.duplicates == 0
    assert existing <= set(seen.ids) <= existing | arriving
    positions = [
        (BASE_MS + 1_000_000 + int(i[4:]), i)
        if i.startswith("new")
        else (order(int(i[4:])).created_at, i)
        for i in seen.ids
    ]
    assert positions == sorted(positions, reverse=True)
    assert len(table) == 650
    assert walk_keyset(table, limit=50).duplicates == 0
