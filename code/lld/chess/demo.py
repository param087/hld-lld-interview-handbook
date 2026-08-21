"""Scholar's mate, an undo that gives the pawn back, a pin, a castle and a promotion."""

from common import FakeClock, SequentialIdGenerator
from lld.chess.board import Board
from lld.chess.models import CastlingRights, Color, IllegalMoveError, Player, Square
from lld.chess.services import ChessGame


def new_game(clock: FakeClock, ids: SequentialIdGenerator, board: Board | None = None) -> ChessGame:
    return ChessGame(
        Player("Ada", Color.WHITE), Player("Linus", Color.BLACK), board=board, clock=clock, ids=ids
    )


def main() -> None:
    clock = FakeClock(start=1_700_000_000)
    ids = SequentialIdGenerator("G")
    game = new_game(clock, ids)
    print(f"{game.id}: white has {len(game.legal_moves())} legal moves in the opening position")

    for index, text in enumerate(["e2e4", "e7e5", "f1c4", "b8c6", "d1h5", "g8f6", "h5f7"]):
        played = game.play(text)
        clock.advance(30)
        print(f"{index // 2 + 1}. {played.color:<5} {played} -> {game.status}")
    print(f"{game.result()}; black has {len(game.legal_moves())} legal moves")

    game.undo()
    restored = game.board.piece_at(Square.of("f7"))
    print(f"undo h5f7 -> {game.status}, f7 holds a {restored.color} {restored.piece_type} again")

    pinned = new_game(clock, ids, Board.from_placement({"e1": "K", "e4": "N", "e8": "r", "h8": "k"}))
    try:
        pinned.play("e4d6")
    except IllegalMoveError as exc:
        print(f"{pinned.id}: {exc}")

    castling = new_game(
        clock,
        ids,
        Board.from_placement(
            {"e1": "K", "h1": "R", "e8": "k"}, castling=CastlingRights(True, False, False, False)
        ),
    )
    castling.play("e1g1")
    print(f"{castling.id}: e1g1 castles -> rank 1 is now {castling.board.row(0)}")

    promoting = new_game(clock, ids, Board.from_placement({"a7": "P", "e1": "K", "e8": "k"}))
    promoting.play("a7a8q")
    promoted = promoting.board.piece_at(Square.of("a8"))
    print(f"{promoting.id}: a7a8q -> {promoted.color} {promoted.piece_type} on a8, {promoting.status}")
    promoting.agree_draw()
    print(f"{promoting.id}: {promoting.result()}")

    pieces = [piece for _, piece in Board.standard().occupied()]
    print(f"{len(pieces)} pieces on the board share {len({id(p) for p in pieces})} Piece objects")


if __name__ == "__main__":
    main()
