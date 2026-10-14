"""Backup recovery checks use temporary files only; no network or NAS access."""

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import server
import restore


SAMPLE = [{"name": "妞妞摔倒互动", "keywords": ["妞妞摔倒"], "reply": "陶妞妞摔倒了", "repeat": 10}]
BACKUP = "commands.20261014T123456.000001Z.0123456789abcdef.json"


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xiaoai-restore-test-")
        self.directory = Path(self.temp.name)
        self.commands = self.directory / "commands.json"
        self.current_rules = copy.deepcopy(SAMPLE)
        self.current_rules[0]["reply"] = "当前回复"
        self.current_raw = (json.dumps(self.current_rules, ensure_ascii=False, indent=3) + "\n").encode("utf-8")
        self.commands.write_bytes(self.current_raw)
        self.backups = self.directory / ".commands-backups"
        self.backups.mkdir()
        self.backup = self.backups / BACKUP
        self.backup_raw = (json.dumps(SAMPLE, ensure_ascii=False, indent=4) + "\n").encode("utf-8")
        self.backup.write_bytes(self.backup_raw)

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self):
        return {path.relative_to(self.directory).as_posix(): path.read_bytes()
                for path in self.directory.rglob("*") if path.is_file()}

    def test_restore_backs_up_current_and_keeps_chosen_backup(self):
        result = restore.restore(str(self.directory), BACKUP)
        self.assertEqual(json.loads(self.commands.read_bytes()), SAMPLE)
        self.assertEqual(self.backup.read_bytes(), self.backup_raw)
        self.assertEqual((self.directory / result["backup"]).read_bytes(), self.current_raw)
        self.assertNotEqual(result["backup"], f".commands-backups/{BACKUP}")

    def test_cli_requires_no_password_and_prints_new_backup(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = restore.main([BACKUP, "--commands-dir", str(self.directory)])
        self.assertEqual(status, 0, err.getvalue())
        self.assertIn(".commands-backups/commands.", out.getvalue())

    def test_rejects_paths_and_unrecognized_names_without_writes(self):
        original = self.snapshot()
        for name in ("../" + BACKUP, ".commands-backups/" + BACKUP, "C:\\temp\\" + BACKUP,
                     "commands.json", "commands.20261014T123456.000001Z.0123456789abcdef.json/..", ""):
            with self.subTest(name=name), self.assertRaises(server.APIError):
                restore.restore(str(self.directory), name)
            self.assertEqual(self.snapshot(), original)

    def test_refuses_missing_or_invalid_current(self):
        for content in (b"{broken", b"{}", b'[{"name":"x","keywords":["x"],"reply":"x","repeat":true}]'):
            self.commands.write_bytes(content)
            original = self.snapshot()
            with self.assertRaises(server.APIError):
                restore.restore(str(self.directory), BACKUP)
            self.assertEqual(self.snapshot(), original)
        self.commands.unlink()
        original = self.snapshot()
        with self.assertRaises(server.APIError) as failure:
            restore.restore(str(self.directory), BACKUP)
        self.assertEqual(failure.exception.status, 404)
        self.assertEqual(self.snapshot(), original)

    def test_refuses_missing_invalid_and_oversized_backup_without_writes(self):
        for content in (b"not json", b"{}", b"x" * (server.MAX_BYTES + 1)):
            self.backup.write_bytes(content)
            original = self.snapshot()
            with self.assertRaises(server.APIError):
                restore.restore(str(self.directory), BACKUP)
            self.assertEqual(self.snapshot(), original)
        self.backup.unlink()
        original = self.snapshot()
        with self.assertRaises(server.APIError) as failure:
            restore.restore(str(self.directory), BACKUP)
        self.assertEqual(failure.exception.status, 404)
        self.assertEqual(self.snapshot(), original)

    def test_preserves_unknown_fields(self):
        rules = copy.deepcopy(SAMPLE)
        rules[0]["metadata"] = {"keep": True, "note": "已有字段"}
        self.backup.write_text(json.dumps(rules, ensure_ascii=False), encoding="utf-8")
        restore.restore(str(self.directory), BACKUP)
        self.assertEqual(json.loads(self.commands.read_bytes()), rules)

    @unittest.skipUnless(os.name == "posix", "POSIX uid/gid/mode are not available on Windows")
    def test_restore_preserves_current_mode_and_owner(self):
        self.commands.chmod(0o640)
        before = self.commands.stat()
        restore.restore(str(self.directory), BACKUP)
        after = self.commands.stat()
        self.assertEqual(stat.S_IMODE(after.st_mode), stat.S_IMODE(before.st_mode))
        self.assertEqual((after.st_uid, after.st_gid), (before.st_uid, before.st_gid))

    def create_symlink(self, target, link, *, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"Creating symlinks is unavailable: {exc}")

    def test_rejects_backup_file_symlink(self):
        outside = self.directory / "outside.json"
        outside.write_bytes(self.backup_raw)
        self.backup.unlink()
        self.create_symlink(outside, self.backup)
        with self.assertRaises(server.APIError) as failure:
            restore.restore(str(self.directory), BACKUP)
        self.assertEqual(failure.exception.status, 409)
        self.assertEqual(self.commands.read_bytes(), self.current_raw)
        self.assertEqual(outside.read_bytes(), self.backup_raw)

    def test_rejects_backup_directory_symlink(self):
        outside = self.directory / "outside-backups"
        outside.mkdir()
        (outside / BACKUP).write_bytes(self.backup_raw)
        self.backup.unlink()
        self.backups.rmdir()
        self.create_symlink(outside, self.backups, directory=True)
        with self.assertRaises(server.APIError) as failure:
            restore.restore(str(self.directory), BACKUP)
        self.assertEqual(failure.exception.status, 409)
        self.assertEqual(self.commands.read_bytes(), self.current_raw)
        self.assertEqual(list(outside.iterdir()), [outside / BACKUP])

    def test_external_change_causes_conflict_without_overwriting_it(self):
        original_read = restore.read_backup
        changed_raw = json.dumps([], indent=2).encode("utf-8")

        def changed_after_read(store, name):
            rules = original_read(store, name)
            self.commands.write_bytes(changed_raw)
            return rules

        with mock.patch.object(restore, "read_backup", changed_after_read), self.assertRaises(server.APIError) as failure:
            restore.restore(str(self.directory), BACKUP)
        self.assertEqual(failure.exception.status, 409)
        self.assertEqual(self.commands.read_bytes(), changed_raw)
        self.assertEqual(list(self.backups.iterdir()), [self.backup])


if __name__ == "__main__":
    unittest.main()
