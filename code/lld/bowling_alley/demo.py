"""Reserve a lane, bowl two cards to the end, and watch the provisional totals settle."""

from collections import deque

from common import FakeClock, HandbookError, SequentialIdGenerator
from lld.bowling_alley.models import Lane
from lld.bowling_alley.services import BowlingAlley, BowlingGame, Scoreboard
from lld.bowling_alley.strategies import HappyHourPricing, PerGamePricing
from lld.tic_tac_toe.base import GameStatus

ANA = [1, 4, 4, 5, 6, 4, 5, 5, 10, 0, 1, 7, 3, 6, 4, 10, 2, 8, 6]
BO = [10] * 12  # nine strikes, then three more balls in the tenth: the perfect 300


def bowl(game: BowlingGame, queues: dict[str, deque[int]], until_frame: int) -> None:
    while game.status in (GameStatus.NOT_STARTED, GameStatus.IN_PROGRESS):
        if game.standings()[0].frame >= until_frame:
            return
        player = game.current_player
        game.roll(player, queues[player].popleft())


def main() -> None:
    alley = BowlingAlley(
        "Sunset Lanes",
        [Lane(f"L{i}") for i in range(1, 4)],
        pricing=PerGamePricing(),
        clock=FakeClock(start=1_700_000_000),
        ids=SequentialIdGenerator("BK"),
    )
    booking = alley.reserve(["Ana", "Bo"], games=1, shoes=2)
    discounted = HappyHourPricing(PerGamePricing(), 20).quote(booking)
    print(f"--- {alley.name}: {booking.id} on {booking.lane_id}, {alley.free_lanes()} lanes still free ---")
    print(f"price {booking.price} for 2 players x 1 game plus 2 pairs of shoes; happy hour {discounted}")

    game = alley.start_game(booking.id)
    board = Scoreboard(game)
    queues = {"Ana": deque(ANA), "Bo": deque(BO)}

    bowl(game, queues, until_frame=5)
    print("--- after four frames, a star marks a total that later balls can still move ---")
    print(board.render())

    bowl(game, queues, until_frame=99)
    print("--- final cards ---")
    print(board.render())
    result = game.result()
    print(f"{result.status}: {result.winner} beats Ana {game.total('Bo')} to {game.total('Ana')} "
          f"in {result.turns} rolls")
    print(f"tenth frame: Ana threw {len(game.card('Ana')[9].rolls)}, Bo threw {len(game.card('Bo')[9].rolls)}")

    print("--- rejections ---")
    fresh = BowlingGame(["Zed"])
    fresh.roll("Zed", 7)
    attempts = (
        ("too many pins", lambda: fresh.roll("Zed", 5)),
        ("game over", lambda: game.roll(game.current_player, 1)),
    )
    for label, attempt in attempts:
        try:
            attempt()
        except HandbookError as exc:
            print(f"{label}: {type(exc).__name__}: {exc}")

    alley.finish(booking.id)
    print(f"lane {booking.lane_id} is {alley.lane(booking.lane_id).status}, {alley.free_lanes()} lanes free")


if __name__ == "__main__":
    main()
