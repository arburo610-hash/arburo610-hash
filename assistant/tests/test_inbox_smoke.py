"""Offline smoke checks for the inbox changes. No Telegram or Google credentials required.

Run from repository root:
    python -m unittest discover -s assistant/tests -v
"""
import ast
import pathlib
import re
import unittest

ASSISTANT = pathlib.Path(__file__).resolve().parents[1]


class InboxSmokeTests(unittest.TestCase):
    def code(self, name):
        return (ASSISTANT / name).read_text(encoding="utf-8")

    def test_changed_modules_have_valid_python_syntax(self):
        for name in ("assistant_bot.py", "notes_sheets.py", "photo_server.py"):
            with self.subTest(name=name):
                ast.parse(self.code(name), filename=name)

    def test_note_id_patterns(self):
        self.assertEqual(re.findall(r"#(\d+)", "Фото #5 #6 #8"), ["5", "6", "8"])
        self.assertEqual(re.findall(r"(?<!\d)\d+(?!\d)", "5 6 8"), ["5", "6", "8"])

    def test_inbox_sync_is_unified_and_nondestructive(self):
        source = self.code("notes_sheets.py")
        self.assertIn("return tables().get(INBOX_KEY)", source)
        self.assertNotIn("delete_rows(", source)

    def test_client_message_and_album_are_owner_only(self):
        module = ast.parse(self.code("assistant_bot.py"))
        for name in ("client_message_draft", "inbox_album"):
            node = next(n for n in module.body if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
            self.assertIn("allowed(update)", ast.unparse(node))
        source = self.code("assistant_bot.py")
        self.assertIn("send_media_group(", source)
        self.assertIn("BRIEF_PROMPT", source)

    def test_public_photo_endpoint_off_by_default(self):
        source = self.code("photo_server.py")
        self.assertIn('os.environ.get("ENABLE_PUBLIC_PHOTOS", "")', source)
        self.assertIn("if not public_photos_enabled():", source)


if __name__ == "__main__":
    unittest.main()
