import json
import tempfile
import unittest
from pathlib import Path

from tca.config_store import load_config, save_config, saved_twitch_user_id
from tca.config_store import Config


class ConfigStoreTests(unittest.TestCase):
    def test_missing_file_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            config = load_config(Path(folder) / "config.json")
        self.assertFalse(config.twitch_ready)
        self.assertFalse(config.spotify_ready)

    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            original = Config(
                twitch_client_id="twitch-id",
                twitch_client_secret="twitch-secret",
                spotify_client_id="spotify-id",
                spotify_client_secret="spotify-secret",
            )
            save_config(path, original)
            loaded = load_config(path)
        self.assertEqual(loaded, original)
        self.assertTrue(loaded.twitch_ready)
        self.assertTrue(loaded.spotify_ready)

    def test_blank_and_invalid_values_are_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(
                json.dumps({"twitch_client_id": "  ", "twitch_client_secret": 5, "spotify_client_id": "ok"}),
                encoding="utf-8",
            )
            config = load_config(path)
        self.assertEqual(config.twitch_client_id, "")
        self.assertEqual(config.spotify_client_id, "ok")
        self.assertFalse(config.twitch_ready)
        self.assertFalse(config.spotify_ready)

    def test_unreadable_file_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text("{", encoding="utf-8")
            config = load_config(path)
        self.assertEqual(config, Config())

    def test_saved_twitch_user_comes_from_the_token_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".tio.tokens.json"
            self.assertIsNone(saved_twitch_user_id(path))
            path.write_text(json.dumps({"12345": {"token": "secret"}}), encoding="utf-8")
            self.assertEqual(saved_twitch_user_id(path), "12345")
            path.write_text("[]", encoding="utf-8")
            self.assertIsNone(saved_twitch_user_id(path))


if __name__ == "__main__":
    unittest.main()
