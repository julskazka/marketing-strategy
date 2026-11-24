import os
import io
import json
import base64
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs
from backend.getcourse import build_payload, upsert_order, GetCourseError, rejection_code

class GetCourseTests(unittest.TestCase):
    def test_same_order_number(self):
        first = build_payload("test@example.invalid", "ms-test", 123, "Анкета начата")
        last = build_payload("test@example.invalid", "ms-test", 123, "Анкета заполнена")
        self.assertEqual(first["deal"]["deal_number"], last["deal"]["deal_number"])
        self.assertNotIn("deal_status", last["deal"])
        self.assertNotIn("deal_is_paid", last["deal"])
        self.assertEqual(last["deal"]["deal_comment"], "Анкета заполнена")

    @patch.dict(os.environ, {"GETCOURSE_LIVE": "0"})
    def test_live_disabled(self):
        with self.assertRaises(GetCourseError):
            upsert_order("test@example.invalid", "ms-test", "Анкета начата")

    @patch.dict(os.environ, {"GETCOURSE_LIVE":"1", "GETCOURSE_ACCOUNT":"test",
                          "GETCOURSE_OFFER_ID":"8952690", "GETCOURSE_API_KEY":"fake"})
    @patch("backend.getcourse.urlopen")
    def test_documented_nested_success_and_form_encoding(self, urlopen):
        class Response(io.BytesIO):
            status = 200
        urlopen.return_value.__enter__.return_value = Response(json.dumps({
            "success": True, "action": "add", "result": {
                "success": True, "error": False, "deal_id": 42, "deal_number": 800000000123
            }}).encode())
        result = upsert_order("test@example.invalid", "800000000123", "Анкета начата")
        self.assertEqual(result["deal_id"], 42)
        request = urlopen.call_args.args[0]
        fields = parse_qs(request.data.decode())
        self.assertEqual(fields["action"], ["add"])
        body = json.loads(base64.b64decode(fields["params"][0]))
        self.assertEqual(body["deal"]["offer_id"], 8952690)
        self.assertEqual(body["deal"]["deal_number"], "800000000123")
        self.assertNotIn("deal_cost", body["deal"])

    @patch.dict(os.environ, {"GETCOURSE_LIVE":"1", "GETCOURSE_ACCOUNT":"test",
                          "GETCOURSE_OFFER_ID":"8952690", "GETCOURSE_API_KEY":"fake"})
    @patch("backend.getcourse.urlopen")
    def test_http_200_nested_error_is_rejection(self, urlopen):
        class Response(io.BytesIO):
            status = 200
        urlopen.return_value.__enter__.return_value = Response(json.dumps({
            "success": True, "action": "add", "result": {
                "success": False, "error": True, "error_message": "№ должен быть целым числом."
            }}).encode())
        with self.assertRaises(GetCourseError) as raised:
            upsert_order("test@example.invalid", "ms-invalid", "Анкета начата")
        self.assertEqual(str(raised.exception), "ORDER_NUMBER")
