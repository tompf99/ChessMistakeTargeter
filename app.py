import os
import json
import queue
import threading
import webbrowser
import atexit
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

from analyzer import analyze, evaluate_position, explore_move

app = Flask(__name__)
CACHE_DIR = Path(__file__).parent / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


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


def analysis_arguments(data):
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object.")
    username = data.get("username", "")
    color = data.get("color")
    if not isinstance(username, str):
        raise ValueError("Username must be text.")
    if color not in ("white", "black"):
        raise ValueError("Choose white or black for the player.")
    return (
        username.strip(), color,
        integer(data, "max_moves", 10, 1, 30),
        game_limit(data),
        integer(data, "depth", 8, 1, 20),
    )


@app.post("/api/analyze")
def analyze_games():
    try:
        result = analyze(*analysis_arguments(request.get_json()))
        return jsonify(result)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 502


@app.post("/api/analyze/stream")
def analyze_stream():
    try:
        arguments = analysis_arguments(request.get_json())
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    events = queue.Queue(maxsize=32)
    stopped = threading.Event()

    class ClientDisconnected(Exception):
        pass

    def send(event):
        encoded = json.dumps(event) + "\n"
        while not stopped.is_set():
            try:
                events.put((event["type"], encoded), timeout=0.2)
                return
            except queue.Full:
                continue
        raise ClientDisconnected()

    def progress(phase, completed, total):
        send({"type": "progress", "phase": phase, "completed": completed, "total": total})

    def interest(item):
        send({"type": "interest", "data": item})

    def worker():
        try:
            from analyzer import analyze_with_streaming
            for event in analyze_with_streaming(*arguments, progress=progress, interest=interest):
                send(event)
        except ClientDisconnected:
            return
        except (ValueError, RuntimeError) as exc:
            try:
                send({"type": "error", "error": str(exc)})
            except ClientDisconnected:
                return
        except Exception:
            app.logger.exception("Analysis failed")
            try:
                send({"type": "error", "error": "Unexpected analysis error. Check server logs."})
            except ClientDisconnected:
                return

    def stream():
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        try:
            while True:
                event_type, encoded = events.get()
                yield encoded
                if event_type in ("result", "error"):
                    break
        finally:
            stopped.set()

    return Response(stream(), mimetype="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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
        depth = integer(data, "depth", 8, 1, 20)
        # Increase depth if user is actively looking at this position
        if data.get("focused"):
            depth = min(depth + 4, 20)
        return jsonify(explore_move(
            fen, uci, color, depth,
            integer(data, "max_moves", 10, 1, 30),
        ))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 502


@app.post("/api/position")
def position():
    try:
        data = request.get_json()
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        fen, moves, color = data.get("fen"), data.get("moves"), data.get("color")
        if not isinstance(fen, str) or not isinstance(moves, list) or len(moves) > 256 or any(
            not isinstance(move, str) for move in moves
        ):
            raise ValueError("Position and moves must be valid.")
        if color not in ("white", "black"):
            raise ValueError("Choose white or black for the player.")
        depth = integer(data, "depth", 8, 1, 20)
        return jsonify(evaluate_position(fen, moves, color, depth))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 502


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    url = f"http://127.0.0.1:{port}"
    
    # Open browser after a brief delay to allow server to start
    def open_browser():
        import time
        time.sleep(1)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    
    browser_thread = threading.Thread(target=open_browser, daemon=True)
    browser_thread.start()
    
    app.run(host="127.0.0.1", port=port, threaded=True)
