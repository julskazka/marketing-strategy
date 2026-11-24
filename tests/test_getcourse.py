import os
import unittest
from unittest.mock import patch
from backend.getcourse import build_payload, upsert_order, GetCourseError

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
