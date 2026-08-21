"""Pipeline and Middleware: inward and outward travel, short-circuits, order as policy, the fold."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, ValidationError
from patterns.pipeline_middleware import (
    AuthMiddleware,
    Handler,
    LoggingMiddleware,
    Middleware,
    MiddlewareChain,
    OrdersHandler,
    RateLimitMiddleware,
    Request,
    Response,
    collapse_spaces,
    pipeline,
    require_header,
    stack,
    truncate,
)

ALICE = {"Authorization": "Bearer t-alice"}
STALE = {"Authorization": "Bearer stale"}


def build(limit: int = 2) -> tuple[FakeClock, LoggingMiddleware, OrdersHandler, MiddlewareChain]:
    clock = FakeClock(start=0.0)
    log = LoggingMiddleware(clock)
    handler = OrdersHandler(work=lambda: clock.advance(0.003))
    stages = [log, RateLimitMiddleware(limit, 60, clock), AuthMiddleware({"t-alice": "alice"})]
    return clock, log, handler, MiddlewareChain(stages, handler)


def test_request_goes_inward_and_the_response_comes_back_out() -> None:
    _, log, handler, chain = build()
    response = chain(Request("GET", "/orders", "10.0.0.1", ALICE))
    assert response == Response(200, "alice: GET /orders")  # auth transformed the request on the way in
    assert handler.calls == 1
    assert log.entries[0].status == 200 and log.entries[0].elapsed_ms == pytest.approx(3.0)


def test_auth_short_circuits_and_the_handler_is_never_called() -> None:
    _, log, handler, chain = build()
    response = chain(Request("GET", "/orders", "10.0.0.1", STALE))
    assert response.status == 401 and handler.calls == 0
    assert [entry.status for entry in log.entries] == [401]  # the outer layer still saw the exit


def test_rate_limit_short_circuits_with_retry_after_and_resets_next_window() -> None:
    clock, _, handler, chain = build(limit=2)
    request = Request("GET", "/orders", "10.0.0.1", ALICE)
    assert [chain(request).status for _ in range(3)] == [200, 200, 429]
    assert chain(request).headers["Retry-After"] == "60" and handler.calls == 2
    clock.advance(60)
    assert chain(request).status == 200


def test_first_in_the_list_is_outermost_so_order_is_policy() -> None:
    clock = FakeClock()
    bad, good = Request("GET", "/o", "10.0.0.1", STALE), Request("GET", "/o", "10.0.0.1", ALICE)
    users = {"t-alice": "alice"}
    limit_first = MiddlewareChain([RateLimitMiddleware(1, 60, clock), AuthMiddleware(users)], OrdersHandler())
    auth_first = MiddlewareChain([AuthMiddleware(users), RateLimitMiddleware(1, 60, clock)], OrdersHandler())
    assert (limit_first(bad).status, limit_first(good).status) == (401, 429)  # bad token used the allowance
    assert (auth_first(bad).status, auth_first(good).status) == (401, 200)  # rejected before being counted
    assert limit_first.layers == ("RateLimitMiddleware", "AuthMiddleware", "OrdersHandler")


def test_logging_records_an_exception_as_500_and_lets_it_propagate() -> None:
    log = LoggingMiddleware(FakeClock())

    def broken(request: Request) -> Response:
        raise RuntimeError("database down")

    chain = MiddlewareChain([log], broken)
    with pytest.raises(RuntimeError):
        chain(Request("GET", "/orders", "10.0.0.1"))
    assert [entry.status for entry in log.entries] == [500]
    assert chain.layers == ("LoggingMiddleware", "broken")


def test_empty_chain_is_the_handler_and_a_test_stage_can_post_process_the_response() -> None:
    class Tagging(Middleware):
        def handle(self, request: Request, call_next: Handler) -> Response:
            response = call_next(request.with_header("X-User", "tagged"))
            return Response(response.status, response.body, {**response.headers, "X-Served-By": "tagging"})

    handler = OrdersHandler()
    assert MiddlewareChain([], handler)(Request("GET", "/", "c")) == Response(200, "anonymous: GET /")
    response = MiddlewareChain([Tagging()], handler)(Request("GET", "/", "c"))
    assert response.body == "tagged: GET /" and response.headers == {"X-Served-By": "tagging"}


def test_rate_limiter_counts_exactly_under_concurrent_requests() -> None:
    handler = OrdersHandler()
    chain = MiddlewareChain([RateLimitMiddleware(100, 60, FakeClock())], handler)
    request = Request("GET", "/orders", "10.0.0.1")
    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(lambda _: chain(request).status, range(400)))
    assert statuses.count(200) == 100 and statuses.count(429) == 300
    assert handler.calls == 100


@pytest.mark.parametrize(("limit", "window"), [(0, 60), (1, 0), (1, -5)])
def test_rate_limiter_validates_its_configuration(limit: int, window: float) -> None:
    with pytest.raises(ValidationError):
        RateLimitMiddleware(limit, window, FakeClock())


def test_linear_pipeline_folds_left_to_right() -> None:
    normalise = pipeline(str.strip, collapse_spaces, str.casefold, truncate(12))
    assert normalise("  Hello   WORLD, again  ") == "hello wor..."
    assert pipeline()("unchanged") == "unchanged"


def test_stacked_decorators_are_middleware_and_the_at_syntax_is_the_same_thing() -> None:
    stacked = stack(OrdersHandler(), require_header("X-Request-Id"), require_header("X-Tenant"))
    assert stacked(Request("GET", "/", "c")).body == "missing X-Request-Id"  # outermost first
    assert stacked(Request("GET", "/", "c", {"X-Request-Id": "r"})).body == "missing X-Tenant"
    assert stacked(Request("GET", "/", "c", {"X-Request-Id": "r", "X-Tenant": "t"})).status == 200

    @require_header("X-Request-Id")
    def decorated(request: Request) -> Response:
        return Response(204, "")

    assert decorated(Request("GET", "/", "c")).status == 400
    assert decorated(Request("GET", "/", "c", {"X-Request-Id": "r"})).status == 204
