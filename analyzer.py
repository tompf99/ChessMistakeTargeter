"""Fetch Chess.com games and evaluate their opening tree."""

import io
import csv
import re
import shutil
import urllib.error
import urllib.request
import json
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
import hashlib
import logging
import os
import sqlite3
import tempfile

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
CACHE_DIR = Path(__file__).parent / "data" / "cache"


def cache_key(username, color, limit):
    """Generate a cache key for a game fetch."""
    key_str = f"{username}:{color}:{limit or 'unlimited'}"
    return hashlib.md5(key_str.encode()).hexdigest()


def load_cached_games(username, color, limit):
    """Load cached games if they exist."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{cache_key(username, color, limit)}.json"
    if cache_file.exists():
        try:
            with open(cache_file, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return None
    return None


def save_cached_games(games, username, color, limit):
    """Save games to cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{cache_key(username, color, limit)}.json"
    temp_file = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=CACHE_DIR, delete=False) as f:
            temp_file = Path(f.name)
            json.dump(games, f)
        os.replace(temp_file, cache_file)
    except OSError as exc:
        raise RuntimeError(f"Could not save games cache: {exc}") from exc
    finally:
        if temp_file is not None and temp_file.exists():
            temp_file.unlink()


def cache_connection():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(CACHE_DIR / "incremental.sqlite3", timeout=30)
    try:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        if mode.lower() != "wal":
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode.lower() != "wal":
                raise sqlite3.OperationalError(f"Could not enable cache write-ahead logging: {mode}")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS cache (kind TEXT NOT NULL, key TEXT NOT NULL, "
            "value TEXT NOT NULL, PRIMARY KEY (kind, key))"
        )
    except sqlite3.Error:
        connection.close()
        raise
    return connection


def cached_archive(url, fetch_key):
    try:
        with closing(cache_connection()) as connection:
            key = f"{fetch_key}:{url}"
            row = connection.execute(
                "SELECT value FROM cache WHERE kind = ? AND key = ?", ("archive", key)
            ).fetchone()
            if row:
                return json.loads(row[0])
            games = get_json(url)["games"]
            connection.execute(
                "INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
                ("archive", key, json.dumps(games)),
            )
            connection.commit()
            return games
    except sqlite3.Error as exc:
        raise RuntimeError(f"Could not use incremental game cache: {exc}") from exc


def clear_archive_checkpoints(fetch_key):
    try:
        with closing(cache_connection()) as connection:
            connection.execute(
                "DELETE FROM cache WHERE kind = ? AND substr(key, 1, ?) = ?",
                ("archive", len(fetch_key) + 1, fetch_key + ":"),
            )
            connection.commit()
    except sqlite3.Error as exc:
        raise RuntimeError(f"Could not clear game cache checkpoints: {exc}") from exc


class CachedEngine:
    """Persist each completed engine search independently of the overall analysis."""
    def __init__(self, engine, executable):
        self.engine = engine
        self.executable = executable
        self.connection = None

    def __enter__(self):
        digest = hashlib.sha256()
        with open(shutil.which(self.executable) or self.executable, "rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        self.identity = digest.hexdigest()
        try:
            self.connection = cache_connection()
        except sqlite3.Error as exc:
            raise RuntimeError(f"Could not open engine cache: {exc}") from exc
        return self

    def __exit__(self, *args):
        self.connection.close()

    def analyse(self, board, limit, multipv=None, root_moves=None):
        key = json.dumps([
            self.identity, board.fen(), limit.depth, multipv,
            None if root_moves is None else [move.uci() for move in root_moves],
        ], separators=(",", ":"))
        try:
            row = self.connection.execute(
                "SELECT value FROM cache WHERE kind = ? AND key = ?", ("engine-v1", key)
            ).fetchone()
            if row:
                entries = json.loads(row[0])
                infos = [
                    {"score": chess.engine.PovScore(chess.engine.Cp(entry["score"]), chess.WHITE),
                     **({"pv": [chess.Move.from_uci(entry["move"])]} if entry["move"] else {})}
                    for entry in entries
                ]
                return infos[0] if multipv is None else infos
            result = self.engine.analyse(board, limit, multipv=multipv, root_moves=root_moves)
            infos = [result] if multipv is None else result
            if multipv is not None and len(infos) != multipv:
                raise RuntimeError("The engine did not return all requested move evaluations.")
            entries = []
            for info in infos:
                value = info["score"].white().score(mate_score=100000)
                if value is None:
                    raise RuntimeError("The engine did not return a usable evaluation.")
                pv = info.get("pv")
                if multipv is not None and (not pv or pv[0] not in board.legal_moves):
                    raise RuntimeError("The engine did not return a usable move evaluation.")
                entries.append({"score": value, "move": pv[0].uci() if pv else None})
            if multipv is not None:
                returned = [entry["move"] for entry in entries]
                if len(set(returned)) != len(returned) or (
                    root_moves is not None
                    and not set(returned).issubset({move.uci() for move in root_moves})
                ):
                    raise RuntimeError("The engine did not evaluate all requested moves.")
            self.connection.execute(
                "INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
                ("engine-v1", key, json.dumps(entries)),
            )
            self.connection.commit()
            return result
        except sqlite3.Error as exc:
            raise RuntimeError(f"Could not use engine cache: {exc}") from exc


def analysis_cache_key(username, color, max_moves, depth, max_games):
    """Generate a cache key for analysis."""
    key_str = f"analysis-v3:{username}:{color}:{max_moves}:{depth}:{max_games or 'unlimited'}"
    return hashlib.md5(key_str.encode()).hexdigest()


def load_cached_analysis(username, color, max_moves, depth, max_games):
    """Load cached analysis tree if it exists."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{analysis_cache_key(username, color, max_moves, depth, max_games)}.json"
    if cache_file.exists():
        try:
            with open(cache_file, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            logging.getLogger(__name__).exception("Could not read cached analysis from %s", cache_file)
            return None
    return None


def save_cached_analysis(root, username, color, max_moves, depth, max_games):
    """Save analyzed tree to cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / f"{analysis_cache_key(username, color, max_moves, depth, max_games)}.json"
    temp_file = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=CACHE_DIR, delete=False) as f:
            temp_file = Path(f.name)
            json.dump(root, f)
        os.replace(temp_file, cache_file)
    except OSError:
        logging.getLogger(__name__).exception("Could not save cached analysis to %s", cache_file)
    finally:
        if temp_file is not None and temp_file.exists():
            temp_file.unlink()


def games_digest(games):
    digest = hashlib.sha256()
    for game in games:
        entry = json.dumps([game.get("pgn"), game.get("end_time", 0)], ensure_ascii=True)
        digest.update(entry.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


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
    # Try to load from cache first
    cached = load_cached_games(username, color, limit)
    if cached is not None:
        if progress:
            progress("fetch", 1, 1)
        return cached
    
    archives = get_json(f"{API}/player/{username}/games/archives")["archives"]
    fetch_key = cache_key(username, color, limit)
    selected = []
    if progress:
        progress("fetch", 0, len(archives))
    for index, archive in enumerate(reversed(archives), 1):
        games = cached_archive(archive, fetch_key)
        games.sort(key=lambda game: game.get("end_time", 0), reverse=True)
        for game in games:
            player = game.get(color, {}).get("username", "")
            if (player.casefold() == username.casefold()
                    and game.get("rules") == "chess" and game.get("pgn")):
                selected.append(game)
                if limit is not None and len(selected) == limit:
                    if progress:
                        progress("fetch", index, len(archives))
                    save_cached_games(selected, username, color, limit)
                    clear_archive_checkpoints(fetch_key)
                    return selected
        if progress:
            progress("fetch", index, len(archives))
    
    save_cached_games(selected, username, color, limit)
    clear_archive_checkpoints(fetch_key)
    return selected


def fetch_games_streaming(username, color, limit, batch_size=5, progress=None):
    """Fetch games in batches, yielding results for concurrent processing."""
    # Try to load from cache first
    cached = load_cached_games(username, color, limit)
    if cached is not None:
        if progress:
            progress("fetch", 1, 1)
        yield cached
        return
    
    archives = get_json(f"{API}/player/{username}/games/archives")["archives"]
    fetch_key = cache_key(username, color, limit)
    all_games = []
    if progress:
        progress("fetch", 0, len(archives))
    
    for index, archive in enumerate(reversed(archives), 1):
        games = cached_archive(archive, fetch_key)
        games.sort(key=lambda game: game.get("end_time", 0), reverse=True)
        for game in games:
            player = game.get(color, {}).get("username", "")
            if (player.casefold() == username.casefold()
                    and game.get("rules") == "chess" and game.get("pgn")):
                all_games.append(game)
                if len(all_games) % batch_size == 0 and len(all_games) > 0:
                    yield list(all_games)
                if limit is not None and len(all_games) == limit:
                    if progress:
                        progress("fetch", index, len(archives))
                    save_cached_games(all_games, username, color, limit)
                    clear_archive_checkpoints(fetch_key)
                    return
        if progress:
            progress("fetch", index, len(archives))
    
    if all_games:
        yield all_games
    save_cached_games(all_games, username, color, limit)
    clear_archive_checkpoints(fetch_key)


def new_node(san, fen, uci=None):
    return {
        "move": san, "uci": uci, "fen": fen, "games": 0, "position_games": 0,
        "path_games": 0, "path_last_played": 0,
        "last_played": 0, "opening_name": None, "transpositions": [],
        "children": {}, "evaluation": None, "swing": None,
        "rank": None, "loss": None, "engine_choice": False,
        "status": None, "player_move": False, "position_key": None, "line_continued": False,
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
    game_paths = []
    position_stats = defaultdict(lambda: {
        "games": set(), "last_played": 0, "routes": defaultdict(set),
    })
    edge_stats = defaultdict(lambda: {"games": set(), "last_played": 0})

    def add_position(key, game_id, date, path):
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
            source_key = position_key(board)
            moves = []
            for move in parsed.mainline_moves():
                if len(moves) == max_moves * 2:
                    break
                san = board.san(move)
                board.push(move)
                target_key = position_key(board)
                moves.append((move.uci(), san, board.fen(), source_key, target_key))
                source_key = target_key
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
        path = []
        node["position_key"] = add_position(moves[0][3], game_id, date, path)
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
            add_position(target_key, game_id, date, path)
        game_paths.append((date, game_id, tuple(move[0] for move in moves)))

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
    root["_analysis"] = {
        "edge_stats": edge_stats, "position_stats": position_stats,
        "game_paths": [path for _, _, path in sorted(game_paths, key=lambda item: (-item[0], item[1]))],
    }
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


def interest_step(child, route, saw_mistake, replied):
    if not child["path_games"]:
        return None
    if not child["player_move"] and child["status"] not in ("Book", "Excellent"):
        return None
    mistake = child["player_move"] and child["status"] in ("Mistake", "Blunder")
    return (
        route + [child],
        saw_mistake or mistake,
        replied or (saw_mistake and not child["player_move"]),
    )


def interest_entry(path):
    child = path[-1]
    return {
        "moves": [item["uci"] for item in path],
        "line": format_line([item["move"] for item in path]),
        "games": child["path_games"],
        "last_played": child["path_last_played"],
        "evaluation": child["evaluation"],
        "opening_name": child["opening_name"],
    }


def evaluate_tree(root, engine, player_color, depth, max_moves, start_ply=0, progress=None, interest=None):
    """Scores and swings are always from the chosen player's perspective."""
    cache = {}
    analysis = root.pop("_analysis", {"edge_stats": {}, "position_stats": {}})
    edge_stats = analysis["edge_stats"]
    position_stats = analysis["position_stats"]
    openings = opening_positions()
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

    def initialize(node, ply, route, previous_opening):
        if node.get("_initialized"):
            return
        node["_initialized"] = True
        node["evaluation"] = score(node["fen"])
        node["opening_name"] = openings.get(position_key(chess.Board(node["fen"])), previous_opening)
        recorded = node["children"]
        node["children"] = []
        apply_position_stats(node, route)
        if ply >= max_moves * 2:
            return
        board = chess.Board(node["fen"])
        transposed_moves = [chess.Move.from_uci(uci) for uci in moves_by_position[position_key(board)]]
        moves = sorted(set(transposed_moves + [
            chess.Move.from_uci(uci) for uci in recorded
        ]), key=lambda move: move.uci())
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
            if not (child["games"] and uci in recorded):
                child["children"] = []
                child["evaluation"] = score(child["fen"])
                # Mark if line may have continued beyond our max_moves
                child["line_continued"] = child["games"] > 0 and ply >= max_moves * 2 - 1
            node["children"].append(child)

    if "game_paths" in analysis:
        game_paths = analysis["game_paths"]
    else:
        def recorded_paths(node, path=()):
            children = [(uci, child) for uci, child in node["children"].items() if child["games"]]
            if not children:
                return [path]
            return [
                route for uci, child in children
                for route in recorded_paths(child, path + (uci,))
            ]
        game_paths = recorded_paths(root)

    initialize(root, start_ply, (), None)
    announced = set()
    for completed_games, game_path in enumerate(game_paths, 1):
        node = root
        route = ()
        interest_state = ([], False, False) if interest else None
        for ply, uci in enumerate(game_path, start_ply + 1):
            child = next(child for child in node["children"] if child["uci"] == uci)
            route += (child["move"],)
            if interest_state is not None:
                interest_state = interest_step(child, *interest_state)
                if interest_state is not None and interest_state[1] and interest_state[2]:
                    child["evaluation"] = score(child["fen"])
                    if child["evaluation"] < 0:
                        if id(child) not in announced:
                            interest(interest_entry(interest_state[0]))
                            announced.add(id(child))
                        interest_state = None
            initialize(child, ply, route, node["opening_name"])
            node = child
        if progress and total_games:
            progress("analysis", completed_games, total_games)

    def remove_internal_fields(node):
        node.pop("_path", None)
        node.pop("_initialized", None)
        node.pop("position_key", None)
        for child in node["children"]:
            remove_internal_fields(child)
    remove_internal_fields(root)


def lines_of_interest(root):
    """Find recorded lines with a player mistake and an effective recorded reply."""
    lines = []

    def walk(node, route, saw_mistake=False, replied=False):
        for child in node["children"]:
            state = interest_step(child, route, saw_mistake, replied)
            if state is None:
                continue
            path, has_mistake, has_reply = state
            if has_mistake and has_reply and child["evaluation"] < 0:
                lines.append(interest_entry(path))
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
                          depth, max(max_moves, board.ply() // 2 + 1), start_ply=board.ply() - 1)
    except FileNotFoundError as exc:
        raise RuntimeError("Stockfish was not found. Install it or set STOCKFISH_PATH.") from exc
    except (chess.engine.EngineError, OSError, TimeoutError) as exc:
        raise RuntimeError(f"Chess engine failed: {exc}") from exc
    selected["games"] = 0
    return selected


def evaluate_position(fen, moves, color, depth):
    try:
        board = chess.Board(fen)
        parsed_moves = [chess.Move.from_uci(uci) for uci in moves]
    except ValueError as exc:
        raise ValueError("Invalid position or move.") from exc
    if not board.is_valid() or any(move not in board.legal_moves for move in parsed_moves):
        raise ValueError("Invalid position or illegal move.")
    root = new_node("", board.fen())
    for move in parsed_moves:
        san = board.san(move)
        board.push(move)
        root["children"][move.uci()] = new_node(san, board.fen(), move.uci())
        board.pop()
    try:
        with chess.engine.SimpleEngine.popen_uci(find_engine(), timeout=30) as engine:
            evaluate_tree(root, engine, chess.WHITE if color == "white" else chess.BLACK, depth, 1)
    except FileNotFoundError as exc:
        raise RuntimeError("Stockfish was not found. Install it or set STOCKFISH_PATH.") from exc
    except (chess.engine.EngineError, OSError, TimeoutError) as exc:
        raise RuntimeError(f"Chess engine failed: {exc}") from exc
    return root


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


def analyze_with_streaming(username, color, max_moves, max_games, depth, progress=None, interest=None):
    """Analyze games and yield partial results as analysis progresses."""
    if not USERNAME.fullmatch(username):
        raise ValueError("Enter a valid Chess.com username (1-25 letters, numbers, underscores or hyphens).")
    if progress:
        progress("fetch", 0, None)
        games = fetch_games(username, color, max_games, progress=progress)
        progress("prepare", 0, None)
    else:
        games = fetch_games(username, color, max_games)
    digest = games_digest(games)
    cached_analysis = load_cached_analysis(username, color, max_moves, depth, max_games)
    if cached_analysis and cached_analysis.get("games_digest") == digest:
        root = cached_analysis["tree"]
        skipped = cached_analysis["skipped_games"]
        initial_result = {
            "tree": root, "selected_games": len(games), "skipped_games": skipped,
            "lines_of_interest": lines_of_interest(root),
            "username": username, "color": color, "max_moves": max_moves,
            "depth": depth,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        yield {"type": "result", "data": initial_result}
        return

    root, skipped = build_tree(games, max_moves)
    if not root["games"]:
        raise ValueError("No usable standard chess games found for that player and color.")

    def public_tree(node):
        result = {
            key: value for key, value in node.items()
            if not key.startswith("_") and key not in ("children", "position_key")
        }
        result["children"] = [public_tree(child) for child in node["children"].values()]
        return result

    initial_result = {
        "tree": public_tree(root), "selected_games": len(games), "skipped_games": skipped,
        "lines_of_interest": [],
        "username": username, "color": color, "max_moves": max_moves,
        "depth": depth,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    yield {"type": "partial", "data": initial_result}
    
    # Now perform analysis and continue streaming updates
    try:
        executable = find_engine()
        with chess.engine.SimpleEngine.popen_uci(executable, timeout=30) as engine:
            with CachedEngine(engine, executable) as cached_engine:
                evaluate_tree(root, cached_engine, chess.WHITE if color == "white" else chess.BLACK,
                              depth, max_moves, progress=progress, interest=interest)
    except FileNotFoundError as exc:
        raise RuntimeError("Stockfish was not found. Install it or set STOCKFISH_PATH.") from exc
    except (chess.engine.EngineError, OSError, TimeoutError) as exc:
        raise RuntimeError(f"Chess engine failed: {exc}") from exc
    
    # Cache the analysis for future use
    save_cached_analysis(
        {"tree": root, "skipped_games": skipped, "games_digest": digest},
        username, color, max_moves, depth, max_games,
    )
    
    # Send final result with complete analysis (now children is a list)
    final_result = {
        "tree": root, "selected_games": len(games), "skipped_games": skipped,
        "lines_of_interest": lines_of_interest(root),
        "username": username, "color": color, "max_moves": max_moves,
        "depth": depth,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    yield {"type": "result", "data": final_result}
