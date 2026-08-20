"""Repository: one contract, two implementations, and the tests that prove they agree."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from common import ConflictError, NotFoundError, SequentialIdGenerator, ValidationError
from patterns.repository import (
    Book,
    InMemoryRepository,
    InMemoryUserRepository,
    RegistrationService,
    Repository,
    SqliteUserRepository,
    User,
    UserRepository,
    connect,
)

ADA = User(id="user-1", email="ada@example.com", name="Ada")
GRACE = User(id="user-2", email="grace@example.com", name="Grace")


@pytest.fixture(params=["in-memory", "sqlite"])
def repo(request: pytest.FixtureRequest) -> Iterator[UserRepository]:
    """Every test below runs once per implementation: that is the contract test."""
    if request.param == "in-memory":
        yield InMemoryUserRepository()
        return
    conn = connect()
    try:
        yield SqliteUserRepository(conn)
    finally:
        conn.close()


def test_add_get_find_update_remove_round_trip(repo: UserRepository) -> None:
    assert repo.get("user-1") is None and repo.count() == 0
    repo.add(ADA)
    repo.add(GRACE)
    assert repo.get("user-1") == ADA
    assert repo.find_by_email("grace@example.com") == GRACE
    assert repo.count() == 2

    repo.update(replace(ADA, name="Ada Lovelace", email="lovelace@example.com"))
    assert repo.get("user-1") == User("user-1", "lovelace@example.com", "Ada Lovelace")
    assert repo.find_by_email("ada@example.com") is None  # the old email is free again

    repo.remove("user-2")
    assert repo.get("user-2") is None and repo.count() == 1


def test_contract_errors_are_domain_errors_not_driver_errors(repo: UserRepository) -> None:
    repo.add(ADA)
    with pytest.raises(ConflictError):
        repo.add(replace(ADA, email="other@example.com"))  # same id
    with pytest.raises(ConflictError):
        repo.add(replace(GRACE, email=ADA.email))  # same email
    repo.add(GRACE)
    with pytest.raises(ConflictError):
        repo.update(replace(GRACE, email=ADA.email))  # steal a taken email
    with pytest.raises(NotFoundError):
        repo.update(User("user-9", "nobody@example.com", "Nobody"))
    with pytest.raises(NotFoundError):
        repo.remove("user-9")
    assert repo.count() == 2  # every rejected call left the store untouched


def test_a_change_is_not_persisted_until_update_on_either_implementation(repo: UserRepository) -> None:
    repo.add(ADA)
    stored = repo.get("user-1")
    assert stored is not None
    changed = replace(stored, name="Ada Lovelace")  # a new value, not a mutation
    assert repo.get("user-1") == ADA  # the store has not seen it
    repo.update(changed)
    assert repo.get("user-1") == changed


def test_service_runs_unchanged_on_both_implementations(repo: UserRepository) -> None:
    service = RegistrationService(repo, SequentialIdGenerator("user"))
    ada = service.register("  Ada@Example.com ", "Ada")
    assert ada == User("user-1", "ada@example.com", "Ada")  # normalised once, in the service
    with pytest.raises(ConflictError):
        service.register("ada@example.com", "Ada again")
    with pytest.raises(ValidationError):
        service.register("not-an-email", "Nobody")
    renamed = service.rename("user-1", "Ada Lovelace")
    assert renamed.name == "Ada Lovelace" and ada.name == "Ada"  # old value untouched
    assert repo.get("user-1") == renamed
    with pytest.raises(NotFoundError):
        service.rename("user-99", "Nobody")
    assert repo.count() == 1


def test_protocol_is_satisfied_by_shape_not_by_inheritance() -> None:
    for implementation in (InMemoryUserRepository(), SqliteUserRepository(connect())):
        assert isinstance(implementation, UserRepository)
        assert UserRepository not in type(implementation).__mro__
    assert not isinstance(object(), UserRepository)


def test_uniqueness_holds_under_concurrent_registration() -> None:
    repo = InMemoryUserRepository()

    def try_add(n: int) -> bool:
        try:
            repo.add(User(id=f"user-{n}", email="same@example.com", name=str(n)))
        except ConflictError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(try_add, range(200)))
    assert outcomes.count(True) == 1
    assert repo.count() == 1
    winner = repo.find_by_email("same@example.com")
    assert winner is not None and repo.get(winner.id) == winner


def test_generic_repository_serves_any_entity_through_a_key_function() -> None:
    books = InMemoryRepository[Book](key=lambda book: book.isbn)
    gof = Book("978-0201633610", "Design Patterns")
    books.add(gof)
    books.add(Book("978-0321127426", "Patterns of Enterprise Application Architecture"))
    assert isinstance(books, Repository)
    assert len(books) == 2 and books.get(gof.isbn) == gof
    assert sorted(book.isbn for book in books) == ["978-0201633610", "978-0321127426"]
    with pytest.raises(ConflictError):
        books.add(gof)
    books.remove(gof.isbn)
    with pytest.raises(NotFoundError):
        books.remove(gof.isbn)
    assert len(books) == 1
