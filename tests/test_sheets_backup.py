import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import MagicMock, patch
from backend.sheets_backup import send_backup, SheetsError


class SheetsBackupTests(unittest.TestCase):
    def test_signed_payload_and_confirmed_order(self):
        row = {"order_number":"803481980570", "updated_at":"2026-11-24T00:00:00+00:00",
               "name":"Анна", "email":"a@example.invalid", "phone":"+70000000000"}
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({"ok":True,"order_number":row["order_number"]}).encode()
        with patch.dict(os.environ, {"GOOGLE_SHEETS_WEBHOOK_URL":"https://script.google.com/macros/s/id/exec",
                                  "GOOGLE_SHEETS_WEBHOOK_SECRET":"private"}), patch("backend.sheets_backup.urlopen", return_value=response) as call:
            send_backup(row, "Вопрос\nОтвет", "Получено")
            body = json.loads(call.call_args.args[0].data)
            expected = hmac.new(b"private", body["payload"].encode(), hashlib.sha256).hexdigest()
            self.assertEqual(body["signature"], expected)
            self.assertEqual(json.loads(body["payload"])["order_number"], row["order_number"])

    def test_does_not_accept_other_order_or_untrusted_endpoint(self):
        row = {k:"x" for k in ("order_number","updated_at","name","email","phone")}
        with patch.dict(os.environ, {"GOOGLE_SHEETS_WEBHOOK_URL":"https://example.invalid/receive", "GOOGLE_SHEETS_WEBHOOK_SECRET":"private"}):
            with self.assertRaises(SheetsError):
                send_backup(row, "answer", "pending")
