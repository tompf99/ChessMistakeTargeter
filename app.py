import os

from flask import Flask, jsonify, render_template, request

from analyzer import analyze

app = Flask(__name__)


@app.get("/")
def index():
    return render_template("index.html")


def integer(data, key, default, minimum, maximum):
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{key} must be an integer between {minimum} and {maximum}.")
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
            integer(data, "max_games", 100, 1, 1000),
            integer(data, "depth", 8, 1, 20),
            integer(data, "threshold", 100, 1, 10000),
        )
        return jsonify(result)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 502


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), threaded=True)
