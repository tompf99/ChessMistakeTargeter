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

    def analyse(self, board, limit):
        return {"score": chess.engine.PovScore(chess.engine.Cp(self.scores[board.fen()]), chess.WHITE)}


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
        evaluate_tree(tree, FakeEngine(scores), chess.BLACK, 8, 100)
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
        evaluate_tree(tree, FakeEngine(scores), chess.WHITE, 8, 100)
        self.assertEqual([node["move"] for node in tree["children"]], ["e4", "d4"])
        self.assertEqual([node["move"] for node in e4["children"]], ["c5", "e5"])
        self.assertFalse(e4["opponent_mistake"])
        self.assertTrue(d4["opponent_mistake"])
        self.assertFalse(d5["our_mistake"])

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

    def test_api_rejects_bad_options_before_fetching(self):
        with patch("app.analyze") as analyze:
            response = app.test_client().post("/api/analyze", json={
                "username": "Alice", "color": "white", "max_moves": 0
            })
        self.assertEqual(response.status_code, 400)
        analyze.assert_not_called()


if __name__ == "__main__":
    unittest.main()
