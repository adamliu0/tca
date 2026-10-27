import json
import tempfile
import unittest
from pathlib import Path

from tca.commands import CommandFile, CommandFileError, HandlerCommand, TextCommand, on_cooldown, parse_commands


class CommandTests(unittest.TestCase):
    def test_parse_text_and_handlers(self) -> None:
        book = parse_commands(
            {
                "cooldown_seconds": 10,
                "!Discord": " https://discord.gg/example ",
                "!song": {"handler": "spotify_song"},
            }
        )
        self.assertEqual(book.cooldown_seconds, 10)
        self.assertEqual(book.match("!discord")[1].text, "https://discord.gg/example")  # type: ignore[union-attr]
        matched = book.match("  !SONG now  ")
        self.assertIsNotNone(matched)
        assert matched is not None
        self.assertIsInstance(matched[1], HandlerCommand)
        self.assertIsNone(book.match("play !song"))
        self.assertIsNone(book.match("hello"))

    def test_rejects_bad_entries(self) -> None:
        with self.assertRaises(CommandFileError):
            parse_commands({"!song": {"handler": "delete_everything"}})
        with self.assertRaises(CommandFileError):
            parse_commands({"song": "missing the bang"})
        with self.assertRaises(CommandFileError):
            parse_commands([])

    def test_cooldown(self) -> None:
        self.assertFalse(on_cooldown(None, 100, 10))
        self.assertTrue(on_cooldown(95, 100, 10))
        self.assertFalse(on_cooldown(90, 100, 10))
        self.assertFalse(on_cooldown(95, 100, 0))

    def test_file_reloads_and_keeps_the_previous_list_when_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "commands.json"
            path.write_text(
                json.dumps({"!song": {"handler": "spotify_song"}}) + "\n",
                encoding="utf-8",
            )
            commands = CommandFile(path)
            self.assertIsInstance(commands.book.commands["!song"], HandlerCommand)

            path.write_text("{", encoding="utf-8")
            commands._mtime = 0
            self.assertFalse(commands.reload_if_changed())
            self.assertIn("!song", commands.book.commands)

            path.write_text(json.dumps({"!discord": "hello"}) + "\n", encoding="utf-8")
            commands._mtime = 0
            self.assertTrue(commands.reload_if_changed())
            self.assertIsInstance(commands.book.commands["!discord"], TextCommand)
            self.assertNotIn("!song", commands.book.commands)

    def test_missing_file_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "commands.json"
            commands = CommandFile(path)
            self.assertTrue(path.exists())
            self.assertIn("!playlist", commands.book.commands)


if __name__ == "__main__":
    unittest.main()
