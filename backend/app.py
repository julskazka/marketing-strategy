import json
from pathlib import Path

from flask import Flask, Response, jsonify
from .integration import api, config

ROOT = Path(__file__).resolve().parent.parent

app = Flask(__name__, static_folder=str(ROOT / "assets"), static_url_path="/assets")
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024
app.register_blueprint(api)


@app.get("/")
@app.get("/index.html")
def questionnaire():
    cfg = config()
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    # Remove preview notices from the served questionnaire and course screen.
    for notice in (
        '<small>Предварительная версия. Контакты никуда не отправляются.</small>',
        '<p class="muted" style="font-size:14px">Это версия для просмотра: можно ввести тестовые данные. Контакты никуда не отправляются.</p>',
        '<p class="support-note">Это предварительный просмотр программы. Кнопка записи на курс пока не подключена.</p>',
    ):
        html = html.replace(notice, "")
    html = html.replace('<div class="preview-bar">', '<div class="preview-bar" hidden style="display:none">', 1)
    safe_config = json.dumps(cfg, ensure_ascii=True).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    injection = '<script>window.STRATEGY_CONFIG=' + safe_config + ';</script><script src="/assets/strategy-integration.js?v=3"></script>'
    html = html.replace("</body>", injection + "</body>", 1)
    response = Response(html, mimetype="text/html")
    response.headers["Cache-Control"] = "no-cache, no-store"
    return response


@app.get("/healthz")
def health():
    enabled = config()["enabled"]
    return jsonify(status="ok", project="marketing-strategy", stage="integration", submissions_enabled=enabled)
