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

The app selects all available public standard games in the chosen color by default. To limit the sample, enter a positive number of the player's most recent games; leave the field blank for unlimited. Invalid, nonstandard starting-position, and empty PGNs are skipped and reported. Positions are merged for statistics when piece placement, side to move, castling rights, and legal en-passant rights match, regardless of move order. The grid shows move counts across those transpositions and lists clickable alternate move orders reaching the current position. Opening names are taken from the games' PGN `Opening` or `ECOUrl` headers when available.

At each recorded position the grid includes the three best engine candidates and every move played from that position or a transposed position, ranked by estimated score for the side to move. The rank is among displayed candidates, not all legal moves; ties are broken by UCI move notation. Loss vs best is in pawns. A top engine choice can also have been played. Engine-only moves show the resulting position but have no further continuations unless that move was played in a transposed line. Engine suggestions and other recorded moves are searched in separate batches, so their scores can vary slightly with engine search conditions.

By default the tree includes up to **10 full moves** (20 half-moves); adjust this in the form. Drag a piece or select its source and destination squares to play a legal move, including one not found in the games. Those additional moves are analyzed on demand and do not count as played games. Hover or focus a move in the grid to see its arrow. Use the left and right arrow keys to navigate backward and forward through the selected line. The evaluation bar and grid show White's perspective: positive favors White. The opening name appears when present in the game headers.

Move classifications use engine loss versus the best displayed engine candidate, from the side-to-move perspective: **Excellent** 0–20 centipawns, **Inaccuracy** 21–100, **Mistake** 101–200, and **Blunder** above 200. A played move is **Book** when its game has an identified opening, it occurs within the first 10 full moves, and it loses no more than 20 centipawns. These are practical centipawn conventions; exact thresholds vary between chess platforms.

The **Lines of interest** tab lists recorded sequences where the selected player made a Mistake or Blunder, all our moves were Book or Excellent, and a recorded reply left the evaluation in our favor. Counts and dates refer to the exact sequence rather than transposed positions. Rows are ordered by frequency, then recency; click a line to explore it. The list ends each sequence at its first qualifying position and does not include engine-only moves.

Engine scores are estimates; depth and the sampled games affect results. Forced mate scores are represented as large centipawn values. The tool does not claim that a line is objectively best or use win/loss percentages.
