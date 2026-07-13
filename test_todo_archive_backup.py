import importlib.util
import os
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parent / "skills/todo-archive-review/scripts/todo_archive_backup.py"
SPEC = importlib.util.spec_from_file_location("todo_archive_backup", SCRIPT)
backup = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(backup)


class BackupPermissionsTest(unittest.TestCase):
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
