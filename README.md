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

Open http://127.0.0.1:5000. Internet access is required to fetch Chess.com games; the engine and web server run locally. Unlimited analysis of large game histories can take a long time and use substantial memory, especially with more moves or higher engine depth. Set a game limit to reduce the work.

## Reading the explorer

The app selects all available public standard games in the chosen color by default. To limit the sample, enter a positive number of the player's most recent games; leave the field blank for unlimited. Invalid, nonstandard starting-position, and empty PGNs are skipped and reported. The grid counts games sharing each exact move sequence in this sample. Click a move to explore the next position; the filter shows paths leading to target positions.

At each recorded position the grid includes the three best engine candidates and every recorded move, ranked by estimated score for the side to move. The rank is among these displayed candidates, not all legal moves; ties are broken by UCI move notation. Loss vs best is the difference from the highest-scoring displayed candidate in pawns. To limit engine work, top choices and other recorded moves are searched in separate batches, so their estimates can vary slightly with engine search conditions. A top engine choice can also be a recorded move; a count of zero means the move wasn't played in the sample. Engine-only moves show the resulting position but do not have further continuations. Engine moves are not added past the configured line length.

By default the tree includes up to **10 full moves** (20 half-moves); adjust this in the form. Every position, including the initial position, is evaluated at the requested engine depth. Evaluations and changes are in pawns from the selected player's perspective: positive favors that player. These separate position evaluations determine line status; loss vs best is a separate comparison of candidate moves from the parent position. A player's mistake is an adverse change of at least the configured threshold (default 100 centipawns, or one pawn) between consecutive positions. A **Target** line contains at least one such mistake and no drop at all in our side's evaluation. An **Avoid** line contains any drop in our side's evaluation, even if the player also made a mistake. The labels apply to the entire path to the position, not just its last move.

Engine scores are estimates; depth, threshold, and the sampled games affect results. Forced mate scores are represented as large centipawn values. The tool does not claim that a line is objectively best or use win/loss percentages.

Many lines may show **Avoid** because even a one-centipawn drop after one of our moves marks the entire continuation as Avoid. Separate engine evaluations can fluctuate by small amounts, particularly at low depths, so such a drop is not necessarily a meaningful mistake. The configurable player mistake threshold does not apply to our moves.
