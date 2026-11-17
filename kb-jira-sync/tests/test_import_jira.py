#!/usr/bin/env python3
"""Tests for import-jira.py — pure rendering and credential resolution.

No credentials, no network, no vault: the script is loaded as a module with a
throwaway vault fixture so its import-time bootstrap (root walk + config read)
can be exercised, then its pure helpers are tested directly.
"""

import base64
import importlib.util
import json
import shutil
import tempfile
import unittest
import urllib.request
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "import-jira.py"


def load_module(vault: Path):
    """Import import-jira.py with its bootstrap pointed at `vault`.

    The module resolves its root from the process cwd at import time, so the
    caller chdir()s into the fixture first. Importing under a unique name keeps
    the two fixture-based tests from sharing module state.
    """
    spec = importlib.util.spec_from_file_location(f"import_jira_{id(vault)}", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VaultCase(unittest.TestCase):
    def setUp(self):
        import os

        self.vault = Path(tempfile.mkdtemp(prefix="import-jira-test-"))
        (self.vault / "wiki").mkdir()
        (self.vault / ".git").mkdir()
        (self.vault / "kb-config.yaml").write_text(
            "jira:\n"
            "  enabled: true\n"
            "  base_url: https://example.atlassian.net\n"
            "  project_key: PROJ\n"
            "  output_dir: .raw/jira\n",
            encoding="utf-8",
        )
        self._cwd = Path.cwd()
        os.chdir(self.vault)

    def tearDown(self):
        import os

        os.chdir(self._cwd)
        shutil.rmtree(self.vault, ignore_errors=True)


class RenderTest(VaultCase):
    """The ADF renderer is a pure function of the API payload."""

    def test_adf_converts_blocks_to_markdown(self):
        mod = load_module(self.vault)
        adf = {
            "type": "doc",
            "content": [
                {"type": "heading", "attrs": {"level": 2}, "content": [
                    {"type": "text", "text": "Scope"}]},
                {"type": "paragraph", "content": [
                    {"type": "text", "text": "First line."}]},
                {"type": "bulletList", "content": [
                    {"type": "listItem", "content": [
                        {"type": "paragraph", "content": [
                            {"type": "text", "text": "bullet one"}]}]},
                ]},
            ],
        }
        out = mod.adf_to_markdown(adf)
        self.assertIn("## Scope", out)
        self.assertIn("First line.", out)
        self.assertIn("- bullet one", out)

    def test_plain_string_payload_passes_through(self):
        mod = load_module(self.vault)
        self.assertEqual(mod.adf_to_markdown("already markdown"), "already markdown")
        self.assertEqual(mod.adf_to_markdown(None), "")

    def test_issue_markdown_renders_fields_and_table(self):
        mod = load_module(self.vault)
        issue = {
            "key": "PROJ-7",
            "fields": {
                "summary": "Broken connector",
                "issuetype": {"name": "Bug"},
                "status": {"name": "Open"},
                "priority": {"name": "High"},
                "assignee": None,
                "reporter": {"displayName": "Ana"},
                "created": "2026-01-02T03:04:05.000+0000",
                "updated": "2026-01-03T03:04:05.000+0000",
                "duedate": None,
                "project": {"key": "PROJ", "name": "Project"},
                "parent": None,
                "description": {"type": "doc", "content": [
                    {"type": "paragraph", "content": [
                        {"type": "text", "text": "It fails."}]}]},
                "labels": ["ops"],
                "subtasks": [],
                "issuelinks": [],
            },
        }
        md = mod.format_issue_markdown(issue, [])
        self.assertTrue(md.startswith("# PROJ-7: Broken connector"))
        self.assertIn("| Assignee | Unassigned |", md)
        self.assertIn("https://example.atlassian.net/browse/PROJ-7", md)
        self.assertIn("It fails.", md)
        self.assertIn("No comments", md)


class CredentialTest(VaultCase):
    """ATLASSIAN_* is canonical; the one-S misspelling warns and still works."""

    def test_canonical_spelling_wins(self):
        import os

        os.environ["ATLASSIAN_EMAIL"] = "canonical@example.com"
        os.environ["ATLASIAN_EMAIL"] = "legacy@example.com"
        try:
            mod = load_module(self.vault)
            self.assertEqual(mod.credential("EMAIL"), "canonical@example.com")
        finally:
            os.environ.pop("ATLASSIAN_EMAIL", None)
            os.environ.pop("ATLASIAN_EMAIL", None)

    def test_legacy_spelling_still_resolves(self):
        import os

        os.environ["ATLASIAN_API_KEY"] = "legacy-token"
        try:
            mod = load_module(self.vault)
            self.assertEqual(mod.credential("API_KEY"), "legacy-token")
        finally:
            os.environ.pop("ATLASIAN_API_KEY", None)

    def test_missing_credential_is_empty(self):
        import os

        os.environ.pop("ATLASSIAN_EMAIL", None)
        os.environ.pop("ATLASIAN_EMAIL", None)
        mod = load_module(self.vault)
        self.assertEqual(mod.credential("EMAIL"), "")


class ConfigTest(VaultCase):
    def test_macro_expansion_reads_the_environment(self):
        self.vault.joinpath("kb-config.yaml").write_text(
            "jira:\n"
            "  enabled: true\n"
            "  base_url: ${JIRA_BASE:-https://fallback.atlassian.net}\n"
            "  project_key: PROJ\n",
            encoding="utf-8",
        )
        mod = load_module(self.vault)
        self.assertEqual(mod.JIRA_BASE, "https://fallback.atlassian.net")

    def test_path_escaping_is_rejected(self):
        mod = load_module(self.vault)
        with self.assertRaises(SystemExit):
            mod.vault_path(self.vault, "../outside", "jira.output_dir")

    def test_cli_flags_reject_unknown_and_accept_documented_flags(self):
        mod = load_module(self.vault)
        with self.assertRaises(SystemExit):
            mod.main(["--bogus"])
        with self.assertRaises(SystemExit) as ctx:
            mod.main(["--render-only", "--full", "--debug"])
        self.assertIn("--render-only", str(ctx.exception))


class FakeResponse:
    """Minimal urllib response driving the real handler chain."""

    def __init__(self, code, body=b"", headers=None,
                 url="https://example.atlassian.net/rest/api/3/search"):
        import email
        import io

        self.code = code
        self.msg = "OK"
        self.headers = email.message.Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value
        self._buf = io.BytesIO(body)
        self.url = url

    def read(self, *args):
        return self._buf.read(*args)

    def geturl(self):
        return self.url

    def info(self):
        return self.headers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def close(self):
        pass


class FakeTransport(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
    """In-memory transport recorded before the production NoRedirect chain."""

    def __init__(self, script):
        super().__init__()
        self.script = list(script)
        self.requests = []

    def http_open(self, req):
        return self._serve(req)

    def https_open(self, req):
        return self._serve(req)

    def _serve(self, req):
        self.requests.append((req.get_method(), req.full_url,
                              dict(req.header_items()), req.data))
        return self.script.pop(0)


class HttpBoundaryTest(VaultCase):
    """Credentials must reach exactly the validated HTTPS origin."""

    def run_call(self, script, call, base_url=None):
        if base_url is not None:
            self.vault.joinpath("kb-config.yaml").write_text(
                "jira:\n"
                "  enabled: true\n"
                f"  base_url: {base_url}\n"
                "  project_key: PROJ\n",
                encoding="utf-8",
            )
        mod = load_module(self.vault)
        transport = FakeTransport(script)
        original = mod.make_opener

        def fake_make_opener():
            return urllib.request.build_opener(transport, mod.NoRedirect())

        mod.make_opener = fake_make_opener
        auth = "Basic " + base64.b64encode(b"user:SENTINEL").decode()
        try:
            if call == "get":
                result = mod.api_get("/rest/api/3/x", auth)
            else:
                result = mod.api_post("/rest/api/3/x", {"jql": "project = X"}, auth)
            return mod, transport, result, None
        except SystemExit as exc:
            return mod, transport, None, str(exc)
        finally:
            mod.make_opener = original

    @staticmethod
    def ok_body():
        return json.dumps({"issues": [], "startAt": 0}).encode()

    def test_get_and_post_send_basic_auth_to_https_origin(self):
        for call in ("get", "post"):
            with self.subTest(call=call):
                _mod, transport, result, error = self.run_call(
                    [FakeResponse(200, body=self.ok_body())], call)
                self.assertIsNone(error)
                self.assertEqual(result, {"issues": [], "startAt": 0})
                self.assertEqual(len(transport.requests), 1)
                method, url, headers, _data = transport.requests[0]
                self.assertEqual(method, "GET" if call == "get" else "POST")
                self.assertTrue(url.startswith("https://example.atlassian.net/"), url)
                self.assertTrue(
                    (headers.get("Authorization") or "").startswith("Basic "), headers)

    def test_redirects_refused_for_every_code(self):
        for call in ("get", "post"):
            for code in (301, 302, 303, 307, 308):
                with self.subTest(call=call, code=code):
                    _mod, transport, _result, error = self.run_call(
                        [FakeResponse(code, headers={
                            "Location": "https://evil.test/steal?Basic-SENTINEL"})],
                        call)
                    self.assertIsNotNone(error)
                    self.assertIn(f"HTTP {code}", error)
                    self.assertNotIn("evil.test", error)
                    self.assertNotIn("SENTINEL", error)
                    self.assertEqual(len(transport.requests), 1)
                    self.assertTrue(transport.requests[0][1].startswith(
                        "https://example.atlassian.net/"))

    def test_remote_http_base_url_refused_before_any_request(self):
        _mod, transport, _result, error = self.run_call(
            [FakeResponse(200, body=self.ok_body())], "get",
            base_url="http://jira.corp.example")
        self.assertIsNotNone(error)
        self.assertIn("HTTPS", error)
        self.assertEqual(transport.requests, [])

    def test_loopback_http_base_url_allowed(self):
        _mod, transport, result, error = self.run_call(
            [FakeResponse(200, body=self.ok_body())], "get",
            base_url="http://127.0.0.1:8080")
        self.assertIsNone(error)
        self.assertEqual(len(transport.requests), 1)
        self.assertTrue(transport.requests[0][1].startswith("http://127.0.0.1:8080/"))

    def test_malformed_base_urls_rejected_without_echo(self):
        for base_url in (
            "https://user:pw@example.atlassian.net",
            "https://example.atlassian.net/prefix?a=1",
            "https://example.atlassian.net/prefix#f",
            "ftp://example.atlassian.net",
            "https://example.atlassian.net:notaport",
        ):
            with self.subTest(base_url=base_url[:34]):
                _mod, transport, _result, error = self.run_call(
                    [FakeResponse(200, body=self.ok_body())], "get",
                    base_url=base_url)
                self.assertIsNotNone(error)
                self.assertIn("error:", error)
                self.assertNotIn("user:pw", error)
                self.assertNotIn("notaport", error)
                self.assertEqual(transport.requests, [])

    def test_error_status_and_transport_failures_are_sanitized(self):
        _mod, transport, _result, error = self.run_call(
            [FakeResponse(401, body=b"Basic SENTINEL echoed")], "get")
        self.assertIn("HTTP 401", error)
        self.assertNotIn("SENTINEL", error)
        _mod, transport, _result, error = self.run_call(
            [FakeResponse(503), FakeResponse(503)], "post")
        self.assertIn("HTTP 503", error)
        self.assertEqual(len(transport.requests), 1)  # Jira does not retry

    def test_path_prefix_and_trailing_slash_normalize(self):
        _mod, transport, result, error = self.run_call(
            [FakeResponse(200, body=self.ok_body())], "get",
            base_url="https://dc-proxy.example/jira/")
        self.assertIsNone(error)
        self.assertTrue(transport.requests[0][1].startswith(
            "https://dc-proxy.example/jira/rest/"))


if __name__ == "__main__":
    unittest.main()
