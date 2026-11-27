import unittest
from unittest.mock import patch

import chess
import chess.engine

from analyzer import build_tree, evaluate_tree, fetch_games
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

    def test_mistakes_are_relative_to_player_and_accumulate_on_path(self):
        tree, _ = build_tree([
            game("1. e4 e5 2. Nf3 *", 100),
            game("1. e4 e5 2. Bc4 *", 200),
        ], 10)
        e4 = tree["children"]["e2e4"]
        e5 = e4["children"]["e7e5"]
        nf3 = e5["children"]["g1f3"]
        bc4 = e5["children"]["f1c4"]
        scores = {tree["fen"]: 0, e4["fen"]: 0, e5["fen"]: 100,
                  nf3["fen"]: 99, bc4["fen"]: 100}
        evaluate_tree(tree, FakeEngine(scores), chess.BLACK, 8, 100, 10)
        self.assertTrue(e5["opponent_mistake"])
        self.assertFalse(e5["our_mistake"])
        self.assertTrue(nf3["opponent_mistake"])
        self.assertTrue(nf3["our_mistake"])
        self.assertTrue(bc4["opponent_mistake"])
        self.assertFalse(bc4["our_mistake"])
        self.assertEqual(e5["swing"], -100)

    def test_white_player_mistake_threshold_and_frequency_sort(self):
        tree, _ = build_tree([
            game("1. e4 e5 *", 100),
            game("1. e4 c5 *", 200),
            game("1. d4 d5 *", 300),
        ], 10)
        e4 = tree["children"]["e2e4"]
        d4 = tree["children"]["d2d4"]
        e5, c5 = e4["children"]["e7e5"], e4["children"]["c7c5"]
        d5 = d4["children"]["d7d5"]
        scores = {tree["fen"]: 0, e4["fen"]: -99, d4["fen"]: -100,
                  e5["fen"]: -99, c5["fen"]: -100, d5["fen"]: -100}
        evaluate_tree(tree, FakeEngine(scores), chess.WHITE, 8, 100, 10)
        self.assertFalse(e4["opponent_mistake"])
        self.assertTrue(d4["opponent_mistake"])
        self.assertFalse(d5["our_mistake"])

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
        evaluate_tree(tree, FakeEngine(scores), chess.WHITE, 8, 100, 1)
        self.assertEqual([child["move"] for child in tree["children"]], ["d4", "Nf3", "c4", "e4"])
        self.assertEqual([child["loss"] for child in tree["children"]], [0, 20, 40, 120])
        self.assertEqual(tree["children"][0]["games"], 0)
        self.assertTrue(tree["children"][0]["engine_choice"])
        e4 = tree["children"][-1]
        self.assertEqual(e4["games"], 2)
        self.assertFalse(e4["engine_choice"])
        self.assertEqual([child["move"] for child in e4["children"][:3]], ["d5", "c5", "e5"])
        self.assertEqual([child["loss"] for child in e4["children"][:3]], [0, 20, 60])
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
        with patch("analyzer.get_json", side_effect=lambda url: {"archives": archives} if url.endswith("/archives") else games[url]):
            selected = fetch_games("Alice", "white", 2)
        self.assertEqual([item["pgn"] for item in selected], ["b", "a"])

    def test_fetch_unlimited_and_limits_over_1000(self):
        games = [
            {"white": {"username": "alice"}, "rules": "chess", "pgn": "*", "end_time": i}
            for i in range(1100)
        ]
        with patch("analyzer.get_json", side_effect=lambda url: (
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
