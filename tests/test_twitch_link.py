import json
import socket
import tempfile
import unittest
from pathlib import Path

from tca.commands import CommandFile
from tca.config_store import Config
from tca.main import (
    Runtime,
    _wait_until_port_free,
    twitch_link_action,
    twitch_save_action,
)
from tca.request_log import RequestLog
from tca.status import AppStatus


class TwitchReconnectTests(unittest.TestCase):
    def test_saving_the_same_app_keeps_a_live_login(self) -> None:
        self.assertEqual(
            twitch_save_action(changed=False, running=True, serving=True, has_user=True),
            "keep",
        )

    def test_saving_without_a_login_opens_the_page_that_is_already_up(self) -> None:
        self.assertEqual(
            twitch_save_action(changed=False, running=True, serving=True, has_user=False),
            "open",
        )

    def test_a_login_page_that_is_not_serving_is_replaced(self) -> None:
        self.assertEqual(
            twitch_save_action(changed=False, running=True, serving=False, has_user=False),
            "restart",
        )
        self.assertEqual(twitch_link_action(running=True, serving=False), "restart")
        self.assertEqual(twitch_link_action(running=True, serving=True), "open")

    def test_saving_again_does_not_leave_twitch_checking(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            runtime = _runtime(Path(folder))
            runtime.service = _QuietBot(serving=True)
            (runtime.root / ".tio.tokens.json").write_text(json.dumps({"viewer": {"user_id": "viewer"}}), encoding="utf-8")
            runtime.status.mark_listening("ada")
            runtime._apply_saved_twitch(False)
            self.assertEqual(runtime.status.snapshot().twitch_state, "connected")
            self.assertFalse(runtime.status.snapshot().twitch_can_link)

    def test_link_restarts_a_bot_that_cannot_take_the_approval(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            runtime = _runtime(Path(folder))
            bot = _QuietBot(serving=False)
            runtime.service = bot
            runtime.status.mark_twitch_unlinked()
            runtime._link_twitch_worker()
            self.assertEqual(bot.stopped, [True])
            self.assertEqual(runtime.status.snapshot().twitch_state, "needs_setup")
            self.assertNotEqual(runtime.status.snapshot().twitch_state, "checking")

    def test_unlink_removes_the_saved_login(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            runtime = _runtime(Path(folder))
            bot = _QuietBot(serving=True)
            runtime.service = bot
            token = runtime.root / ".tio.tokens.json"
            token.write_text(json.dumps({"viewer": {"user_id": "viewer"}}), encoding="utf-8")
            runtime._unlink_twitch()
            self.assertEqual(bot.stopped, [False])
            self.assertFalse(token.exists())
            self.assertEqual(runtime.status.snapshot().twitch_state, "needs_setup")

    def test_a_busy_port_is_not_treated_as_free(self) -> None:
        held = socket.socket()
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        try:
            self.assertFalse(_wait_until_port_free(port, 0.3))
        finally:
            held.close()
        self.assertTrue(_wait_until_port_free(port, 1.5))


class _QuietBot:
    def __init__(self, *, serving: bool) -> None:
        self._serving = serving
        self.stopped: list[bool] = []

    def is_running(self) -> bool:
        return True

    def accepts_oauth(self) -> bool:
        return self._serving

    def stop(self, *, save_tokens: bool = True) -> None:
        self.stopped.append(save_tokens)


def _runtime(root: Path) -> Runtime:
    status = AppStatus()
    return Runtime(
        root=root,
        config_path=root / "config.json",
        config=Config(),
        commands=CommandFile(root / "commands.json"),
        requests=RequestLog(root / ".song_requests.json"),
        status=status,
    )


if __name__ == "__main__":
    unittest.main()
