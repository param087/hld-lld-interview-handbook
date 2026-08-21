"""Pipeline and Middleware: ordered stages that transform or short-circuit a request.

The running example is an HTTP middleware chain. A ``Request`` travels inward
through ``LoggingMiddleware``, ``RateLimitMiddleware`` and ``AuthMiddleware``
to the terminal ``OrdersHandler``, and the ``Response`` travels back out through
the same stages in reverse. Any stage may short-circuit by answering without
calling the next one, and any stage may act on the response on its way out.
``MiddlewareChain`` folds the stages around the handler once, with
``functools.reduce``, so a request costs one call per stage. The second half
restates the idea as a linear data pipeline (``reduce`` over transforms) and as
stacked decorators, the Pythonic forms.
"""

from __future__ import annotations

import math
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import reduce

from common import Clock, FakeClock, ValidationError

MS_PER_SECOND = 1000
HANDLER_WORK_SECONDS = 0.003


# --8<-- [start:messages]
@dataclass(frozen=True, slots=True)
class Request:
    """What travels inward. Frozen: a stage transforms it by returning a copy."""

    method: str
    path: str
    client_id: str
    headers: Mapping[str, str] = field(default_factory=dict)

    def with_header(self, name: str, value: str) -> Request:
        return replace(self, headers={**self.headers, name: value})


@dataclass(frozen=True, slots=True)
class Response:
    """What travels back out."""

    status: int
    body: str
    headers: Mapping[str, str] = field(default_factory=dict)


# The rest of the chain folded into one callable: what a stage calls to continue.
type Handler = Callable[[Request], Response]
# --8<-- [end:messages]


# --8<-- [start:middleware]
class Middleware(ABC):
    """One layer of the onion.

    ``handle`` receives the request and ``call_next``, the rest of the chain folded
    into a single callable. A stage has three moves: transform the request and call
    on; answer without calling on (short-circuit); call on, then act on the response
    (post-process). It cannot see, skip or reorder the other stages.
    """

    @abstractmethod
    def handle(self, request: Request, call_next: Handler) -> Response: ...


@dataclass(frozen=True, slots=True)
class LogEntry:
    method: str
    path: str
    status: int
    elapsed_ms: float


class LoggingMiddleware(Middleware):
    """Outermost layer: never short-circuits, times everything inside it, records every exit.

    ``try/finally`` writes the entry even when an inner stage raises, as a 500.
    ``_lock`` protects ``_entries``: ``handle`` runs on whichever thread sent the request.
    """

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._entries: list[LogEntry] = []
        self._lock = threading.Lock()

    @property
    def entries(self) -> list[LogEntry]:
        with self._lock:
            return list(self._entries)

    def handle(self, request: Request, call_next: Handler) -> Response:
        started = self._clock.now()
        status = 500
        try:
            response = call_next(request)
            status = response.status
            return response
        finally:
            elapsed_ms = (self._clock.now() - started) * MS_PER_SECOND
            with self._lock:
                self._entries.append(LogEntry(request.method, request.path, status, elapsed_ms))


class RateLimitMiddleware(Middleware):
    """Short-circuits with 429 once a client exceeds ``limit`` requests per window.

    A fixed-window counter keyed by ``client_id``: the simplest limiter, enough to show
    the short-circuit (the rate limiter problem page has the real algorithms). It sits
    outside authentication on purpose, so a brute-force client is refused before any
    token lookup. ``_lock`` protects ``_windows``.
    """

    def __init__(self, limit: int, window_seconds: float, clock: Clock) -> None:
        if limit < 1 or window_seconds <= 0:
            raise ValidationError("limit must be at least 1 and the window positive")
        self._limit = limit
        self._window_seconds = window_seconds
        self._clock = clock
        self._windows: dict[str, tuple[int, int]] = {}  # client_id -> (window index, count)
        self._lock = threading.Lock()

    def handle(self, request: Request, call_next: Handler) -> Response:
        now = self._clock.now()
        window = int(now // self._window_seconds)
        with self._lock:
            seen_window, count = self._windows.get(request.client_id, (window, 0))
            if seen_window != window:
                count = 0
            if count >= self._limit:
                retry_after = math.ceil((window + 1) * self._window_seconds - now)
                return Response(429, "rate limited", {"Retry-After": str(retry_after)})
            self._windows[request.client_id] = (window, count + 1)
        return call_next(request)


class AuthMiddleware(Middleware):
    """Short-circuits with 401 unless the bearer token is known; otherwise names the user."""

    def __init__(self, users_by_token: Mapping[str, str]) -> None:
        self._users_by_token = dict(users_by_token)

    def handle(self, request: Request, call_next: Handler) -> Response:
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        user = self._users_by_token.get(token)
        if user is None:
            return Response(401, "unauthorized")
        return call_next(request.with_header("X-User", user))


# --8<-- [end:middleware]


# --8<-- [start:chain]
class OrdersHandler:
    """The terminal handler: the application code every layer protects.

    ``work`` stands in for the database call (the demo advances a fake clock with it),
    so the log can show that a short-circuited request costs nothing. ``_lock``
    protects ``_calls``.
    """

    def __init__(self, work: Callable[[], None] | None = None) -> None:
        self._work = work
        self._calls = 0
        self._lock = threading.Lock()

    @property
    def calls(self) -> int:
        with self._lock:
            return self._calls

    def __call__(self, request: Request) -> Response:
        with self._lock:
            self._calls += 1
        if self._work is not None:
            self._work()
        user = request.headers.get("X-User", "anonymous")
        return Response(200, f"{user}: {request.method} {request.path}")


class MiddlewareChain:
    """The pipeline object: owns the order and folds the stages around the handler once.

    ``reduce`` walks the list from the innermost stage outward, wrapping each around
    the callable built so far, so the first stage in the list is the outermost layer:
    first to see the request, last to see the response. The fold runs at construction;
    a request costs one call per stage and no list walk.
    """

    def __init__(self, middlewares: Sequence[Middleware], handler: Handler) -> None:
        self._middlewares = tuple(middlewares)
        self._handler = handler
        self._entry: Handler = reduce(self._wrap, reversed(self._middlewares), handler)

    @staticmethod
    def _wrap(call_next: Handler, middleware: Middleware) -> Handler:
        def layer(request: Request) -> Response:
            return middleware.handle(request, call_next)

        return layer

    @property
    def layers(self) -> tuple[str, ...]:
        """Outermost first, the handler last."""
        names = [type(middleware).__name__ for middleware in self._middlewares]
        handler_name = getattr(self._handler, "__name__", type(self._handler).__name__)
        return (*names, handler_name)

    def __call__(self, request: Request) -> Response:
        return self._entry(request)


# --8<-- [end:chain]


# --8<-- [start:pythonic]
# A linear pipeline: every stage is a transform, and one stage's output is the next one's input.
type Stage[T] = Callable[[T], T]


def pipeline[T](*stages: Stage[T]) -> Stage[T]:
    """Compose left to right: ``pipeline(a, b, c)(x) == c(b(a(x)))``. No stage can stop the line."""
    return lambda value: reduce(lambda acc, stage: stage(acc), stages, value)


def collapse_spaces(text: str) -> str:
    return " ".join(text.split())


def truncate(limit: int) -> Stage[str]:
    return lambda text: text if len(text) <= limit else text[: limit - 3] + "..."


# Middleware as a decorator: a function from handler to handler. Stacking is composition.
type HandlerDecorator = Callable[[Handler], Handler]


def require_header(name: str) -> HandlerDecorator:
    def decorate(call_next: Handler) -> Handler:
        def layer(request: Request) -> Response:
            if name not in request.headers:
                return Response(400, f"missing {name}")
            return call_next(request)

        return layer

    return decorate


def stack(handler: Handler, *decorators: HandlerDecorator) -> Handler:
    """Outermost first, like ``MiddlewareChain``: ``stack(h, a, b) == a(b(h))``."""
    return reduce(lambda inner, decorate: decorate(inner), reversed(decorators), handler)


# --8<-- [end:pythonic]


def main() -> None:
    clock = FakeClock(start=0.0)
    log = LoggingMiddleware(clock)
    handler = OrdersHandler(work=lambda: clock.advance(HANDLER_WORK_SECONDS))
    chain = MiddlewareChain(
        [
            log,
            RateLimitMiddleware(limit=2, window_seconds=60, clock=clock),
            AuthMiddleware({"t-alice": "alice"}),
        ],
        handler,
    )
    print(f"--- {' -> '.join(chain.layers)} ---")
    alice = {"Authorization": "Bearer t-alice"}
    requests = [
        Request("GET", "/orders", "10.0.0.1", alice),
        Request("GET", "/orders/7", "10.0.0.1", {"Authorization": "Bearer stale"}),
        Request("POST", "/orders", "10.0.0.1", alice),
        Request("GET", "/orders", "10.0.0.2"),
    ]
    for request in requests:
        response = chain(request)
        retry = response.headers.get("Retry-After")
        suffix = f" (Retry-After: {retry} s)" if retry else ""
        print(f"  {request.client_id} {request.method:<4} {request.path:<9} -> {response.status} {response.body}{suffix}")
    print(f"  handler calls: {handler.calls} of {len(requests)} (the rest were answered by a stage)")

    print("--- the log, written on the way out, covers every exit ---")
    for entry in log.entries:
        print(f"  {entry.method:<4} {entry.path:<9} {entry.status} in {entry.elapsed_ms:.1f} ms")

    print("--- a minute later the window has reset ---")
    clock.advance(60)
    response = chain(requests[2])
    print(f"  POST /orders -> {response.status} {response.body}; handler calls: {handler.calls}")

    print("--- a handler that raises still gets a log line ---")

    def broken(request: Request) -> Response:
        raise RuntimeError("database down")

    fragile = MiddlewareChain([log], broken)
    try:
        fragile(requests[0])
    except RuntimeError as exc:
        last = log.entries[-1]
        print(f"  RuntimeError: {exc}; logged as {last.status} in {last.elapsed_ms:.1f} ms")

    print("--- pythonic: a linear pipeline folded with reduce ---")
    normalise = pipeline(str.strip, collapse_spaces, str.casefold, truncate(12))
    raw = "  Hello   WORLD, again  "
    print(f"  {raw!r} -> {normalise(raw)!r}")

    print("--- pythonic: middleware as stacked decorators ---")
    traced = stack(handler, require_header("X-Request-Id"))
    plain = Request("GET", "/orders", "10.0.0.1")
    without = traced(plain)
    with_id = traced(plain.with_header("X-Request-Id", "r-1"))
    print(f"  without X-Request-Id: {without.status} {without.body}")
    print(f"  with X-Request-Id:    {with_id.status} {with_id.body}")


if __name__ == "__main__":
    main()
