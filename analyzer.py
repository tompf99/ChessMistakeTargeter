"""Fetch Chess.com games and evaluate their opening tree."""

import io
import csv
import re
import shutil
import urllib.error
import urllib.request
import json
from collections import defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import chess
import chess.engine
import chess.pgn


API = "https://api.chess.com/pub"
HEADERS = {"User-Agent": "ChessMistakeTargeter/1.0 (local opening analysis)"}
USERNAME = re.compile(r"^[A-Za-z0-9_-]{1,25}$")
ENGINE_CHOICES = 3
EXCELLENT_MAX_LOSS = 20
INACCURACY_MAX_LOSS = 100
MISTAKE_MAX_LOSS = 200


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


def fetch_games(username, color, limit, progress=None):
    archives = get_json(f"{API}/player/{username}/games/archives")["archives"]
    selected = []
    if progress:
        progress("fetch", 0, len(archives))
    for index, archive in enumerate(reversed(archives), 1):
        games = get_json(archive)["games"]
        games.sort(key=lambda game: game.get("end_time", 0), reverse=True)
        for game in games:
            player = game.get(color, {}).get("username", "")
            if (player.casefold() == username.casefold()
                    and game.get("rules") == "chess" and game.get("pgn")):
                selected.append(game)
                if limit is not None and len(selected) == limit:
                    if progress:
                        progress("fetch", index, len(archives))
                    return selected
        if progress:
            progress("fetch", index, len(archives))
    return selected


def new_node(san, fen, uci=None):
    return {
        "move": san, "uci": uci, "fen": fen, "games": 0, "position_games": 0,
        "path_games": 0, "path_last_played": 0,
        "last_played": 0, "opening_name": None, "transpositions": [],
        "children": {}, "evaluation": None, "swing": None,
        "rank": None, "loss": None, "engine_choice": False,
        "status": None, "player_move": False, "position_key": None,
    }


def position_key(board):
    ep_square = board.ep_square if board.has_legal_en_passant() else None
    return (board.board_fen(), board.turn, board.castling_xfen(), ep_square)


@lru_cache(maxsize=1)
def opening_positions():
    positions = {}
    directory = Path(__file__).parent / "data" / "openings"
    for volume in "abcde":
        with (directory / f"{volume}.tsv").open(encoding="utf-8", newline="") as source:
            for entry in csv.DictReader(source, delimiter="\t"):
                board = chess.Board()
                for token in entry["pgn"].split():
                    if not token.endswith("."):
                        board.push_san(token)
                positions[position_key(board)] = entry["name"]
    return positions


def format_line(moves):
    result = []
    for index, move in enumerate(moves):
        if index % 2 == 0:
            result.append(f"{index // 2 + 1}.")
        result.append(move)
    return " ".join(result)


def build_tree(games, max_moves):
    root = new_node("", chess.Board().fen())
    skipped = 0
    position_stats = defaultdict(lambda: {
        "games": set(), "last_played": 0, "routes": defaultdict(set),
    })
    edge_stats = defaultdict(lambda: {"games": set(), "last_played": 0})

    def add_position(board, game_id, date, path):
        key = position_key(board)
        stat = position_stats[key]
        stat["games"].add(game_id)
        stat["last_played"] = max(stat["last_played"], date)
        stat["routes"][tuple(path)].add(game_id)
        return key

    for game_id, game in enumerate(games):
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
                source_key = position_key(board)
                board.push(move)
                moves.append((move.uci(), san, board.fen(), source_key, position_key(board)))
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
        node["path_games"] += 1
        node["path_last_played"] = max(node["path_last_played"], date)
        board = chess.Board()
        path = []
        node["position_key"] = add_position(board, game_id, date, path)
        node["_path"] = tuple(path)
        for uci, san, fen, source_key, target_key in moves:
            edge = edge_stats[(source_key, uci)]
            edge["games"].add(game_id)
            edge["last_played"] = max(edge["last_played"], date)
            path.append(san)
            node = node["children"].setdefault(uci, new_node(san, fen))
            node["games"] += 1
            node["last_played"] = max(node["last_played"], date)
            node["path_games"] += 1
            node["path_last_played"] = max(node["path_last_played"], date)
            node["uci"] = uci
            node["position_key"] = target_key
            node["_path"] = tuple(path)
            board.push_uci(uci)
            add_position(board, game_id, date, path)
        node["_finished_games"] = node.get("_finished_games", 0) + 1

    # Preserve the trie edge keys while adding aggregate position metadata.
    def prepare(node):
        child_map = node["children"]
        for uci, child in child_map.items():
            edge = edge_stats.get((node["position_key"], uci))
            if edge:
                child["games"] = len(edge["games"])
                child["last_played"] = edge["last_played"]
            prepare(child)
        stat = position_stats.get(node["position_key"])
        if stat:
            node["position_games"] = len(stat["games"])
            routes = sorted(stat["routes"].items(), key=lambda item: (-len(item[1]), item[0]))
            node["transpositions"] = [
                {"line": format_line(route), "moves": list(route), "games": len(route_games)}
                for route, route_games in routes if route != node.get("_path", ())
            ][:8]
        node["children"] = child_map

    prepare(root)
    root["_analysis"] = {"edge_stats": edge_stats, "position_stats": position_stats}
    return root, skipped


def classify_move(loss, is_book=False):
    if is_book:
        return "Book"
    if loss <= EXCELLENT_MAX_LOSS:
        return "Excellent"
    if loss <= INACCURACY_MAX_LOSS:
        return "Inaccuracy"
    if loss <= MISTAKE_MAX_LOSS:
        return "Mistake"
    return "Blunder"


def evaluate_tree(root, engine, player_color, depth, max_moves, start_ply=0, progress=None):
    """Scores and swings are always from the chosen player's perspective."""
    cache = {}
    analysis = root.pop("_analysis", {"edge_stats": {}, "position_stats": {}})
    edge_stats = analysis["edge_stats"]
    position_stats = analysis["position_stats"]
    openings = opening_positions()
    completed_games = 0
    total_games = root["path_games"]
    if progress:
        progress("analysis", 0, total_games)
    moves_by_position = defaultdict(list)
    for source_key, uci in edge_stats:
        moves_by_position[source_key].append(uci)

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

    def apply_position_stats(node, route):
        stat = position_stats.get(position_key(chess.Board(node["fen"])))
        if not stat:
            return
        node["position_games"] = len(stat["games"])
        node["transpositions"] = [
            {"line": format_line(path), "moves": list(path), "games": len(route_games)}
            for path, route_games in sorted(
                stat["routes"].items(), key=lambda item: (-len(item[1]), item[0])
            ) if path != tuple(route)
        ][:8]

    def finish(node):
        nonlocal completed_games
        completed_games += node.get("_finished_games", 0)
        if progress and node.get("_finished_games", 0):
            progress("analysis", completed_games, total_games)

    def walk(node, ply=0, route=(), previous_opening=None):
        node["evaluation"] = score(node["fen"])
        node["opening_name"] = openings.get(position_key(chess.Board(node["fen"])), previous_opening)
        recorded = node["children"]
        node["children"] = []
        apply_position_stats(node, route)
        if ply >= max_moves * 2:
            finish(node)
            return
        board = chess.Board(node["fen"])
        transposed_moves = [chess.Move.from_uci(uci) for uci in moves_by_position[position_key(board)]]
        moves = sorted(set(transposed_moves + [
            chess.Move.from_uci(uci) for uci in recorded
        ]), key=lambda move: move.uci())
        scores = candidate_scores(board, moves)
        if not scores:
            finish(node)
            return
        ranked = sorted(scores, key=lambda move: (-scores[move], move.uci()))
        best = scores[ranked[0]]
        for index, move in enumerate(ranked):
            uci = move.uci()
            child = recorded.get(uci)
            if child is None:
                san = board.san(move)
                board.push(move)
                child = new_node(san, board.fen(), uci)
                board.pop()
                child["position_key"] = position_key(chess.Board(child["fen"]))
                edge = edge_stats.get((position_key(board), uci))
                if edge:
                    child["games"] = len(edge["games"])
                    child["last_played"] = edge["last_played"]
            child["rank"] = index + 1
            child["loss"] = best - scores[move]
            child["engine_choice"] = index < ENGINE_CHOICES
            child["player_move"] = board.turn == player_color
            child["opening_name"] = openings.get(position_key(chess.Board(child["fen"])), node["opening_name"])
            child_route = route + (child["move"],)
            apply_position_stats(child, child_route)
            child["status"] = classify_move(
                child["loss"],
                is_book=bool(
                    child["games"] and ply < 20 and child["opening_name"]
                    and child["loss"] <= EXCELLENT_MAX_LOSS
                ),
            )
            if child["games"] and uci in recorded:
                walk(child, ply + 1, child_route, node["opening_name"])
            else:
                child["children"] = []
                child["evaluation"] = score(child["fen"])
            node["children"].append(child)
        finish(node)

    walk(root, start_ply)

    def remove_internal_fields(node):
        node.pop("_path", None)
        node.pop("_finished_games", None)
        node.pop("position_key", None)
        for child in node["children"]:
            remove_internal_fields(child)
    remove_internal_fields(root)


def lines_of_interest(root):
    """Find recorded lines with a player mistake and an effective recorded reply."""
    lines = []

    def walk(node, route, saw_mistake=False, replied=False):
        for child in node["children"]:
            if not child["path_games"]:
                continue
            if not child["player_move"] and child["status"] not in ("Book", "Excellent"):
                continue
            mistake = child["player_move"] and child["status"] in ("Mistake", "Blunder")
            has_mistake = saw_mistake or mistake
            has_reply = replied or (saw_mistake and not child["player_move"])
            path = route + [child]
            if has_mistake and has_reply and child["evaluation"] < 0:
                lines.append({
                    "moves": [item["uci"] for item in path],
                    "line": format_line([item["move"] for item in path]),
                    "games": child["path_games"],
                    "last_played": child["path_last_played"],
                    "evaluation": child["evaluation"],
                    "opening_name": child["opening_name"],
                })
            else:
                walk(child, path, has_mistake, has_reply)

    walk(root, [])
    return sorted(lines, key=lambda item: (-item["games"], -item["last_played"], item["line"]))


def explore_move(fen, uci, color, depth, max_moves):
    try:
        board = chess.Board(fen)
        move = chess.Move.from_uci(uci)
    except ValueError as exc:
        raise ValueError("Invalid position or move.") from exc
    if not board.is_valid() or board.is_game_over():
        raise ValueError("This position cannot be played.")
    if board.ply() >= max_moves * 2:
        raise ValueError("The configured line length has been reached.")
    if move not in board.legal_moves:
        raise ValueError("That move is not legal.")
    root = new_node("", board.fen())
    san = board.san(move)
    board.push(move)
    selected = new_node(san, board.fen(), move.uci())
    selected["games"] = 1
    root["children"][move.uci()] = selected
    try:
        with chess.engine.SimpleEngine.popen_uci(find_engine(), timeout=30) as engine:
            evaluate_tree(root, engine, chess.WHITE if color == "white" else chess.BLACK,
                          depth, max_moves, start_ply=board.ply() - 1)
    except FileNotFoundError as exc:
        raise RuntimeError("Stockfish was not found. Install it or set STOCKFISH_PATH.") from exc
    except (chess.engine.EngineError, OSError, TimeoutError) as exc:
        raise RuntimeError(f"Chess engine failed: {exc}") from exc
    selected["games"] = 0
    return selected


def find_engine():
    import os
    configured = os.environ.get("STOCKFISH_PATH")
    if configured:
        return configured
    return shutil.which("stockfish") or "/usr/games/stockfish"


def analyze(username, color, max_moves, max_games, depth, progress=None):
    if not USERNAME.fullmatch(username):
        raise ValueError("Enter a valid Chess.com username (1-25 letters, numbers, underscores or hyphens).")
    if progress:
        progress("fetch", 0, None)
        games = fetch_games(username, color, max_games, progress=progress)
        progress("prepare", 0, None)
    else:
        games = fetch_games(username, color, max_games)
    root, skipped = build_tree(games, max_moves)
    if not root["games"]:
        raise ValueError("No usable standard chess games found for that player and color.")
    try:
        with chess.engine.SimpleEngine.popen_uci(find_engine(), timeout=30) as engine:
            evaluate_tree(root, engine, chess.WHITE if color == "white" else chess.BLACK, depth, max_moves,
                          progress=progress)
    except FileNotFoundError as exc:
        raise RuntimeError("Stockfish was not found. Install it or set STOCKFISH_PATH.") from exc
    except (chess.engine.EngineError, OSError, TimeoutError) as exc:
        raise RuntimeError(f"Chess engine failed: {exc}") from exc
    return {
        "tree": root, "selected_games": len(games), "skipped_games": skipped,
        "lines_of_interest": lines_of_interest(root),
        "username": username, "color": color, "max_moves": max_moves,
        "depth": depth,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
