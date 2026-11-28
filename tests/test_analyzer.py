import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch
from pathlib import Path

import chess
import chess.engine

from analyzer import CachedEngine, build_tree, cache_connection, classify_move, evaluate_position, evaluate_tree, explore_move, fetch_games, lines_of_interest
from app import app


def game(pgn, end_time):
    return {"pgn": pgn, "end_time": end_time}


class FakeEngine:
    def __init__(self, scores):
        self.scores = scores

    def analyse(self, board, limit, multipv=None, root_moves=None):
        if multipv is None:
            return {"score": chess.engine.PovScore(chess.engine.Cp(self.scores.get(board.fen(), 0)), chess.WHITE)}
        results = []
        for move in root_moves if root_moves is not None else board.legal_moves:
            board.push(move)
            score = self.scores.get(board.fen(), 0)
            board.pop()
            results.append((move, score))
        results.sort(key=lambda item: (-item[1] if board.turn == chess.WHITE else item[1], item[0].uci()))
        return [
            {"pv": [move], "score": chess.engine.PovScore(chess.engine.Cp(score), chess.WHITE)}
            for move, score in results[:multipv]
        ]


class AnalyzerTests(unittest.TestCase):
    def test_lines_of_interest_require_recorded_mistake_good_reply_and_favorable_score(self):
        def node(uci, move, status, player_move, evaluation, children=None, games=1):
            return {
                "uci": uci, "move": move, "status": status, "player_move": player_move,
                "evaluation": evaluation, "children": children or [], "path_games": games,
                "path_last_played": 100, "opening_name": None,
            }
        good = node("e7e5", "e5", "Excellent", False, -50, games=3)
        mistake = node("e2e4", "e4", "Mistake", True, 50, [good], games=4)
        poor_reply = node("d7d5", "d5", "Inaccuracy", False, -100)
        mistake["children"].append(poor_reply)
        no_advantage = node("g8f6", "Nf6", "Book", False, 10)
        mistake["children"].append(no_advantage)
        unplayed = node("d2d4", "d4", "Blunder", True, -200, [good], games=0)
        result = lines_of_interest({"children": [mistake, unplayed]})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["moves"], ["e2e4", "e7e5"])
        self.assertEqual(result[0]["games"], 3)
        self.assertEqual(result[0]["evaluation"], -50)

    def test_explore_unrecorded_move_and_reject_illegal_moves(self):
        board = chess.Board()
        with patch("analyzer.chess.engine.SimpleEngine.popen_uci") as factory:
            factory.return_value.__enter__.return_value = FakeEngine({})
            result = explore_move(board.fen(), "e2e4", "white", 2, 2)
        self.assertEqual((result["uci"], result["move"], result["games"]), ("e2e4", "e4", 0))
        self.assertTrue(result["children"])
        self.assertTrue(all(child["games"] == 0 for child in result["children"]))
        with self.assertRaisesRegex(ValueError, "not legal"):
            explore_move(board.fen(), "e2e5", "white", 2, 2)
        with patch("analyzer.chess.engine.SimpleEngine.popen_uci") as factory:
            factory.return_value.__enter__.return_value = FakeEngine({})
            result = explore_move(
                chess.Board("rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2").fen(),
                "g1f3", "white", 2, 1,
            )
        self.assertEqual(len([child for child in result["children"] if child["engine_choice"]]), 3)

    def test_move_api_reports_illegal_moves(self):
        response = app.test_client().post("/api/move", json={
            "fen": chess.Board().fen(), "uci": "e2e5", "color": "white",
            "depth": 2, "max_moves": 10,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("not legal", response.get_json()["error"])

    def test_position_evaluation_includes_recorded_moves_and_loss(self):
        board = chess.Board()
        scores = {}
        for san, score in [("d4", 100), ("Nf3", 90), ("c4", 80), ("e4", 0)]:
            move = board.parse_san(san)
            board.push(move)
            scores[board.fen()] = score
            board.pop()
        with patch("analyzer.chess.engine.SimpleEngine.popen_uci") as factory:
            factory.return_value.__enter__.return_value = FakeEngine(scores)
            result = evaluate_position(board.fen(), ["e2e4"], "white", 2)
        played = next(child for child in result["children"] if child["uci"] == "e2e4")
        self.assertEqual((played["evaluation"], played["loss"], played["rank"]), (0, 100, 4))
        self.assertEqual(len([child for child in result["children"] if child["engine_choice"]]), 3)
        with self.assertRaisesRegex(ValueError, "illegal"):
            evaluate_position(board.fen(), ["e2e5"], "white", 2)

    def test_terminal_position_has_no_engine_moves(self):
        board = chess.Board("7k/6Q1/6K1/8/8/8/8/8 b - - 0 1")
        self.assertTrue(board.is_checkmate())
        with patch("analyzer.chess.engine.SimpleEngine.popen_uci") as factory:
            factory.return_value.__enter__.return_value = FakeEngine({})
            result = evaluate_position(board.fen(), [], "white", 2)
        self.assertEqual(result["children"], [])

    def test_position_respects_requested_depth_and_legal_move_count(self):
        class DepthEngine(FakeEngine):
            def __init__(self):
                super().__init__({})
                self.depths = []

            def analyse(self, board, limit, multipv=None, root_moves=None):
                self.depths.append(limit.depth)
                return super().analyse(board, limit, multipv, root_moves)

        board = chess.Board("7k/8/5K2/6Q1/8/8/8/8 b - - 0 1")
        engine = DepthEngine()
        with patch("analyzer.chess.engine.SimpleEngine.popen_uci") as factory:
            factory.return_value.__enter__.return_value = engine
            result = evaluate_position(board.fen(), [], "white", 6)
        self.assertEqual(len(result["children"]), 1)
        self.assertTrue(result["children"][0]["engine_choice"])
        self.assertTrue(engine.depths)
        self.assertEqual(set(engine.depths), {6})

    def test_position_api_rejects_invalid_input(self):
        client = app.test_client()
        for moves in (["e2e5"], [None], "e2e4"):
            response = client.post("/api/position", json={
                "fen": chess.Board().fen(), "moves": moves, "color": "white", "depth": 2,
            })
            self.assertEqual(response.status_code, 400)

    def test_tree_counts_recency_and_full_move_limit(self):
        tree, skipped = build_tree([
            game("1. e4 e5 2. Nf3 Nc6 *", 100),
            game("1. e4 e5 2. Bc4 *", 200),
            game("1. d4 d5 *", 300),
            game("", 400),
        ], 1)
        self.assertEqual(skipped, 1)
        self.assertEqual(tree["games"], 3)
        e4 = tree["children"]["e2e4"]
        self.assertEqual((e4["games"], e4["last_played"]), (2, 200))
        self.assertEqual(e4["children"]["e7e5"]["children"], {})
        self.assertEqual((e4["uci"], e4["path_games"], e4["path_last_played"]), ("e2e4", 2, 200))

    def test_transpositions_aggregate_position_games_and_list_alternate_lines(self):
        games = [
            game('1. Nf3 d5 2. g3 Nf6 *', 100),
            game('1. g3 d5 2. Nf3 Nf6 *', 200),
        ]
        tree, skipped = build_tree(games, 10)
        first = tree["children"]["g1f3"]["children"]["d7d5"]["children"]["g2g3"]
        second = tree["children"]["g2g3"]["children"]["d7d5"]["children"]["g1f3"]
        self.assertEqual(skipped, 0)
        self.assertEqual(first["position_key"], second["position_key"])
        self.assertEqual(first["position_games"], 2)
        self.assertEqual(second["position_games"], 2)
        self.assertEqual(first["games"], 1)
        self.assertEqual(second["games"], 1)
        self.assertIn(
            {"line": "1. g3 d5 2. Nf3", "moves": ["g3", "d5", "Nf3"], "games": 1},
            first["transpositions"],
        )
        self.assertIn(
            {"line": "1. Nf3 d5 2. g3", "moves": ["Nf3", "d5", "g3"], "games": 1},
            second["transpositions"],
        )

    def test_opening_names_follow_positions_not_game_headers(self):
        tree, _ = build_tree([
            game('[Opening "Ruy Lopez"]\n\n1. e4 e5 2. Nf3 Nc6 3. Bb5 *', 100),
        ], 3)
        evaluate_tree(tree, FakeEngine({}), chess.WHITE, 2, 3)
        self.assertIsNone(tree["opening_name"])
        node = tree
        names = []
        for san in ["e4", "e5", "Nf3", "Nc6", "Bb5"]:
            node = next(child for child in node["children"] if child["move"] == san)
            names.append(node["opening_name"])
        self.assertEqual(names[0], "King's Pawn Game")
        self.assertEqual(names[2], "King's Knight Opening")
        self.assertEqual(names[-1], "Ruy Lopez")

    def test_engine_progress_counts_completed_games_including_shared_lines(self):
        tree, skipped = build_tree([
            game("1. e4 e5 *", 1), game("1. e4 e5 *", 2),
            game("1. e4 *", 3), game("1. d4 d5 *", 4), game("", 5),
        ], 2)
        updates = []
        evaluate_tree(tree, FakeEngine({}), chess.WHITE, 2, 2,
                      progress=lambda phase, done, total: updates.append((phase, done, total)))
        self.assertEqual(skipped, 1)
        self.assertEqual(updates[0], ("analysis", 0, 4))
        self.assertEqual(updates[-1], ("analysis", 4, 4))
        self.assertEqual([done for _, done, _ in updates], [0, 1, 2, 3, 4])
        self.assertTrue(all(total == 4 for _, _, total in updates))
        self.assertNotIn("_finished_games", json.dumps(tree))

    def test_analysis_completes_games_in_recency_order_across_shared_branches(self):
        games = [
            game("1. e4 c5 2. Nf3 d6 *", 100),
            game("1. d4 d5 2. c4 e6 *", 200),
            game("1. e4 e5 2. Nf3 Nc6 *", 300),
        ]
        def final_fen(pgn):
            board = chess.Board()
            for san in pgn.split():
                if not san.endswith(".") and san != "*":
                    board.push_san(san)
            return board.fen()

        class RecordingEngine(FakeEngine):
            def __init__(self):
                super().__init__({})
                self.positions = set()

            def analyse(self, board, limit, multipv=None, root_moves=None):
                self.positions.add(board.fen())
                return super().analyse(board, limit, multipv, root_moves)

        expected = [final_fen(item["pgn"]) for item in reversed(games)]
        tree, _ = build_tree(games, 2)
        engine = RecordingEngine()
        snapshots = []
        evaluate_tree(
            tree, engine, chess.WHITE, 2, 2,
            progress=lambda phase, done, total: snapshots.append((done, set(engine.positions))),
        )
        for done in range(1, 4):
            seen = next(positions for count, positions in snapshots if count == done)
            self.assertTrue(all(fen in seen for fen in expected[:done]))
            self.assertTrue(all(fen not in seen for fen in expected[done:]))
        self.assertEqual([child["rank"] for child in tree["children"]],
                         list(range(1, len(tree["children"]) + 1)))

    def test_interest_is_emitted_before_full_analysis_finishes(self):
        board = chess.Board()
        scores = {}
        board.push_san("d4")
        scores[board.fen()] = 200
        board = chess.Board()
        board.push_san("e4")
        board.push_san("e5")
        scores[board.fen()] = -100
        tree, _ = build_tree([game("1. e4 e5 *", 1), game("1. d4 d5 *", 2)], 1)
        events = []
        evaluate_tree(
            tree, FakeEngine(scores), chess.WHITE, 2, 1,
            progress=lambda *args: events.append(("progress", args)),
            interest=lambda item: events.append(("interest", item)),
        )
        found = [data for kind, data in events if kind == "interest"]
        self.assertEqual(found, lines_of_interest(tree))
        self.assertTrue(found)
        self.assertLess(next(i for i, event in enumerate(events) if event[0] == "interest"),
                        max(i for i, event in enumerate(events) if event[0] == "progress"))

    def test_interest_follows_recency_without_repeating_shared_lines(self):
        board = chess.Board()
        board.push_san("c4")
        scores = {board.fen(): 200}
        for first, reply in [("e4", "e5"), ("d4", "d5")]:
            board = chess.Board()
            board.push_san(first)
            board.push_san(reply)
            scores[board.fen()] = -100
        tree, _ = build_tree([
            game("1. e4 e5 *", 1),
            game("1. d4 d5 *", 2),
            game("1. e4 e5 *", 3),
        ], 1)
        discovered = []
        evaluate_tree(tree, FakeEngine(scores), chess.WHITE, 2, 1, interest=discovered.append)
        self.assertEqual([item["moves"] for item in discovered],
                         [["e2e4", "e7e5"], ["d2d4", "d7d5"]])
        self.assertEqual(discovered, lines_of_interest(tree))

    def test_engine_cache_survives_interrupted_analysis(self):
        class CountingEngine(FakeEngine):
            def __init__(self):
                super().__init__({})
                self.calls = 0

            def analyse(self, board, limit, multipv=None, root_moves=None):
                self.calls += 1
                return super().analyse(board, limit, multipv, root_moves)

        games = [game("1. e4 e5 *", 1), game("1. d4 d5 *", 2)]
        with tempfile.TemporaryDirectory() as directory, patch("analyzer.CACHE_DIR", Path(directory)):
            engine = CountingEngine()
            tree, _ = build_tree(games, 1)
            def interrupt(phase, completed, total):
                if completed:
                    raise RuntimeError("interrupted")
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                with CachedEngine(engine, sys.executable) as cached:
                    evaluate_tree(tree, cached, chess.WHITE, 2, 1, progress=interrupt)
            self.assertGreater(engine.calls, 0)
            resumed_engine = CountingEngine()
            resumed, _ = build_tree(games, 1)
            with CachedEngine(resumed_engine, sys.executable) as cached:
                evaluate_tree(resumed, cached, chess.WHITE, 2, 1)
            fresh_engine = CountingEngine()
            fresh, _ = build_tree(games, 1)
            evaluate_tree(fresh, fresh_engine, chess.WHITE, 2, 1)
            self.assertLess(resumed_engine.calls, fresh_engine.calls)
            self.assertEqual(resumed, fresh)
            other_depth = CountingEngine()
            tree, _ = build_tree(games, 1)
            with CachedEngine(other_depth, sys.executable) as cached:
                evaluate_tree(tree, cached, chess.WHITE, 3, 1)
            self.assertGreater(other_depth.calls, 0)

    def test_engine_cache_commits_while_another_connection_is_reading(self):
        with tempfile.TemporaryDirectory() as directory, patch("analyzer.CACHE_DIR", Path(directory)):
            with closing(cache_connection()) as reader:
                reader.execute("BEGIN")
                self.assertEqual(reader.execute("SELECT COUNT(*) FROM cache").fetchone()[0], 0)
                with CachedEngine(FakeEngine({}), sys.executable) as writer:
                    writer.connection.execute("PRAGMA busy_timeout = 100")
                    writer.analyse(chess.Board(), chess.engine.Limit(depth=2))
                    self.assertEqual(reader.execute("SELECT COUNT(*) FROM cache").fetchone()[0], 0)
                reader.rollback()
                self.assertEqual(reader.execute("SELECT COUNT(*) FROM cache").fetchone()[0], 1)

    def test_engine_cache_survives_abrupt_process_exit(self):
        script = """
import os, sys
from pathlib import Path
import analyzer, chess, chess.engine
analyzer.CACHE_DIR = Path(sys.argv[1])
class Engine:
    def analyse(self, board, limit, multipv=None, root_moves=None):
        return {'score': chess.engine.PovScore(chess.engine.Cp(42), chess.WHITE)}
cached = analyzer.CachedEngine(Engine(), sys.executable)
cached.__enter__()
cached.analyse(chess.Board(), chess.engine.Limit(depth=2))
os._exit(0)
"""
        class NoSearch:
            def analyse(self, *args, **kwargs):
                raise AssertionError("The completed search should be cached")

        with tempfile.TemporaryDirectory() as directory, patch("analyzer.CACHE_DIR", Path(directory)):
            subprocess.run([sys.executable, "-c", script, directory], check=True, timeout=30)
            with CachedEngine(NoSearch(), sys.executable) as cached:
                info = cached.analyse(chess.Board(), chess.engine.Limit(depth=2))
            self.assertEqual(info["score"].white().score(), 42)

    def test_existing_incremental_cache_remains_readable(self):
        with tempfile.TemporaryDirectory() as directory, patch("analyzer.CACHE_DIR", Path(directory)):
            path = Path(directory) / "incremental.sqlite3"
            with closing(sqlite3.connect(path)) as old:
                old.execute("CREATE TABLE cache (kind TEXT, key TEXT, value TEXT, PRIMARY KEY (kind, key))")
                old.execute("INSERT INTO cache VALUES (?, ?, ?)", ("engine-v1", "key", "value"))
                old.commit()
            with closing(cache_connection()) as upgraded:
                self.assertEqual(upgraded.execute(
                    "SELECT value FROM cache WHERE kind = ? AND key = ?", ("engine-v1", "key")
                ).fetchone(), ("value",))

    def test_interrupted_fetch_reuses_downloaded_archives(self):
        first = {"white": {"username": "Alice"}, "rules": "chess", "pgn": "1. e4 *", "end_time": 2}
        second = {"white": {"username": "Alice"}, "rules": "chess", "pgn": "1. d4 *", "end_time": 1}
        def failing_fetch(url):
            if url.endswith("/archives"):
                return {"archives": ["old", "new"]}
            if url == "new":
                return {"games": [first]}
            raise RuntimeError("connection lost")
        with tempfile.TemporaryDirectory() as directory, patch("analyzer.CACHE_DIR", Path(directory)):
            with patch("analyzer.get_json", side_effect=failing_fetch):
                with self.assertRaisesRegex(RuntimeError, "connection lost"):
                    fetch_games("Alice", "white", None)
            def retry(url):
                if url.endswith("/archives"):
                    return {"archives": ["old", "new"]}
                self.assertEqual(url, "old")
                return {"games": [second]}
            with patch("analyzer.get_json", side_effect=retry):
                self.assertEqual(fetch_games("Alice", "white", None), [first, second])

    def test_engine_cache_distinguishes_searches_and_preserves_mate_scores(self):
        class MateEngine(FakeEngine):
            def __init__(self):
                super().__init__({})
                self.calls = 0

            def analyse(self, board, limit, multipv=None, root_moves=None):
                self.calls += 1
                score = chess.engine.PovScore(chess.engine.Mate(4), chess.BLACK)
                if multipv is None:
                    return {"score": score}
                moves = root_moves if root_moves is not None else list(board.legal_moves)
                return [{"score": score, "pv": [move]} for move in moves[:multipv]]

        board = chess.Board()
        moves = [chess.Move.from_uci("e2e4"), chess.Move.from_uci("d2d4")]
        limit = chess.engine.Limit(depth=2)
        with tempfile.TemporaryDirectory() as directory, patch("analyzer.CACHE_DIR", Path(directory)):
            engine = MateEngine()
            with CachedEngine(engine, sys.executable) as cached:
                original = cached.analyse(board, limit)
                restored = cached.analyse(board, limit)
                for color in (chess.WHITE, chess.BLACK):
                    self.assertEqual(original["score"].pov(color).score(mate_score=100000),
                                     restored["score"].pov(color).score(mate_score=100000))
                first = cached.analyse(board, limit, multipv=2, root_moves=moves)
                again = cached.analyse(board, limit, multipv=2, root_moves=moves)
                self.assertEqual([x["pv"][0] for x in first], [x["pv"][0] for x in again])
                cached.analyse(board, limit, multipv=2, root_moves=list(reversed(moves)))
                cached.analyse(board, limit, multipv=1, root_moves=moves)
                self.assertEqual(engine.calls, 4)
            other_engine = MateEngine()
            with CachedEngine(other_engine, __file__) as cached:
                cached.analyse(board, limit)
            self.assertEqual(other_engine.calls, 1)

    def test_incomplete_engine_results_are_not_cached(self):
        class IncompleteEngine(FakeEngine):
            def __init__(self):
                super().__init__({})
                self.calls = 0

            def analyse(self, board, limit, multipv=None, root_moves=None):
                self.calls += 1
                if self.calls == 1:
                    return []
                return super().analyse(board, limit, multipv, root_moves)

        with tempfile.TemporaryDirectory() as directory, patch("analyzer.CACHE_DIR", Path(directory)):
            engine = IncompleteEngine()
            with CachedEngine(engine, sys.executable) as cached:
                with self.assertRaisesRegex(RuntimeError, "all requested"):
                    cached.analyse(chess.Board(), chess.engine.Limit(depth=2), multipv=3)
                self.assertEqual(len(cached.analyse(chess.Board(), chess.engine.Limit(depth=2), multipv=3)), 3)
            self.assertEqual(engine.calls, 2)

    def test_stream_sends_actual_discoveries_before_final_result(self):
        board = chess.Board()
        board.push_san("d4")
        scores = {board.fen(): 200}
        board = chess.Board()
        board.push_san("e4")
        board.push_san("e5")
        scores[board.fen()] = -100
        games = [game("1. e4 e5 *", 1), game("1. d4 d5 *", 2)]
        with tempfile.TemporaryDirectory() as directory, \
             patch("analyzer.CACHE_DIR", Path(directory)), \
             patch("analyzer.fetch_games", return_value=games), \
             patch("analyzer.find_engine", return_value=sys.executable), \
             patch("analyzer.chess.engine.SimpleEngine.popen_uci") as factory:
            factory.return_value.__enter__.return_value = FakeEngine(scores)
            response = app.test_client().post("/api/analyze/stream", json={
                "username": "Alice", "color": "white", "depth": 2, "max_moves": 1,
            })
            events = [json.loads(row) for row in response.get_data(as_text=True).splitlines()]
        discoveries = [event["data"] for event in events if event["type"] == "interest"]
        self.assertTrue(discoveries)
        self.assertEqual(discoveries, events[-1]["data"]["lines_of_interest"])
        self.assertLess(next(i for i, event in enumerate(events) if event["type"] == "interest"),
                        next(i for i, event in enumerate(events) if event["type"] == "result"))

    def test_stream_emits_progress_result_and_errors(self):
        def fake_analyze(*args, progress, interest):
            progress("fetch", 1, 2)
            yield {"type": "partial", "data": {"ok": False}}
            interest({"line": "1. e4 e5"})
            progress("analysis", 1, 1)
            yield {"type": "result", "data": {"ok": True}}
        client = app.test_client()
        with patch("analyzer.analyze_with_streaming", side_effect=fake_analyze):
            response = client.post("/api/analyze/stream", json={"username": "Alice", "color": "white"})
            events = [json.loads(row) for row in response.get_data(as_text=True).splitlines()]
        self.assertEqual([event["type"] for event in events], ["progress", "partial", "interest", "progress", "result"])
        self.assertEqual(events[2]["data"]["line"], "1. e4 e5")
        self.assertEqual(events[0]["total"], 2)
        self.assertEqual(events[-1]["data"], {"ok": True})
        with patch("analyzer.analyze_with_streaming", side_effect=RuntimeError("Engine failed")):
            response = client.post("/api/analyze/stream", json={"username": "Alice", "color": "white"})
            self.assertEqual(json.loads(response.get_data(as_text=True))["error"], "Engine failed")
        response = client.post("/api/analyze/stream", json={"username": "Alice", "color": "white", "max_moves": 0})
        self.assertEqual(response.status_code, 400)

    def test_stream_partial_tree_then_final_result_and_cached_result(self):
        client = app.test_client()
        payload = {"username": "Alice", "color": "white", "depth": 2, "max_moves": 1}
        with tempfile.TemporaryDirectory() as directory, \
             patch("analyzer.CACHE_DIR", Path(directory)), \
             patch("analyzer.fetch_games", return_value=[game("1. e4 e5 *", 1)]), \
             patch("analyzer.find_engine", return_value=sys.executable), \
             patch("analyzer.chess.engine.SimpleEngine.popen_uci") as factory:
            factory.return_value.__enter__.return_value = FakeEngine({})
            response = client.post("/api/analyze/stream", json=payload)
            events = [json.loads(row) for row in response.get_data(as_text=True).splitlines()]
            partial = next(event["data"] for event in events if event["type"] == "partial")
            final = events[-1]
            self.assertEqual(final["type"], "result")
            self.assertEqual(partial["tree"]["children"][0]["uci"], "e2e4")
            self.assertIsNone(partial["tree"]["children"][0]["status"])
            self.assertNotIn("_analysis", partial["tree"])
            self.assertIsInstance(final["data"]["tree"]["children"], list)
            self.assertIsNotNone(final["data"]["tree"]["evaluation"])
            self.assertTrue(any(child["uci"] == "e2e4" for child in final["data"]["tree"]["children"]))
            self.assertTrue(factory.called)
            factory.reset_mock()
            with patch("analyzer.build_tree", side_effect=AssertionError("cache should skip preparation")):
                response = client.post("/api/analyze/stream", json=payload)
                cached = [json.loads(row) for row in response.get_data(as_text=True).splitlines()]
            self.assertEqual(cached[-1]["type"], "result")
            self.assertEqual(cached[-1]["data"]["tree"], final["data"]["tree"])
            factory.assert_not_called()
            with patch("analyzer.fetch_games", return_value=[game("1. d4 d5 *", 2)]):
                response = client.post("/api/analyze/stream", json=payload)
                changed = [json.loads(row) for row in response.get_data(as_text=True).splitlines()]
            self.assertEqual(changed[-1]["type"], "result")
            self.assertTrue(any(child["uci"] == "d2d4" for child in changed[-1]["data"]["tree"]["children"]))
            factory.assert_called()

    def test_stream_reports_serialization_errors(self):
        def bad_stream(*args, progress, interest):
            yield {"type": "partial", "data": {("invalid",): 1}}
        with patch("analyzer.analyze_with_streaming", side_effect=bad_stream):
            response = app.test_client().post(
                "/api/analyze/stream", json={"username": "Alice", "color": "white"}
            )
            events = [json.loads(row) for row in response.get_data(as_text=True).splitlines()]
        self.assertEqual(events[-1]["type"], "error")

    def test_move_classification_uses_centipawn_loss_bands(self):
        self.assertEqual(classify_move(0), "Excellent")
        self.assertEqual(classify_move(20), "Excellent")
        self.assertEqual(classify_move(21), "Inaccuracy")
        self.assertEqual(classify_move(100), "Inaccuracy")
        self.assertEqual(classify_move(101), "Mistake")
        self.assertEqual(classify_move(200), "Mistake")
        self.assertEqual(classify_move(201), "Blunder")
        self.assertEqual(classify_move(0, is_book=True), "Book")

    def test_book_requires_recognized_opening_and_small_loss(self):
        tree, _ = build_tree([
            game('[Opening "Italian Game"]\n\n1. e4 *', 100),
        ], 1)
        board = chess.Board()
        scores = {}
        for san, score in [("e4", 100), ("d4", 80), ("Nf3", 60), ("c4", 0)]:
            move = board.parse_san(san)
            board.push(move)
            scores[board.fen()] = score
            board.pop()
        evaluate_tree(tree, FakeEngine(scores), chess.WHITE, 8, 1)
        e4 = next(child for child in tree["children"] if child["move"] == "e4")
        self.assertEqual(e4["status"], "Book")

        tree, _ = build_tree([
            game('[Opening "Italian Game"]\n\n1. e4 *', 100),
        ], 1)
        scores = {}
        for san, score in [("d4", 100), ("Nf3", 90), ("c4", 80), ("e4", 0)]:
            move = board.parse_san(san)
            board.push(move)
            scores[board.fen()] = score
            board.pop()
        evaluate_tree(tree, FakeEngine(scores), chess.WHITE, 8, 1)
        e4 = next(child for child in tree["children"] if child["move"] == "e4")
        self.assertEqual(e4["status"], "Inaccuracy")

    def test_move_ranking_includes_engine_choices_and_preserves_played_counts(self):
        tree, _ = build_tree([game("1. e4 e5 *", 100), game("1. e4 c5 *", 200)], 1)
        board = chess.Board()
        scores = {}
        for san, score in [("d4", 100), ("Nf3", 80), ("c4", 60), ("e4", -20)]:
            move = board.parse_san(san)
            board.push(move)
            scores[board.fen()] = score
            board.pop()
        board.push_san("e4")
        for san, score in [("e5", -20), ("c5", -60), ("d5", -80)]:
            move = board.parse_san(san)
            board.push(move)
            scores[board.fen()] = score
            board.pop()
        evaluate_tree(tree, FakeEngine(scores), chess.WHITE, 8, 1)
        self.assertEqual([child["move"] for child in tree["children"]], ["d4", "Nf3", "c4", "e4"])
        self.assertEqual([child["loss"] for child in tree["children"]], [0, 20, 40, 120])
        self.assertEqual(tree["children"][0]["games"], 0)
        self.assertTrue(tree["children"][0]["engine_choice"])
        self.assertEqual(tree["children"][0]["status"], "Excellent")
        e4 = tree["children"][-1]
        self.assertEqual(e4["games"], 2)
        self.assertFalse(e4["engine_choice"])
        self.assertEqual(e4["status"], "Mistake")
        self.assertEqual([child["move"] for child in e4["children"][:3]], ["d5", "c5", "e5"])
        self.assertEqual([child["loss"] for child in e4["children"][:3]], [0, 20, 60])
        self.assertEqual([child["status"] for child in e4["children"][:3]], [
            "Excellent", "Book", "Inaccuracy"
        ])
        self.assertTrue(all(not child["children"] for child in e4["children"]))
        self.assertTrue(all(not child["children"] for child in tree["children"][:-1]))

    def test_fetch_picks_recent_matching_color_across_archives(self):
        archives = ["first", "second"]
        games = {
            "first": {"games": [
                {"white": {"username": "Alice"}, "rules": "chess", "pgn": "a", "end_time": 1}
            ]},
            "second": {"games": [
                {"white": {"username": "alice"}, "rules": "chess", "pgn": "b", "end_time": 2},
                {"white": {"username": "other"}, "rules": "chess", "pgn": "c", "end_time": 3},
                {"white": {"username": "alice"}, "rules": "chess960", "pgn": "d", "end_time": 4},
            ]},
        }
        with tempfile.TemporaryDirectory() as directory, \
             patch("analyzer.CACHE_DIR", Path(directory)), \
             patch("analyzer.get_json", side_effect=lambda url: {"archives": archives} if url.endswith("/archives") else games[url]):
            progress = []
            selected = fetch_games("Alice", "white", 2, lambda *event: progress.append(event))
        self.assertEqual([item["pgn"] for item in selected], ["b", "a"])
        self.assertEqual(progress, [("fetch", 0, 2), ("fetch", 1, 2), ("fetch", 2, 2)])

    def test_fetch_unlimited_and_limits_over_1000(self):
        games = [
            {"white": {"username": "alice"}, "rules": "chess", "pgn": "*", "end_time": i}
            for i in range(1100)
        ]
        with tempfile.TemporaryDirectory() as directory, \
             patch("analyzer.CACHE_DIR", Path(directory)), \
             patch("analyzer.get_json", side_effect=lambda url: (
            {"archives": ["archive"]} if url.endswith("/archives") else {"games": games}
        )):
            self.assertEqual(len(fetch_games("alice", "white", None)), 1100)
            self.assertEqual(len(fetch_games("alice", "white", 1001)), 1001)

    def test_api_game_limit_defaults_to_unlimited_and_accepts_large_limits(self):
        client = app.test_client()
        with patch("app.analyze", return_value={"ok": True}) as analyze:
            for extra, expected in [({}, None), ({"max_games": None}, None), ({"max_games": 1001}, 1001)]:
                response = client.post("/api/analyze", json={"username": "Alice", "color": "white", **extra})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(analyze.call_args.args[3], expected)
            for invalid in [0, -1, True, "1001", 1.5]:
                response = client.post("/api/analyze", json={
                    "username": "Alice", "color": "white", "max_games": invalid
                })
                self.assertEqual(response.status_code, 400)

    def test_api_rejects_bad_options_before_fetching(self):
        with patch("app.analyze") as analyze:
            response = app.test_client().post("/api/analyze", json={
                "username": "Alice", "color": "white", "max_moves": 0
            })
        self.assertEqual(response.status_code, 400)
        analyze.assert_not_called()


if __name__ == "__main__":
    unittest.main()
