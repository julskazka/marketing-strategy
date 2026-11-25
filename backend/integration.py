"""Questionnaire sessions and GetCourse delivery. Never expose credentials."""
import ast
import hashlib
import json
import os
import re
import sqlite3
import threading
import logging
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Blueprint, jsonify, request
from .getcourse import upsert_order, add_legacy_order_comment, GetCourseError
from .sheets_backup import send_backup, configured as sheets_configured, SheetsError

api = Blueprint("strategy_api", __name__, url_prefix="/api/strategy")
ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger("gunicorn.error")
delivery_wakeup = threading.Event()


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
        columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
        if "delivery_parts" not in columns:
            db.execute("ALTER TABLE sessions ADD COLUMN delivery_parts TEXT NOT NULL DEFAULT '[]'")
        if "delivery_index" not in columns:
            db.execute("ALTER TABLE sessions ADD COLUMN delivery_index INTEGER NOT NULL DEFAULT 0")
        if "delivery_rejections" not in columns:
            db.execute("ALTER TABLE sessions ADD COLUMN delivery_rejections INTEGER NOT NULL DEFAULT 0")
        if "delivery_error" not in columns:
            db.execute("ALTER TABLE sessions ADD COLUMN delivery_error TEXT NOT NULL DEFAULT ''")
        if "sheet_delivery" not in columns:
            db.execute("ALTER TABLE sessions ADD COLUMN sheet_delivery TEXT NOT NULL DEFAULT 'pending'")
        if "sheet_retry_at" not in columns:
            db.execute("ALTER TABLE sessions ADD COLUMN sheet_retry_at TEXT NOT NULL DEFAULT ''")
        if "legacy_progress" not in columns:
            db.execute("ALTER TABLE sessions ADD COLUMN legacy_progress TEXT NOT NULL DEFAULT '{}'") 
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


def order_number(token_hash):
    """Stable numeric GetCourse deal number for one questionnaire session."""
    return str(800_000_000_000 + int(token_hash[:10], 16) % 100_000_000_000)


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


def comment_parts(text, limit=300):
    """Keep every character, including separators, in readable-sized comments."""
    parts = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        cut = max(text.rfind("\n", 1, limit + 1), text.rfind(" ", 1, limit + 1))
        cut = cut + 1 if cut > limit // 2 else limit
        parts.append(text[:cut])
        text = text[cut:]
    return parts


def send(row, text):
    return upsert_order(row["email"], row["order_number"], text, row["name"], row["phone"])


# Pilot only on the previously used, confirmed existing order.
LEGACY_PILOT_ORDER = "803481980570"


def auto_order_number_enabled():
    return os.getenv("GETCOURSE_AUTO_ORDER_NUMBER", "0") == "1"


def legacy_rollout_enabled():
    return os.getenv("GETCOURSE_COMMENT_ROLLOUT", "0") == "1"


def legacy_comment_parts(text, limit=180):
    """Split on UTF-8 bytes without losing characters."""
    pieces = []
    while text:
        size, count = 0, 0
        for ch in text:
            n = len(ch.encode("utf-8"))
            if size + n > limit:
                break
            size += n
            count += 1
        if count < len(text):
            cut = max(text.rfind("\n", 0, count), text.rfind(" ", 0, count))
            if cut >= count // 2:
                count = cut + 1
        if not count:
            raise ValueError("Invalid comment part limit")
        pieces.append(text[:count])
        text = text[count:]
    return pieces


def deliver_pending_once():
    """One-key comment pilot; store progress before each request."""
    rollout = legacy_rollout_enabled()
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        if rollout:
            db.execute("""UPDATE sessions SET delivery='finished',delivery_error='',
                          sheet_delivery='pending',sheet_retry_at=''
                          WHERE order_number=? AND delivery='delivery_failed'
                          AND delivery_error='LEGACY_PILOT_CHECK'""", (LEGACY_PILOT_ORDER,))
        # Reopen only the rejected pilot, once, for the corrected offer-bearing request.
        pilot = db.execute("""SELECT * FROM sessions WHERE order_number=?
            AND delivery='delivery_failed' AND delivery_error='LEGACY_COMMENT_VALIDATION'""",
            (LEGACY_PILOT_ORDER,)).fetchone()
        if pilot:
            previous = json.loads(pilot["legacy_progress"])
            if previous.get("index") == 0 and not previous.get("inflight") and previous.get("short_probe") in ("API_REJECTED", "COMMENT_VALIDATION") and not previous.get("offer_retry"):
                db.execute("""UPDATE sessions SET delivery='finish_pending',delivery_error='',legacy_progress=?
                    WHERE token_hash=?""", (json.dumps({"parts":[comment(json.loads(pilot["answers"]))],
                    "index":0,"inflight":False,"offer_retry":True}, ensure_ascii=False), pilot["token_hash"]))
        row = db.execute("""SELECT * FROM sessions WHERE deal_id IS NOT NULL
            AND delivery IN ('finish_pending','delivery_failed')
            AND delivery_error IN ('','COMMENT_VALIDATION','V2_NOT_CONFIGURED')
            AND (?=1 OR order_number=?)
            ORDER BY updated_at LIMIT 1""", (int(rollout), LEGACY_PILOT_ORDER)).fetchone()
        if not row:
            db.commit()
            return False
        key, snapshot = row["token_hash"], row["answers"]
        progress = json.loads(row["legacy_progress"])
        if progress.get("inflight"):
            db.execute("UPDATE sessions SET delivery='delivery_failed',delivery_error='LEGACY_UNCERTAIN' WHERE token_hash=?", (key,))
            db.commit()
            logger.warning("STRATEGY_DELIVERY legacy_uncertain")
            return False
        if not progress:
            progress = {"parts": [comment(json.loads(snapshot))], "index": 0, "inflight": False}
        parts, index = progress["parts"], progress["index"]
        if index >= len(parts):
            db.commit()
            return False
        progress["inflight"] = True
        db.execute("UPDATE sessions SET legacy_progress=? WHERE token_hash=?",
                   (json.dumps(progress, ensure_ascii=False), key))
        db.commit()
    part = parts[index]
    text = part if len(parts) == 1 else "[Анкета %d/%d]\n%s" % (index + 1, len(parts), part)
    try:
        add_legacy_order_comment(row["email"], row["order_number"], row["deal_id"], text)
    except GetCourseError as exc:
        code = str(exc)
        with database() as db:
            progress["inflight"] = False
            if code == "COMMENT_VALIDATION" and not progress.get("offer_retry") and len(parts) == 1 and len(part.encode("utf-8")) > 180:
                progress = {"parts": legacy_comment_parts(part), "index": 0, "inflight": False}
                db.execute("""UPDATE sessions SET legacy_progress=?,delivery='finish_pending',delivery_error=''
                           WHERE token_hash=? AND answers=? AND delivery IN ('finish_pending','delivery_failed')""",
                           (json.dumps(progress, ensure_ascii=False), key, snapshot))
                db.commit()
                logger.info("STRATEGY_DELIVERY legacy_split parts=%d", len(progress["parts"]))
                return True
            db.execute("""UPDATE sessions SET legacy_progress=?,delivery='delivery_failed',delivery_error=?
                       WHERE token_hash=? AND answers=? AND delivery IN ('finish_pending','delivery_failed')""",
                       (json.dumps(progress, ensure_ascii=False), "LEGACY_" + code, key, snapshot))
            db.commit()
        logger.warning("STRATEGY_DELIVERY legacy_stopped code=%s part=%d/%d", code, index + 1, len(parts))
        return False
    progress["inflight"] = False
    progress["index"] = index + 1
    complete = progress["index"] == len(parts)
    state = "finished" if complete and rollout else "delivery_failed" if complete else "finish_pending"
    error_code = "LEGACY_PILOT_CHECK" if complete and not rollout else ""
    with database() as db:
        db.execute("""UPDATE sessions SET legacy_progress=?,delivery=?,delivery_error=?,
                   sheet_delivery=CASE WHEN ? THEN 'pending' ELSE sheet_delivery END,
                   sheet_retry_at=CASE WHEN ? THEN '' ELSE sheet_retry_at END
                   WHERE token_hash=? AND answers=? AND delivery IN ('finish_pending','delivery_failed')""",
                   (json.dumps(progress, ensure_ascii=False), state, error_code,
                    complete and rollout, complete and rollout, key, snapshot))
        db.commit()
    logger.info("STRATEGY_DELIVERY legacy_part_confirmed part=%d/%d", index + 1, len(parts))
    if complete:
        logger.info("STRATEGY_DELIVERY %s", "legacy_complete" if rollout else "legacy_pilot_api_confirmed_check_order_803481980570")
    return True


def backup_pending_once():
    if not sheets_configured():
        return False
    with database() as db:
        row = db.execute("""SELECT * FROM sessions WHERE deal_id IS NOT NULL
            AND delivery IN ('finish_pending','delivery_failed','finished')
            AND sheet_delivery='pending' AND sheet_retry_at<=?
            ORDER BY updated_at LIMIT 1""", (now(),)).fetchone()
        if not row:
            return False
    status = "Получено" if row["delivery"] == "finished" else "Ожидает подтверждения"
    try:
        send_backup(row, comment(json.loads(row["answers"])), status)
    except SheetsError as exc:
        logger.warning("STRATEGY_BACKUP code=%s", str(exc))
        with database() as db:
            db.execute("UPDATE sessions SET sheet_retry_at=? WHERE token_hash=? AND answers=? AND sheet_delivery='pending'",
                       ((datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(), row["token_hash"], row["answers"]))
            db.commit()
        return False
    with database() as db:
        db.execute("UPDATE sessions SET sheet_delivery='saved',sheet_retry_at='' WHERE token_hash=? AND answers=? AND sheet_delivery='pending'",
                   (row["token_hash"], row["answers"]))
        db.commit()
    logger.info("STRATEGY_BACKUP confirmed")
    return True


FRESH_TEST_ORDER = "833386061443"


def legacy_full_comment_probe():
    """Send the full questionnaire once, only after the short probe was confirmed."""
    if legacy_rollout_enabled():
        return False
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""SELECT * FROM sessions WHERE order_number=? AND deal_id IS NOT NULL
            AND delivery IN ('finish_pending','delivery_failed')""", (FRESH_TEST_ORDER,)).fetchone()
        if not row:
            db.commit()
            return False
        progress = json.loads(row["legacy_progress"])
        if progress.get("inflight") or progress.get("fresh_probe") != "CONFIRMED" or progress.get("fresh_full_probe"):
            db.commit()
            return False
        progress["fresh_full_probe"] = "inflight"
        db.execute("UPDATE sessions SET legacy_progress=? WHERE token_hash=?",
                   (json.dumps(progress, ensure_ascii=False), row["token_hash"]))
        db.commit()
    try:
        add_legacy_order_comment(row["email"], row["order_number"], row["deal_id"], comment(json.loads(row["answers"])))
        outcome = "CONFIRMED"
    except GetCourseError as exc:
        outcome = str(exc)
    with database() as db:
        db.execute("""UPDATE sessions SET legacy_progress=?
                   WHERE token_hash=? AND answers=? AND delivery IN ('finish_pending','delivery_failed')""",
                   (json.dumps({**progress, "fresh_full_probe":outcome}, ensure_ascii=False),
                    row["token_hash"], row["answers"]))
        db.commit()
    logger.info("STRATEGY_DELIVERY fresh_full_probe result=%s order=%s", outcome, FRESH_TEST_ORDER)
    return True


def start_delivery_worker():
    if not config()["enabled"]:
        return
    def loop():
        # Retry the saved, completed questionnaires immediately after a restart
        # (for example, after the owner fixes the webhook URL or secret).
        # Do not touch incomplete questionnaires or already confirmed backups.
        try:
            with database() as db:
                pending = db.execute("""SELECT COUNT(*) FROM sessions
                    WHERE deal_id IS NOT NULL
                    AND delivery IN ('finish_pending','delivery_failed','finished')
                    AND sheet_delivery='pending'""").fetchone()[0]
                if sheets_configured():
                    db.execute("""UPDATE sessions SET sheet_retry_at=''
                        WHERE deal_id IS NOT NULL
                        AND delivery IN ('finish_pending','delivery_failed','finished')
                        AND sheet_delivery='pending'""")
                    db.commit()
                gc_pending = db.execute("""SELECT COUNT(*) FROM sessions
                    WHERE deal_id IS NOT NULL
                    AND delivery IN ('finish_pending','delivery_failed')""").fetchone()[0]
            logger.info("STRATEGY_BACKUP startup configured=%s pending_completed=%d", sheets_configured(), pending)
            logger.info("STRATEGY_DELIVERY startup method=legacy_comment rollout=%s pending_completed=%d", legacy_rollout_enabled(), gc_pending)
        except (OSError, sqlite3.Error) as exc:
            logger.warning("STRATEGY_BACKUP startup_error=%s", type(exc).__name__)
        while True:
            try:
                if backup_pending_once():
                    continue
                if legacy_full_comment_probe():
                    continue
                if legacy_rollout_enabled() and deliver_pending_once():
                    continue
            except (OSError, sqlite3.Error, ValueError) as exc:
                logger.warning("STRATEGY_DELIVERY worker_error=%s", type(exc).__name__)
            delivery_wakeup.wait(10)
            delivery_wakeup.clear()
    threading.Thread(target=loop, name="strategy-delivery", daemon=True).start()


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
        # Only new sessions may opt into GetCourse-generated numbers. A unique local
        # placeholder is never sent to GetCourse and is replaced after confirmation.
        initial_number = "auto:" + key if auto_order_number_enabled() else order_number(key)
        db.execute("""INSERT OR IGNORE INTO sessions
            (token_hash,order_number,name,email,phone,consent_at,consent_url,policy_url,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (key, initial_number, name, email, phone, now(), cfg["consent_url"], cfg["policy_url"], now()))
        row = db.execute("SELECT * FROM sessions WHERE token_hash=?", (key,)).fetchone()
        # Earlier versions stored an alphanumeric number rejected by GetCourse.
        # Migrate only sessions without a confirmed deal, preserving established IDs.
        if not row["deal_id"] and not row["order_number"].isdigit() and not row["order_number"].startswith("auto:"):
            db.execute("UPDATE sessions SET order_number=? WHERE token_hash=?",
                       (order_number(key), key))
            row = db.execute("SELECT * FROM sessions WHERE token_hash=?", (key,)).fetchone()
        db.commit()
        if row["email"] != email:
            return error("Эта анкета уже привязана к другой почте.", 409)
        if row["deal_id"]:
            return jsonify(ok=True, answers=json.loads(row["answers"]), person={k:row[k] for k in ("name","email","phone")})
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM sessions WHERE token_hash=?", (key,)).fetchone()
        if not row["deal_id"]:
            auto = row["order_number"].startswith("auto:")
            if auto:
                # Persist uncertainty BEFORE calling an API without an idempotency key.
                if row["delivery"] == "auto_create_inflight":
                    db.commit()
                    return error("Не удалось подтвердить заказ. Обратитесь в поддержку.", 502)
                db.execute("UPDATE sessions SET delivery='auto_create_inflight' WHERE token_hash=?", (key,))
                db.commit()
            try:
                initial_comment = "Индивидуальная маркетинговая стратегия\nАнкета начата, ответы ещё не отправлены."
                result = upsert_order(row["email"], None, initial_comment, row["name"], row["phone"]) if auto else send(row, initial_comment)
            except GetCourseError:
                db.commit()
                return error("Не удалось подтвердить заказ. Обратитесь в поддержку." if auto else "Не удалось подтвердить заказ. Нажмите кнопку ещё раз.", 502)
            number = str(result["deal_number"]) if auto else row["order_number"]
            db.execute("UPDATE sessions SET order_number=?,deal_id=?,delivery='started',updated_at=? WHERE token_hash=?",
                       (number, str(result["deal_id"]), now(), key))
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


@api.post("/delivery")
def delivery_status():
    try:
        key = digest(payload() or {})
    except ValueError as exc:
        return error(str(exc))
    with database() as db:
        row = db.execute("SELECT delivery FROM sessions WHERE token_hash=? AND deal_id IS NOT NULL", (key,)).fetchone()
        if not row:
            return error("Анкета не найдена.", 404)
        return jsonify(ok=True, delivery=row["delivery"])


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
            return jsonify(ok=True, complete=True)
        if final and row["answers"] == serialized and row["delivery"] in ("finish_pending", "delivery_failed"):
            db.commit()
            return jsonify(ok=True, queued=True)
        state = "finish_pending" if final else "started"
        if not final and row["answers"] == serialized and row["delivery"] == "finished":
            state = "finished"
        changed = row["answers"] != serialized
        db.execute("""UPDATE sessions SET answers=?,delivery=?,updated_at=?,
                    sheet_delivery=CASE WHEN ? THEN 'pending' ELSE sheet_delivery END,
                    sheet_retry_at=CASE WHEN ? THEN '' ELSE sheet_retry_at END,
                    delivery_parts=CASE WHEN ? THEN '[]' ELSE delivery_parts END,
                    delivery_index=CASE WHEN ? THEN 0 ELSE delivery_index END,
                    delivery_rejections=CASE WHEN ? THEN 0 ELSE delivery_rejections END,
                    delivery_error=CASE WHEN ? THEN '' ELSE delivery_error END,
                    legacy_progress=CASE WHEN ? THEN '{}' ELSE legacy_progress END
                    WHERE token_hash=?""",
                   (serialized, state, now(), final or changed, final or changed,
                    changed, changed, changed, changed, changed, key))
        db.commit()
        if final:
            delivery_wakeup.set()
            return jsonify(ok=True, queued=True)
        return jsonify(ok=True)
