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
ENGINE_CHOICES = 3


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
                if limit is not None and len(selected) == limit:
                    return selected
    return selected


def new_node(san, fen):
    return {
        "move": san, "fen": fen, "games": 0, "last_played": 0,
        "children": {}, "evaluation": None, "swing": None,
        "rank": None, "loss": None, "engine_choice": False,
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


def evaluate_tree(root, engine, player_color, depth, threshold, max_moves):
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

    def candidate_scores(board, recorded_moves):
        legal_moves = list(board.legal_moves)
        if not legal_moves:
            return {}

        def read_scores(infos):
            scores = {}
            for info in infos:
                pv = info.get("pv")
                value = info["score"].pov(board.turn).score(mate_score=100000)
                if not pv or value is None:
                    raise RuntimeError("The engine did not return a usable move evaluation.")
                scores[pv[0]] = value
            return scores

        scores = read_scores(engine.analyse(
            board, chess.engine.Limit(depth=depth), multipv=min(ENGINE_CHOICES, len(legal_moves))
        ))
        missing = [move for move in recorded_moves if move not in scores]
        if missing:
            scores.update(read_scores(engine.analyse(
                board, chess.engine.Limit(depth=depth), multipv=len(missing), root_moves=missing
            )))
        if not scores or any(move not in scores for move in recorded_moves):
            raise RuntimeError("The engine did not evaluate all requested moves.")
        return scores

    def walk(node, ply=0, previous_score=None, opponent_mistake=False, our_mistake=False):
        node["evaluation"] = score(node["fen"])
        if previous_score is not None:
            node["swing"] = node["evaluation"] - previous_score
            # FEN's side to move is the opposite of the player who just moved.
            mover = not chess.Board(node["fen"]).turn
            opponent_mistake |= mover == player_color and node["swing"] <= -threshold
            our_mistake |= mover != player_color and node["swing"] > 0
        node["opponent_mistake"] = opponent_mistake
        node["our_mistake"] = our_mistake
        recorded = node["children"]
        node["children"] = []
        if ply >= max_moves * 2:
            return
        board = chess.Board(node["fen"])
        moves = [chess.Move.from_uci(uci) for uci in recorded]
        scores = candidate_scores(board, moves)
        if not scores:
            return
        ranked = sorted(scores, key=lambda move: (-scores[move], move.uci()))
        best = scores[ranked[0]]
        for index, move in enumerate(ranked):
            uci = move.uci()
            child = recorded.get(uci)
            if child is None:
                san = board.san(move)
                board.push(move)
                child = new_node(san, board.fen())
                board.pop()
            child["rank"] = index + 1
            child["loss"] = best - scores[move]
            child["engine_choice"] = index < ENGINE_CHOICES or not child["games"]
            if child["games"]:
                walk(child, ply + 1, node["evaluation"], opponent_mistake, our_mistake)
            else:
                child["children"] = []
                child["evaluation"] = score(child["fen"])
                child["swing"] = child["evaluation"] - node["evaluation"]
                child["opponent_mistake"] = opponent_mistake or (
                    board.turn == player_color and child["swing"] <= -threshold
                )
                child["our_mistake"] = our_mistake or (
                    board.turn != player_color and child["swing"] > 0
                )
            node["children"].append(child)

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
            evaluate_tree(root, engine, chess.WHITE if color == "white" else chess.BLACK, depth, threshold, max_moves)
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
