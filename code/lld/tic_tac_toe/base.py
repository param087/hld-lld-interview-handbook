"""The Template Method kernel shared by every turn-based game in this handbook.

``BoardGame.play`` fixes the order of a game once — set up, take turns until the
game is over, report a result — and leaves the rules to subclasses. Snake and
ladder (``lld.snake_and_ladder``) and the bowling scorer (``lld.bowling_alley``)
import this module, which is why it lives in the first board-game package rather
than in ``common``: it is domain vocabulary, not infrastructure.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from common import ConflictError, InvalidStateError, ValidationError

DEFAULT_TURN_LIMIT = 1_000


# --8<-- [start:status]
class GameStatus(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    WON = "won"
    DRAWN = "drawn"
    ABANDONED = "abandoned"


TERMINAL_STATUSES = frozenset({GameStatus.WON, GameStatus.DRAWN, GameStatus.ABANDONED})


class NotYourTurnError(ConflictError):
    """A player acted out of turn."""


class TurnLimitError(InvalidStateError):
    """The game ran past its turn limit; a rule or a bot is looping."""


@dataclass(frozen=True, slots=True)
class GameResult:
    status: GameStatus
    winner: str | None  # None for a draw or an abandoned game
    turns: int


@dataclass(frozen=True, slots=True)
class TurnCursor:
    """Memento of the base class's own state: everything a turn changes off the board."""

    status: GameStatus
    turn_index: int
    turns: int


# --8<-- [end:status]


# --8<-- [start:observer]
@dataclass(frozen=True, slots=True)
class GameEvent:
    """One immutable line of the game's story, pushed to observers."""

    turn: int
    actor: str | None
    text: str

    def __str__(self) -> str:
        return f"[{self.turn:>2}] {self.text}"


class GameObserver(Protocol):
    """Observer interface: renderers, logs and scoreboards implement exactly this."""

    def on_event(self, event: GameEvent) -> None: ...


class GameLog:
    """The plainest observer: it remembers every event. Reused by all three games."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[GameEvent] = []

    def on_event(self, event: GameEvent) -> None:
        with self._lock:
            self._events.append(event)

    def events(self) -> tuple[GameEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def lines(self) -> list[str]:
        return [str(event) for event in self.events()]


# --8<-- [end:observer]


# --8<-- [start:template]
class BoardGame[M](ABC):
    """The template. ``play`` and ``play_turn`` own the order of the steps; subclasses own the rules.

    Abstract steps (``choose_move``, ``apply_move``, ``is_over``, ``winner``) must be
    supplied by every game. Hooks (``setup``, ``after_move``, ``advance_turn``) have a
    default that most games keep. ``M`` is the game's move type: a ``Cell`` in
    tic-tac-toe, a dice roll in snake and ladder, a pin count in bowling.

    ``_lock`` guards the turn cursor and, by extension, whatever the subclass mutates
    inside ``apply_move``. It is an ``RLock`` because the hooks call back into public
    methods that lock again.
    """

    MIN_PLAYERS: int = 2
    MAX_PLAYERS: int = 8

    def __init__(self, players: Sequence[str], *, turn_limit: int = DEFAULT_TURN_LIMIT) -> None:
        if not self.MIN_PLAYERS <= len(players) <= self.MAX_PLAYERS:
            raise ValidationError(
                f"{type(self).__name__} takes {self.MIN_PLAYERS}-{self.MAX_PLAYERS} players, got {len(players)}"
            )
        if len(set(players)) != len(players):
            raise ValidationError("player names must be unique")
        if turn_limit < 1:
            raise ValidationError("turn_limit must be positive")
        self.players = tuple(players)
        self.turn_limit = turn_limit
        self._lock = threading.RLock()
        self._status = GameStatus.NOT_STARTED
        self._turn_index = 0
        self._turns = 0
        self._observers: list[GameObserver] = []
        self._event_buffer: list[GameEvent] = []

    # -- the template methods: never overridden -------------------------------------------
    def play(self) -> GameResult:
        """Play the whole game and report. The order of these steps is fixed here, for every game."""
        while self.status in (GameStatus.NOT_STARTED, GameStatus.IN_PROGRESS):
            self.play_turn()
        return self.result()

    def play_turn(self) -> None:
        """One turn: pick a move, apply it, let the game re-judge itself, rotate."""
        try:
            with self._lock:
                if self._status is GameStatus.NOT_STARTED:
                    self.start()
                if self._status is not GameStatus.IN_PROGRESS:
                    raise InvalidStateError(f"game is {self._status}; no turn can be played")
                if self._turns >= self.turn_limit:
                    self._status = GameStatus.ABANDONED
                    raise TurnLimitError(f"no result after {self._turns} turns")
                player = self.players[self._turn_index]
                move = self.choose_move(player)
                self.apply_move(player, move)
                self._turns += 1
                self.after_move(player, move)
                self._settle()
        finally:
            self.flush_events()

    def start(self) -> None:
        with self._lock:
            if self._status is not GameStatus.NOT_STARTED:
                raise InvalidStateError(f"game is already {self._status}")
            self.setup()
            self._status = GameStatus.IN_PROGRESS
            self.emit("game started")

    def abandon(self, reason: str) -> None:
        try:
            with self._lock:
                if self._status in TERMINAL_STATUSES:
                    raise InvalidStateError(f"game is already {self._status}")
                self._status = GameStatus.ABANDONED
                self.emit(f"abandoned: {reason}")
        finally:
            self.flush_events()

    def _settle(self) -> None:
        """Ask the rules whether the game ended; rotate the turn if it did not."""
        if self.is_over():
            champion = self.winner()
            self._status = GameStatus.WON if champion is not None else GameStatus.DRAWN
            self.emit(f"{champion} wins" if champion else "draw", actor=champion)
        else:
            self.advance_turn()

    # -- hooks: a default every game can live with -----------------------------------------
    def setup(self) -> None:
        """Called once, before the first turn. Games with nothing to reset keep this no-op."""
        return None

    def after_move(self, player: str, move: M) -> None:
        """Called after every applied move, before the game re-judges itself."""
        self.emit(f"{player} -> {move}", actor=player)

    def advance_turn(self) -> None:
        """Round-robin by default; bowling rotates only when a frame closes."""
        self._turn_index = (self._turn_index + 1) % len(self.players)

    # -- abstract steps: every game supplies them -----------------------------------------
    @abstractmethod
    def choose_move(self, player: str) -> M: ...

    @abstractmethod
    def apply_move(self, player: str, move: M) -> None: ...

    @abstractmethod
    def is_over(self) -> bool: ...

    @abstractmethod
    def winner(self) -> str | None: ...

    # -- shared state, read-only to callers -------------------------------------------------
    @property
    def status(self) -> GameStatus:
        with self._lock:
            return self._status

    @property
    def turns(self) -> int:
        with self._lock:
            return self._turns

    @property
    def current_player(self) -> str:
        with self._lock:
            return self.players[self._turn_index]

    def result(self) -> GameResult:
        with self._lock:
            champion = self.winner() if self._status is GameStatus.WON else None
            return GameResult(self._status, champion, self._turns)

    def require_turn(self, player: str) -> None:
        """Turn enforcement in one place: every externally driven move calls this first."""
        with self._lock:
            if player not in self.players:
                raise ValidationError(f"{player} is not in this game")
            if player != self.players[self._turn_index]:
                raise NotYourTurnError(f"it is {self.players[self._turn_index]}'s turn, not {player}'s")

    # -- the Memento the undo feature restores ---------------------------------------------
    def cursor(self) -> TurnCursor:
        with self._lock:
            return TurnCursor(self._status, self._turn_index, self._turns)

    def restore(self, cursor: TurnCursor) -> None:
        with self._lock:
            self._status = cursor.status
            self._turn_index = cursor.turn_index
            self._turns = cursor.turns

    # -- observers --------------------------------------------------------------------------
    def subscribe(self, observer: GameObserver) -> None:
        with self._lock:
            self._observers.append(observer)

    def emit(self, text: str, actor: str | None = None) -> None:
        """Buffer an event; ``flush_events`` delivers it once the lock is released."""
        with self._lock:
            self._event_buffer.append(GameEvent(self._turns, actor, text))

    def flush_events(self) -> None:
        """Deliver with no lock held, so a slow renderer can never stall the turn loop."""
        with self._lock:
            events, self._event_buffer = self._event_buffer, []
            observers = tuple(self._observers)
        for event in events:
            for observer in observers:
                observer.on_event(event)


# --8<-- [end:template]
