import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parent / "skills/todo-archive-review/scripts/todo_archive_backup.py"
SPEC = importlib.util.spec_from_file_location("todo_archive_backup", SCRIPT)
backup = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(backup)


class BackupPermissionsTest(unittest.TestCase):
    def test_cli_persists_candidate_content_fingerprint_for_ok_recheck(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / ".todo_archive" / "backups"
            begin = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--backup-dir",
                    str(backup_dir),
                    "begin",
                    "--label",
                    "todo",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            session_id = begin.stdout.strip()
            fingerprint = "a" * 64
            record = {
                "action": "candidate",
                "status": "candidate",
                "batch": 1,
                "number": 1,
                "category": "research",
                "name_hint": "review result",
                "source_url": "https://example.invalid/source",
                "content_fingerprint": fingerprint,
                "unrelated_secret": "must-not-persist",
            }

            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--backup-dir",
                    str(backup_dir),
                    "add",
                    session_id,
                    "--record-json",
                    json.dumps(record),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            shown = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--backup-dir",
                    str(backup_dir),
                    "show",
                    session_id,
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            persisted = json.loads(shown.stdout)
            self.assertEqual(
                persisted["records"][0]["content_fingerprint"], fingerprint
            )
            self.assertNotIn("unrelated_secret", persisted["records"][0])

    def test_save_json_creates_and_tightens_private_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime_dir = Path(tmp) / ".todo_archive"
            backup_dir = runtime_dir / "backups"
            path = backup_dir / "session.json"
            data = {"session_id": "test", "records": []}
            old_umask = os.umask(0o022)
            try:
                backup.save_json(path, data)
            finally:
                os.umask(old_umask)

            self.assertEqual(runtime_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(backup_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(backup.load_json(path), data)

            runtime_dir.chmod(0o755)
            backup_dir.chmod(0o755)
            path.chmod(0o644)
            backup.save_json(path, data)
            self.assertEqual(runtime_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(backup_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
