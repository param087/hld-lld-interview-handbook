from concurrent.futures import ThreadPoolExecutor

import pytest

from common import FakeClock, SequentialIdGenerator
from lld.chess.board import Board
from lld.chess.models import (
    CastlingRights,
    Color,
    GameOverError,
    GameStatus,
    IllegalMoveError,
    NotYourTurnError,
    PieceType,
    Player,
    Square,
)
from lld.chess.services import ChessGame

SCHOLARS_MATE = ["e2e4", "e7e5", "f1c4", "b8c6", "d1h5", "g8f6", "h5f7"]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(start=1_000_000)


def make_game(clock: FakeClock, board: Board | None = None) -> ChessGame:
    return ChessGame(
        Player("W", Color.WHITE),
        Player("B", Color.BLACK),
        board=board,
        clock=clock,
        ids=SequentialIdGenerator("G"),
    )


def test_opening_position_has_twenty_legal_moves_for_each_side(clock: FakeClock) -> None:
    game = make_game(clock)
    assert len(game.legal_moves()) == 20  # 16 pawn moves + 4 knight moves
    game.play("e2e4")
    assert game.side_to_move is Color.BLACK and len(game.legal_moves()) == 20
    assert game.board.en_passant == Square.of("e3")  # the double push set the target


def test_scholars_mate_ends_the_game_and_locks_it(clock: FakeClock) -> None:
    game = make_game(clock)
    for text in SCHOLARS_MATE:
        game.play(text)
    assert game.status is GameStatus.CHECKMATE and game.winner is Color.WHITE
    assert game.legal_moves() == [] and game.result() == "W wins by checkmate"
    with pytest.raises(GameOverError):
        game.play("e8f7")


# --8<-- [start:pin]
def test_a_pinned_knight_cannot_move(clock: FakeClock) -> None:
    # Black rook on e8, white king on e1: the knight on e4 is the only thing between them.
    board = Board.from_placement({"e1": "K", "e4": "N", "e8": "r", "h8": "k"})
    game = make_game(clock, board)
    assert game.status is GameStatus.ACTIVE  # geometry says the knight has 8 moves
    with pytest.raises(IllegalMoveError, match="leaves the white king in check"):
        game.play("e4d6")
    assert all(move.origin != Square.of("e4") for move in game.legal_moves())


# --8<-- [end:pin]


def test_stalemate_is_not_checkmate(clock: FakeClock) -> None:
    board = Board.from_placement({"a8": "k", "b5": "Q", "c6": "K"}, side_to_move=Color.WHITE)
    game = make_game(clock, board)
    game.play("b5b6")  # every black king move is covered, but the king is not in check
    assert game.status is GameStatus.STALEMATE
    assert game.winner is None and game.result() == "draw by stalemate"


# --8<-- [start:undo]
def test_undo_restores_the_captured_rook_and_the_castling_right(clock: FakeClock) -> None:
    board = Board.from_placement(
        {"e1": "K", "h1": "R", "e8": "k", "h8": "r", "a1": "R"},
        side_to_move=Color.BLACK,
        castling=CastlingRights(True, True, True, True),
    )
    game = make_game(clock, board)
    game.play("h8h1")  # the black rook takes on h1 and kills white's king-side castle
    assert game.board.castling.allows(Color.WHITE, king_side=True) is False
    assert game.board.piece_at(Square.of("h1")).color is Color.BLACK

    game.undo()
    restored = game.board.piece_at(Square.of("h1"))
    assert restored is not None and restored.color is Color.WHITE
    assert game.board.castling.allows(Color.WHITE, king_side=True) is True
    assert game.side_to_move is Color.BLACK and len(game.history) == 0


# --8<-- [end:undo]


def test_undo_after_checkmate_resumes_the_game_but_a_draw_is_final(clock: FakeClock) -> None:
    game = make_game(clock)
    for text in SCHOLARS_MATE:
        game.play(text)
    game.undo()
    assert game.status is GameStatus.ACTIVE and game.side_to_move is Color.WHITE
    game.agree_draw()
    with pytest.raises(GameOverError):
        game.undo()


@pytest.mark.parametrize(
    ("placement", "rights", "move", "expected"),
    [
        ({"e1": "K", "h1": "R", "e8": "k"}, CastlingRights(True, False, False, False), "e1g1", True),
        ({"e1": "K", "a1": "R", "e8": "k"}, CastlingRights(False, True, False, False), "e1c1", True),
        ({"e1": "K", "h1": "R", "e8": "k"}, CastlingRights(False, False, False, False), "e1g1", False),
        ({"e1": "K", "h1": "R", "e8": "k", "f8": "r"}, CastlingRights(True, False, False, False), "e1g1", False),
        ({"e1": "K", "h1": "R", "g1": "N", "e8": "k"}, CastlingRights(True, False, False, False), "e1g1", False),
    ],
)
def test_castling_rules(
    clock: FakeClock, placement: dict[str, str], rights: CastlingRights, move: str, expected: bool
) -> None:
    game = make_game(clock, Board.from_placement(placement, castling=rights))
    if not expected:
        with pytest.raises(IllegalMoveError):
            game.play(move)
        return
    game.play(move)
    king_side = move.endswith("g1")
    assert game.board.piece_at(Square.of("g1" if king_side else "c1")).piece_type is PieceType.KING
    assert game.board.piece_at(Square.of("f1" if king_side else "d1")).piece_type is PieceType.ROOK


@pytest.mark.parametrize("letter", ["q", "r", "b", "n"])
def test_promotion_needs_a_choice_and_creates_that_piece(clock: FakeClock, letter: str) -> None:
    game = make_game(clock, Board.from_placement({"a7": "P", "e1": "K", "e8": "k"}))
    with pytest.raises(IllegalMoveError, match="needs a choice"):
        game.play("a7a8")
    game.play(f"a7a8{letter}")
    promoted = game.board.piece_at(Square.of("a8"))
    assert promoted is not None and promoted.letter.lower() == letter and promoted.color is Color.WHITE


def test_en_passant_captures_the_pawn_that_is_not_on_the_target_square(clock: FakeClock) -> None:
    board = Board.from_placement(
        {"e5": "P", "d7": "p", "e1": "K", "e8": "k"}, side_to_move=Color.BLACK
    )
    game = make_game(clock, board)
    game.play("d7d5")  # double push past the white pawn
    assert game.board.en_passant == Square.of("d6")
    game.play("e5d6")
    assert game.board.piece_at(Square.of("d5")) is None  # the captured pawn was never on d6
    game.undo()
    assert game.board.piece_at(Square.of("d5")).color is Color.BLACK


def test_moving_the_other_side_is_rejected(clock: FakeClock) -> None:
    game = make_game(clock)
    with pytest.raises(NotYourTurnError):
        game.play("e7e5")
    with pytest.raises(IllegalMoveError):
        game.play("e2e5")  # a pawn cannot jump three squares


# --8<-- [start:concurrency]
def test_only_one_of_many_racing_moves_is_played(clock: FakeClock) -> None:
    game = make_game(clock)
    openings = ["e2e4", "d2d4", "g1f3", "b1c3", "c2c4", "f2f4", "a2a3", "h2h3"]

    def try_move(text: str) -> str | None:
        try:
            return str(game.play(text))
        except (NotYourTurnError, IllegalMoveError, GameOverError):
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(try_move, openings))

    played = [text for text in results if text is not None]
    assert len(played) == 1 and played[0] in openings  # the lock serialises the turn
    assert len(game.history) == 1 and game.side_to_move is Color.BLACK


# --8<-- [end:concurrency]
