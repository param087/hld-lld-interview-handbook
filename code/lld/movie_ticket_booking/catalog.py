"""The read side: cities, cinemas, screens, movies and shows behind a Repository.

Nothing here mutates seat state, so the catalog needs only one coarse lock: it is
read constantly and written by an admin a few times a day.
"""

from __future__ import annotations

import threading
from typing import Protocol

from lld.movie_ticket_booking.models import Cinema, City, Movie, Screen, Show, ShowNotFoundError


# --8<-- [start:repository]
class ShowRepository(Protocol):
    """Collection-like persistence boundary. Swap in SQL without touching the services."""

    def add(self, show: Show) -> None: ...

    def get(self, show_id: str) -> Show: ...

    def list_all(self) -> list[Show]: ...

    def for_cinema(self, cinema_id: str) -> list[Show]: ...


class InMemoryShowRepository:
    """Dict-backed implementation used by the demo and the tests."""

    def __init__(self) -> None:
        self._shows: dict[str, Show] = {}
        self._lock = threading.Lock()

    def add(self, show: Show) -> None:
        with self._lock:
            self._shows[show.id] = show

    def get(self, show_id: str) -> Show:
        with self._lock:
            try:
                return self._shows[show_id]
            except KeyError:
                raise ShowNotFoundError(f"unknown show {show_id!r}") from None

    def list_all(self) -> list[Show]:
        with self._lock:
            return list(self._shows.values())

    def for_cinema(self, cinema_id: str) -> list[Show]:
        with self._lock:
            return [s for s in self._shows.values() if s.cinema_id == cinema_id]


# --8<-- [end:repository]


# --8<-- [start:catalog]
class Catalog:
    """Browse and search. The booking service depends on this, not on the dicts.

    ``_meta_lock`` guards the four registries below. It is never held while a seat
    lock is held, so the two lock families cannot form a cycle.
    """

    def __init__(self, shows: ShowRepository | None = None) -> None:
        self._shows = shows or InMemoryShowRepository()
        self._cities: dict[str, City] = {}
        self._cinemas: dict[str, Cinema] = {}
        self._movies: dict[str, Movie] = {}
        self._meta_lock = threading.Lock()

    def add_city(self, city: City) -> None:
        with self._meta_lock:
            self._cities[city.id] = city

    def add_cinema(self, cinema: Cinema) -> None:
        with self._meta_lock:
            self._cinemas[cinema.id] = cinema

    def add_movie(self, movie: Movie) -> None:
        with self._meta_lock:
            self._movies[movie.id] = movie

    def add_show(self, show: Show) -> None:
        self._shows.add(show)

    def show(self, show_id: str) -> Show:
        return self._shows.get(show_id)

    def shows(self) -> list[Show]:
        return self._shows.list_all()

    def movie(self, movie_id: str) -> Movie:
        with self._meta_lock:
            try:
                return self._movies[movie_id]
            except KeyError:
                raise ShowNotFoundError(f"unknown movie {movie_id!r}") from None

    def cinemas_in(self, city_id: str) -> list[Cinema]:
        with self._meta_lock:
            return sorted(
                (c for c in self._cinemas.values() if c.city_id == city_id), key=lambda c: c.name
            )

    def screen(self, cinema_id: str, screen_id: str) -> Screen:
        with self._meta_lock:
            cinema = self._cinemas.get(cinema_id)
        if cinema is None:
            raise ShowNotFoundError(f"unknown cinema {cinema_id!r}")
        for screen in cinema.screens:
            if screen.id == screen_id:
                return screen
        raise ShowNotFoundError(f"cinema {cinema_id} has no screen {screen_id!r}")

    def search(self, query: str, city_id: str | None = None) -> list[Show]:
        """Title substring match, optionally restricted to one city, earliest first."""
        needle = query.strip().lower()
        with self._meta_lock:
            matching = {m.id for m in self._movies.values() if needle in m.title.lower()}
            in_city = {
                c.id for c in self._cinemas.values() if city_id is None or c.city_id == city_id
            }
        found = [
            s for s in self._shows.list_all() if s.movie_id in matching and s.cinema_id in in_city
        ]
        return sorted(found, key=lambda s: (s.starts_at, s.id))


# --8<-- [end:catalog]
