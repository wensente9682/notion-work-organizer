"""T8 synthetic-only public-seam red gate."""

import copy
import errno
import json
import multiprocessing
import os
import pickle
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


CANARY = "synthetic-private-marker"
BAD_ID = "synthetic-id-marker"


def facts(**changes):
    value = {
        "action_digest": "a" * 64,
        "attempt_fingerprint": "b" * 32,
        "read_attempted": False,
        "write_attempted": False,
        "write_started": False,
    }
    value.update(changes)
    return value


def facts_for(event, interrupted_from=None, **changes):
    """Independent frozen-state oracle; it does not mirror implementation code."""
    values = facts()
    if event in {"read-completed", "write-ready", "write-started", "confirmed-applied",
                 "confirmed-not-applied", "outcome-unknown", "possible-partial", "consumed"}:
        values["read_attempted"] = True
    if event in {"write-started", "confirmed-applied", "confirmed-not-applied",
                 "outcome-unknown", "possible-partial", "consumed"}:
        values.update(write_attempted=True, write_started=True)
    if event == "interrupted":
        if interrupted_from in {"read-completed", "write-ready", "write-started"}:
            values["read_attempted"] = True
        if interrupted_from == "write-started":
            values.update(write_attempted=True, write_started=True)
    values.update(changes)
    return values


def record_for(phase="prepared", **changes):
    values = {"schema_version": 1, "phase": phase, "action_digest": "a" * 64,
              "attempt_fingerprint": "b" * 32, "read_attempted": False,
              "write_attempted": False, "write_started": False, "review_generated": False}
    values.update(facts_for(phase, **changes))
    return {key: values[key] for key in ("schema_version", "phase", "action_digest", "attempt_fingerprint", "read_attempted", "write_attempted", "write_started", "review_generated")}


def assert_public(test, result, status, failure_class="none"):
    public = result.to_public_dict()
    test.assertEqual({"status", "phase", "classification", "failure_class", "write_authority", "review_generated"}, set(public))
    test.assertEqual(status, public["status"])
    test.assertEqual(failure_class, public["failure_class"])
    test.assertFalse(public["write_authority"])


def journal(root):
    from new_system_recovery_journal import RecoveryJournal
    return RecoveryJournal(root)


def _child_transition(root, queue):
    """Independent-process public-entry probe; no private implementation hook."""
    try:
        queue.put(journal(Path(root)).transition("prepared", facts_for("prepared")).to_public_dict()["status"])
    except Exception:
        queue.put("child-failed")


class DynamicTrap:
    def __init__(self, calls): self.calls = calls
    def __getattr__(self, _name):
        self.calls.append("getattr")
        raise RuntimeError(CANARY)
    def __repr__(self):
        self.calls.append("repr")
        raise RuntimeError(CANARY)


class RecoveryJournalRedGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()

    def test_no_record_all_entries_have_no_authority(self):
        subject = journal(self.root)
        self.assertEqual("fresh-review-required", subject.resume_review().to_public_dict()["status"])
        self.assertEqual("prepared", subject.transition("prepared", facts_for("prepared")).to_public_dict()["phase"])
        self.assertEqual("rejected", subject.clear_terminal().to_public_dict()["status"])

    def test_write_started_requires_the_mandatory_write_ready_gate(self):
        subject = journal(self.root)
        self.assertEqual("accepted", subject.transition("prepared", facts_for("prepared")).to_public_dict()["status"])
        self.assertEqual("accepted", subject.transition("read-completed", facts_for("read-completed")).to_public_dict()["status"])
        rejected = subject.transition("write-started", facts_for("write-started")).to_public_dict()
        self.assertEqual("rejected", rejected["status"])
        self.assertFalse(rejected["write_authority"])

    def test_frozen_graph_each_source_has_legal_and_illegal_symmetric_events(self):
        paths = (
            (("prepared",), ("read-completed", "interrupted"), "write-started"),
            (("prepared", "read-completed"), ("write-ready", "interrupted"), "write-started"),
            (("prepared", "read-completed", "write-ready"), ("write-started", "interrupted"), "prepared"),
            (("prepared", "read-completed", "write-ready", "write-started"),
             ("confirmed-applied", "confirmed-not-applied", "outcome-unknown", "possible-partial", "interrupted"), "prepared"),
        )
        for prefix, legal, illegal in paths:
            for event in legal:
                with self.subTest(source=prefix[-1], event=event):
                    subject = journal(self.root / (prefix[-1] + event))
                    previous = "no-record"
                    for step in prefix:
                        subject.transition(step, facts_for(step, interrupted_from=previous)); previous = step
                    self.assertEqual(event, subject.transition(event, facts_for(event, interrupted_from=previous)).to_public_dict()["phase"])
            with self.subTest(source=prefix[-1], event="illegal"):
                subject = journal(self.root / (prefix[-1] + "bad"))
                previous = "no-record"
                for step in prefix:
                    subject.transition(step, facts_for(step, interrupted_from=previous)); previous = step
                self.assertEqual("rejected", subject.transition(illegal, facts_for(illegal, interrupted_from=previous)).to_public_dict()["status"])

    def test_terminal_phases_cannot_transition_and_resume_classifies_facts(self):
        for phase, classification in (("outcome-unknown", "fresh-review-required"), ("possible-partial", "fresh-review-required"), ("interrupted", "fresh-review-required"), ("consumed", "fresh-review-required")):
            with self.subTest(phase=phase):
                subject = journal(self.root / phase)
                subject.transition("prepared", facts_for("prepared"))
                subject.transition("read-completed", facts_for("read-completed"))
                subject.transition("write-ready", facts_for("write-ready"))
                subject.transition("write-started", facts_for("write-started"))
                if phase == "consumed":
                    subject.transition("confirmed-applied", facts_for("confirmed-applied"))
                subject.transition(phase, facts_for(phase, interrupted_from="write-started"))
                self.assertEqual("rejected", subject.transition("prepared", facts_for("prepared")).to_public_dict()["status"])
                self.assertEqual(classification, journal(self.root / phase).resume_review().to_public_dict()["status"])

    def test_restart_classification_uses_durable_io_facts_and_pre_io_uncertainty(self):
        for event, interrupted_from, classification in (
            ("write-ready", None, "possible-partial"),
            ("write-started", None, "possible-partial"),
            ("interrupted", "read-completed", "fresh-reinspection"),
            ("interrupted", "write-started", "possible-partial"),
        ):
            with self.subTest(event=event, interrupted_from=interrupted_from):
                root = self.root / (event + "-" + classification)
                subject = journal(root)
                subject.transition("prepared", facts_for("prepared"))
                subject.transition("read-completed", facts_for("read-completed"))
                if event == "write-started" or interrupted_from == "write-started":
                    subject.transition("write-ready", facts_for("write-ready"))
                    subject.transition("write-started", facts_for("write-started"))
                if event != "write-started":
                    subject.transition(event, facts_for(event, interrupted_from=interrupted_from or "read-completed"))
                if event == "write-ready":
                    durable = json.loads(
                        (root / ".todo_archive" / "new_system_recovery.json").read_text()
                    )
                    self.assertEqual(
                        (True, False, False),
                        tuple(
                            durable[key]
                            for key in (
                                "read_attempted",
                                "write_attempted",
                                "write_started",
                            )
                        ),
                    )
                public = journal(root).resume_review().to_public_dict()
                self.assertEqual(classification, public["classification"])
                self.assertFalse(public["write_authority"])

    def test_phase_facts_are_independent_and_illegal_combinations_are_rejected(self):
        bad = (("prepared", facts_for("prepared", read_attempted=True)),
               ("read-completed", facts_for("prepared")),
               ("write-started", facts_for("read-completed")),
               ("confirmed-applied", facts_for("read-completed")))
        for event, supplied in bad:
            with self.subTest(event=event):
                subject = journal(self.root / ("bad-facts-" + event))
                self.assertEqual("rejected", subject.transition(event, supplied).to_public_dict()["status"])

    def test_invalid_dynamic_and_different_action_poison_every_phase_and_entry(self):
        for phase in ("no-record", "prepared", "read-completed", "write-started", "consumed"):
            with self.subTest(phase=phase):
                subject = journal(self.root / phase)
                if phase != "no-record": subject.transition("prepared", facts_for("prepared"))
                if phase in {"read-completed", "write-started", "consumed"}: subject.transition("read-completed", facts_for("read-completed"))
                if phase in {"write-started", "consumed"}: subject.transition("write-ready", facts_for("write-ready")); subject.transition("write-started", facts_for("write-started"))
                if phase == "consumed": subject.transition("confirmed-applied", facts_for("confirmed-applied")); subject.transition("consumed", facts_for("consumed"))
                calls = []
                self.assertEqual("rejected", subject.transition("prepared", facts(extra=DynamicTrap(calls))).to_public_dict()["status"])
                self.assertEqual([], calls)
                self.assertFalse(subject.resume_review().to_public_dict()["write_authority"])
                self.assertEqual("rejected", subject.clear_terminal().to_public_dict()["status"])

    def test_real_owner_conflict_same_different_action_and_process_simulation(self):
        first, second = journal(self.root), journal(self.root)
        entered, release = threading.Event(), threading.Event()
        blocked = [False]; parent_result = []
        real_write = os.write
        def held_write(_fd, buffer):
            if not blocked[0]:
                blocked[0] = True; entered.set(); release.wait(1)
            return real_write(_fd, buffer)
        with mock.patch("new_system_recovery_journal.os.write", side_effect=held_write):
            worker = threading.Thread(target=lambda: parent_result.append(first.transition("prepared", facts_for("prepared"))))
            worker.start()
            self.assertTrue(entered.wait(1))
            entries = (lambda: second.transition("prepared", facts_for("prepared")),
                       lambda: second.transition("prepared", facts_for("prepared", action_digest="c" * 64)),
                       second.resume_review, second.clear_terminal)
            for entry in entries:
                with self.subTest(entry=entry):
                    self.assertEqual("lock-conflicted", entry().to_public_dict()["status"])
            # Production implementation must use an OS-visible fixed lock, not only
            # an instance mutex; a child uses the same public transition seam.
            queue = multiprocessing.Queue()
            child = multiprocessing.Process(target=_child_transition, args=(str(self.root), queue))
            child.start(); child.join(2)
            self.assertEqual(0, child.exitcode)
            self.assertEqual("lock-conflicted", queue.get(timeout=1))
            release.set(); worker.join(1); self.assertFalse(worker.is_alive())
        assert_public(self, parent_result[0], "accepted")
        leaf = self.root / ".todo_archive" / "new_system_recovery.json"
        self.assertEqual("prepared", json.loads(leaf.read_text())["phase"])
        assert_public(self, journal(self.root).resume_review(), "fresh-review-required")

    def test_os_write_callback_reentry_and_copy_lifecycle_are_rejected(self):
        subject = journal(self.root)
        with mock.patch("new_system_recovery_journal.os.write", side_effect=lambda *_: subject.resume_review()):
            self.assertEqual("rejected", subject.transition("prepared", facts()).to_public_dict()["status"])
        for operation in (copy.copy, copy.deepcopy, pickle.dumps):
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(TypeError): operation(subject)

    def test_copy_before_inflight_and_persisted_do_not_change_original_authority(self):
        for stage in ("before", "inflight", "persisted"):
            with self.subTest(stage=stage):
                subject = journal(self.root / stage)
                leaf = self.root / stage / ".todo_archive" / "new_system_recovery.json"
                before = None
                if stage == "inflight":
                    entered, release = threading.Event(), threading.Event()
                    worker_result = []
                    real_write = os.write
                    def block(_fd, buffer): entered.set(); release.wait(1); return real_write(_fd, buffer)
                    with mock.patch("new_system_recovery_journal.os.write", side_effect=block):
                        worker = threading.Thread(target=lambda: worker_result.append(subject.transition("prepared", facts_for("prepared"))))
                        worker.start(); self.assertTrue(entered.wait(1))
                        for operation in (copy.copy, copy.deepcopy, pickle.dumps):
                            with self.assertRaises(TypeError): operation(subject)
                        release.set(); worker.join(1); self.assertFalse(worker.is_alive()); assert_public(self, worker_result[0], "accepted")
                elif stage == "persisted":
                    assert_public(self, subject.transition("prepared", facts_for("prepared")), "accepted")
                    before = (leaf.stat().st_ino, leaf.read_bytes(), json.loads(leaf.read_text())["phase"])
                    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
                        with self.assertRaises(TypeError): operation(subject)
                else:
                    for operation in (copy.copy, copy.deepcopy, pickle.dumps):
                        with self.assertRaises(TypeError): operation(subject)
                after = None if not leaf.exists() else (leaf.stat().st_ino, leaf.read_bytes(), json.loads(leaf.read_text())["phase"])
                if stage == "before": self.assertIsNone(after)
                elif stage == "persisted": self.assertEqual(before, after)
                else: self.assertEqual("prepared", after[2])

    def test_fixed_paths_modes_atomicity_and_private_content(self):
        result = journal(self.root).transition("prepared", facts_for("prepared"))
        todo = self.root / ".todo_archive"
        path, lock = todo / "new_system_recovery.json", todo / "new_system_recovery.lock"
        self.assertTrue(path.is_file() and lock.is_file())
        self.assertEqual(0o700, todo.stat().st_mode & 0o777)
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertEqual(0o600, lock.stat().st_mode & 0o777)
        raw = path.read_text()
        for secret in (CANARY, BAD_ID, "https://synthetic.invalid/marker", "synthetic-response-marker"):
            self.assertNotIn(secret, raw)
            self.assertNotIn(secret, repr(result))

    def test_every_diagnostic_and_on_disk_exit_is_private(self):
        subject = journal(self.root)
        hostile = {"action_digest": "a" * 64, "attempt_fingerprint": "b" * 32,
                   "raw_action": CANARY, "id": BAD_ID, "url": "https://synthetic.invalid/marker",
                   "response": "synthetic-response-marker"}
        result = subject.transition("prepared", hostile)
        rendered = repr(result) + repr(subject) + json.dumps(result.to_public_dict(), sort_keys=True)
        for secret in (CANARY, BAD_ID, "https://synthetic.invalid/marker", "synthetic-response-marker"):
            self.assertNotIn(secret, rendered)
        todo = self.root / ".todo_archive"
        for entry in todo.iterdir() if todo.exists() else ():
            self.assertNotIn(CANARY, entry.read_text(errors="ignore"))

    def test_symlink_hardlink_modes_owner_inode_directory_and_lock_replacement_fail_closed(self):
        todo = self.root / ".todo_archive"; todo.mkdir(mode=0o755)
        path = todo / "new_system_recovery.json"; lock = todo / "new_system_recovery.lock"
        for target in (path, lock):
            target.symlink_to(self.root / "outside")
            self.assertEqual("rejected", journal(self.root).transition("prepared", facts()).to_public_dict()["status"])
            target.unlink()
        path.write_text("{}"); os.chmod(path, 0o644)
        self.assertEqual("rejected", journal(self.root).resume_review().to_public_dict()["status"])

    def test_unpredictable_temp_open_uses_same_directory_safe_flags_and_never_deletes_foreign_path(self):
        subject = journal(self.root / "temp-open"); seen = []; real_open = os.open
        def observe(path, flags, *args, **kwargs):
            seen.append((path, flags, kwargs.get("dir_fd")))
            return real_open(path, flags, *args, **kwargs)
        with mock.patch("new_system_recovery_journal.os.open", side_effect=observe):
            subject.transition("prepared", facts_for("prepared"))
        temp = [item for item in seen if item[1] & os.O_EXCL]
        self.assertTrue(temp); self.assertTrue(all(flags & os.O_NOFOLLOW for _, flags, _ in temp))
        self.assertTrue(all(dir_fd is not None for _, _, dir_fd in temp))
        foreign = self.root / "foreign"; foreign.write_bytes(b"foreign")
        with mock.patch("new_system_recovery_journal.os.open", side_effect=OSError(errno.EEXIST, "exists")):
            assert_public(self, journal(self.root / "temp-exists").transition("prepared", facts_for("prepared")), "rejected", "storage-unsafe")
        self.assertTrue(foreign.exists())

    def test_wrong_owner_is_checked_at_each_public_entry(self):
        for entry in ("transition", "resume_review", "clear_terminal"):
            with self.subTest(entry=entry):
                getuid = mock.Mock(return_value=os.getuid() + 1)
                subject = journal(self.root / ("wrong-owner-" + entry))
                with mock.patch("new_system_recovery_journal.os.getuid", getuid):
                    result = subject.transition("prepared", facts_for("prepared")) if entry == "transition" else getattr(subject, entry)()
                assert_public(self, result, "rejected", "storage-unsafe"); getuid.assert_called()

    def test_replace_post_verify_inode_swap_is_identity_drift(self):
        subject = journal(self.root / "inode-swap")
        real_replace, called = os.replace, mock.Mock()
        def replace_then_swap(src, dst):
            real_replace(src, dst); before = os.stat(dst).st_ino
            saved = str(dst) + ".saved"; real_replace(dst, saved); Path(dst).write_bytes(b"replacement")
            self.assertNotEqual(before, os.stat(dst).st_ino); called()
        with mock.patch("new_system_recovery_journal.os.replace", side_effect=replace_then_swap):
            assert_public(self, subject.transition("prepared", facts_for("prepared")), "rejected", "identity-drift")
        called.assert_called_once()

    def test_directory_swap_and_lock_replacement_are_observed_at_os_boundaries(self):
        root = self.root / "directory-swap"; subject = journal(root); called = mock.Mock(); real_fsync = os.fsync
        def fsync_then_swap(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode) and not called.called:
                todo, saved = root / ".todo_archive", root / ".todo_archive.saved"
                os.rename(todo, saved); todo.mkdir(mode=0o700); called()
            return real_fsync(fd)
        with mock.patch("new_system_recovery_journal.os.fsync", side_effect=fsync_then_swap):
            assert_public(self, subject.transition("prepared", facts_for("prepared")), "rejected", "identity-drift")
        called.assert_called_once()

        root = self.root / "lock-replacement"; subject = journal(root); called = mock.Mock()
        def flock_then_replace(fd, operation):
            if not called.called and operation:
                lock = root / ".todo_archive" / "new_system_recovery.lock"
                replacement = lock.with_suffix(".replacement"); replacement.write_bytes(b"lock"); os.replace(replacement, lock); called()
            return original_flock(fd, operation)
        import fcntl
        original_flock = fcntl.flock
        with mock.patch("new_system_recovery_journal.fcntl.flock", side_effect=flock_then_replace):
            assert_public(self, subject.transition("prepared", facts_for("prepared")), "rejected", "storage-unsafe")
        called.assert_called_once()

    def test_system_boundary_failures_and_write_progress_are_bounded(self):
        real_write = os.write
        for syscall in ("fsync", "replace"):
            with self.subTest(syscall=syscall):
                called = mock.Mock(side_effect=OSError)
                with mock.patch("new_system_recovery_journal.os." + syscall, called):
                    self.assertEqual("rejected", journal(self.root / syscall).transition("prepared", facts_for("prepared")).to_public_dict()["status"])
                self.assertGreaterEqual(called.call_count, 1)
        subject = journal(self.root / "unlink")
        subject.transition("prepared", facts_for("prepared")); subject.transition("read-completed", facts_for("read-completed"))
        subject.transition("write-ready", facts_for("write-ready"))
        subject.transition("write-started", facts_for("write-started")); subject.transition("confirmed-applied", facts_for("confirmed-applied")); subject.resume_review()
        called = mock.Mock(side_effect=OSError)
        with mock.patch("new_system_recovery_journal.os.unlink", called):
            self.assertEqual("rejected", subject.clear_terminal().to_public_dict()["status"])
        self.assertEqual(1, called.call_count)
        for writes, expected_calls, expected in (("one", 3, "accepted"), ("sixteen", 16, "accepted"), ("seventeen", 16, "rejected"), ("zero", 1, "rejected")):
            with self.subTest(writes=writes):
                calls = []
                def progress(_fd, buffer):
                    calls.append(len(buffer))
                    if writes == "zero": return 0
                    if writes == "one": return real_write(_fd, buffer)
                    if writes == "sixteen":
                        if len(calls) == 1 or len(calls) == 16: return real_write(_fd, buffer)
                        remaining = 16 - len(calls)
                        return real_write(_fd, buffer[:max(1, (len(buffer) + remaining - 1) // remaining)])
                    return real_write(_fd, buffer[:1])  # real progress leaves the record unfinished at the shared cap
                with mock.patch("new_system_recovery_journal.os.write", side_effect=progress):
                    result = journal(self.root / writes).transition("prepared", facts_for("prepared")).to_public_dict()["status"]
                self.assertEqual(expected_calls, len(calls)); self.assertEqual(expected, result)

    def test_real_callback_reentry_hits_every_entry_and_never_continues(self):
        for boundary in ("write", "fsync"):
            for entry in ("transition", "resume_review", "clear_terminal"):
                with self.subTest(boundary=boundary, entry=entry):
                    subject = journal(self.root / (boundary + entry))
                    callback = (lambda *_: subject.transition("prepared", facts()) if entry == "transition"
                                else getattr(subject, entry)())
                    with mock.patch("new_system_recovery_journal.os." + boundary, side_effect=callback):
                        self.assertEqual("rejected", subject.transition("prepared", facts()).to_public_dict()["status"])

    def test_exact_resource_oracles_are_not_schema_padding(self):
        for size in (4095, 4096, 4097):
            with self.subTest(raw_bytes=size):
                root = self.root / ("bytes-" + str(size)); todo = root / ".todo_archive"; todo.mkdir(parents=True, mode=0o700)
                raw = json.dumps(record_for("prepared"), separators=(",", ":")).encode()
                raw += b" " * (size - len(raw))
                self.assertEqual(size, len(raw))
                leaf = todo / "new_system_recovery.json"; leaf.write_bytes(raw); os.chmod(leaf, 0o600)
                assert_public(self, journal(root).resume_review(), "fresh-review-required", "none" if size <= 4096 else "resource-bound")
        for digest, expected in (("a" * 63, "rejected"), ("a" * 64, "accepted"), ("a" * 65, "rejected")):
            with self.subTest(digest=len(digest)):
                self.assertEqual(expected, journal(self.root / ("digest-" + str(len(digest)))).transition("prepared", facts_for("prepared", action_digest=digest)).to_public_dict()["status"])
        for fingerprint, expected in (("b" * 31, "rejected"), ("b" * 32, "accepted"), ("b" * 33, "rejected")):
            with self.subTest(fingerprint=len(fingerprint)):
                self.assertEqual(expected, journal(self.root / ("fingerprint-" + str(len(fingerprint)))).transition("prepared", facts_for("prepared", attempt_fingerprint=fingerprint)).to_public_dict()["status"])
        for key, bad in (("action_digest", "A" * 64), ("action_digest", "g" * 64), ("attempt_fingerprint", "B" * 32), ("attempt_fingerprint", "z" * 32)):
            with self.subTest(key=key, bad=bad[:1]):
                self.assertEqual("rejected", journal(self.root / (key + bad[:1])).transition("prepared", facts_for("prepared", **{key: bad})).to_public_dict()["status"])

    def test_corrupt_truncated_extra_identity_drift_and_cleanup_never_delete_wrong_file(self):
        todo = self.root / ".todo_archive"; todo.mkdir(mode=0o700)
        path = todo / "new_system_recovery.json"
        for raw in (b"{", b'{"extra":true}', b"x" * 4097):
            path.write_bytes(raw); os.chmod(path, 0o600)
            subject = journal(self.root)
            self.assertEqual("fresh-review-required" if len(raw) > 4096 else "rejected", subject.resume_review().to_public_dict()["status"])
            self.assertEqual("rejected", subject.clear_terminal().to_public_dict()["status"])

    def test_cleanup_abnormal_phases_identity_drift_and_system_failures_never_delete(self):
        for phase in ("outcome-unknown", "possible-partial", "interrupted", "corrupt", "stale", "inflight"):
            with self.subTest(phase=phase):
                subject = journal(self.root / phase)
                todo = self.root / phase / ".todo_archive"; todo.mkdir(parents=True, mode=0o700)
                if phase in {"outcome-unknown", "possible-partial", "interrupted"}:
                    subject.transition("prepared", facts_for("prepared")); subject.transition("read-completed", facts_for("read-completed")); subject.transition("write-ready", facts_for("write-ready")); subject.transition("write-started", facts_for("write-started"))
                    subject.transition(phase, facts_for(phase, interrupted_from="write-started"))
                elif phase == "inflight":
                    subject.transition("prepared", facts_for("prepared")); subject.transition("read-completed", facts_for("read-completed")); subject.transition("write-ready", facts_for("write-ready")); subject.transition("write-started", facts_for("write-started"))
                elif phase == "stale":
                    subject.transition("prepared", facts_for("prepared"))
                    leaf = todo / "new_system_recovery.json"; before = (leaf.stat().st_ino, leaf.read_bytes())
                    assert_public(self, journal(self.root / phase).transition("read-completed", facts_for("read-completed", action_digest="c" * 64)), "rejected", "state-invalid")
                    self.assertEqual(before, (leaf.stat().st_ino, leaf.read_bytes()))
                else:
                    (todo / "new_system_recovery.json").write_text("{")
                    os.chmod(todo / "new_system_recovery.json", 0o600)
                subject.resume_review()
                self.assertEqual("rejected", subject.clear_terminal().to_public_dict()["status"])
        for failure in ("identity-drift", "unlink", "fsync"):
            with self.subTest(failure=failure):
                subject = journal(self.root / failure)
                subject.transition("prepared", facts_for("prepared")); subject.transition("read-completed", facts_for("read-completed"))
                subject.transition("write-ready", facts_for("write-ready"))
                subject.transition("write-started", facts_for("write-started"))
                subject.transition("confirmed-applied", facts_for("confirmed-applied"))
                subject.resume_review()
                patch = "os.unlink" if failure == "unlink" else "os.fsync"
                context = mock.patch("new_system_recovery_journal." + patch, side_effect=OSError) if failure != "identity-drift" else mock.patch("new_system_recovery_journal.os.stat", side_effect=OSError)
                with context:
                    self.assertEqual("rejected", subject.clear_terminal().to_public_dict()["status"])

    def test_terminal_cleanup_allowed_only_after_review_for_each_confirmed_terminal(self):
        for phase in ("confirmed-applied", "confirmed-not-applied", "consumed"):
            with self.subTest(phase=phase):
                subject = journal(self.root / phase)
                subject.transition("prepared", facts_for("prepared"))
                subject.transition("read-completed", facts_for("read-completed"))
                subject.transition("write-ready", facts_for("write-ready"))
                subject.transition("write-started", facts_for("write-started"))
                if phase == "consumed":
                    subject.transition("confirmed-applied", facts_for("confirmed-applied"))
                subject.transition(phase, facts_for(phase))
                self.assertEqual("rejected", subject.clear_terminal().to_public_dict()["status"])
                subject.resume_review()
                self.assertEqual("cleared", subject.clear_terminal().to_public_dict()["status"])

    def test_target_phase_fact_tampering_preserves_prior_record(self):
        cases = (("read-completed", ("prepared",), facts_for("prepared")),
                 ("write-started", ("prepared", "read-completed", "write-ready"), facts_for("read-completed")),
                 ("confirmed-applied", ("prepared", "read-completed", "write-ready", "write-started"), facts_for("read-completed")))
        for target, prefix, tampered in cases:
            with self.subTest(target=target):
                root = self.root / ("facts-" + target); subject = journal(root)
                for event in prefix: subject.transition(event, facts_for(event))
                leaf = root / ".todo_archive" / "new_system_recovery.json"; before = (leaf.stat().st_ino, leaf.read_bytes())
                assert_public(self, subject.transition(target, tampered), "rejected", "state-invalid")
                self.assertEqual(before, (leaf.stat().st_ino, leaf.read_bytes()))

    def test_same_different_action_and_replay_across_lifecycle(self):
        cases = (("prepared", ("prepared",), "read-completed"),
                 ("read-completed", ("prepared", "read-completed"), "write-ready"),
                 ("write-started", ("prepared", "read-completed", "write-ready", "write-started"), "confirmed-applied"),
                 ("confirmed-applied", ("prepared", "read-completed", "write-ready", "write-started", "confirmed-applied"), "consumed"),
                 ("consumed", ("prepared", "read-completed", "write-ready", "write-started", "confirmed-applied", "consumed"), None))
        for name, prefix, next_event in cases:
            with self.subTest(phase=name):
                root = self.root / ("actions-" + name); subject = journal(root)
                result = None
                for event in prefix: result = subject.transition(event, facts_for(event))
                self.assertEqual(name, result.to_public_dict()["phase"])
                if next_event:
                    # Each variation starts from the same fully-constructed phase.
                    assert_public(self, subject.transition(next_event, facts_for(next_event)), "accepted")
                for changed in ({"action_digest": "c" * 64}, {"attempt_fingerprint": "d" * 32}):
                    variant = journal(self.root / ("actions-" + name + changed.keys().__iter__().__next__()))
                    for event in prefix: variant.transition(event, facts_for(event))
                    assert_public(self, variant.transition(next_event or "consumed", facts_for(next_event or "consumed", **changed)), "rejected", "state-invalid")
                replay = journal(self.root / ("actions-" + name + "replay"))
                for event in prefix: replay.transition(event, facts_for(event))
                assert_public(self, replay.transition(prefix[-1], facts_for(prefix[-1])), "rejected", "state-invalid")

    def test_independent_link_and_mode_attacks_are_not_masked(self):
        for kind in ("journal-symlink", "lock-symlink", "journal-hardlink", "lock-hardlink", "file-0644", "lock-0644", "dir-0755"):
            with self.subTest(kind=kind):
                root = self.root / kind; todo = root / ".todo_archive"; todo.mkdir(parents=True, mode=0o700)
                leaf, lock = todo / "new_system_recovery.json", todo / "new_system_recovery.lock"
                if kind.endswith("symlink"): (leaf if kind.startswith("journal") else lock).symlink_to(root / "outside")
                elif kind.endswith("hardlink"):
                    target = leaf if kind.startswith("journal") else lock; target.write_bytes(b"x"); os.chmod(target, 0o600); os.link(target, target.with_suffix(".linked")); self.assertEqual(2, target.stat().st_nlink)
                elif kind in {"file-0644", "lock-0644"}:
                    target = leaf if kind == "file-0644" else lock; target.write_bytes(b"{}"); os.chmod(target, 0o644)
                else: os.chmod(todo, 0o755)
                assert_public(self, journal(root).transition("prepared", facts_for("prepared")), "rejected", "storage-unsafe")

    def test_temp_open_hook_only_intercepts_actual_temp_open(self):
        for code in (errno.EEXIST, errno.ELOOP):
            with self.subTest(errno=code):
                root = self.root / ("temp-hook-" + str(code)); subject = journal(root); real_open = os.open; seen = []; foreign = root / "foreign"; root.mkdir(); foreign.write_bytes(b"keep")
                def fail_temp(path, flags, *args, **kwargs):
                    if flags & os.O_EXCL:
                        seen.append((path, flags, kwargs.get("dir_fd"))); raise OSError(code, "temp")
                    return real_open(path, flags, *args, **kwargs)
                with mock.patch("new_system_recovery_journal.os.open", side_effect=fail_temp):
                    assert_public(self, subject.transition("prepared", facts_for("prepared")), "rejected", "storage-unsafe")
                self.assertEqual(1, len(seen)); self.assertTrue(seen[0][1] & os.O_NOFOLLOW); self.assertIsNotNone(seen[0][2]); self.assertEqual(b"keep", foreign.read_bytes())

    def test_callback_reentry_is_rejected_after_nested_public_call(self):
        subject = journal(self.root / "callback")
        nested = []
        real_write = os.write
        def reenter(_fd, buffer):
            if not nested: nested.append(subject.resume_review().to_public_dict()["status"])
            return real_write(_fd, buffer)
        with mock.patch("new_system_recovery_journal.os.write", side_effect=reenter):
            assert_public(self, subject.transition("prepared", facts_for("prepared")), "rejected")
        self.assertEqual(1, len(nested))

    def test_complete_record_and_public_schema_are_exact(self):
        root = self.root / "schema"; result = journal(root).transition("prepared", facts_for("prepared")); leaf = root / ".todo_archive" / "new_system_recovery.json"
        stored = json.loads(leaf.read_text())
        self.assertEqual(set(record_for()), set(stored)); self.assertTrue(all(type(stored[key]) is type(record_for()[key]) for key in stored))
        assert_public(self, result, "accepted"); self.assertIs(type(result.to_public_dict()["review_generated"]), bool)

    def test_one_record_one_owner_boundaries_are_independent(self):
        root = self.root / "record-owner"; subject = journal(root)
        assert_public(self, subject.resume_review(), "fresh-review-required")  # zero record
        assert_public(self, subject.transition("prepared", facts_for("prepared")), "accepted")  # one record/owner
        other = journal(root)
        assert_public(self, other.transition("prepared", facts_for("prepared", action_digest="c" * 64, attempt_fingerprint="d" * 32)), "rejected", "state-invalid")  # second record after lock release

    def test_each_secret_is_not_exposed_by_storage_exception_or_diagnostics(self):
        secrets = (CANARY, BAD_ID, "https://synthetic.invalid/marker", "synthetic-response-marker")
        for secret in secrets:
            with self.subTest(secret_type=len(secret)):
                root = self.root / ("secret-" + str(len(secret))); subject = journal(root)
                with mock.patch("new_system_recovery_journal.os.write", side_effect=OSError(secret)):
                    result = subject.transition("prepared", facts_for("prepared"))
                rendered = repr(result) + json.dumps(result.to_public_dict(), sort_keys=True)
                self.assertNotIn(secret, rendered)
                todo = root / ".todo_archive"
                for entry in todo.iterdir() if todo.exists() else ():
                    self.assertNotIn(secret, entry.name); self.assertNotIn(secret, entry.read_text(errors="ignore"))

    def test_publication_file_and_directory_fsync_failures_are_distinct(self):
        for target_type in ("regular", "directory"):
            with self.subTest(target_type=target_type):
                root = self.root / ("fsync-" + target_type); real_fsync = os.fsync; hit = []
                def fail_target(fd):
                    mode = os.fstat(fd).st_mode
                    if (target_type == "regular" and stat.S_ISREG(mode)) or (target_type == "directory" and stat.S_ISDIR(mode)):
                        hit.append(target_type); raise OSError("private")
                    return real_fsync(fd)
                with mock.patch("new_system_recovery_journal.os.fsync", side_effect=fail_target):
                    assert_public(self, journal(root).transition("prepared", facts_for("prepared")), "rejected", "storage-failed")
                self.assertEqual([target_type], hit)

    def test_replace_callback_reentry_returns_legal_os_value_and_poison_outer(self):
        root = self.root / "replace-callback"; subject = journal(root); real_replace = os.replace; hit = []
        def reenter(src, dst):
            hit.append(subject.resume_review().to_public_dict()["status"]); real_replace(src, dst); return None
        with mock.patch("new_system_recovery_journal.os.replace", side_effect=reenter):
            assert_public(self, subject.transition("prepared", facts_for("prepared")), "rejected")
        self.assertEqual(1, len(hit))

    def test_cleanup_unlink_and_directory_fsync_callback_reentry_are_distinct(self):
        for boundary in ("unlink", "directory-fsync"):
            with self.subTest(boundary=boundary):
                root = self.root / ("cleanup-" + boundary); subject = journal(root)
                for event in ("prepared", "read-completed", "write-ready", "write-started", "confirmed-applied"):
                    subject.transition(event, facts_for(event))
                subject.resume_review(); leaf = root / ".todo_archive" / "new_system_recovery.json"; replacement = b"replacement"; hit = []
                if boundary == "unlink":
                    real_unlink = os.unlink
                    def callback(path, *args, **kwargs):
                        hit.append(subject.resume_review().to_public_dict()["status"]); saved = str(path) + ".saved"; os.rename(path, saved); Path(path).write_bytes(replacement); return None
                    context = mock.patch("new_system_recovery_journal.os.unlink", side_effect=callback)
                else:
                    real_fsync = os.fsync
                    def callback(fd):
                        if stat.S_ISDIR(os.fstat(fd).st_mode): hit.append(subject.resume_review().to_public_dict()["status"]); return real_fsync(fd)
                        return real_fsync(fd)
                    context = mock.patch("new_system_recovery_journal.os.fsync", side_effect=callback)
                with context: assert_public(self, subject.clear_terminal(), "rejected")
                self.assertTrue(hit)
                if boundary == "unlink": self.assertEqual(replacement, leaf.read_bytes()); self.assertTrue(Path(str(leaf) + ".saved").exists())

    def test_identity_drift_before_unlink_preserves_replacement_and_saved_leaf(self):
        root = self.root / "unlink-identity"; subject = journal(root)
        for event in ("prepared", "read-completed", "write-ready", "write-started", "confirmed-applied"):
            subject.transition(event, facts_for(event))
        subject.resume_review(); leaf = root / ".todo_archive" / "new_system_recovery.json"
        original_inode, original = leaf.stat().st_ino, leaf.read_bytes(); saved = Path(str(leaf) + ".saved"); replacement = b"replacement"; real_stat, real_unlink = os.stat, os.unlink; journal_stat_calls = []; initial_seen = []; final_hook_seen = []; unlink_order = []
        def final_stat(path, *args, **kwargs):
            name = Path(path).name if isinstance(path, (str, Path)) else ""
            if name == "new_system_recovery.json" and kwargs.get("follow_symlinks") is False:
                journal_stat_calls.append(True)
                if len(journal_stat_calls) == 1:
                    initial_seen.append(real_stat(path, *args, **kwargs).st_ino)
                elif len(journal_stat_calls) == 2:
                    self.assertTrue(initial_seen); os.rename(leaf, saved); leaf.write_bytes(replacement); os.chmod(leaf, 0o600); final_hook_seen.append(True)
            return real_stat(path, *args, **kwargs)
        unlink = mock.Mock(side_effect=lambda path, *args, **kwargs: unlink_order.append((len(journal_stat_calls), bool(final_hook_seen))) or real_unlink(path, *args, **kwargs))
        with mock.patch("new_system_recovery_journal.os.stat", side_effect=final_stat), mock.patch("new_system_recovery_journal.os.unlink", unlink):
            assert_public(self, subject.clear_terminal(), "rejected", "identity-drift")
        self.assertGreaterEqual(len(journal_stat_calls), 2); self.assertTrue(initial_seen); self.assertEqual([True], final_hook_seen); self.assertEqual(0, unlink.call_count); self.assertEqual([], unlink_order)
        self.assertTrue(saved.exists()); self.assertEqual(original_inode, saved.stat().st_ino); self.assertEqual(original, saved.read_bytes())
        self.assertNotEqual(original_inode, leaf.stat().st_ino); self.assertEqual(replacement, leaf.read_bytes())

    def test_fixed_lock_path_replacement_is_rejected_by_all_entries(self):
        for entry in ("transition", "resume_review", "clear_terminal"):
            with self.subTest(entry=entry):
                root = self.root / ("lock-path-" + entry); subject = journal(root)
                subject.transition("prepared", facts_for("prepared"))
                lock = root / ".todo_archive" / "new_system_recovery.lock"; replacement = lock.with_suffix(".new")
                replacement.write_bytes(b"lock"); os.chmod(replacement, 0o600); os.replace(replacement, lock)
                result = subject.transition("read-completed", facts_for("read-completed")) if entry == "transition" else getattr(subject, entry)()
                assert_public(self, result, "rejected", "identity-drift")

    def test_identity_bound_read_rejects_legal_record_swap_for_all_entries(self):
        for entry in ("transition", "resume_review", "clear_terminal"):
            with self.subTest(entry=entry):
                root = self.root / ("read-swap-" + entry); subject = journal(root)
                for event in ("prepared", "read-completed", "write-ready", "write-started", "confirmed-applied"):
                    subject.transition(event, facts_for(event))
                subject.resume_review(); leaf = root / ".todo_archive" / "new_system_recovery.json"; original = leaf.read_bytes(); real_stat = os.stat; swapped = []
                def swap_after_stat(path, *args, **kwargs):
                    result = real_stat(path, *args, **kwargs)
                    if Path(path).name == leaf.name and kwargs.get("follow_symlinks") is False and not swapped:
                        saved = leaf.with_suffix(".saved"); os.rename(leaf, saved); leaf.write_bytes(original); os.chmod(leaf, 0o600); swapped.append(True)
                    return result
                with mock.patch("new_system_recovery_journal.os.stat", side_effect=swap_after_stat):
                    result = subject.transition("consumed", facts_for("consumed")) if entry == "transition" else getattr(subject, entry)()
                self.assertTrue(swapped); self.assertEqual("rejected", result.to_public_dict()["status"])

    def test_publish_failure_persists_fail_closed_gate_across_new_instance(self):
        root = self.root / "publish-failure"; subject = journal(root)
        with mock.patch("new_system_recovery_journal.os.fsync", side_effect=OSError):
            assert_public(self, subject.transition("prepared", facts_for("prepared")), "rejected", "storage-failed")
        fresh = journal(root)
        assert_public(self, fresh.transition("prepared", facts_for("prepared")), "rejected", "state-invalid")
        assert_public(self, fresh.resume_review(), "fresh-review-required")
        assert_public(self, fresh.clear_terminal(), "rejected", "state-invalid")

    def test_atomic_publish_uses_at_most_sixteen_write_family_calls(self):
        root = self.root / "write-budget"; calls = []
        real_write = os.write
        def counted(fd, data): calls.append(True); return real_write(fd, data)
        with mock.patch("new_system_recovery_journal.os.write", side_effect=counted):
            assert_public(self, journal(root).transition("prepared", facts_for("prepared")), "accepted")
        self.assertLessEqual(len(calls), 16)

    def test_publication_closes_directory_fd(self):
        root = self.root / "fd-close"; opened = []; real_open, real_close = os.open, os.close
        def tracked_open(*args, **kwargs):
            fd = real_open(*args, **kwargs); opened.append(fd); return fd
        closed = []
        def tracked_close(fd): closed.append(fd); return real_close(fd)
        with mock.patch("new_system_recovery_journal.os.open", side_effect=tracked_open), mock.patch("new_system_recovery_journal.os.close", side_effect=tracked_close):
            assert_public(self, journal(root).transition("prepared", facts_for("prepared")), "accepted")
        self.assertTrue(set(opened).issubset(set(closed)))

    def test_raw_duplicate_keys_are_corrupt_not_folded(self):
        root = self.root / "duplicate-json"; todo = root / ".todo_archive"; todo.mkdir(parents=True, mode=0o700)
        raw = json.dumps(record_for("prepared"), separators=(",", ":"))[:-1] + ',"phase":"read-completed"}'
        leaf = todo / "new_system_recovery.json"; leaf.write_text(raw); os.chmod(leaf, 0o600)
        assert_public(self, journal(root).resume_review(), "rejected", "corrupt")

    def test_all_entries_revoke_on_corrupt_oversize_invalid_and_str_subclass(self):
        class EvilStr(str): pass
        for attack in ("corrupt", "oversize", "invalid", "subclass"):
            with self.subTest(attack=attack):
                root = self.root / ("revoke-" + attack); subject = journal(root)
                if attack == "corrupt":
                    todo = root / ".todo_archive"; todo.mkdir(parents=True, mode=0o700); (todo / "new_system_recovery.json").write_text("{")
                elif attack == "oversize":
                    todo = root / ".todo_archive"; todo.mkdir(parents=True, mode=0o700); (todo / "new_system_recovery.json").write_bytes(b"x" * 4097)
                else:
                    subject.transition("prepared", facts_for("prepared"))
                    bad = facts_for("read-completed") if attack == "invalid" else dict(facts_for("read-completed"), action_digest=EvilStr("a" * 64))
                    subject.transition("read-completed", bad)
                self.assertFalse(subject.resume_review().to_public_dict()["write_authority"])
                self.assertEqual("rejected", subject.clear_terminal().to_public_dict()["status"])

    def test_live_lock_path_replacement_is_revalidated_before_return(self):
        for entry in ("transition", "resume_review", "clear_terminal"):
            with self.subTest(entry=entry):
                root = self.root / ("live-lock-" + entry); outer = journal(root)
                outer.transition("prepared", facts_for("prepared"))
                lock = root / ".todo_archive" / "new_system_recovery.lock"; real_flock = __import__("fcntl").flock; swapped = []
                def replace_while_held(fd, operation):
                    value = real_flock(fd, operation)
                    if operation & __import__("fcntl").LOCK_EX and not swapped:
                        moved = lock.with_suffix(".held"); os.rename(lock, moved); lock.write_bytes(b"lock"); os.chmod(lock, 0o600); swapped.append(True)
                    return value
                with mock.patch("new_system_recovery_journal.fcntl.flock", side_effect=replace_while_held):
                    result = outer.transition("read-completed", facts_for("read-completed")) if entry == "transition" else getattr(outer, entry)()
                self.assertTrue(swapped); assert_public(self, result, "rejected", "identity-drift")

    def test_replace_then_directory_fsync_failure_persists_gate(self):
        root = self.root / "replace-dir-fsync"; subject = journal(root); real_fsync = os.fsync; hit = []
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode): hit.append(True); raise OSError("dir")
            return real_fsync(fd)
        with mock.patch("new_system_recovery_journal.os.fsync", side_effect=fail_directory):
            assert_public(self, subject.transition("prepared", facts_for("prepared")), "rejected", "storage-failed")
        self.assertEqual([True], hit)
        fresh = journal(root); assert_public(self, fresh.transition("read-completed", facts_for("read-completed")), "rejected", "state-invalid")
        assert_public(self, fresh.resume_review(), "fresh-review-required")

    def test_corrupt_and_invalid_transition_do_not_create_review(self):
        root = self.root / "corrupt-three-entry"; todo = root / ".todo_archive"; todo.mkdir(parents=True, mode=0o700); (todo / "new_system_recovery.json").write_text("{")
        for entry in ("transition", "resume_review", "clear_terminal"):
            result = journal(root).transition("prepared", facts_for("prepared")) if entry == "transition" else getattr(journal(root), entry)()
            self.assertFalse(result.to_public_dict()["review_generated"])
        root = self.root / "invalid-no-review"; subject = journal(root); subject.transition("prepared", facts_for("prepared"))
        subject.transition("read-completed", facts_for("prepared")); self.assertFalse(subject.resume_review().to_public_dict()["review_generated"])

    def test_write_and_pwrite_share_one_total_budget(self):
        root = self.root / "write-family"; calls = []; real_write, real_pwrite = os.write, os.pwrite
        with mock.patch("new_system_recovery_journal.os.write", side_effect=lambda fd, data: calls.append("w") or real_write(fd, data)), mock.patch("new_system_recovery_journal.os.pwrite", side_effect=lambda fd, data, offset: calls.append("p") or real_pwrite(fd, data, offset)):
            assert_public(self, journal(root).transition("prepared", facts_for("prepared")), "accepted")
        self.assertLessEqual(len(calls), 16)

    def test_str_subclass_key_and_non_plain_values_are_input_invalid(self):
        class EvilKey(str): pass
        class EvilValue(str): pass
        for index, supplied in enumerate(({EvilKey("action_digest"): "a" * 64}, {"action_digest": EvilValue("a" * 64)}, {"action_digest": ["a" * 64]})):
            values = facts_for("prepared"); values.update(supplied)
            if any(type(key) is not str for key in supplied):
                values.pop("action_digest"); values[EvilKey("action_digest")] = "a" * 64
            assert_public(self, journal(self.root / ("plain-" + str(index))).transition("prepared", values), "rejected", "input-invalid")

    def test_cleanup_directory_fsync_failure_closes_fd(self):
        root = self.root / "clear-fsync-close"; subject = journal(root)
        for event in ("prepared", "read-completed", "write-ready", "write-started", "confirmed-applied"): subject.transition(event, facts_for(event))
        subject.resume_review(); closed = []; real_close, real_fsync = os.close, os.fsync
        def close(fd): closed.append(fd); return real_close(fd)
        def fail_dir(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode): raise OSError("dir")
            return real_fsync(fd)
        with mock.patch("new_system_recovery_journal.os.close", side_effect=close), mock.patch("new_system_recovery_journal.os.fsync", side_effect=fail_dir):
            assert_public(self, subject.clear_terminal(), "rejected", "storage-failed")
        self.assertTrue(closed)

    def test_lock_replacement_after_initial_check_never_creates_two_owners(self):
        root = self.root / "late-lock-replacement"; outer = journal(root); outer.transition("prepared", facts_for("prepared")); lock = root / ".todo_archive" / "new_system_recovery.lock"; real_flock = __import__("fcntl").flock; calls = []
        def late_replace(fd, operation):
            result = real_flock(fd, operation)
            if operation & __import__("fcntl").LOCK_EX and not calls:
                calls.append(True); moved = lock.with_suffix(".old"); os.rename(lock, moved); lock.write_text("clean"); os.chmod(lock, 0o600)
            return result
        with mock.patch("new_system_recovery_journal.fcntl.flock", side_effect=late_replace):
            first = outer.transition("read-completed", facts_for("read-completed"))
        second = journal(root).transition("read-completed", facts_for("read-completed", action_digest="c" * 64))
        self.assertTrue(calls); self.assertEqual("rejected", first.to_public_dict()["status"]); self.assertNotEqual("accepted", second.to_public_dict()["status"])

    def test_directory_fsync_failure_survives_module_reload(self):
        import importlib, sys
        root = self.root / "reload-dirty"; real_fsync = os.fsync
        def fail_dir(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode): raise OSError("dir")
            return real_fsync(fd)
        with mock.patch("new_system_recovery_journal.os.fsync", side_effect=fail_dir):
            assert_public(self, journal(root).transition("prepared", facts_for("prepared")), "rejected", "storage-failed")
        importlib.reload(sys.modules["new_system_recovery_journal"])
        result = journal(root).transition("read-completed", facts_for("read-completed"))
        assert_public(self, result, "rejected", "state-invalid")

    def test_short_write_exact_sixteen_and_seventeenth_are_total_write_budget(self):
        for allowed in (True, False):
            with self.subTest(allowed=allowed):
                root = self.root / ("short-budget-" + str(allowed)); calls = []; real_write = os.write
                def short(fd, data):
                    calls.append(True)
                    if not allowed: return real_write(fd, data[:1])
                    if len(calls) == 1 or len(calls) == 16: return real_write(fd, data)
                    remaining = 16 - len(calls)
                    return real_write(fd, data[:max(1, (len(data) + remaining - 1) // remaining)])
                with mock.patch("new_system_recovery_journal.os.write", side_effect=short):
                    result = journal(root).transition("prepared", facts_for("prepared"))
                self.assertEqual(16, len(calls)); self.assertEqual("accepted" if allowed else "rejected", result.to_public_dict()["status"])

    def test_corrupt_and_invalid_evidence_is_closed_across_three_entries(self):
        root = self.root / "closed-corrupt"; todo = root / ".todo_archive"; todo.mkdir(parents=True, mode=0o700); (todo / "new_system_recovery.json").write_text("{")
        for entry in ("transition", "resume_review", "clear_terminal"):
            subject = journal(root); result = subject.transition("prepared", facts_for("prepared")) if entry == "transition" else getattr(subject, entry)()
            public = result.to_public_dict(); self.assertFalse(public["write_authority"]); self.assertFalse(public["review_generated"])
        root = self.root / "closed-invalid"; subject = journal(root); subject.transition("prepared", facts_for("prepared")); subject.transition("read-completed", facts_for("prepared"))
        assert_public(self, subject.resume_review(), "rejected", "state-invalid")

    def test_marker_and_journal_share_sixteen_write_family_budget(self):
        root = self.root / "shared-budget"; calls = []; real_write = os.write
        def short(fd, data):
            calls.append(True)
            if len(calls) == 1 or len(calls) == 16: return real_write(fd, data)
            remaining = 16 - len(calls)
            return real_write(fd, data[:max(1, (len(data) + remaining - 1) // remaining)])
        with mock.patch("new_system_recovery_journal.os.write", side_effect=short):
            result = journal(root).transition("prepared", facts_for("prepared"))
        self.assertLessEqual(len(calls), 16); self.assertEqual("accepted", result.to_public_dict()["status"])

    def test_marker_zero_write_is_fail_closed_and_persists_dirty(self):
        root = self.root / "marker-zero"
        with mock.patch("new_system_recovery_journal.os.write", return_value=0):
            assert_public(self, journal(root).transition("prepared", facts_for("prepared")), "rejected", "storage-failed")
        assert_public(self, journal(root).resume_review(), "fresh-review-required")

    def test_corrupt_evidence_has_one_closed_class_across_entries(self):
        root = self.root / "corrupt-evidence"; todo = root / ".todo_archive"; todo.mkdir(parents=True, mode=0o700); leaf = todo / "new_system_recovery.json"; leaf.write_text("{"); os.chmod(leaf, 0o600)
        for entry in ("transition", "resume_review", "clear_terminal"):
            subject = journal(root); result = subject.transition("prepared", facts_for("prepared")) if entry == "transition" else getattr(subject, entry)()
            public = result.to_public_dict(); self.assertEqual("corrupt", public["failure_class"]); self.assertFalse(public["review_generated"]); self.assertFalse(public["write_authority"])

    def test_live_lock_swap_second_instance_is_blocked_by_owned_sentinel(self):
        root = self.root / "sentinel-lock"; first = journal(root); first.transition("prepared", facts_for("prepared")); lock = root / ".todo_archive" / "new_system_recovery.lock"; real_write = os.write; swapped = []
        def replace_after_initial_check(fd, data):
            if not swapped:
                swapped.append(True); moved = lock.with_suffix(".old"); os.rename(lock, moved); lock.write_bytes(b"CLEAN___"); os.chmod(lock, 0o600)
            return real_write(fd, data)
        with mock.patch("new_system_recovery_journal.os.write", side_effect=replace_after_initial_check):
            outer = first.transition("read-completed", facts_for("read-completed"))
        second = journal(root).transition("read-completed", facts_for("read-completed", action_digest="c" * 64))
        self.assertTrue(swapped); assert_public(self, outer, "rejected", "identity-drift"); self.assertNotEqual("accepted", second.to_public_dict()["status"])
        self.assertTrue(any(path.name.startswith(".new-system-inflight-") for path in (root / ".todo_archive").iterdir()))

    def test_zero_clean_marker_retains_sentinel_across_reload(self):
        import importlib, sys
        root = self.root / "sentinel-clean-zero"; real_write = os.write; calls = []
        def zero_clean(fd, data):
            calls.append(bytes(data))
            if bytes(data).startswith(b"CLEAN"): return 0
            return real_write(fd, data)
        with mock.patch("new_system_recovery_journal.os.write", side_effect=zero_clean):
            assert_public(self, journal(root).transition("prepared", facts_for("prepared")), "rejected", "storage-failed")
        importlib.reload(sys.modules["new_system_recovery_journal"])
        assert_public(self, journal(root).resume_review(), "fresh-review-required")

    def test_post_publish_and_post_unlink_failures_leave_restart_review_gate(self):
        import importlib, sys
        root = self.root / "sentinel-resume-reload"; subject = journal(root); real_fsync = os.fsync
        def fail_publish_dir(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode): raise OSError("private")
            return real_fsync(fd)
        with mock.patch("new_system_recovery_journal.os.fsync", side_effect=fail_publish_dir):
            assert_public(self, subject.transition("prepared", facts_for("prepared")), "rejected", "storage-failed")
        importlib.reload(sys.modules["new_system_recovery_journal"])
        assert_public(self, journal(root).resume_review(), "fresh-review-required")
        root = self.root / "sentinel-clear-reload"; subject = journal(root)
        for event in ("prepared", "read-completed", "write-ready", "write-started", "confirmed-applied"): subject.transition(event, facts_for(event))
        subject.resume_review()
        def fail_cleanup_dir(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode): raise OSError("private")
            return real_fsync(fd)
        with mock.patch("new_system_recovery_journal.os.fsync", side_effect=fail_cleanup_dir):
            assert_public(self, subject.clear_terminal(), "rejected", "storage-failed")
        importlib.reload(sys.modules["new_system_recovery_journal"])
        assert_public(self, journal(root).resume_review(), "fresh-review-required")

    def test_budget_exhaustion_and_corrupt_evidence_are_restart_safe_and_closed(self):
        import importlib, sys
        root = self.root / "sentinel-budget"; real_write = os.write; calls = []
        def short(fd, data):
            calls.append(True); return real_write(fd, data[:1])
        with mock.patch("new_system_recovery_journal.os.write", side_effect=short):
            assert_public(self, journal(root).transition("prepared", facts_for("prepared")), "rejected", "storage-failed")
        self.assertEqual(16, len(calls)); importlib.reload(sys.modules["new_system_recovery_journal"])
        assert_public(self, journal(root).resume_review(), "fresh-review-required")
        root = self.root / "sentinel-corrupt"; todo = root / ".todo_archive"; todo.mkdir(parents=True, mode=0o700); (todo / "new_system_recovery.json").write_text("{"); os.chmod(todo / "new_system_recovery.json", 0o600)
        for entry in ("transition", "resume_review", "clear_terminal"):
            subject = journal(root); result = subject.transition("prepared", facts_for("prepared")) if entry == "transition" else getattr(subject, entry)()
            public = result.to_public_dict(); self.assertEqual("rejected", public["status"]); self.assertEqual("corrupt", public["failure_class"]); self.assertFalse(public["review_generated"]); self.assertFalse(public["write_authority"])

    def test_each_mutating_public_entry_uses_an_owned_inflight_sentinel(self):
        root = self.root / "sentinel-entries"; subject = journal(root); real_open = os.open; names = []
        def record_open(path, flags, *args, **kwargs):
            if type(path) is str and path.startswith(".new-system-inflight-"):
                names.append(path); self.assertTrue(flags & os.O_EXCL); self.assertTrue(flags & os.O_NOFOLLOW)
            return real_open(path, flags, *args, **kwargs)
        with mock.patch("new_system_recovery_journal.os.open", side_effect=record_open):
            subject.transition("prepared", facts_for("prepared"))
            subject.transition("read-completed", facts_for("read-completed"))
            subject.transition("write-ready", facts_for("write-ready"))
            subject.transition("write-started", facts_for("write-started"))
            subject.transition("confirmed-applied", facts_for("confirmed-applied"))
            subject.resume_review(); subject.clear_terminal()
        self.assertGreaterEqual(len(names), 6)

    def test_sentinel_cleanup_rechecks_identity_before_unlink_across_entries(self):
        import importlib, sys
        for entry in ("transition", "resume_review", "clear_terminal"):
            with self.subTest(entry=entry):
                root = self.root / ("sentinel-cleanup-" + entry); subject = journal(root)
                if entry != "transition":
                    for event in ("prepared", "read-completed", "write-ready", "write-started", "confirmed-applied"):
                        subject.transition(event, facts_for(event))
                    if entry == "clear_terminal": subject.resume_review()
                real_stat, real_unlink = os.stat, os.unlink; swapped = []; unlinks = []
                def swap_after_first_identity(path, *args, **kwargs):
                    info = real_stat(path, *args, **kwargs)
                    if Path(path).name.startswith(".new-system-inflight-") and not swapped:
                        saved = Path(str(path) + ".saved"); os.rename(path, saved); Path(path).write_bytes(b""); os.chmod(path, 0o600); swapped.extend((saved, Path(path)))
                    return info
                def record_sentinel_unlink(path):
                    if Path(path).name.startswith(".new-system-inflight-"):
                        unlinks.append(Path(path)); return None
                    return real_unlink(path)
                with mock.patch("new_system_recovery_journal.os.stat", side_effect=swap_after_first_identity), mock.patch("new_system_recovery_journal.os.unlink", side_effect=record_sentinel_unlink):
                    result = subject.transition("prepared", facts_for("prepared")) if entry == "transition" else getattr(subject, entry)()
                self.assertTrue(swapped); assert_public(self, result, "rejected", "identity-drift"); self.assertEqual([], unlinks)
                self.assertTrue(swapped[0].exists()); self.assertTrue(swapped[1].exists())
                importlib.reload(sys.modules["new_system_recovery_journal"])
                assert_public(self, journal(root).resume_review(), "fresh-review-required")

    def test_real_sentinel_unlink_then_error_keeps_restart_gate_across_entries(self):
        import importlib, sys
        for entry in ("transition", "resume_review", "clear_terminal"):
            with self.subTest(entry=entry):
                root = self.root / ("sentinel-unlink-error-" + entry); subject = journal(root)
                if entry != "transition":
                    for event in ("prepared", "read-completed", "write-ready", "write-started", "confirmed-applied"):
                        subject.transition(event, facts_for(event))
                    if entry == "clear_terminal": subject.resume_review()
                real_unlink, hit = os.unlink, []
                def unlink_then_fail(path):
                    if Path(path).name.startswith(".new-system-inflight-"):
                        hit.append(True); real_unlink(path); raise OSError("private")
                    return real_unlink(path)
                with mock.patch("new_system_recovery_journal.os.unlink", side_effect=unlink_then_fail):
                    result = subject.transition("prepared", facts_for("prepared")) if entry == "transition" else getattr(subject, entry)()
                self.assertTrue(hit); self.assertEqual("rejected", result.to_public_dict()["status"])
                importlib.reload(sys.modules["new_system_recovery_journal"])
                fresh = journal(root)
                assert_public(self, fresh.transition("prepared", facts_for("prepared")), "rejected", "state-invalid")
                self.assertNotEqual("cleared", fresh.clear_terminal().to_public_dict()["status"])


if __name__ == "__main__": unittest.main()
