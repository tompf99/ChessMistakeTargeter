# Chess Mistake Targeter

A local opening explorer for identifying mistakes in a Chess.com player's games. Enter the **player's** username and color; your side is the opposite color. The app fetches public standard-chess games and uses Stockfish evaluations, not game results.

## Run

Install Python 3.10+ and [Stockfish](https://stockfishchess.org/download/). Stockfish must be on `PATH`, in `/usr/games/stockfish`, or specified by the `STOCKFISH_PATH` environment variable.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000. Internet access is required to fetch Chess.com games; the engine and web server run locally. Analysis can take several minutes, especially with more games, moves, or engine depth.

## Reading the explorer

The app selects up to the requested number of the player's most recent public standard games in the chosen color (100 by default). Invalid, nonstandard starting-position, and empty PGNs are skipped and reported. The grid counts games sharing each exact move sequence in this sample, and sorts by frequency, then most recently played. Click a move to explore the next position; the filter shows paths leading to target positions.

By default the tree includes up to **10 full moves** (20 half-moves); adjust this in the form. Every position, including the initial position, is evaluated at the requested engine depth. Evaluations and changes are in pawns from the selected player's perspective: positive favors that player. A player's mistake is an adverse change of at least the configured threshold (default 100 centipawns, or one pawn) between consecutive positions. A **Target** line contains at least one such mistake and no drop at all in our side's evaluation. An **Avoid** line contains any drop in our side's evaluation, even if the player also made a mistake. The labels apply to the entire path to the position, not just its last move.

Engine scores are estimates; depth, threshold, and the sampled games affect results. Forced mate scores are represented as large centipawn values. The tool does not claim that a line is objectively best or use win/loss percentages.
