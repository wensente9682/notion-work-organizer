import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from new_system_blueprint import canonical_blueprint
from new_system_finalizer import NewSystemFinalizer


class Reader:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = 0

    def read_exact(self):
        self.calls += 1
        return self.snapshot


class Total:
    def __init__(self, *, fail=False):
        self.calls = 0
        self.fail = fail

    def verify_read_only(self, profile):
        self.calls += 1
        if self.fail:
            raise RuntimeError("private-total-canary")
        return "ready"


class Organize:
    def __init__(self):
        self.calls = 0
        self.writes = 0

    def preview_read_only(self, profile):
        self.calls += 1
        return "ready"

    def write(self, *args):
        self.writes += 1
        raise AssertionError("must not write")


def ready_snapshot():
    snapshot = canonical_blueprint(
        archive_container_id="container-canary",
        archive_databases={"category-canary": "archive-canary"},
    )
    snapshot["source"]["id"] = "source-canary"
    snapshot["view"]["id"] = "view-canary"
    return snapshot


class NewSystemFinalizerTests(unittest.TestCase):
    def test_generate_profile_is_validation_only_and_never_runs_product_features(self):
        class Trap:
            def __getattr__(self, name):
                raise AssertionError(f"unexpected product feature call: {name}")

        with tempfile.TemporaryDirectory() as root:
            finalizer = NewSystemFinalizer(
                Path(root) / ".todo_archive" / "new_system_profile.json",
                ignore_checker=lambda _: True,
            )
            result = finalizer.generate_profile(ready_snapshot())
            self.assertEqual("ready", result.status)
            self.assertIsInstance(result.content, bytes)
            self.assertEqual("new-system-v0.3", json.loads(result.content)["mode"])
            self.assertFalse((Path(root) / ".todo_archive" / "new_system_profile.json").exists())
            _ = Trap()  # the generation seam has no Total/Organize collaborators to call

            invalid = ready_snapshot()
            invalid["source"]["id"] = ""
            rejected = finalizer.generate_profile(invalid)
            self.assertEqual("not-ready", rejected.status)
            self.assertIsNone(rejected.content)
    def test_ready_creates_dedicated_private_profile_then_total_then_organize(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".todo_archive" / "new_system_profile.json"
            total, organize = Total(), Organize()
            snapshot = ready_snapshot()
            snapshot["source"]["properties"]["private-canary"] = "private-canary"
            result = NewSystemFinalizer(path, ignore_checker=lambda _: True).finalize(
                Reader(snapshot), total, organize
            )
            self.assertEqual("ready", result.status)
            self.assertEqual("ready", result.phase)
            self.assertEqual(1, total.calls)
            self.assertEqual(1, organize.calls)
            self.assertEqual(0, organize.writes)
            self.assertEqual(0o700, path.parent.stat().st_mode & 0o777)
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            profile = json.loads(path.read_text())
            self.assertEqual("new-system-v0.3", profile["mode"])
            self.assertNotIn("url", repr(result).lower())
            self.assertNotIn("private-canary", json.dumps(profile))

    def test_wrong_child_or_total_failure_never_reaches_organize(self):
        for snapshot, total in ((dict(ready_snapshot(), archives={}), Total()), (ready_snapshot(), Total(fail=True))):
            with self.subTest(total=total.fail):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / ".todo_archive" / "new_system_profile.json"
                    organize = Organize()
                    result = NewSystemFinalizer(path, ignore_checker=lambda _: True).finalize(
                        Reader(snapshot), total, organize
                    )
                    self.assertEqual("not-ready", result.status)
                    self.assertEqual(0, organize.calls)
                    self.assertEqual(0, organize.writes)

    def test_existing_destination_and_private_exception_are_sanitized(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".todo_archive" / "new_system_profile.json"
            path.parent.mkdir(mode=0o700)
            path.write_text("{}")
            path.chmod(0o600)
            canary = "ID-URL-TASK-PRIVATE-CANARY"
            result = NewSystemFinalizer(path, ignore_checker=lambda _: True).finalize(
                Reader(ready_snapshot()), Total(), Organize()
            )
            self.assertEqual("not-ready", result.status)
            self.assertNotIn(canary, repr(result))

    def test_profile_replacement_after_total_and_malicious_snapshot_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".todo_archive" / "new_system_profile.json"

            class ReplacingTotal(Total):
                def verify_read_only(self, profile):
                    result = super().verify_read_only(profile)
                    path.unlink()
                    path.write_text("{}")
                    path.chmod(0o600)
                    return result

            organize = Organize()
            result = NewSystemFinalizer(path, ignore_checker=lambda _: True).finalize(
                Reader(ready_snapshot()), ReplacingTotal(), organize
            )
            self.assertEqual("not-ready", result.status)
            self.assertEqual(0, organize.calls)

        class EvilDict(dict):
            def __repr__(self):
                raise RuntimeError("private-canary")

            def items(self):
                raise RuntimeError("private-canary")

        result = NewSystemFinalizer(Path(tempfile.gettempdir()) / ".todo_archive" / "new_system_profile.json", ignore_checker=lambda _: True).finalize(
            Reader(EvilDict()), Total(), Organize()
        )
        self.assertEqual("not-ready", result.status)
        self.assertNotIn("private-canary", repr(result))

    def test_two_finalizers_share_one_destination_with_at_most_one_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".todo_archive" / "new_system_profile.json"
            barrier = threading.Barrier(2)
            results = []

            def run():
                barrier.wait()
                results.append(NewSystemFinalizer(path, ignore_checker=lambda _: True).finalize(
                    Reader(ready_snapshot()), Total(), Organize()
                ))

            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(1, sum(result.status == "ready" for result in results))

    def test_undeclared_resource_and_hostile_unused_container_never_reach_total(self):
        for snapshot in (dict(ready_snapshot(), raw_page={"private": "canary"}),):
            with tempfile.TemporaryDirectory() as directory:
                total, organize = Total(), Organize()
                result = NewSystemFinalizer(
                    Path(directory) / ".todo_archive" / "new_system_profile.json",
                    ignore_checker=lambda _: True,
                ).finalize(Reader(snapshot), total, organize)
                self.assertEqual("not-ready", result.status)
                self.assertEqual(0, total.calls)
                self.assertEqual(0, organize.calls)

        snapshot = ready_snapshot()
        snapshot["unused"] = ["x"] * 200_000
        with tempfile.TemporaryDirectory() as directory:
            total, organize = Total(), Organize()
            result = NewSystemFinalizer(
                Path(directory) / ".todo_archive" / "new_system_profile.json",
                ignore_checker=lambda _: True,
            ).finalize(Reader(snapshot), total, organize)
            self.assertEqual("not-ready", result.status)
            self.assertEqual(0, total.calls)
            self.assertEqual(0, organize.calls)

    def test_no_progress_write_and_unconfirmed_phase_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".todo_archive" / "new_system_profile.json"
            total, organize = Total(), Organize()
            with patch("new_system_finalizer.os.write", return_value=0):
                result = NewSystemFinalizer(path, ignore_checker=lambda _: True).finalize(
                    Reader(ready_snapshot()), total, organize
                )
            self.assertEqual("not-ready", result.status)
            self.assertNotEqual("profile-created", result.phase)
            self.assertEqual(0, total.calls)
            self.assertEqual(0, organize.calls)

    def test_transient_mode_or_link_metadata_drift_after_total_blocks_organize(self):
        for mutation in ("mode", "link"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / ".todo_archive" / "new_system_profile.json"

                class DriftingTotal(Total):
                    def verify_read_only(self, profile):
                        result = super().verify_read_only(profile)
                        if mutation == "mode":
                            path.chmod(0o644)
                            path.chmod(0o600)
                        else:
                            sibling = path.with_name("profile-link")
                            os.link(path, sibling)
                            sibling.unlink()
                        return result

                organize = Organize()
                result = NewSystemFinalizer(path, ignore_checker=lambda _: True).finalize(
                    Reader(ready_snapshot()), DriftingTotal(), organize
                )
                self.assertEqual("not-ready", result.status)
                self.assertEqual(0, organize.calls)

    def test_temp_entry_swap_is_never_name_unlinked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".todo_archive" / "new_system_profile.json"

            def swap(parent_fd, name):
                os.unlink(name, dir_fd=parent_fd)
                fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
                os.write(fd, b"replacement-canary")
                os.close(fd)
                return False

            with patch("new_system_finalizer._rename_no_replace", side_effect=swap):
                result = NewSystemFinalizer(path, ignore_checker=lambda _: True).finalize(
                    Reader(ready_snapshot()), Total(), Organize()
                )
            self.assertEqual("not-ready", result.status)
            leftovers = list(path.parent.glob(".new-system-*.tmp"))
            self.assertEqual(1, len(leftovers))
            self.assertEqual("replacement-canary", leftovers[0].read_text())

    def test_temp_or_final_inode_takeover_never_reaches_total(self):
        import new_system_finalizer as module

        for takeover in ("temp", "final"):
            with self.subTest(takeover=takeover), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / ".todo_archive" / "new_system_profile.json"
                original = module._rename_no_replace

                def replace(parent_fd, name):
                    if takeover == "temp":
                        old_fd = os.open(name, os.O_RDONLY, dir_fd=parent_fd)
                        content = os.read(old_fd, 4096)
                        os.close(old_fd)
                        os.unlink(name, dir_fd=parent_fd)
                        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
                        os.write(fd, content)
                        os.close(fd)
                    result = original(parent_fd, name)
                    if takeover == "final" and result:
                        old_fd = os.open("new_system_profile.json", os.O_RDONLY, dir_fd=parent_fd)
                        content = os.read(old_fd, 4096)
                        os.close(old_fd)
                        os.unlink("new_system_profile.json", dir_fd=parent_fd)
                        fd = os.open("new_system_profile.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
                        os.write(fd, content)
                        os.close(fd)
                    return result

                total, organize = Total(), Organize()
                with patch("new_system_finalizer._rename_no_replace", side_effect=replace):
                    result = NewSystemFinalizer(path, ignore_checker=lambda _: True).finalize(
                        Reader(ready_snapshot()), total, organize
                    )
                self.assertEqual("not-ready", result.status)
                self.assertEqual(0, total.calls)
                self.assertEqual(0, organize.calls)

    def test_parent_identity_and_temp_ignore_are_rechecked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".todo_archive" / "new_system_profile.json"

            class ParentDriftTotal(Total):
                def verify_read_only(self, profile):
                    result = super().verify_read_only(profile)
                    old = path.parent.with_name("saved-private")
                    path.parent.rename(old)
                    old.rename(path.parent)
                    return result

            organize = Organize()
            result = NewSystemFinalizer(path, ignore_checker=lambda _: True).finalize(
                Reader(ready_snapshot()), ParentDriftTotal(), organize
            )
            self.assertEqual("not-ready", result.status)
            self.assertEqual(0, organize.calls)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".todo_archive" / "new_system_profile.json"
            result = NewSystemFinalizer(
                path,
                ignore_checker=lambda candidate: candidate.name == "new_system_profile.json",
            ).finalize(Reader(ready_snapshot()), Total(), Organize())
            self.assertEqual("not-ready", result.status)

    def test_read_time_metadata_drift_is_detected_after_fd_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".todo_archive" / "new_system_profile.json"
            original_read = os.read

            def drift(fd, size):
                path.chmod(0o644)
                path.chmod(0o600)
                return original_read(fd, size)

            total, organize = Total(), Organize()
            with patch("new_system_finalizer.os.read", side_effect=drift):
                result = NewSystemFinalizer(path, ignore_checker=lambda _: True).finalize(
                    Reader(ready_snapshot()), total, organize
                )
            self.assertEqual("not-ready", result.status)
            self.assertEqual(0, total.calls)
            self.assertEqual(0, organize.calls)

    def test_settled_root_takeover_cannot_rebase_authority(self):
        import new_system_finalizer as module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / ".todo_archive" / "new_system_profile.json"
            original = module._rename_no_replace

            def takeover(parent_fd, name):
                result = original(parent_fd, name)
                if result:
                    moved = root.with_name(root.name + "-moved")
                    root.rename(moved)
                    root.mkdir()
                    (moved / ".todo_archive").rename(root / ".todo_archive")
                return result

            total, organize = Total(), Organize()
            with patch("new_system_finalizer._rename_no_replace", side_effect=takeover):
                result = NewSystemFinalizer(path, ignore_checker=lambda _: True).finalize(
                    Reader(ready_snapshot()), total, organize
                )
            self.assertEqual("not-ready", result.status)
            self.assertEqual(0, total.calls)
            self.assertEqual(0, organize.calls)

    def test_parent_fd_is_closed_when_fstat_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".todo_archive").mkdir(mode=0o700)
            finalizer = NewSystemFinalizer(
                root / ".todo_archive" / "new_system_profile.json",
                ignore_checker=lambda _: True,
            )
            original_open, original_fstat = os.open, os.fstat
            parent_fds = []

            def capture_open(name, *args, **kwargs):
                fd = original_open(name, *args, **kwargs)
                if name == ".todo_archive":
                    parent_fds.append(fd)
                return fd

            def fail_parent_fstat(fd):
                if fd in parent_fds:
                    raise RuntimeError("private-fstat-canary")
                return original_fstat(fd)

            with patch("new_system_finalizer.os.open", side_effect=capture_open), patch(
                "new_system_finalizer.os.fstat", side_effect=fail_parent_fstat
            ):
                self.assertIsNone(finalizer._safe_parent())
            self.assertEqual(1, len(parent_fds))
            with self.assertRaises(OSError):
                os.fstat(parent_fds[0])


if __name__ == "__main__":
    unittest.main()
