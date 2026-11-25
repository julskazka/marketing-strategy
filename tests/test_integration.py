import os
import tempfile
import unittest
from unittest.mock import patch
from backend.app import app
from backend.getcourse import GetCourseError
from backend.integration import comment, route, schema, database, digest, order_number, deliver_pending_once, backup_pending_once, legacy_comment_parts


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        env = patch.dict(os.environ, {
            "STRATEGY_DATA_DIR": self.folder.name,
            "GETCOURSE_ACCOUNT": "test",
            "GETCOURSE_OFFER_ID": "123",
            "GETCOURSE_API_KEY": "fake",
            "GETCOURSE_LIVE": "1",
            "GETCOURSE_COMMENT_ROLLOUT": "0",
            "GETCOURSE_POLICY_URL": "https://example.invalid/policy",
            "GETCOURSE_CONSENT_URL": "https://example.invalid/consent",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = app.test_client()
        self.token = "a" * 64
        self.person = {"token": self.token, "name": "Тест", "email": "test@example.invalid",
                       "phone": "+7 999 000-00-00", "consent": True}
        self.answers = {
            "region":"foreign", "stage":"none", "format":"home", "product":"Другое",
            "goal":"Больше обращений", "channels":["Заказов пока нет"], "change":"no",
            "maps_existing":False, "inquiries":"no", "ready":"yes", "time":"yes",
            "capacity":"room", "budget":"no", "access":"ok", "note":"Тест"
        }

    def post(self, method, **data):
        return self.client.post("/api/strategy/" + method, json=data)

    @patch("backend.integration.upsert_order", return_value={"deal_id": 7})
    def test_same_order_and_readable_answers(self, send):
        self.assertEqual(self.post("start", **self.person).status_code, 200)
        self.assertEqual(self.post("start", **self.person).status_code, 200)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.post("save", token=self.token, answers=self.answers).status_code, 200)
        self.assertEqual(self.post("resume", token=self.token).json["answers"], self.answers)
        first = self.post("finish", token=self.token, answers=self.answers)
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json["queued"])
        self.assertEqual(send.call_count, 1)  # result is available before GetCourse delivery
        with patch.dict(os.environ, {"GETCOURSE_COMMENT_ROLLOUT":"1"}), patch("backend.integration.add_legacy_order_comment") as add:
            self.assertTrue(deliver_pending_once())
            self.assertFalse(deliver_pending_once())
            add.assert_called_once_with(self.person["email"], order_number(digest(self.person)), "7", comment(self.answers))
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.post("delivery", token=self.token).json["delivery"], "finished")

    @patch("backend.integration.upsert_order")
    def test_start_retry(self, send):
        send.side_effect = [GetCourseError("temporary"), {"deal_id":7}]
        self.assertEqual(self.post("start", **self.person).status_code, 502)
        self.assertEqual(self.post("start", **self.person).status_code, 200)
        self.assertEqual(send.call_args_list[0].args[1], send.call_args_list[1].args[1])
        self.assertTrue(send.call_args_list[0].args[1].isdigit())

    @patch("backend.integration.upsert_order", return_value={"deal_id":7})
    def test_old_pending_session_migrates_without_new_order(self, send):
        key = digest(self.person)
        with database() as db:
            db.execute("""INSERT INTO sessions
                (token_hash,order_number,name,email,phone,consent_at,consent_url,policy_url,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)""", (key, "ms-" + key[:32], "Тест",
                "test@example.invalid", "+7 999 000-00-00", "now", "https://loonsisters.getcourse.ru/privacy", "", "now"))
            db.commit()
        self.assertEqual(self.post("start", **self.person).status_code, 200)
        self.assertEqual(self.post("start", **self.person).status_code, 200)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[1], order_number(key))
        with database() as db:
            row = db.execute("SELECT order_number,deal_id FROM sessions WHERE token_hash=?", (key,)).fetchone()
            self.assertEqual(row["order_number"], order_number(key))
            self.assertEqual(row["deal_id"], "7")

    @patch("backend.integration.upsert_order", return_value={"deal_id":7})
    def test_legacy_pilot_keeps_other_answers_queued_until_approval(self, send):
        self.assertEqual(self.post("start", **self.person).status_code, 200)
        self.assertTrue(self.post("finish", token=self.token, answers=self.answers).json["queued"])
        with patch("backend.integration.add_legacy_order_comment") as add:
            self.assertFalse(deliver_pending_once())
            add.assert_not_called()
        with database() as db:
            db.execute("UPDATE sessions SET delivery='delivery_failed',delivery_error='V2_NOT_CONFIGURED'")
            db.commit()
        with patch("backend.integration.LEGACY_PILOT_ORDER", order_number(digest(self.person))), patch("backend.integration.add_legacy_order_comment") as add:
            self.assertTrue(deliver_pending_once())
            self.assertFalse(deliver_pending_once())
            add.assert_called_once_with(self.person["email"], order_number(digest(self.person)), "7", comment(self.answers))
            self.assertEqual(self.post("resume", token=self.token).json["answers"], self.answers)
            with patch.dict(os.environ, {"GETCOURSE_COMMENT_ROLLOUT":"1"}):
                self.assertFalse(deliver_pending_once())
            self.assertEqual(add.call_count, 1)
        self.assertEqual(self.post("delivery", token=self.token).json["delivery"], "finished")

    @patch("backend.integration.upsert_order", return_value={"deal_id":7})
    def test_legacy_splits_only_explicit_validation_failure(self, send):
        self.post("start", **self.person)
        self.post("finish", token=self.token, answers=self.answers)
        with patch("backend.integration.LEGACY_PILOT_ORDER", order_number(digest(self.person))), patch("backend.integration.add_legacy_order_comment") as add:
            add.side_effect = [GetCourseError("COMMENT_VALIDATION")] + [None] * 100
            self.assertTrue(deliver_pending_once())
            for _ in range(100):
                if not deliver_pending_once():
                    break
            texts = [c.args[3].split("\n", 1)[1] for c in add.call_args_list[1:]]
            self.assertEqual("".join(texts), comment(self.answers))
            self.assertTrue(all(len(t.encode("utf-8")) <= 180 for t in texts))
            self.assertFalse(deliver_pending_once())

    @patch("backend.integration.upsert_order", return_value={"deal_id":7})
    def test_sheet_backup_upserts_same_order_and_preserves_saved_answers(self, send):
        self.post("start", **self.person)
        self.post("finish", token=self.token, answers=self.answers)
        with patch.dict(os.environ, {"GOOGLE_SHEETS_WEBHOOK_URL":"https://script.google.com/macros/s/test/exec",
                                  "GOOGLE_SHEETS_WEBHOOK_SECRET":"fake"}), patch("backend.integration.send_backup") as backup:
            self.assertTrue(backup_pending_once())
            self.assertFalse(backup_pending_once())
            self.assertEqual(backup.call_count, 1)
            row, answers, status = backup.call_args.args
            self.assertEqual(row["order_number"], order_number(digest(self.person)))
            self.assertIn("Анкета заполнена", answers)
            self.assertIn("Ожидает", status)
            self.assertEqual(self.post("resume", token=self.token).json["answers"], self.answers)
            self.post("finish", token=self.token, answers=self.answers)
            self.assertFalse(backup_pending_once())
            self.assertEqual(backup.call_count, 1)
        self.assertEqual(send.call_count, 1)

    @patch("backend.integration.upsert_order", return_value={"deal_id":7})
    def test_legacy_ambiguous_failure_stops_retries_without_losing_answers(self, send):
        self.assertEqual(self.post("start", **self.person).status_code, 200)
        self.assertTrue(self.post("finish", token=self.token, answers=self.answers).json["queued"])
        with patch("backend.integration.LEGACY_PILOT_ORDER", order_number(digest(self.person))), patch("backend.integration.add_legacy_order_comment", side_effect=GetCourseError("TIMEOUT")) as add:
            self.assertFalse(deliver_pending_once())
            self.assertFalse(deliver_pending_once())
            add.assert_called_once()
        self.assertEqual(self.post("delivery", token=self.token).json["delivery"], "delivery_failed")

    @patch("backend.integration.upsert_order", return_value={"deal_id":7})
    def test_consent_and_required_answers(self, send):
        self.assertEqual(self.post("start", **dict(self.person, consent=False)).status_code, 400)
        send.assert_not_called()
        self.assertEqual(self.post("start", **self.person).status_code, 200)
        self.assertEqual(self.post("finish", token=self.token, answers={}).status_code, 400)
        self.assertEqual(self.post("resume", token="b"*64).status_code, 404)
        self.assertEqual(self.post("start", **dict(self.person, email="other@example.invalid")).status_code, 409)
        self.assertEqual(send.call_count, 1)

    def test_disabled(self):
        with patch.dict(os.environ, {"GETCOURSE_LIVE":"0"}):
            self.assertFalse(self.client.get("/healthz").json["submissions_enabled"])
            self.assertEqual(self.post("start", **self.person).status_code, 503)
