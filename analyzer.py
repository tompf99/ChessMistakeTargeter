"""Fetch Chess.com games and evaluate their opening tree."""

import io
import re
import shutil
import urllib.error
import urllib.request
import json
from datetime import datetime, timezone

import chess
import chess.engine
import chess.pgn


API = "https://api.chess.com/pub"
HEADERS = {"User-Agent": "ChessMistakeTargeter/1.0 (local opening analysis)"}
USERNAME = re.compile(r"^[A-Za-z0-9_-]{1,25}$")


def get_json(url):
    request = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ValueError("Chess.com user not found.") from exc
        raise RuntimeError(f"Chess.com returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Could not reach Chess.com: {exc}") from exc


def fetch_games(username, color, limit):
    archives = get_json(f"{API}/player/{username}/games/archives")["archives"]
    selected = []
    for archive in reversed(archives):
        games = get_json(archive)["games"]
        games.sort(key=lambda game: game.get("end_time", 0), reverse=True)
        for game in games:
            player = game.get(color, {}).get("username", "")
            if (player.casefold() == username.casefold()
                    and game.get("rules") == "chess" and game.get("pgn")):
                selected.append(game)
                if len(selected) == limit:
                    return selected
    return selected


def new_node(san, fen):
    return {
        "move": san, "fen": fen, "games": 0, "last_played": 0,
        "children": {}, "evaluation": None, "swing": None,
        "opponent_mistake": False, "our_mistake": False,
    }


def build_tree(games, max_moves):
    root = new_node("", chess.Board().fen())
    skipped = 0
    for game in games:
        try:
            parsed = chess.pgn.read_game(io.StringIO(game["pgn"]))
            if parsed is None or parsed.errors or parsed.board().fen() != chess.Board().fen():
                skipped += 1
                continue
            board = parsed.board()
            moves = []
            for move in parsed.mainline_moves():
                if len(moves) == max_moves * 2:
                    break
                san = board.san(move)
                board.push(move)
                moves.append((move.uci(), san, board.fen()))
        except (ValueError, KeyError, IndexError):
            skipped += 1
            continue
        if not moves:
            skipped += 1
            continue
        date = game.get("end_time", 0)
        node = root
        node["games"] += 1
        node["last_played"] = max(node["last_played"], date)
        for uci, san, fen in moves:
            node = node["children"].setdefault(uci, new_node(san, fen))
            node["games"] += 1
            node["last_played"] = max(node["last_played"], date)
    return root, skipped


def evaluate_tree(root, engine, player_color, depth, threshold):
    """Scores and swings are always from the chosen player's perspective."""
    cache = {}

    def score(fen):
        if fen not in cache:
            board = chess.Board(fen)
            info = engine.analyse(board, chess.engine.Limit(depth=depth))
            value = info["score"].pov(player_color).score(mate_score=100000)
            if value is None:
                raise RuntimeError("The engine did not return a usable evaluation.")
            cache[fen] = value
        return cache[fen]

    def walk(node, previous_score=None, opponent_mistake=False, our_mistake=False):
        node["evaluation"] = score(node["fen"])
        if previous_score is not None:
            node["swing"] = node["evaluation"] - previous_score
            # FEN's side to move is the opposite of the player who just moved.
            mover = not chess.Board(node["fen"]).turn
            opponent_mistake |= mover == player_color and node["swing"] <= -threshold
            our_mistake |= mover != player_color and node["swing"] > 0
        node["opponent_mistake"] = opponent_mistake
        node["our_mistake"] = our_mistake
        children = list(node["children"].values())
        children.sort(key=lambda child: (-child["games"], -child["last_played"], child["move"]))
        node["children"] = children
        for child in children:
            walk(child, node["evaluation"], opponent_mistake, our_mistake)

    walk(root)


def find_engine():
    import os
    configured = os.environ.get("STOCKFISH_PATH")
    if configured:
        return configured
    return shutil.which("stockfish") or "/usr/games/stockfish"


def analyze(username, color, max_moves, max_games, depth, threshold):
    if not USERNAME.fullmatch(username):
        raise ValueError("Enter a valid Chess.com username (1-25 letters, numbers, underscores or hyphens).")
    games = fetch_games(username, color, max_games)
    root, skipped = build_tree(games, max_moves)
    if not root["games"]:
        raise ValueError("No usable standard chess games found for that player and color.")
    try:
        with chess.engine.SimpleEngine.popen_uci(find_engine(), timeout=30) as engine:
            evaluate_tree(root, engine, chess.WHITE if color == "white" else chess.BLACK, depth, threshold)
    except FileNotFoundError as exc:
        raise RuntimeError("Stockfish was not found. Install it or set STOCKFISH_PATH.") from exc
    except (chess.engine.EngineError, OSError, TimeoutError) as exc:
        raise RuntimeError(f"Chess engine failed: {exc}") from exc
    return {
        "tree": root, "selected_games": len(games), "skipped_games": skipped,
        "username": username, "color": color, "max_moves": max_moves,
        "depth": depth, "threshold": threshold,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
