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

Open http://127.0.0.1:5000. Internet access is required to fetch Chess.com games; the engine and web server run locally. Unlimited analysis of large game histories can take a long time and use substantial memory, especially with more moves or higher engine depth. Set a game limit to reduce the work. Progress shows monthly archives fetched and games whose opening analysis has completed. Games are analyzed from newest to oldest, reusing work for shared lines; games with no end date are processed last.

Preparing games parses their PGNs and builds position and transposition statistics. Downloaded archives are saved as each finishes, and completed engine searches are saved immediately, so interrupted work can be reused. The incremental cache uses SQLite write-ahead logging: committed searches survive an application interruption, but a power loss or operating-system crash may lose recent writes. Completed game lists and analysis trees are also cached on disk; matching analyses can skip rebuilding the tree. These caches persist across restarts and do not automatically refresh when new games are played.

## Reading the explorer

The app selects all available public standard games in the chosen color by default. To limit the sample, enter a positive number of the player's most recent games; leave the field blank for unlimited. Invalid, nonstandard starting-position, and empty PGNs are skipped and reported. Positions are merged for statistics when piece placement, side to move, castling rights, and legal en-passant rights match, regardless of move order. The grid shows move counts across those transpositions and lists clickable alternate move orders reaching the current position. Opening names use the latest named position in the current line, not the game's final PGN header. The bundled [Lichess chess-openings dataset](https://github.com/lichess-org/chess-openings) is public domain under CC0.

At each position the grid includes up to three engine candidates (or fewer if fewer legal moves exist) and recorded moves from that position or a transposition, ranked by estimated score for the side to move. The rank is among displayed candidates, not all legal moves; ties are broken by UCI move notation. Loss vs best is in pawns. A top engine choice can also have been played. Engine-only moves can be explored further. Engine suggestions and other recorded moves are searched in separate batches, so their scores can vary slightly with engine search conditions.

During and after the full analysis, the displayed position is evaluated separately: the initial search uses at least the requested depth minus two (minimum 1), followed by a deeper search up to four plies above the requested depth (maximum 20). Evaluations and losses can change as the search finishes.

By default the bulk analysis includes up to **10 full moves** (20 half-moves); adjust this in the form. A lower limit reduces the number of positions processed; recorded moves beyond the limit are not included. You can still explore legal moves beyond that limit on demand, including moves not found in the games; those additional moves do not count as played games. Drag a piece or select its source and destination squares to move. Hover or focus a move in the grid to see its arrow. Use the left and right arrow keys to navigate backward and forward through the selected line. The evaluation bar and grid show White's perspective: positive favors White.

Move classifications use engine loss versus the best displayed engine candidate, from the side-to-move perspective: **Excellent** 0–20 centipawns, **Inaccuracy** 21–100, **Mistake** 101–200, and **Blunder** above 200. A played move is **Book** when its game has an identified opening, it occurs within the first 10 full moves, and it loses no more than 20 centipawns. These are practical centipawn conventions; exact thresholds vary between chess platforms.

The **Lines of interest** tab lists recorded sequences where the selected player made a Mistake or Blunder, all our moves were Book or Excellent, and a recorded reply left the evaluation in our favor. Lines appear as the analysis finds them. Counts and dates refer to the exact sequence rather than transposed positions. Rows are ordered by frequency, then recency; click a line to explore it. The list ends each sequence at its first qualifying position and does not include engine-only moves.

Engine scores are estimates; depth and the sampled games affect results. Forced mate scores are represented as large centipawn values. The tool does not claim that a line is objectively best or use win/loss percentages.
