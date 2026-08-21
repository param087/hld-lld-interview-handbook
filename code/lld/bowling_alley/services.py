"""The game on a lane, the alley that hands lanes out, and the live scoreboard."""

from __future__ import annotations

import threading
from collections.abc import Iterable, Sequence
from dataclasses import replace

from common import (
    Clock,
    IdGenerator,
    InvalidStateError,
    Money,
    SequentialIdGenerator,
    SystemClock,
    ValidationError,
)
from lld.bowling_alley.models import (
    FRAMES,
    Booking,
    BookingNotFoundError,
    Frame,
    FrameScore,
    FrameStatus,
    Lane,
    LaneUnavailableError,
    Standing,
)
from lld.bowling_alley.strategies import (
    PerGamePricing,
    PricingStrategy,
    ScoreCalculator,
    StandardScoring,
)
from lld.tic_tac_toe.base import DEFAULT_TURN_LIMIT, BoardGame, GameEvent


# --8<-- [start:game]
class BowlingGame(BoardGame[int]):
    """Ten-pin bowling on the shared ``BoardGame`` template, with one twist.

    Tic-tac-toe and snake and ladder rotate after every move. Bowling rotates after
    every *frame*, which is why ``advance_turn`` is a hook on the base class: a
    strike ends your turn after one ball, an open frame after two, and the tenth
    frame after three. Overriding one method buys all of that.
    """

    MIN_PLAYERS = 1
    MAX_PLAYERS = 6

    def __init__(
        self,
        players: Sequence[str],
        calculator: ScoreCalculator | None = None,
        *,
        turn_limit: int = DEFAULT_TURN_LIMIT,
    ) -> None:
        super().__init__(players, turn_limit=turn_limit)
        self._calculator = calculator or StandardScoring()
        self._cards: dict[str, list[Frame]] = {p: self._new_card() for p in self.players}
        self._frame_index = 0
        self._pending_pins: int | None = None

    @staticmethod
    def _new_card() -> list[Frame]:
        return [Frame(number, is_last=number == FRAMES) for number in range(1, FRAMES + 1)]

    # -- the template's steps ---------------------------------------------------------
    def setup(self) -> None:
        self._cards = {p: self._new_card() for p in self.players}
        self._frame_index = 0

    def choose_move(self, player: str) -> int:
        if self._pending_pins is None:
            raise InvalidStateError("a bowling game is driven by roll(pins), not by a bot")
        pins, self._pending_pins = self._pending_pins, None
        return pins

    def apply_move(self, player: str, move: int) -> None:
        self.current_frame(player).add(move)  # raises on an over-count or a finished frame

    def after_move(self, player: str, move: int) -> None:
        frame = self.current_frame(player)
        self.emit(f"{player} frame {frame.number}: {frame.marks()}", actor=player)

    def is_over(self) -> bool:
        return all(card[FRAMES - 1].is_complete() for card in self._cards.values())

    def winner(self) -> str | None:
        totals = {player: self.total(player) for player in self.players}
        best = max(totals.values())
        leaders = [player for player, total in totals.items() if total == best]
        return leaders[0] if len(leaders) == 1 else None  # a tie is a draw

    def advance_turn(self) -> None:
        """Hook override: the ball passes only when the roller's frame is finished."""
        if not self.current_frame(self.current_player).is_complete():
            return
        super().advance_turn()
        if self._turn_index == 0:
            self._frame_index += 1

    # -- what the template does not give you -------------------------------------------
    def roll(self, player: str, pins: int) -> FrameScore:
        """The pin-setter's call. Turn-checked, then validated against the standing pins."""
        with self._lock:
            self.require_turn(player)
            number = self.current_frame(player).number
            self._pending_pins = pins
            try:
                self.play_turn()
            finally:
                self._pending_pins = None
            return self.scorecard(player)[number - 1]

    def current_frame(self, player: str) -> Frame:
        return self._cards[player][self._frame_index]

    def card(self, player: str) -> tuple[Frame, ...]:
        with self._lock:
            return tuple(self._cards[player])

    def scorecard(self, player: str) -> list[FrameScore]:
        with self._lock:
            return self._calculator.score(self._cards[player])

    def total(self, player: str) -> int:
        return self.scorecard(player)[-1].running_total

    def standings(self) -> tuple[Standing, ...]:
        """A consistent snapshot of every card, computed under the lock."""
        with self._lock:
            return tuple(
                Standing(
                    player=player,
                    frame=self._frame_index + 1,
                    total=self.total(player),
                    final=all(s.status is FrameStatus.SCORED for s in self.scorecard(player)),
                    card=" ".join(f.marks() for f in self._cards[player] if f.rolls),
                )
                for player in self.players
            )


# --8<-- [end:game]


# --8<-- [start:scoreboard]
class Scoreboard:
    """Observer: a *live* board. It renders the newest standings, not the ones at emit time.

    That is deliberate. A scoreboard shows now; if you want the frame-by-frame history,
    subscribe a ``GameLog`` to the same game. Two observers, two different jobs, and
    the game knows about neither.
    """

    def __init__(self, game: BowlingGame) -> None:
        self._game = game
        self._lock = threading.Lock()
        self._rows: tuple[Standing, ...] = ()
        game.subscribe(self)

    def on_event(self, event: GameEvent) -> None:
        rows = self._game.standings()  # taken outside the game lock, by design
        with self._lock:
            self._rows = rows

    def rows(self) -> tuple[Standing, ...]:
        with self._lock:
            return self._rows

    def render(self) -> str:
        return "\n".join(
            f"{row.player:<5} frame {row.frame:>2}  {row.total:>3}{' ' if row.final else '*'}  {row.card}"
            for row in self.rows()
        )


# --8<-- [end:scoreboard]


# --8<-- [start:alley]
class BowlingAlley:
    """The house. It owns the lanes and hands them out one at a time.

    This is an Object Pool with a domain name: ``reserve`` acquires a lane, ``finish``
    returns it. ``_lock`` guards the lane statuses and the booking registry, and the
    race it prevents is two receptionists giving away the last lane. It is
    deliberately *not* a Singleton class - it is built once in ``main`` and injected,
    so tests build a dozen alleys and a second branch is a second object.
    """

    def __init__(
        self,
        name: str,
        lanes: Iterable[Lane],
        pricing: PricingStrategy | None = None,
        clock: Clock | None = None,
        ids: IdGenerator | None = None,
    ) -> None:
        self.name = name
        self._lanes: dict[str, Lane] = {lane.id: lane for lane in lanes}
        self._pricing = pricing or PerGamePricing()
        self._clock = clock or SystemClock()
        self._ids = ids or SequentialIdGenerator("BK")
        self._bookings: dict[str, Booking] = {}
        self._games: dict[str, BowlingGame] = {}
        self._lock = threading.Lock()

    def reserve(self, players: Sequence[str], games: int = 1, shoes: int = 0) -> Booking:
        """Claim the first free lane atomically, then price the booking."""
        if not BowlingGame.MIN_PLAYERS <= len(players) <= BowlingGame.MAX_PLAYERS:
            raise ValidationError(
                f"a game takes {BowlingGame.MIN_PLAYERS}-{BowlingGame.MAX_PLAYERS} players, got {len(players)}"
            )
        if games < 1 or shoes < 0 or shoes > len(players):
            raise ValidationError("games must be positive and shoes cannot exceed the party size")
        with self._lock:
            lane = next((candidate for candidate in self._lanes.values() if candidate.is_free()), None)
            if lane is None:
                raise LaneUnavailableError(f"no free lane at {self.name}")
            draft = Booking(
                id=self._ids.next_id(),
                lane_id=lane.id,
                players=tuple(players),
                games=games,
                shoes=shoes,
                price=Money(0),
                created_at=self._clock.now(),
            )
            booking = replace(draft, price=self._pricing.quote(draft))
            lane.reserve(booking.id)
            self._bookings[booking.id] = booking
            return booking

    def start_game(self, booking_id: str) -> BowlingGame:
        with self._lock:
            booking = self._booking(booking_id)
            self._lanes[booking.lane_id].start_play()
            game = BowlingGame(booking.players)
            self._games[booking_id] = game
            return game

    def finish(self, booking_id: str) -> None:
        """Return the lane to the pool. Releasing an already free lane is an error, not a no-op."""
        with self._lock:
            booking = self._booking(booking_id)
            self._lanes[booking.lane_id].release()
            self._games.pop(booking_id, None)

    def game(self, booking_id: str) -> BowlingGame:
        with self._lock:
            if booking_id not in self._games:
                raise BookingNotFoundError(f"no game running for booking {booking_id}")
            return self._games[booking_id]

    def lane(self, lane_id: str) -> Lane:
        with self._lock:
            if lane_id not in self._lanes:
                raise BookingNotFoundError(f"unknown lane {lane_id}")
            return self._lanes[lane_id]

    def free_lanes(self) -> int:
        with self._lock:
            return sum(1 for lane in self._lanes.values() if lane.is_free())

    def take_out_of_service(self, lane_id: str) -> None:
        with self._lock:
            self._lanes[lane_id].take_out_of_service()

    def reprice(self, booking: Booking) -> Booking:
        """Re-quote an existing booking, for example after a happy-hour rule is swapped in."""
        return replace(booking, price=self._pricing.quote(booking))

    def _booking(self, booking_id: str) -> Booking:
        if booking_id not in self._bookings:
            raise BookingNotFoundError(f"unknown booking {booking_id}")
        return self._bookings[booking_id]


# --8<-- [end:alley]
