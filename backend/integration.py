"""Questionnaire sessions and GetCourse delivery. Never expose credentials."""
import ast
import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, request
from .getcourse import upsert_order, GetCourseError

api = Blueprint("strategy_api", __name__, url_prefix="/api/strategy")
ROOT = Path(__file__).resolve().parent.parent


CONSENT_URL = "https://loonsisters.getcourse.ru/privacy"


def config():
    enabled = (os.getenv("GETCOURSE_LIVE") == "1"
               and bool(re.fullmatch(r"[A-Za-z0-9-]+", os.getenv("GETCOURSE_ACCOUNT", "")))
               and os.getenv("GETCOURSE_OFFER_ID", "").isdigit()
               and bool(os.getenv("GETCOURSE_API_KEY")))
    return {"enabled": bool(enabled), "consent_url": CONSENT_URL, "policy_url": ""}


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def database():
    root = Path(os.getenv("STRATEGY_DATA_DIR", "/data"))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / "strategy.sqlite3"
    db = sqlite3.connect(path, timeout=25)
    try:
        os.chmod(path, 0o600)
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY, order_number TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL, email TEXT NOT NULL, phone TEXT NOT NULL,
            consent_at TEXT NOT NULL, consent_url TEXT NOT NULL,
            policy_url TEXT NOT NULL, answers TEXT NOT NULL DEFAULT '{}',
            deal_id TEXT, delivery TEXT NOT NULL DEFAULT 'start_pending',
            updated_at TEXT NOT NULL)""")
        db.commit()
        yield db
    finally:
        db.close()


def payload():
    if not request.is_json:
        return None
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def digest(data):
    token = data.get("token", "")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError("Неверная сессия анкеты.")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def error(message, status=400):
    return jsonify(ok=False, message=message), status


@api.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store"
    return response


def schema():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    questions = json.JSONDecoder().raw_decode(html.split("window.CONTENT=", 1)[1].lstrip())[0]["questions"]
    questions["inquiries"] = ["Люди уже писали вам насчёт заказа?",
                             ["Да, но до оплаты пока не дошло", "Нет, пока не обращались", "Не помню"]]
    raw = re.search(r"const values=(\{[^\n]+\});", html).group(1)
    raw = re.sub(r"([,{])\s*(\w+):", r'\1"\2":', raw)
    raw = re.sub(r"\btrue\b", "True", raw)
    raw = re.sub(r"\bfalse\b", "False", raw)
    return questions, ast.literal_eval(raw)


def route(a):
    q = ["region", "stage", "format", "product", "goal", "channels", "change"]
    if a.get("format") not in ("studio", "shop"):
        q.append("maps_existing")
    if a.get("stage") == "none":
        q.append("inquiries")
        if a.get("inquiries") == "yes":
            q += ["sales", "responder"]
    elif a.get("stage"):
        q += ["numbers", "sales", "responder", "economy", "base", "restore"]
    if a.get("region") == "ru":
        q.append("avito")
        if a.get("avito") not in ("none", None, "unknown"):
            q += ["analysis", "demand"]
            if a.get("avito") in ("leads", "orders", "past"):
                q.append("mismatch")
    if a.get("format") in ("studio", "shop") or a.get("maps_existing") is True:
        q.append("maps")
    if a.get("region") == "ru":
        q.append("vk")
    return q + ["ready", "time", "capacity", "budget", "access", "note"]


def validate_answers(a, final=False):
    if not isinstance(a, dict):
        raise ValueError("Ответы не получены.")
    questions, values = schema()
    allowed_extra = {"city": 100, "currency": 12}
    result = {}
    for key, value in a.items():
        if key in ("leads", "orders", "budget_amount"):
            if type(value) not in (float, int) or not 0 <= value <= 10**9:
                raise ValueError("Проверьте числовые ответы.")
            if key != "budget_amount" and int(value) != value:
                raise ValueError("Количество должно быть целым.")
        elif key in allowed_extra or key in ("goal", "note"):
            limit = allowed_extra.get(key, 500 if key == "goal" else 1500)
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError("Слишком длинный или неверный ответ.")
        elif key == "channels":
            if (not isinstance(value, list) or not value or len(value) > 11
                or any(not isinstance(x, str) or x not in questions[key][1] for x in value)):
                raise ValueError("Проверьте выбранные каналы.")
        elif key in questions and key != "review":
            options = values.get(key, questions[key][1])
            if not any(type(value) is type(x) and value == x for x in options) and value != "unknown":
                raise ValueError("Неизвестный вариант ответа.")
        else:
            continue
        result[key] = value
    if final:
        if any(k not in result for k in route(result) if k != "note"):
            raise ValueError("Ответьте на все обязательные вопросы.")
        if (result.get("orders", 0) > result.get("leads", float("inf"))
            or (result.get("stage") == "none" and
                (result.get("orders", 0) > 0 or result.get("avito") == "orders" or result.get("vk") == "orders"))):
            raise ValueError("Проверьте количество заказов и обращений.")
    return result


def comment(a):
    questions, values = schema()
    lines = ["Индивидуальная маркетинговая стратегия", "Анкета заполнена", ""]
    for k in route(a):
        if k not in a or (k == "note" and not a[k]):
            continue
        v = a[k]
        options = values.get(k, [])
        match = next((i for i, x in enumerate(options) if type(x) is type(v) and x == v), None)
        label = questions[k][1][match] if match is not None else ", ".join(v) if isinstance(v, list) else "Не знаю" if v == "unknown" else str(v)
        if k == "region" and a.get("city"):
            label += "; " + a["city"]
        if k == "numbers" and ("leads" in a or "orders" in a):
            label += "; обращений: " + str(a.get("leads", "—")) + "; заказов: " + str(a.get("orders", "—"))
        if k == "budget" and a.get("budget") == "yes" and "budget_amount" in a:
            label += "; " + str(a["budget_amount"]) + " " + a.get("currency", "₽")
        lines += [questions[k][0], label, ""]
    return "\n".join(lines)


def send(row, text):
    return upsert_order(row["email"], row["order_number"], text, row["name"], row["phone"])


@api.get("/config")
def get_config():
    return jsonify(config())


@api.post("/start")
def start():
    cfg = config()
    if not cfg["enabled"]:
        return error("Приём анкет пока не включён.", 503)
    data = payload()
    if data is None:
        return error("Неверный запрос.")
    try:
        key = digest(data)
        name, email, phone = [data.get(k, "") for k in ("name", "email", "phone")]
        if not all(isinstance(v, str) for v in (name, email, phone)):
            raise ValueError("Проверьте контакты.")
        name, email, phone = name.strip(), email.strip().lower(), phone.strip()
        if (not 1 <= len(name) <= 80 or not 3 <= len(email) <= 254
            or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
            or not re.fullmatch(r"[+0-9 ()-]{10,24}", phone)
            or not 10 <= len(re.sub(r"\D", "", phone)) <= 15
            or data.get("consent") is not True):
            raise ValueError("Проверьте контакты и согласие.")
    except ValueError as exc:
        return error(str(exc))
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""INSERT OR IGNORE INTO sessions
            (token_hash,order_number,name,email,phone,consent_at,consent_url,policy_url,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (key, "ms-" + key[:32], name, email, phone, now(), cfg["consent_url"], cfg["policy_url"], now()))
        row = db.execute("SELECT * FROM sessions WHERE token_hash=?", (key,)).fetchone()
        db.commit()
        if row["email"] != email:
            return error("Эта анкета уже привязана к другой почте.", 409)
        if row["deal_id"]:
            return jsonify(ok=True, answers=json.loads(row["answers"]), person={k:row[k] for k in ("name","email","phone")})
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM sessions WHERE token_hash=?", (key,)).fetchone()
        if not row["deal_id"]:
            try:
                result = send(row, "Индивидуальная маркетинговая стратегия\nАнкета начата, ответы ещё не отправлены.")
            except GetCourseError:
                db.commit()
                return error("Не удалось подтвердить заказ. Нажмите кнопку ещё раз.", 502)
            db.execute("UPDATE sessions SET deal_id=?,delivery='started',updated_at=? WHERE token_hash=?",
                       (str(result["deal_id"]), now(), key))
        db.commit()
        return jsonify(ok=True, answers=json.loads(row["answers"]), person={k:row[k] for k in ("name","email","phone")})


@api.post("/resume")
def resume():
    if not config()["enabled"]:
        return error("Приём анкет пока не включён.", 503)
    try:
        key = digest(payload() or {})
    except ValueError as exc:
        return error(str(exc))
    with database() as db:
        row = db.execute("SELECT * FROM sessions WHERE token_hash=? AND deal_id IS NOT NULL", (key,)).fetchone()
        if not row:
            return error("Анкета не найдена.", 404)
        return jsonify(ok=True, answers=json.loads(row["answers"]), person={k:row[k] for k in ("name","email","phone")})


@api.post("/save")
@api.post("/finish")
def save_answers():
    if not config()["enabled"]:
        return error("Приём анкет пока не включён.", 503)
    final = request.path.endswith("/finish")
    data = payload() or {}
    try:
        key = digest(data)
        a = validate_answers(data.get("answers"), final)
    except ValueError as exc:
        return error(str(exc))
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM sessions WHERE token_hash=? AND deal_id IS NOT NULL", (key,)).fetchone()
        if not row:
            db.rollback()
            return error("Сначала подтвердите контакты.", 403)
        serialized = json.dumps(a, ensure_ascii=False, sort_keys=True)
        if final and row["delivery"] == "finished" and row["answers"] == serialized:
            db.commit()
            return jsonify(ok=True)
        state = "finish_pending" if final else "started"
        if not final and row["answers"] == serialized and row["delivery"] == "finished":
            state = "finished"
        db.execute("UPDATE sessions SET answers=?,delivery=?,updated_at=? WHERE token_hash=?",
                   (serialized, state, now(), key))
        db.commit()
        if final:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM sessions WHERE token_hash=?", (key,)).fetchone()
            if row["answers"] != serialized:
                db.rollback()
                return error("Ответы изменились. Отправьте анкету ещё раз.", 409)
            try:
                send(row, comment(a))
            except GetCourseError:
                db.commit()
                return error("Ответы сохранены. GetCourse пока не подтвердил отправку. Повторите попытку.", 502)
            db.execute("UPDATE sessions SET delivery='finished',updated_at=? WHERE token_hash=?", (now(), key))
            db.commit()
        return jsonify(ok=True)
