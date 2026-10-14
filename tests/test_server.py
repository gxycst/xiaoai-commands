"""Run with: python -m unittest discover -s tests -v

Integration checks use only temporary copies of the sample commands file.
They never read or modify the NAS commands file.
"""

from concurrent.futures import ThreadPoolExecutor
import copy
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.py"
PASSWORD = "TestPassword-9528"
SAMPLE = [
    {
        "name": "妞妞摔倒互动",
        "keywords": ["妞妞摔倒", "妞妞跌倒", "妞妞摔到了", "陶妞妞摔到了"],
        "reply": "陶妞妞摔倒了",
        "repeat": 10,
    },
    {
        "name": "大名摔倒互动",
        "keywords": ["陶润心摔倒了", "陶雨欣摔倒了"],
        "reply": "陶润心摔倒了",
        "repeat": 10,
    },
]


def raw_sample(rules=None):
    # Include nondefault whitespace and a trailing newline to test exact backups.
    return (json.dumps(SAMPLE if rules is None else rules, ensure_ascii=False, indent=3) + "\n").encode("utf-8")


class ServerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xiaoai-test-")
        self.directory = Path(self.temp.name)
        self.skills = self.directory / "skills"
        self.skills.mkdir()
        self.commands = self.skills / "commands.json"
        self.original = raw_sample()
        self.commands.write_bytes(self.original)
        self.cookie = ""
        self.csrf = ""
        self.process = None
        self.log = None

    def tearDown(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.log is not None:
            self.log.close()
        self.temp.cleanup()

    def start(self):
        if not SERVER.is_file():
            self.fail(f"Backend source is missing: {SERVER}")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        environment = os.environ.copy()
        environment.update({
            "COMMANDS_DIR": str(self.skills),
            "APP_PASSWORD": PASSWORD,
            "HOST": "127.0.0.1",
            "PORT": str(self.port),
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "ALLOWED_NETWORKS": "127.0.0.0/8,::1/128",
        })
        self.log_path = self.directory / "server.log"
        self.log = self.log_path.open("w", encoding="utf-8")
        self.process = subprocess.Popen(
            [sys.executable, str(SERVER)], cwd=ROOT, env=environment,
            stdout=self.log, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail("Server exited during startup:\n" + self.log_path.read_text(encoding="utf-8", errors="replace"))
            try:
                status, _, _ = self.request("GET", "/api/session", auth=False)
                if status == 200:
                    return
            except (ConnectionError, OSError):
                pass
            time.sleep(0.05)
        self.fail("Server did not become ready:\n" + self.log_path.read_text(encoding="utf-8", errors="replace"))

    def request(self, method, path, data=None, *, auth=True, csrf=True, headers=None, raw=None):
        request_headers = {}
        if auth and self.cookie:
            request_headers["Cookie"] = self.cookie
        if auth and csrf and self.csrf:
            request_headers["X-CSRF-Token"] = self.csrf
        body = raw
        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        if headers:
            request_headers.update(headers)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            content = response.read()
            response_headers = dict(response.getheaders())
            try:
                parsed = json.loads(content)
            except (ValueError, UnicodeDecodeError):
                parsed = content.decode("utf-8", errors="replace")
            return response.status, parsed, response_headers
        finally:
            connection.close()

    def login(self):
        status, data, headers = self.request("POST", "/api/login", {"password": PASSWORD}, auth=False)
        self.assertEqual(status, 200, data)
        self.csrf = data["csrfToken"]
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertTrue(self.csrf)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])

    def read_rules(self):
        status, data, _ = self.request("GET", "/api/rules")
        self.assertEqual(status, 200, data)
        self.assertIn("version", data)
        return data

    def test_startup_and_reads_preserve_original_bytes(self):
        self.start()
        self.assertEqual(self.commands.read_bytes(), self.original)
        self.login()
        data = self.read_rules()
        self.assertEqual(data["rules"], SAMPLE)
        self.assertEqual(self.commands.read_bytes(), self.original)
        self.assertEqual(list(self.skills.iterdir()), [self.commands])

    def test_save_makes_exact_backup_and_preserves_unknown_fields(self):
        rules = copy.deepcopy(SAMPLE)
        rules[0]["customMetadata"] = {"note": "保留已有扩展字段", "flags": [1, True]}
        self.original = raw_sample(rules)
        self.commands.write_bytes(self.original)
        self.start()
        self.login()
        data = self.read_rules()
        rules[0]["keywords"].append("妞妞识别错字")
        rules[1]["repeat"] = 3
        status, saved, _ = self.request("PUT", "/api/rules", {"rules": rules, "version": data["version"]})
        self.assertEqual(status, 200, saved)
        self.assertEqual(self.read_rules()["rules"], rules)
        self.assertNotEqual(saved["version"], data["version"])
        backups = [p for p in self.skills.rglob("*") if p.is_file() and p != self.commands]
        self.assertTrue(any(p.read_bytes() == self.original for p in backups), backups)
        self.assertIn("backup", saved)

    def test_rule_add_delete_reorder_roundtrip(self):
        self.start()
        self.login()
        data = self.read_rules()
        rules = [SAMPLE[1], {"name": "新规则", "keywords": ["新关键词"], "reply": "新回复", "repeat": 2}]
        status, saved, _ = self.request("PUT", "/api/rules", {"rules": rules, "version": data["version"]})
        self.assertEqual(status, 200, saved)
        self.assertEqual(self.read_rules()["rules"], rules)

    def test_matching_uses_first_rule_then_first_keyword(self):
        self.start()
        self.login()
        rules = [
            {"name": "先匹配", "keywords": ["摔倒", "妞妞摔倒"], "reply": "第一条", "repeat": 2},
            {"name": "后匹配", "keywords": ["妞妞摔倒"], "reply": "第二条", "repeat": 8},
        ]
        status, result, _ = self.request("POST", "/api/test", {"query": "小爱说妞妞摔倒了", "rules": rules})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["matched"])
        self.assertEqual(result["index"], 0)
        self.assertEqual(result["keyword"], "摔倒")
        self.assertEqual(result["rule"], rules[0])
        self.assertEqual(result["reply"], "第一条")
        self.assertEqual(result["repeat"], 2)
        self.assertEqual(self.commands.read_bytes(), self.original)

    def test_matching_current_file_and_case_sensitive_no_hit(self):
        self.start()
        self.login()
        status, result, _ = self.request("POST", "/api/test", {"query": "请说陶雨欣摔倒了"})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["index"], 1)
        rules = [{"name": "English", "keywords": ["Hello"], "reply": "Hi", "repeat": 1}]
        status, result, _ = self.request("POST", "/api/test", {"query": "hello", "rules": rules})
        self.assertEqual(status, 200, result)
        self.assertFalse(result["matched"])

    def test_validation_rejects_bad_rule_shapes_without_modifying_file(self):
        self.start()
        self.login()
        version = self.read_rules()["version"]
        invalid = [None, {}, "rules", [None], [{"name": "缺字段"}]]
        for field, value in [("name", 5), ("keywords", "词"), ("keywords", [3]), ("keywords", []), ("keywords", [""]),
                             ("reply", ["句子"]), ("repeat", True), ("repeat", False),
                             ("repeat", 0), ("repeat", -1), ("repeat", 2.5), ("repeat", "10")]:
            rule = copy.deepcopy(SAMPLE[0])
            rule[field] = value
            invalid.append([rule])
        for rules in invalid:
            with self.subTest(rules=rules):
                status, result, _ = self.request("PUT", "/api/rules", {"rules": rules, "version": version})
                self.assertEqual(status, 422, result)
                self.assertEqual(self.commands.read_bytes(), self.original)

    def test_malformed_request_json_is_rejected(self):
        self.start()
        self.login()
        status, result, _ = self.request("PUT", "/api/rules", raw=b'{"rules": [}')
        self.assertEqual(status, 422, result)
        self.assertEqual(self.commands.read_bytes(), self.original)

    def test_strict_json_rejects_duplicates_nonfinite_and_invalid_unicode(self):
        self.start()
        self.login()
        invalid = [
            b'{"query":"a","query":"b"}',
            b'{"query":"a","metadata":NaN}',
            b'{"query":"a","metadata":Infinity}',
            b'{"query":"a","metadata":1e999}',
            b'{"query":"\\ud800"}',
        ]
        for raw in invalid:
            with self.subTest(raw=raw):
                status, result, _ = self.request("POST", "/api/test", raw=raw)
                self.assertEqual(status, 422, result)
        self.assertEqual(self.commands.read_bytes(), self.original)

    def test_oversized_request_and_file_are_rejected_without_rewrite(self):
        self.start()
        self.login()
        oversized = b" " * (2 * 1024 * 1024 + 1)
        status, result, _ = self.request("PUT", "/api/rules", raw=oversized)
        self.assertEqual(status, 413, result)
        self.assertEqual(self.commands.read_bytes(), self.original)
        self.commands.write_bytes(oversized)
        status, result, _ = self.request("GET", "/api/rules")
        self.assertEqual(status, 413, result)
        self.assertEqual(self.commands.read_bytes(), oversized)

    def test_stale_version_and_external_file_change_rejected(self):
        self.start()
        self.login()
        data = self.read_rules()
        external = raw_sample(list(reversed(SAMPLE)))
        self.commands.write_bytes(external)
        status, result, _ = self.request("PUT", "/api/rules", {"rules": SAMPLE, "version": data["version"]})
        self.assertEqual(status, 409, result)
        self.assertEqual(self.commands.read_bytes(), external)

    def test_concurrent_same_version_allows_only_one_save(self):
        self.start()
        self.login()
        version = self.read_rules()["version"]
        first, second = copy.deepcopy(SAMPLE), copy.deepcopy(SAMPLE)
        first[0]["reply"] = "第一种修改"
        second[0]["reply"] = "第二种修改"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda rules: self.request("PUT", "/api/rules", {"rules": rules, "version": version}), [first, second]))
        self.assertEqual(sorted(result[0] for result in results), [200, 409], results)
        self.assertIn(json.loads(self.commands.read_bytes()), [first, second])

    def test_missing_file_never_seeded(self):
        self.commands.unlink()
        self.start()
        self.login()
        status, result, _ = self.request("GET", "/api/rules")
        self.assertEqual(status, 404, result)
        status, result, _ = self.request("PUT", "/api/rules", {"rules": SAMPLE, "version": "0" * 64})
        self.assertIn(status, (404, 409), result)
        self.assertFalse(self.commands.exists())

    def test_invalid_existing_file_never_rewritten(self):
        for invalid in (b'{"oops": ', b'{"rules": []}', b'[{}]'):
            self.commands.write_bytes(invalid)
            if self.process is None:
                self.start()
                self.login()
            status, result, _ = self.request("GET", "/api/rules")
            self.assertEqual(status, 422, result)
            status, result, _ = self.request("PUT", "/api/rules", {"rules": SAMPLE, "version": "0" * 64})
            self.assertIn(status, (409, 422), result)
            self.assertEqual(self.commands.read_bytes(), invalid)

    def test_html_is_plain_data(self):
        self.start()
        self.login()
        data = self.read_rules()
        rules = copy.deepcopy(SAMPLE)
        rules[0]["name"] = '<img src=x onerror="alert(1)">'
        rules[0]["reply"] = '<script>alert("x")</script>'
        status, result, headers = self.request("PUT", "/api/rules", {"rules": rules, "version": data["version"]})
        self.assertEqual(status, 200, result)
        status, result, headers = self.request("GET", "/api/rules")
        self.assertEqual(status, 200, result)
        self.assertTrue(headers.get("Content-Type", "").startswith("application/json"))
        self.assertEqual(result["rules"], rules)

    def test_unauthenticated_protected_requests_rejected(self):
        self.start()
        for method, path, data in [("GET", "/api/rules", None), ("PUT", "/api/rules", {"rules": SAMPLE, "version": "x"}),
                                    ("POST", "/api/test", {"query": "妞妞摔倒"})]:
            status, result, _ = self.request(method, path, data, auth=False)
            self.assertEqual(status, 401, result)
        status, result, _ = self.request("POST", "/api/login", {"password": "wrong"}, auth=False)
        self.assertEqual(status, 401, result)
        status, result, _ = self.request("POST", "/api/login", {"password": PASSWORD}, auth=False, headers={"Origin": "http://evil.local"})
        self.assertEqual(status, 403, result)
        self.assertEqual(self.commands.read_bytes(), self.original)

    def test_csrf_origin_content_type_and_logout(self):
        self.start()
        self.login()
        data = self.read_rules()
        payload = {"rules": SAMPLE, "version": data["version"]}
        status, result, _ = self.request("PUT", "/api/rules", payload, csrf=False)
        self.assertEqual(status, 403, result)
        status, result, _ = self.request("POST", "/api/test", {"query": "妞妞摔倒"}, headers={"Origin": "http://evil.local"})
        self.assertEqual(status, 403, result)
        status, result, _ = self.request("PUT", "/api/rules", payload, headers={"Content-Type": "text/plain"})
        self.assertIn(status, (400, 415), result)
        self.assertEqual(self.commands.read_bytes(), self.original)
        status, result, _ = self.request("POST", "/api/logout", {})
        self.assertEqual(status, 200, result)
        status, result, _ = self.request("GET", "/api/rules")
        self.assertEqual(status, 401, result)

    def test_static_path_traversal_cannot_expose_data(self):
        self.start()
        for path in ("/../server.py", "/%2e%2e/server.py", "/..%2fserver.py", "/static/../../server.py",
                     "/commands.json", "/config/skills/commands.json", "/server.py", "/.env"):
            with self.subTest(path=path):
                status, result, _ = self.request("GET", path, auth=False)
                self.assertIn(status, (400, 403, 404), result)
                self.assertNotIn("TestPassword", str(result))

    def test_commands_symlink_outside_mount_is_rejected(self):
        external = self.directory / "outside.json"
        external.write_bytes(self.original)
        self.commands.unlink()
        try:
            self.commands.symlink_to(external)
        except OSError as error:
            self.skipTest(f"Creating symlinks is unavailable on this platform: {error}")
        self.start()
        self.login()
        status, result, _ = self.request("GET", "/api/rules")
        self.assertIn(status, (403, 409), result)
        self.assertEqual(external.read_bytes(), self.original)

    def test_backup_directory_symlink_cannot_write_outside_mount(self):
        external = self.directory / "external-backups"
        external.mkdir()
        try:
            (self.skills / ".commands-backups").symlink_to(external, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"Creating symlinks is unavailable on this platform: {error}")
        self.start()
        self.login()
        data = self.read_rules()
        rules = copy.deepcopy(SAMPLE)
        rules[0]["reply"] = "尝试修改"
        status, result, _ = self.request("PUT", "/api/rules", {"rules": rules, "version": data["version"]})
        self.assertEqual(status, 409, result)
        self.assertEqual(self.commands.read_bytes(), self.original)
        self.assertEqual(list(external.iterdir()), [])

    @unittest.skipUnless(os.name == "posix", "File uid/gid/mode preservation applies to Linux deployment")
    def test_atomic_save_preserves_original_linux_permissions(self):
        self.commands.chmod(0o640)
        original_stat = self.commands.stat()
        self.start()
        self.login()
        data = self.read_rules()
        rules = copy.deepcopy(SAMPLE)
        rules[0]["repeat"] = 4
        status, result, _ = self.request("PUT", "/api/rules", {"rules": rules, "version": data["version"]})
        self.assertEqual(status, 200, result)
        current = self.commands.stat()
        self.assertEqual(current.st_mode & 0o7777, 0o640)
        self.assertEqual((current.st_uid, current.st_gid), (original_stat.st_uid, original_stat.st_gid))


if __name__ == "__main__":
    unittest.main()
