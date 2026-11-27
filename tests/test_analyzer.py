import unittest
from unittest.mock import patch

import chess
import chess.engine

from analyzer import build_tree, classify_move, evaluate_tree, explore_move, fetch_games, lines_of_interest, opening_name
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
        with self.assertRaisesRegex(ValueError, "line length"):
            explore_move(chess.Board("rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq - 0 2").fen(),
                         "g1f3", "white", 2, 1)

    def test_move_api_reports_illegal_moves(self):
        response = app.test_client().post("/api/move", json={
            "fen": chess.Board().fen(), "uci": "e2e5", "color": "white",
            "depth": 2, "max_moves": 10,
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("not legal", response.get_json()["error"])

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

    def test_opening_name_comes_from_headers_or_chess_com_opening_url(self):
        self.assertEqual(opening_name({"Opening": "Queen's Gambit"}), "Queen's Gambit")
        self.assertEqual(
            opening_name({"ECOUrl": "https://www.chess.com/openings/Queens-Gambit"}),
            "Queens Gambit",
        )
        self.assertEqual(opening_name({"ECO": "C20"}), "ECO C20")

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
            "Excellent", "Excellent", "Inaccuracy"
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
