from pathlib import Path

from flask import Flask, jsonify, send_from_directory

ROOT = Path(__file__).resolve().parent.parent

app = Flask(__name__, static_folder=str(ROOT / "assets"), static_url_path="/assets")
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024


@app.get("/")
@app.get("/index.html")
def questionnaire():
    response = send_from_directory(ROOT, "index.html")
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/healthz")
def health():
    return jsonify(status="ok", project="marketing-strategy", stage="setup", submissions_enabled=False)
