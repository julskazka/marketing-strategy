from flask import Flask, jsonify

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024

@app.get("/")
@app.get("/healthz")
def health():
    return jsonify(status="ok", project="marketing-strategy", stage="setup", submissions_enabled=False)
