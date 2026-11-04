"""Offline contract tests for durable project update lifecycle."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import update_state


class UpdateStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = update_state.DB
        update_state.DB = os.path.join(self.tmp.name, "updates.sqlite3")

    def tearDown(self):
        update_state.DB = self.previous
        self.tmp.cleanup()

    def test_ready_claim_prevents_duplicate_apply(self):
        record = update_state.start(123, 45, "ЖК МОД", "описание факта")
        self.assertEqual(record["state"], "received")
        key = "123:45"
        self.assertEqual(update_state.get(key)["state"], "received")
        update_state.save(key, "ready", '{"project":"ЖК МОД"}')
        self.assertTrue(update_state.claim_apply(key))
        self.assertFalse(update_state.claim_apply(key))
        self.assertEqual(update_state.get(key)["state"], "applying")

    def test_completed_request_cannot_be_applied_again_after_restart(self):
        update_state.start(5, 99, "ЖК МОД", "одна новая задача")
        update_state.save("5:99", "ready", '{"project":"ЖК МОД"}')
        self.assertTrue(update_state.claim_apply("5:99"))
        update_state.save("5:99", "applied", detail="Новая задача MOD-T-022")
        self.assertFalse(update_state.claim_apply("5:99"))
        self.assertEqual(update_state.get("5:99")["state"], "applied")

    def test_cancelled_request_cannot_be_applied(self):
        update_state.start(5, 100, "ЖК МОД", "тест отмены")
        update_state.save("5:100", "ready", '{"project":"ЖК МОД"}')
        update_state.save("5:100", "cancelled")
        self.assertFalse(update_state.claim_apply("5:100"))

    def test_restart_read_and_status(self):
        update_state.start(1, 11, "ЖК МОД", "текст")
        update_state.save("1:11", "failed", detail="потеря соединения")
        self.assertEqual(update_state.latest(1)["state"], "failed")
        self.assertEqual(update_state.latest(1)["detail"], "потеря соединения")
        self.assertIsNone(update_state.latest(2))

    def test_different_messages_are_independent(self):
        update_state.start(1, 1, "ЖК МОД", "первый")
        update_state.start(1, 2, "ЖК МОД", "второй")
        update_state.save("1:1", "cancelled")
        self.assertEqual(update_state.get("1:1")["state"], "cancelled")
        self.assertEqual(update_state.get("1:2")["state"], "received")


if __name__ == "__main__":
    unittest.main()
