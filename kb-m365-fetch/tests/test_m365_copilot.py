#!/usr/bin/env python3
"""Tests for m365-copilot.py — browser isolation, profile secrecy, transport.

No real browser, no login, no mail: the script is loaded from a throwaway
vault; Playwright and websockets are replaced by minimal in-memory fakes that
capture launch/connect parameters. Sentinels prove secrets never reach the
browser environment or any diagnostic stream.
"""

import asyncio
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.parse
import uuid
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "m365-copilot.py"
GIT_ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_SYSTEM": "/dev/null", "HOME": "/nonexistent"}


def load_module(vault: Path):
    spec = importlib.util.spec_from_file_location(
        f"m365_copilot_{id(vault)}", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VaultCase(unittest.TestCase):
    def setUp(self):
        self.vault = Path(tempfile.mkdtemp(prefix="m365-test-"))
        (self.vault / "wiki").mkdir()
        subprocess.run(["git", "init", "-q", str(self.vault)],
                       check=True, capture_output=True, env=GIT_ENV)
        (self.vault / ".gitignore").write_text(".browser-session/\n")
        self._cwd = Path.cwd()
        os.chdir(self.vault)

    def tearDown(self):
        os.chdir(self._cwd)
        import shutil
        shutil.rmtree(self.vault, ignore_errors=True)


class FakeWebSocket:
    def __init__(self, url):
        self.url = url


class FakePage:
    def __init__(self, context):
        self._context = context
        self.url = "https://outlook.cloud.microsoft/mail/"

    async def goto(self, *args, **kwargs):
        # The interceptor is registered before navigation: fire it here,
        # exactly like the real site opening its Copilot socket.
        for handler in self._context.ws_handlers + self.ws_handlers:
            handler(FakeWebSocket(self._context.ws_url))

    async def wait_for_timeout(self, ms):
        for handler in self._context.ws_handlers + self.ws_handlers:
            handler(FakeWebSocket(self._context.ws_url))

    async def wait_for_url(self, *args, **kwargs):
        pass

    def on(self, event, handler):
        if event == "websocket":
            self.ws_handlers.append(handler)

    ws_handlers = []

    def get_by_role(self, *args, **kwargs):
        raise AssertionError("button interaction should not be needed")


class FakeContext:
    def __init__(self, ws_url):
        self.ws_url = ws_url
        self.ws_handlers = []
        self.pages = [FakePage(self)]
        self.launch_kwargs = None
        self.closed = False

    def on(self, event, handler):
        if event == "web_socket":
            self.ws_handlers.append(handler)

    async def close(self):
        self.closed = True


def install_fake_playwright(launch_recorder, ws_url):
    context = FakeContext(ws_url)

    class FakeChromium:
        async def launch_persistent_context(self, user_data_dir, **kwargs):
            launch_recorder["user_data_dir"] = user_data_dir
            launch_recorder["kwargs"] = kwargs
            return context

    class FakeAsyncPlaywright:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        @property
        def chromium(self):
            return FakeChromium()

    fake_api = types.ModuleType("playwright.async_api")
    fake_api.async_playwright = FakeAsyncPlaywright
    fake_pw = types.ModuleType("playwright")
    fake_pw.async_api = fake_api
    sys.modules["playwright"] = fake_pw
    sys.modules["playwright.async_api"] = fake_api
    return context


class FakeWSConnection:
    def __init__(self, script):
        self.script = list(script)
        self.sent = []

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class ConnectionClosedOK(Exception):
    pass


def install_fake_websockets(connect_recorder, script):
    fake_exc = types.ModuleType("websockets.exceptions")
    fake_exc.ConnectionClosedOK = ConnectionClosedOK
    fake_exc.ConnectionClosedError = type("ConnectionClosedError", (Exception,), {})

    def connect(url, **kwargs):
        connect_recorder["url"] = url
        connect_recorder["kwargs"] = kwargs
        return FakeWSConnection(script)

    fake_ws = types.ModuleType("websockets")
    fake_ws.exceptions = fake_exc
    fake_ws.connect = connect
    sys.modules["websockets"] = fake_ws
    sys.modules["websockets.exceptions"] = fake_exc


class BrowserIsolationTest(VaultCase):
    def setUp(self):
        super().setUp()
        self._env = dict(os.environ)
        os.environ["CANARY_SECRET"] = "ambient-canary"
        (self.vault / ".env.local").write_text(
            "ATLASSIAN_API_KEY=file-canary\nCOPILOT_DEBUG=1\n")
        self.module = load_module(self.vault)
        self.launch = {}
        self.token = "tok&with#+=specials chars"
        self.oid, self.tid = str(uuid.uuid4()), str(uuid.uuid4())
        encoded = urllib.parse.urlencode({"access_token": self.token})
        self.ws_url = (f"wss://substrate.office.com/m365Copilot/Chathub/"
                       f"{self.oid}@{self.tid}?{encoded}")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        sys.modules.pop("playwright", None)
        sys.modules.pop("playwright.async_api", None)
        super().tearDown()

    def test_browser_env_is_allowlisted_and_session_private(self):
        context = install_fake_playwright(self.launch, self.ws_url)
        token_data = asyncio.run(self.module.extract_token_from_browser())
        kwargs = self.launch["kwargs"]
        env = kwargs["env"]
        self.assertNotIn("CANARY_SECRET", env)
        self.assertNotIn("ATLASSIAN_API_KEY", env)
        self.assertNotIn("COPILOT_DEBUG", env)
        self.assertIn("PATH", env)
        mode = stat.S_IMODE(os.stat(self.vault / ".browser-session").st_mode)
        self.assertEqual(mode, 0o700)
        self.assertTrue(context.closed)
        # token decoded from the encoded browser URL
        self.assertEqual(token_data["access_token"], self.token)
        self.assertEqual(token_data["oid"], self.oid)

    def test_debug_flag_loaded_but_never_passed_to_browser(self):
        self.module.load_env()
        self.assertEqual(os.environ.get("COPILOT_DEBUG"), "1")
        install_fake_playwright(self.launch, self.ws_url)
        asyncio.run(self.module.extract_token_from_browser())
        self.assertNotIn("COPILOT_DEBUG", self.launch["kwargs"]["env"])

    def test_existing_0755_session_dir_tightened(self):
        session = self.vault / ".browser-session"
        session.mkdir()
        session.chmod(0o755)
        install_fake_playwright(self.launch, self.ws_url)
        asyncio.run(self.module.extract_token_from_browser())
        self.assertEqual(
            stat.S_IMODE(os.stat(session).st_mode), 0o700)

    def test_symlink_profile_refused(self):
        session = self.vault / ".browser-session"
        real = self.vault / "real-session"
        real.mkdir()
        session.symlink_to(real)
        with self.assertRaises(SystemExit):
            self.module.ensure_private_dir(session)

    def test_nonignored_session_fails_without_writing_gitignore(self):
        (self.vault / ".gitignore").write_text("node_modules/\n")
        gitignore_before = (self.vault / ".gitignore").read_text()
        with self.assertRaises(SystemExit) as ctx:
            with redirect_stderr(StringIO()):
                asyncio.run(self.module.extract_token_from_browser())
        self.assertIn("ignored by Git", str(ctx.exception))
        self.assertEqual((self.vault / ".gitignore").read_text(),
                         gitignore_before)

    def test_invalid_uuid_identity_refused(self):
        bad = ("wss://substrate.office.com/m365Copilot/Chathub/"
               f"not-a-uuid@{self.tid}?access_token=x")
        context = install_fake_playwright(self.launch, bad)
        with self.assertRaises(SystemExit):
            with redirect_stderr(StringIO()):
                asyncio.run(self.module.extract_token_from_browser())
        self.assertTrue(context.closed)


class LoadEnvTest(VaultCase):
    def test_only_copilot_debug_is_loaded_with_file_precedence(self):
        (self.vault / ".env").write_text("COPILOT_DEBUG=0\nOTHER_VAR=a\n")
        (self.vault / ".env.local").write_text(
            "ATLASSIAN_API_KEY=file-canary\nCOPILOT_DEBUG=1\n")
        module = load_module(self.vault)
        module.load_env()
        self.assertEqual(os.environ.get("COPILOT_DEBUG"), "1")
        self.assertNotIn("OTHER_VAR", os.environ)
        self.assertNotIn("ATLASSIAN_API_KEY", os.environ)

    def test_process_environment_wins(self):
        os.environ["COPILOT_DEBUG"] = "process"
        (self.vault / ".env.local").write_text("COPILOT_DEBUG=file\n")
        module = load_module(self.vault)
        module.load_env()
        self.assertEqual(os.environ.get("COPILOT_DEBUG"), "process")
        del os.environ["COPILOT_DEBUG"]


class TransportTest(VaultCase):
    def setUp(self):
        super().setUp()
        self._env = dict(os.environ)
        self.module = load_module(self.vault)
        self.connect = {}

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        sys.modules.pop("websockets", None)
        sys.modules.pop("websockets.exceptions", None)
        super().tearDown()

    @staticmethod
    def token_data(token="tok&with#+=specials"):
        return {"access_token": token,
                "oid": str(uuid.uuid4()), "tid": str(uuid.uuid4())}

    def frames(self, text="Reply body."):
        rs = "\x1e"
        return [json.dumps({}) + rs,
                json.dumps({"type": 2, "item": {"messages": [
                    {"author": "bot", "text": text}]}}) + rs]

    def test_token_round_trips_through_query_string(self):
        sentinel = self.token_data()
        install_fake_websockets(self.connect, self.frames("Hello."))
        result = asyncio.run(self.module.query_copilot("prompt", sentinel))
        self.assertEqual(result, "Hello.")
        url = self.connect["url"]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(query["access_token"][0], sentinel["access_token"])
        self.assertIn(f"{sentinel['oid']}@{sentinel['tid']}", url)

    def test_invalid_uuid_identity_fails_before_connect(self):
        install_fake_websockets(self.connect, self.frames())
        with self.assertRaises(SystemExit):
            asyncio.run(self.module.query_copilot(
                "p", {"access_token": "t", "oid": "nope", "tid": "nope"}))
        self.assertNotIn("url", self.connect)

    def test_connection_exception_is_sanitized_and_fails_closed(self):
        sentinel = "tok-with-SENTINEL-inside"
        install_fake_websockets(
            self.connect, [RuntimeError(f"connect failed: {sentinel}")])
        stderr = StringIO()
        with self.assertRaises(SystemExit) as ctx, redirect_stderr(stderr):
            asyncio.run(self.module.query_copilot(
                "p", self.token_data(sentinel)))
        self.assertEqual(ctx.exception.code, 1)
        self.assertNotIn("SENTINEL", stderr.getvalue())
        self.assertIn("Copilot connection failed", stderr.getvalue())
        self.assertNotIn("SENTINEL", str(ctx.exception))

    def test_graceful_close_returns_collected_text(self):
        sentinel = self.token_data()
        install_fake_websockets(
            self.connect, self.frames("Partial.") + [ConnectionClosedOK()])
        result = asyncio.run(self.module.query_copilot("p", sentinel))
        self.assertEqual(result, "Partial.")

    def test_silence_timeout_returns_empty_not_crash(self):
        install_fake_websockets(
            self.connect, [json.dumps({}) + "\x1e", asyncio.TimeoutError()])
        result = asyncio.run(self.module.query_copilot(
            "p", self.token_data()))
        self.assertEqual(result, "")

    def test_debug_frames_log_type_and_length_only(self):
        os.environ["COPILOT_DEBUG"] = "1"
        sentinel = "tok-SENTINEL"
        rs = "\x1e"
        body = json.dumps({"type": 2, "item": {"messages": [
            {"author": "bot", "text": f"{sentinel} secret text"}]}}) + rs
        install_fake_websockets(
            self.connect, [json.dumps({}) + rs, body])
        stderr = StringIO()
        with redirect_stderr(stderr):
            asyncio.run(self.module.query_copilot(
                "p", self.token_data(sentinel)))
        out = stderr.getvalue()
        self.assertIn("[debug] frame type=", out)
        self.assertNotIn("SENTINEL", out)
        self.assertNotIn("secret text", out)


if __name__ == "__main__":
    unittest.main()
