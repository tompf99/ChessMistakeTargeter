import os

from flask import Flask, jsonify, render_template, request

from analyzer import analyze, explore_move

app = Flask(__name__)


@app.get("/")
def index():
    return render_template("index.html")


def integer(data, key, default, minimum, maximum):
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{key} must be an integer between {minimum} and {maximum}.")
    return value


def game_limit(data):
    value = data.get("max_games")
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
        raise ValueError("max_games must be a positive integer or null for unlimited.")
    return value


@app.post("/api/analyze")
def analyze_games():
    try:
        data = request.get_json()
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        username = data.get("username", "")
        color = data.get("color")
        if not isinstance(username, str):
            raise ValueError("Username must be text.")
        username = username.strip()
        if color not in ("white", "black"):
            raise ValueError("Choose white or black for the player.")
        result = analyze(
            username, color,
            integer(data, "max_moves", 10, 1, 30),
            game_limit(data),
            integer(data, "depth", 8, 1, 20),
        )
        return jsonify(result)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 502


@app.post("/api/move")
def move():
    try:
        data = request.get_json()
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        fen, uci, color = data.get("fen"), data.get("uci"), data.get("color")
        if not isinstance(fen, str) or not isinstance(uci, str):
            raise ValueError("Position and move must be text.")
        if color not in ("white", "black"):
            raise ValueError("Choose white or black for the player.")
        return jsonify(explore_move(
            fen, uci, color, integer(data, "depth", 8, 1, 20),
            integer(data, "max_moves", 10, 1, 30),
        ))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 502


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), threaded=True)
