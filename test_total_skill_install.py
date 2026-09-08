import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
SKILL_SOURCE = ROOT / "skills" / "todo-archive-review"
INSTALLER = SKILL_SOURCE / "scripts" / "install.py"
SKILL_DOCUMENT = SKILL_SOURCE / "SKILL.md"


class TotalSkillInstallTest(unittest.TestCase):
    def test_monthly_total_documents_direct_views_keychain_exception(self):
        document = SKILL_DOCUMENT.read_text(encoding="utf-8")

        self.assertIn(
            "Monthly Total is an explicit exception to the connector-first rule.",
            document,
        )
        self.assertIn("Stage 4 Views API", document)
        self.assertIn("single-use sandbox-external authorization", document)
        self.assertIn("must not request or suggest persistent authorization", document)
        self.assertIn("do not fall back to the Notion connector", document)
        self.assertIn(
            "temporarily unavailable because the Notion Views API dependency is unavailable",
            document,
        )
        self.assertIn("<!-- Direct Views route retained for recovery:", document)

    def run_installer(self, target, *, cwd, home):
        return subprocess.run(
            [sys.executable, "-B", str(INSTALLER), "--target", str(target)],
            cwd=cwd,
            env=dict(os.environ, HOME=str(home)),
            text=True,
            capture_output=True,
            check=False,
        )

    def run_installed_total(self, installed, *, cwd, home):
        return subprocess.run(
            [
                sys.executable,
                "-B",
                str(installed / "scripts" / "run_total.py"),
                "0000-01",
            ],
            cwd=cwd,
            env=dict(os.environ, HOME=str(home)),
            text=True,
            capture_output=True,
            check=False,
        )

    def test_copied_skill_runs_total_from_a_non_repository_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory).resolve()
            outside = sandbox / "outside"
            outside.mkdir()
            installed = sandbox / "home" / ".agents" / "skills" / "todo-archive-review"
            environment = dict(os.environ, HOME=str(sandbox / "home"))

            install = subprocess.run(
                [sys.executable, "-B", str(INSTALLER), "--target", str(installed)],
                cwd=outside,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, install.returncode, install.stderr)
            self.assertTrue((installed / "SKILL.md").is_file())
            self.assertFalse((installed / "total.py").exists())
            self.assertFalse((installed / "total_views.py").exists())
            self.assertEqual(
                0o600,
                (installed / ".repository-root").stat().st_mode & 0o777,
            )

            run = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(installed / "scripts" / "run_total.py"),
                    "0000-01",
                ],
                cwd=outside,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(1, run.returncode)
        self.assertEqual("", run.stdout)
        self.assertEqual(
            "error: total requires a month in YYYY-MM format\n",
            run.stderr,
        )
        self.assertNotIn("Traceback", run.stderr)

    def test_installer_rejects_a_symlink_target_without_touching_its_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory).resolve()
            outside = sandbox / "outside"
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("unchanged", encoding="utf-8")
            target = sandbox / "todo-archive-review"
            target.symlink_to(outside, target_is_directory=True)

            install = self.run_installer(
                target,
                cwd=sandbox,
                home=sandbox / "home",
            )

            self.assertEqual(1, install.returncode)
            self.assertEqual(
                "error: skill installation is unsafe or failed\n",
                install.stderr,
            )
            self.assertEqual("unchanged", sentinel.read_text(encoding="utf-8"))
            self.assertFalse((outside / "SKILL.md").exists())

    def test_installer_rejects_a_symlinked_parent_without_touching_outside(self):
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory).resolve()
            outside = sandbox / "outside"
            outside.mkdir()
            sentinel = outside / "sentinel.txt"
            sentinel.write_text("unchanged", encoding="utf-8")
            linked_parent = sandbox / "linked-parent"
            linked_parent.symlink_to(outside, target_is_directory=True)
            target = linked_parent / "todo-archive-review"

            install = self.run_installer(
                target,
                cwd=sandbox,
                home=sandbox / "home",
            )

            self.assertEqual(1, install.returncode)
            self.assertEqual(
                "error: skill installation is unsafe or failed\n",
                install.stderr,
            )
            self.assertEqual("unchanged", sentinel.read_text(encoding="utf-8"))
            self.assertEqual(["sentinel.txt"], sorted(item.name for item in outside.iterdir()))

    def test_installer_rejects_a_locator_symlink_without_touching_its_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory).resolve()
            target = sandbox / "todo-archive-review"
            target.mkdir()
            sentinel = sandbox / "outside.txt"
            sentinel.write_text("unchanged", encoding="utf-8")
            (target / ".repository-root").symlink_to(sentinel)

            install = self.run_installer(
                target,
                cwd=sandbox,
                home=sandbox / "home",
            )

            self.assertEqual(1, install.returncode)
            self.assertEqual(
                "error: skill installation is unsafe or failed\n",
                install.stderr,
            )
            self.assertEqual("unchanged", sentinel.read_text(encoding="utf-8"))
            self.assertTrue((target / ".repository-root").is_symlink())

    def test_installer_replaces_a_normal_install_without_carrying_old_private_files(self):
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory).resolve()
            target = sandbox / "todo-archive-review"
            first = self.run_installer(
                target,
                cwd=sandbox,
                home=sandbox / "home",
            )
            self.assertEqual(0, first.returncode, first.stderr)
            (target / "private.txt").write_text("do not copy", encoding="utf-8")
            cache = target / "__pycache__"
            cache.mkdir()
            (cache / "stale.pyc").write_bytes(b"stale")

            second = self.run_installer(
                target,
                cwd=sandbox,
                home=sandbox / "home",
            )

            self.assertEqual(0, second.returncode, second.stderr)
            self.assertFalse((target / "private.txt").exists())
            self.assertFalse(cache.exists())
            self.assertTrue((target / "SKILL.md").is_file())
            locator = target / ".repository-root"
            self.assertTrue(locator.is_file())
            self.assertEqual(0o600, locator.stat().st_mode & 0o777)

    def test_installed_wrapper_never_executes_an_ancestor_directory_decoy(self):
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory).resolve()
            installed = sandbox / "home" / ".agents" / "skills" / "todo-archive-review"
            shutil.copytree(SKILL_SOURCE, installed)
            for name in ("total.py", "total_views.py"):
                (sandbox / name).write_text("", encoding="utf-8")
            (sandbox / "total_command.py").write_text(
                "from pathlib import Path\n"
                "Path(__file__).with_name('hijacked.txt').write_text('ran')\n",
                encoding="utf-8",
            )

            run = self.run_installed_total(
                installed,
                cwd=sandbox,
                home=sandbox / "home",
            )

            self.assertEqual(1, run.returncode)
            self.assertEqual("", run.stdout)
            self.assertEqual(
                "error: total installation is incomplete\n",
                run.stderr,
            )
            self.assertFalse((sandbox / "hijacked.txt").exists())

    def test_installed_wrapper_rejects_a_locator_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory).resolve()
            installed = sandbox / "todo-archive-review"
            install = self.run_installer(
                installed,
                cwd=sandbox,
                home=sandbox / "home",
            )
            self.assertEqual(0, install.returncode, install.stderr)
            locator = installed / ".repository-root"
            locator.unlink()
            outside = sandbox / "outside-locator.txt"
            outside.write_text(f"{ROOT}\n", encoding="utf-8")
            locator.symlink_to(outside)

            run = self.run_installed_total(
                installed,
                cwd=sandbox,
                home=sandbox / "home",
            )

            self.assertEqual(1, run.returncode)
            self.assertEqual("", run.stdout)
            self.assertEqual(
                "error: total installation is incomplete\n",
                run.stderr,
            )
            self.assertEqual(f"{ROOT}\n", outside.read_text(encoding="utf-8"))

    def test_installed_wrapper_rejects_missing_malformed_or_stale_locators(self):
        cases = ("missing", "empty", "multiline", "permissions", "moved")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                sandbox = Path(directory).resolve()
                installed = sandbox / "todo-archive-review"
                install = self.run_installer(
                    installed,
                    cwd=sandbox,
                    home=sandbox / "home",
                )
                self.assertEqual(0, install.returncode, install.stderr)
                locator = installed / ".repository-root"
                if case == "missing":
                    locator.unlink()
                elif case == "empty":
                    locator.write_text("", encoding="utf-8")
                elif case == "multiline":
                    locator.write_text(f"{ROOT}\n{ROOT}\n", encoding="utf-8")
                elif case == "permissions":
                    locator.chmod(0o644)
                else:
                    locator.write_text(
                        str(sandbox / "moved-checkout") + "\n",
                        encoding="utf-8",
                    )

                run = self.run_installed_total(
                    installed,
                    cwd=sandbox,
                    home=sandbox / "home",
                )

                self.assertEqual(1, run.returncode)
                self.assertEqual("", run.stdout)
                self.assertEqual(
                    "error: total installation is incomplete\n",
                    run.stderr,
                )
                self.assertNotIn(str(ROOT), run.stderr)

    def test_symlinked_skill_runs_total_from_a_non_repository_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory).resolve()
            outside = sandbox / "outside"
            outside.mkdir()
            installed = sandbox / "todo-archive-review"
            installed.symlink_to(SKILL_SOURCE, target_is_directory=True)

            run = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    str(installed / "scripts" / "run_total.py"),
                    "0000-01",
                ],
                cwd=outside,
                env=dict(os.environ, HOME=str(sandbox / "home")),
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(1, run.returncode)
        self.assertEqual("", run.stdout)
        self.assertEqual(
            "error: total requires a month in YYYY-MM format\n",
            run.stderr,
        )
        self.assertNotIn("Traceback", run.stderr)


if __name__ == "__main__":
    unittest.main()
