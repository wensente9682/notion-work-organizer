"""Private, single-record recovery facts for the guided New System write path."""

import copy
import fcntl
import hashlib
import json
import os
import stat
import threading
from pathlib import Path

_LOCK_FSYNC = os.fsync


_PHASES = {"prepared", "read-completed", "write-started", "confirmed-applied",
           "confirmed-not-applied", "outcome-unknown", "possible-partial", "interrupted", "consumed"}
_NEXT = {
    None: {"prepared"}, "prepared": {"read-completed", "interrupted"},
    "read-completed": {"write-started", "interrupted"},
    "write-started": {"confirmed-applied", "confirmed-not-applied", "outcome-unknown", "possible-partial", "interrupted"},
    "confirmed-applied": {"consumed"}, "confirmed-not-applied": {"consumed"},
}
_KEYS = ("schema_version", "phase", "action_digest", "attempt_fingerprint", "read_attempted", "write_attempted", "write_started", "review_generated")
_SENTINEL_PREFIX = ".new-system-inflight-"
class _StorageFailed(Exception): pass
class _IdentityDrift(Exception): pass
class _Poisoned(Exception): pass


class _Result:
    __slots__ = ("_data",)
    def __init__(self, status, phase=None, classification="fresh-reinspection", failure_class="none", review_generated=False):
        self._data = {"status": status, "phase": phase, "classification": classification,
                      "failure_class": failure_class, "write_authority": False,
                      "review_generated": bool(review_generated)}
    def to_public_dict(self): return dict(self._data)
    def __repr__(self): return "RecoveryResult(" + self._data["status"] + ")"


class RecoveryJournal:
    __slots__ = ("_root", "_dir", "_leaf", "_lock", "_active", "_revoked", "_lock_identity")
    def __init__(self, root):
        self._root = Path(root); self._dir = self._root / ".todo_archive"
        self._leaf = self._dir / "new_system_recovery.json"; self._lock = self._dir / "new_system_recovery.lock"
        self._active = threading.local(); self._revoked = False; self._lock_identity = None
    def __copy__(self): raise TypeError("opaque")
    def __deepcopy__(self, memo): raise TypeError("opaque")
    def __reduce__(self): raise TypeError("opaque")

    def _result(self, status, phase=None, failure="none", classification="fresh-reinspection", reviewed=False):
        return _Result(status, phase, classification, failure, reviewed)
    def _safe(self, fn):
        try: return fn()
        except _Poisoned: return self._result("rejected")
        except _StorageFailed: return self._result("rejected", failure="storage-failed")
        except _IdentityDrift: return self._result("rejected", failure="identity-drift")
        except OSError: return self._result("rejected", failure="storage-unsafe")
        except Exception: return self._result("rejected", failure="storage-failed")
    def _ensure_dir(self):
        self._root.mkdir(parents=True, exist_ok=True)
        if not self._dir.exists(): self._dir.mkdir(mode=0o700)
        info = os.stat(self._dir, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or (info.st_mode & 0o777) != 0o700 or info.st_uid != os.getuid(): raise OSError
    def _valid_file(self, path, allow_missing=False):
        try: info = os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            if allow_missing: return None
            raise
        if not stat.S_ISREG(info.st_mode) or (info.st_mode & 0o777) != 0o600 or info.st_uid != os.getuid() or info.st_nlink != 1: raise OSError
        return info
    def _has_incomplete_publication(self):
        for entry in self._dir.iterdir():
            if entry.name.startswith(".new-system-recovery-"):
                self._valid_file(entry)
                return True
        return False
    def _has_sentinel(self):
        for entry in self._dir.iterdir():
            if entry.name.startswith(_SENTINEL_PREFIX):
                self._valid_file(entry)
                return True
        return False
    def _claim(self):
        if getattr(self._active, "inside", False): self._active.poison = True; return None
        self._ensure_dir()
        fd = os.open(self._lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or (info.st_mode & 0o777) != 0o600 or info.st_nlink != 1 or info.st_uid != os.getuid(): raise OSError
            lock_identity = (info.st_dev, info.st_ino)
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: os.close(fd); return "locked"
            path_info = self._valid_file(self._lock)
            if (path_info.st_dev, path_info.st_ino) != lock_identity: raise _IdentityDrift
            if self._lock_identity is not None and self._lock_identity != lock_identity: raise _IdentityDrift
            self._lock_identity = lock_identity
            directory = os.stat(self._dir, follow_symlinks=False)
            self._active.dir_identity = (directory.st_dev, directory.st_ino)
            marker = os.pread(fd, 8, 0)
            if marker not in {b"", b"clean", b"dirty", b"CLEAN___", b"DIRTY___"}: raise OSError
            self._active.inside = True; self._active.poison = False; self._active.dirty = marker in {b"dirty", b"DIRTY___"}; self._active.writes = 0; return fd
        except Exception:
            os.close(fd); raise
    def _release(self, fd):
        if isinstance(fd, int):
            try: fcntl.flock(fd, fcntl.LOCK_UN)
            finally: os.close(fd)
        self._active.inside = False
    def _mark(self, fd, state):
        if state not in {b"DIRTY___", b"CLEAN___"}: raise _StorageFailed
        os.lseek(fd, 0, os.SEEK_SET); self._write_all(fd, state)
        try: _LOCK_FSYNC(fd)
        except OSError as exc: raise _StorageFailed from exc
        os.lseek(fd, 0, os.SEEK_SET)
        if os.read(fd, len(state)) != state: raise _StorageFailed
    def _write_all(self, fd, data):
        offset = 0
        while offset < len(data):
            used = getattr(self._active, "writes", 0)
            if used >= 16: raise _StorageFailed
            count = os.write(fd, data[offset:])
            self._active.writes = used + 1
            if type(count) is not int or count <= 0 or count > len(data) - offset: raise _StorageFailed
            offset += count
    def _lock_matches(self, fd):
        held = os.fstat(fd); path = self._valid_file(self._lock)
        if (held.st_dev, held.st_ino) != (path.st_dev, path.st_ino): raise _IdentityDrift
    def _dir_matches(self):
        info = os.stat(self._dir, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != getattr(self._active, "dir_identity", None): raise _IdentityDrift
    def _sentinel_begin(self):
        if self._has_sentinel() or self._has_incomplete_publication(): raise _StorageFailed
        name = _SENTINEL_PREFIX + os.urandom(8).hex()
        dfd = os.open(self._dir, os.O_RDONLY)
        try:
            try: sfd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            except OSError: raise
            try:
                info = os.fstat(sfd)
                if not stat.S_ISREG(info.st_mode) or (info.st_mode & 0o777) != 0o600 or info.st_nlink != 1 or info.st_uid != os.getuid(): raise OSError
                try: os.fsync(dfd)
                except OSError as exc: raise _StorageFailed from exc
            finally: os.close(sfd)
        finally: os.close(dfd)
        return self._dir / name, (info.st_dev, info.st_ino)
    def _sentinel_end(self, sentinel, identity):
        sfd = os.open(sentinel, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            held = os.fstat(sfd)
            if (not stat.S_ISREG(held.st_mode) or (held.st_mode & 0o777) != 0o600
                    or held.st_uid != os.getuid() or held.st_nlink != 1
                    or (held.st_dev, held.st_ino) != identity):
                raise _IdentityDrift
            first = self._valid_file(sentinel)
            second = self._valid_file(sentinel)
            held_identity = (held.st_dev, held.st_ino)
            if (first.st_dev, first.st_ino) != held_identity or (second.st_dev, second.st_ino) != held_identity:
                raise _IdentityDrift
            try: os.unlink(sentinel)
            except OSError as exc:
                self._restore_missing_sentinel(sentinel)
                raise _StorageFailed from exc
            if self._valid_file(sentinel, True) is not None: raise _IdentityDrift
        finally: os.close(sfd)
        dfd = os.open(self._dir, os.O_RDONLY)
        try:
            try: os.fsync(dfd)
            except OSError as exc:
                self._restore_missing_sentinel(sentinel)
                raise _StorageFailed from exc
        finally: os.close(dfd)
    def _restore_missing_sentinel(self, sentinel):
        if self._valid_file(sentinel, True) is not None: return
        dfd = os.open(self._dir, os.O_RDONLY)
        try:
            try: sfd = os.open(sentinel.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dfd)
            except OSError: return
            try:
                info = os.fstat(sfd)
                if not stat.S_ISREG(info.st_mode) or (info.st_mode & 0o777) != 0o600 or info.st_uid != os.getuid() or info.st_nlink != 1: return
                try: os.fsync(dfd)
                except OSError: return
            finally: os.close(sfd)
        finally: os.close(dfd)
    def _lifecycle(self, fd, operation):
        sentinel, identity = self._sentinel_begin()
        self._dir_matches()
        self._mark(fd, b"DIRTY___")
        result = operation()
        if getattr(self._active, "poison", False): raise _Poisoned
        self._dir_matches()
        self._lock_matches(fd)
        self._sentinel_end(sentinel, identity)
        self._dir_matches()
        self._lock_matches(fd)
        self._mark(fd, b"CLEAN___")
        if getattr(self._active, "poison", False): raise _Poisoned
        return result
    def _read(self):
        info = self._valid_file(self._leaf, True)
        if info is None: return None, None
        fd = os.open(self._leaf, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            bound = os.fstat(fd)
            if (bound.st_dev, bound.st_ino) != (info.st_dev, info.st_ino): raise _IdentityDrift
            raw = b""
            while True:
                block = os.read(fd, 4097 - len(raw))
                raw += block
                if not block or len(raw) > 4096: break
        finally: os.close(fd)
        if len(raw) > 4096: raise ValueError("resource")
        def no_duplicates(pairs):
            result = {}
            for key, value in pairs:
                if key in result: raise ValueError("duplicate")
                result[key] = value
            return result
        try: data = json.loads(raw, object_pairs_hook=no_duplicates)
        except Exception: raise ValueError("corrupt")
        if type(data) is not dict or tuple(data) != _KEYS and set(data) != set(_KEYS): raise ValueError("corrupt")
        if type(data.get("schema_version")) is not int or data["schema_version"] != 1 or data.get("phase") not in _PHASES: raise ValueError("corrupt")
        if not _hex(data.get("action_digest"), 64) or not _hex(data.get("attempt_fingerprint"), 32): raise ValueError("corrupt")
        if any(type(data[k]) is not bool for k in _KEYS[4:]): raise ValueError("corrupt")
        return data, info
    def _write(self, data):
        raw = json.dumps(data, separators=(",", ":")).encode()
        if len(raw) > 4096: raise ValueError
        name = ".new-system-recovery-" + os.urandom(8).hex()
        dirfd = os.open(self._dir, os.O_RDONLY)
        try: fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dirfd)
        finally: os.close(dirfd)
        # Keep this small implementation explicit: the temp fd owns its own publication.
        temp = self._dir / name
        try:
            self._write_all(fd, raw)
            try: os.fsync(fd)
            except OSError as exc: raise _StorageFailed from exc
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or (info.st_mode & 0o777) != 0o600 or info.st_nlink != 1: raise OSError
        finally: os.close(fd)
        os.replace(temp, self._leaf)
        try: before = self._valid_file(self._leaf)
        except OSError as exc: raise _IdentityDrift from exc
        if before.st_ino != info.st_ino: raise _IdentityDrift
        dfd = os.open(self._dir, os.O_RDONLY)
        try: os.fsync(dfd)
        except OSError as exc: raise _StorageFailed from exc
        finally: os.close(dfd)
        try: after = self._valid_file(self._leaf)
        except OSError as exc: raise _IdentityDrift from exc
        if before.st_ino != after.st_ino: raise _IdentityDrift
    def transition(self, event, facts):
        return self._safe(lambda: self._transition(event, facts))
    def _transition(self, event, facts):
        fd = self._claim()
        if fd == "locked": return self._result("lock-conflicted", failure="lock-conflicted")
        if fd is None: return self._result("rejected", failure="state-invalid")
        try:
            try: data, _ = self._read()
            except ValueError:
                return self._result("rejected", failure="corrupt")
            if self._revoked or getattr(self._active, "dirty", False) or self._has_incomplete_publication() or self._has_sentinel(): return self._result("rejected", failure="state-invalid")
            if type(event) is not str or event not in _PHASES or type(facts) is not dict: self._revoked=True; return self._result("rejected", failure="input-invalid")
            phase = None if data is None else data["phase"]
            if event not in _NEXT.get(phase, set()): self._revoked=True; return self._result("rejected", phase, "state-invalid")
            if not _plain_facts(facts): self._revoked=True; return self._result("rejected", phase, "input-invalid")
            fact = _facts(event, facts, phase)
            if fact is None: self._revoked=True; return self._result("rejected", phase, "state-invalid")
            if data and (fact["action_digest"] != data["action_digest"] or fact["attempt_fingerprint"] != data["attempt_fingerprint"]): self._revoked=True; return self._result("rejected", phase, "state-invalid")
            record = {"schema_version": 1, "phase": event, **fact, "review_generated": False}
            self._lifecycle(fd, lambda: self._write(record))
            return self._result("accepted", event)
        finally: self._release(fd)
    def resume_review(self):
        return self._safe(self._resume)
    def _resume(self):
        fd = self._claim()
        if fd == "locked": return self._result("lock-conflicted", failure="lock-conflicted")
        if fd is None: return self._result("rejected", failure="state-invalid")
        try:
            try: data, _ = self._read()
            except ValueError as exc: return self._result("fresh-review-required" if str(exc)=="resource" else "rejected", failure="resource-bound" if str(exc)=="resource" else "corrupt")
            if self._revoked: return self._result("rejected", failure="state-invalid")
            if getattr(self._active, "dirty", False) or self._has_sentinel(): return self._result("fresh-review-required")
            if data is None: return self._result("fresh-review-required")
            classification = "possible-partial" if data["phase"] in {"write-started", "interrupted"} else "fresh-reinspection"
            if data["phase"] in {"confirmed-applied", "confirmed-not-applied", "consumed"} and not data["review_generated"]:
                data = dict(data); data["review_generated"] = True; self._lifecycle(fd, lambda: self._write(data))
            if getattr(self._active, "poison", False): return self._result("rejected", data["phase"])
            return self._result("fresh-review-required", data["phase"], classification=classification, reviewed=data["review_generated"])
        finally: self._release(fd)
    def clear_terminal(self):
        return self._safe(self._clear)
    def _clear(self):
        fd = self._claim()
        if fd == "locked": return self._result("lock-conflicted", failure="lock-conflicted")
        if fd is None: return self._result("rejected", failure="state-invalid")
        try:
            try: data, first = self._read()
            except ValueError:
                return self._result("rejected", failure="corrupt")
            if self._revoked or self._has_sentinel() or not data or data["phase"] not in {"confirmed-applied", "confirmed-not-applied", "consumed"} or not data["review_generated"]: return self._result("rejected", failure="state-invalid")
            second = self._valid_file(self._leaf)
            if first.st_ino != second.st_ino: return self._result("rejected", failure="identity-drift")
            if getattr(self._active, "poison", False): return self._result("rejected", data["phase"])
            def remove():
                latest = self._valid_file(self._leaf)
                if (latest.st_dev, latest.st_ino) != (first.st_dev, first.st_ino): raise _IdentityDrift
                os.unlink(self._leaf)
                dfd=os.open(self._dir, os.O_RDONLY)
                try: os.fsync(dfd)
                except OSError as exc: raise _StorageFailed from exc
                finally: os.close(dfd)
            self._lifecycle(fd, remove)
            return self._result("cleared")
        finally: self._release(fd)


def _hex(value, size):
    return type(value) is str and len(value) == size and all(c in "0123456789abcdef" for c in value)

def _facts(event, facts, previous):
    keys = {"action_digest", "attempt_fingerprint", "read_attempted", "write_attempted", "write_started"}
    if any(type(key) is not str for key in facts) or set(facts) != keys or not _hex(facts.get("action_digest"), 64) or not _hex(facts.get("attempt_fingerprint"), 32): return None
    if any(type(facts[k]) is not bool for k in keys - {"action_digest", "attempt_fingerprint"}): return None
    expected = {"read_attempted": event in {"read-completed", "write-started", "confirmed-applied", "confirmed-not-applied", "outcome-unknown", "possible-partial", "consumed"},
                "write_attempted": event in {"write-started", "confirmed-applied", "confirmed-not-applied", "outcome-unknown", "possible-partial", "consumed"},
                "write_started": event in {"write-started", "confirmed-applied", "confirmed-not-applied", "outcome-unknown", "possible-partial", "consumed"}}
    if event == "interrupted":
        expected = {"read_attempted": previous in {"read-completed", "write-started"}, "write_attempted": previous == "write-started", "write_started": previous == "write-started"}
    if any(facts[k] != expected[k] for k in expected): return None
    return dict(facts)

def _plain_facts(facts):
    keys = {"action_digest", "attempt_fingerprint", "read_attempted", "write_attempted", "write_started"}
    return (type(facts) is dict and all(type(key) is str for key in facts) and set(facts) == keys
            and _hex(facts.get("action_digest"), 64) and _hex(facts.get("attempt_fingerprint"), 32)
            and all(type(facts[key]) is bool for key in keys - {"action_digest", "attempt_fingerprint"}))
