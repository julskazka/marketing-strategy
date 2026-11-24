"""Private, idempotent Google Sheets webhook transport for questionnaire backups."""
import hashlib
import hmac
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class SheetsError(RuntimeError):
    pass


def configured():
    return bool(os.getenv("GOOGLE_SHEETS_WEBHOOK_URL") and os.getenv("GOOGLE_SHEETS_WEBHOOK_SECRET"))


def send_backup(row, readable_answers, getcourse_status):
    url = os.getenv("GOOGLE_SHEETS_WEBHOOK_URL", "").strip()
    secret = os.getenv("GOOGLE_SHEETS_WEBHOOK_SECRET", "")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "script.google.com" or not parsed.path.startswith("/macros/s/") or not secret:
        raise SheetsError("CONFIG")
    data = json.dumps({"order_number": row["order_number"], "updated_at": row["updated_at"],
                       "name": row["name"], "email": row["email"], "phone": row["phone"],
                       "answers": readable_answers, "getcourse": getcourse_status},
                      ensure_ascii=False, separators=(",", ":"))
    signature = hmac.new(secret.encode("utf-8"), data.encode("utf-8"), hashlib.sha256).hexdigest()
    body = json.dumps({"payload": data, "signature": signature}).encode("utf-8")
    req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=12) as response:
            result = json.load(response)
    except HTTPError as exc:
        raise SheetsError("HTTP_" + str(exc.code)) from None
    except (URLError, TimeoutError, OSError):
        raise SheetsError("NETWORK") from None
    except (ValueError, UnicodeError):
        raise SheetsError("RESPONSE") from None
    if isinstance(result, dict) and result.get("ok") is False:
        code = result.get("code")
        if isinstance(code, str) and code in {"CONFIGURATION", "REQUEST", "SIGNATURE", "PAYLOAD", "LOCK", "SHEET_OPEN", "SHEET_MISSING", "SHEET_WRITE"}:
            raise SheetsError("GOOGLE_" + code)
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise SheetsError("UNCONFIRMED")
    if str(result.get("order_number")) != str(row["order_number"]):
        raise SheetsError("ORDER_MISMATCH")
