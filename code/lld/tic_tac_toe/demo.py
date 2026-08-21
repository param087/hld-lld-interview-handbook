"""A scripted win on the main diagonal, an undo that reopens the game, then perfect play."""

from common import HandbookError
from lld.tic_tac_toe.models import Cell, PlayerKind, Symbol
from lld.tic_tac_toe.services import BoardRenderer, PlayerFactory, TicTacToeGame


def scripted_game() -> tuple[TicTacToeGame, BoardRenderer]:
    cross = PlayerFactory.create(
        PlayerKind.HUMAN, "X", Symbol.CROSS, moves=[Cell(0, 0), Cell(1, 1), Cell(2, 2)]
    )
    nought = PlayerFactory.create(PlayerKind.HUMAN, "O", Symbol.NOUGHT, moves=[Cell(0, 1), Cell(0, 2)])
    game = TicTacToeGame([cross, nought])
    return game, BoardRenderer(game)


def main() -> None:
    game, renderer = scripted_game()
    print("--- game 1: X takes the main diagonal ---")
    result = game.play()
    for line in renderer.transcript():
        print(line)
    print(f"result: {result.status}, winner {result.winner}, {result.turns} turns")
    print(game.board.render())

    game.undo()
    print(f"--- undo: {game.status}, {game.current_player} to play, {game.turns} turns on the clock ---")
    for player, cell in (("O", Cell(2, 2)), ("X", Cell(0, 0))):
        try:
            game.submit_move(player, cell)
        except HandbookError as exc:
            print(f"{type(exc).__name__}: {exc}")

    print("--- game 2: minimax against minimax ---")
    bots = TicTacToeGame(
        [
            PlayerFactory.create(PlayerKind.PERFECT_BOT, "bot-X", Symbol.CROSS),
            PlayerFactory.create(PlayerKind.PERFECT_BOT, "bot-O", Symbol.NOUGHT),
        ]
    )
    perfect = bots.play()
    frames = len(bots.replay())
    print(f"result: {perfect.status} after {perfect.turns} turns; replay rebuilds {frames} frames")
    print(bots.board.render())


if __name__ == "__main__":
    main()
