"""Server-only GetCourse API adapter with privacy-safe diagnostics."""
import base64
import json
import logging
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger("gunicorn.error")


def rejection_code(body):
    result = body.get("result")
    result = result if isinstance(result, dict) else {}
    msg = " ".join(str(x) for x in (body.get("error"), body.get("error_message"),
                                     result.get("error"), result.get("error_message"))).lower()
    if any(x in msg for x in ("неавториз", "invalid key", "unauthoriz", "неверный ключ")):
        return "AUTH_KEY"
    if any(x in msg for x in ("действие запрещено", "forbidden", "access denied", "нет прав", "read only")):
        return "WRITE_PERMISSION"
    if "лимит" in msg or "limit" in msg:
        return "API_LIMIT"
    if ("предложен" in msg or "offer" in msg) and any(x in msg for x in ("не найден", "not found", "не существует", "invalid")):
        return "OFFER_NOT_FOUND"
    if any(x in msg for x in ("пустой параметр action", "не указаны параметры", "params", "base64")):
        return "REQUEST_PARAMS"
    if any(x in msg for x in ("эл. адрес", "email", "телефон", "phone")):
        return "USER_PARAMS"
    if any(x in msg for x in ("номер заказа", "deal_number", "order number")):
        return "ORDER_NUMBER"
    return "API_REJECTED"


def safe_error_message(body, secrets):
    """Diagnostic message only; redact submitted values and credentials."""
    result = body.get("result")
    result = result if isinstance(result, dict) else {}
    parts = []
    for obj in (result, body):
        for field in ("error_message", "message", "error", "errors"):
            val = obj.get(field)
            if isinstance(val, str) and val.lower() not in ("true", "false", "0", "1"):
                parts.append(val)
            elif isinstance(val, (list, dict)):
                parts.append(json.dumps(val, ensure_ascii=False))
    msg = " | ".join(parts)[:4000]
    for secret in sorted((str(x) for x in secrets if x), key=len, reverse=True):
        msg = re.sub(re.escape(secret), "[redacted]", msg, flags=re.IGNORECASE)
    msg = re.sub(r"https?://[^\s<>'\"]+", "[url]", msg)
    msg = re.sub(r"[^\s<>@]+@[^\s<>@]+", "[email]", msg)
    msg = re.sub(r"(?<!\w)\+?\d[\d\s().-]{7,}\d", "[number]", msg)
    msg = re.sub(r"[A-Za-z0-9_+/=-]{20,}", "[token]", msg)
    msg = re.sub(r"<[^>]*>", "", msg)
    return " ".join(msg.split())[:350] or "(no error message)"


def fail(code, **details):
    # Fixed codes and redacted diagnostic messages only; never log entire bodies.
    logger.warning("GETCOURSE_DIAG %s %s", code, details)
    raise GetCourseError(code)


class GetCourseError(RuntimeError):
    pass

def build_payload(email, order_number, offer_id, comment, first_name="", phone=""):
    if not email or not order_number or not offer_id:
        raise ValueError("Required order parameters missing")
    user = {"email": email}
    if first_name:
        user["first_name"] = first_name
    if phone:
        user["phone"] = phone
    return {
        "user": user,
        "system": {"refresh_if_exists": 0, "return_deal_number": 1},
        "deal": {"deal_number": order_number, "offer_id": offer_id,
                 "deal_comment": comment},
    }

def upsert_order(email, order_number, comment, first_name="", phone=""):
    if os.getenv("GETCOURSE_LIVE", "0") != "1":
        raise GetCourseError("Live integration disabled")
    account = os.getenv("GETCOURSE_ACCOUNT", "").strip()
    key = os.getenv("GETCOURSE_API_KEY", "").strip()
    offer = os.getenv("GETCOURSE_OFFER_ID", "").strip()
    if not re.fullmatch(r"[a-zA-Z0-9-]+", account) or not key or not offer.isdigit():
        fail("CONFIG_MISSING")
    payload = build_payload(email, order_number, int(offer), comment, first_name, phone)
    encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
    secrets = (key, email, first_name, phone, re.sub(r"\D", "", phone), order_number, comment, encoded)
    request = Request(
        "https://" + account + ".getcourse.ru/pl/api/deals",
        data=urlencode({"action": "add", "key": key, "params": encoded}).encode("ascii"),
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            status = response.status
            body = json.load(response)
    except HTTPError as exc:
        try:
            body = json.load(exc)
        except (ValueError, UnicodeError, OSError):
            body = {}
        code = rejection_code(body) if isinstance(body, dict) else "API_REJECTED"
        if code == "API_REJECTED":
            code = "HTTP_" + str(exc.code)
        fail(code, status=exc.code, detail=safe_error_message(body, secrets) if isinstance(body, dict) else "(no error message)")
    except URLError as exc:
        fail("NETWORK_ERROR", reason_type=type(exc.reason).__name__)
    except TimeoutError:
        fail("TIMEOUT")
    except (ValueError, UnicodeError):
        fail("NON_JSON_RESPONSE")
    except OSError:
        fail("NETWORK_ERROR")
    if not isinstance(body, dict):
        fail("UNEXPECTED_RESPONSE")
    result = body.get("result", {})
    def yes(value):
        return value is True or value == 1 or (isinstance(value, str) and value.lower() in ("true", "1"))
    if not isinstance(result, dict) or not yes(body.get("success")) or not yes(result.get("success")) or yes(result.get("error")):
        fail(rejection_code(body), status=status, top_success=yes(body.get("success")),
             result_success=isinstance(result, dict) and yes(result.get("success")),
             detail=safe_error_message(body, secrets))
    if str(result.get("deal_number", "")) != str(order_number) or not result.get("deal_id"):
        fail("ORDER_CONFIRMATION_INCOMPLETE", has_id=bool(result.get("deal_id")),
             has_number=bool(result.get("deal_number")), number_matches=str(result.get("deal_number", "")) == str(order_number))
    logger.info("GETCOURSE_DIAG ORDER_CONFIRMED")
    return {"deal_id": result["deal_id"], "deal_number": result["deal_number"]}
