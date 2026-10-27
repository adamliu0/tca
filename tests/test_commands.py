import json
import tempfile
import unittest
from pathlib import Path

from tca.commands import (
    CommandFile,
    CommandFileError,
    HandlerCommand,
    TextCommand,
    command_name,
    on_cooldown,
    parse_commands,
)


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

    def test_command_name_accepts_a_word_with_or_without_a_bang(self) -> None:
        self.assertEqual(command_name("Discord"), "!discord")
        self.assertEqual(command_name("  !Song  "), "!song")
        with self.assertRaises(CommandFileError):
            command_name("my command")
        with self.assertRaises(CommandFileError):
            command_name("!")

    def test_add_and_remove_a_text_command_without_touching_built_ins(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "commands.json"
            path.write_text(
                json.dumps({"cooldown_seconds": 10, "!song": {"handler": "spotify_song"}}) + "\n",
                encoding="utf-8",
            )
            commands = CommandFile(path)
            self.assertEqual(commands.add_text_command("discord", " https://discord.gg/example "), "!discord")
            self.assertEqual(commands.text_commands(), [("!discord", "https://discord.gg/example")])
            self.assertEqual(commands.handler_names(), ["!song"])
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["cooldown_seconds"], 10)
            self.assertEqual(saved["!song"], {"handler": "spotify_song"})
            self.assertEqual(saved["!discord"], "https://discord.gg/example")

            with self.assertRaises(CommandFileError):
                commands.add_text_command("!song", "nope")
            with self.assertRaises(CommandFileError):
                commands.add_text_command("!discord", "again")
            with self.assertRaises(CommandFileError):
                commands.add_text_command("!empty", "   ")
            with self.assertRaises(CommandFileError):
                commands.remove_text_command("!song")

            commands.remove_text_command("!discord")
            self.assertEqual(commands.text_commands(), [])
            self.assertIn("!song", commands.book.commands)
            self.assertNotIn("!discord", json.loads(path.read_text(encoding="utf-8")))

    def test_missing_file_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "commands.json"
            commands = CommandFile(path)
            self.assertTrue(path.exists())
            self.assertIn("!playlist", commands.book.commands)
            self.assertIn("!queue", commands.book.commands)


if __name__ == "__main__":
    unittest.main()
