"""Server-only API adapter. No public endpoint calls it in the starter."""
import base64
import json
import os
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen

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
    account = os.getenv("GETCOURSE_ACCOUNT", "")
    key = os.getenv("GETCOURSE_API_KEY", "")
    offer = os.getenv("GETCOURSE_OFFER_ID", "")
    if not re.fullmatch(r"[a-zA-Z0-9-]+", account) or not key or not offer.isdigit():
        raise GetCourseError("Integration configuration missing")
    payload = build_payload(email, order_number, int(offer), comment, first_name, phone)
    encoded = base64.b64encode(json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
    request = Request(
        "https://" + account + ".getcourse.ru/pl/api/deals",
        data=urlencode({"action": "add", "key": key, "params": encoded}).encode("ascii"),
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            body = json.load(response)
    except Exception:
        raise GetCourseError("GetCourse request failed") from None
    result = body.get("result", {}) if isinstance(body, dict) else {}
    def yes(value):
        return value is True or value == 1 or value == "true" or value == "1"
    if not isinstance(result, dict) or not yes(body.get("success")) or not yes(result.get("success")) or yes(result.get("error")):
        raise GetCourseError("GetCourse rejected the request")
    if str(result.get("deal_number", "")) != str(order_number) or not result.get("deal_id"):
        raise GetCourseError("Order confirmation incomplete")
    return {"deal_id": result["deal_id"], "deal_number": result["deal_number"]}
