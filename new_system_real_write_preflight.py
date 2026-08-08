"""Synthetic-only, one-read capability preflight for the guided archive target."""

from __future__ import annotations

from dataclasses import dataclass
from types import FunctionType
from typing import Mapping
import threading
import time


_LOCK = threading.Lock()
_REENTRY = False
_KINDS = ("database", "property", "view", "sort", "archive-target")
_LABELS = ("tool-surface-present", "request-shape-ready", "real-write-unproven")
TARGET_ATTESTATION_PROMPT = "请确认：你提供的页面就是本次 New System creation 的目标父页面。确认本身不会修改 Notion；后续每个创建或修改动作仍会单独向你展示并要求批准。"
_CONTRACT = {
    "database": ("create_database", ("parent", "schema")),
    "property": ("configure_property", ("database", "property", "type")),
    "view": ("create_view", ("database", "view")),
    "sort": ("configure_view_sort", ("database", "view", "sort")),
    "archive-target": ("create_archive_target", ("container", "category", "schema")),
}


@dataclass(frozen=True)
class CapabilityPreflightResult:
    failure_class: str | None
    _public: dict[str, object]

    def to_public_dict(self) -> dict[str, object]:
        return dict(self._public)

    def __repr__(self) -> str:
        return "CapabilityPreflightResult(status='safe')"


def _result(failure: str | None, **values: object) -> CapabilityPreflightResult:
    if failure is not None:
        return CapabilityPreflightResult(failure, {"status": "rejected"})
    return CapabilityPreflightResult(None, values)


def _plain_attestation(value: object, target: object, session: object) -> bool:
    if type(value) is not dict or len(value) != 6:
        return False
    for key, item in dict.items(value):
        if type(key) is not str or key not in {"target_parent_attested", "creation_flow_attested", "dedicated_blank_parent", "not_t6", "target", "session"}:
            return False
        if key in {"target", "session"}:
            continue
        if type(item) is not bool or item is not True:
            return False
    return (
        set(value) == {"target_parent_attested", "creation_flow_attested", "dedicated_blank_parent", "not_t6", "target", "session"}
        and type(value["target"]) is str
        and type(value["session"]) is str
        and value["target"] == target
        and value["session"] == session
    )


def _contract_ok(value: object) -> bool:
    if type(value) is not dict or set(value) != set(_KINDS):
        return False
    for kind in _KINDS:
        item = value.get(kind)
        if type(item) is not dict or set(item) != {"surface", "request_schema"}:
            return False
        surface, schema = _CONTRACT[kind]
        if type(item["surface"]) is not str or item["surface"] != surface:
            return False
        if type(item["request_schema"]) is not tuple or item["request_schema"] != schema:
            return False
        if any(type(field) is not str for field in item["request_schema"]):
            return False
    return True


def _reader(connector: object) -> FunctionType | None:
    cls = type(connector)
    try:
        mro = type.__getattribute__(cls, "__mro__")
        instance = object.__getattribute__(connector, "__dict__")
    except BaseException:
        return None
    if type(instance) is not dict:
        return None
    unsafe = ("search", "write", "create", "update", "delete", "switch", "discover", "query", "move", "comment", "duplicate", "__getattr__", "__getattribute__")
    for owner in (*mro, instance):
        if owner is object:
            continue
        try:
            names = type.__getattribute__(owner, "__dict__") if isinstance(owner, type) else dict.keys(owner)
        except BaseException:
            return None
        for name in names:
            if type(name) is not str or name != "read_target" and any(word in name.lower() for word in unsafe):
                return None
    try:
        method = type.__getattribute__(cls, "__dict__").get("read_target")
    except BaseException:
        return None
    return method if type(method) is FunctionType else None


def _clock(value: object) -> int | float | None:
    try:
        point = value()
    except Exception:
        return None
    return point if type(point) in {int, float} else None


def _preview_spec_ok(value: object, target: str) -> bool:
    if value is None:
        return True
    if type(value) is not dict or len(value) != 3:
        return False
    expected = {"parent", "blank_state", "schema"}
    if set(value) != expected or any(type(key) is not str for key in dict.keys(value)):
        return False
    return (
        type(value["parent"]) is str and value["parent"] == target
        and type(value["blank_state"]) is str and value["blank_state"] == "blank"
        and type(value["schema"]) is str and value["schema"] == "canonical-archive-v1"
    )


def _resource(value: object, depth: int = 0, total: list[int] | None = None) -> bool:
    total = [0] if total is None else total
    if depth > 3:
        return False
    if value is None or type(value) in {bool, int}:
        return True
    if type(value) is str:
        size = len(value.encode("utf-8"))
        total[0] += size
        return size <= 128 and total[0] <= 256
    if type(value) is list:
        return len(value) <= 16 and all(_resource(item, depth + 1, total) for item in list.__iter__(value))
    if type(value) is dict:
        return len(value) <= 16 and all(type(key) is str and _resource(item, depth + 1, total) for key, item in dict.items(value))
    return False


def _response(value: object) -> str | None:
    if type(value) is not dict:
        return "response-contract"
    access = value.get("access")
    if access in {"unauthenticated", "forbidden"}:
        return "access-denied"
    if set(value) != {"kind", "blank", "resources"}:
        return "response-contract"
    if not _resource(value["resources"], -1):
        return "resource-bound"
    if type(value["kind"]) is not str or value["kind"] != "page":
        return "response-contract"
    if type(value["blank"]) is not bool or value["blank"] is not True or type(value["resources"]) is not list or value["resources"]:
        return "response-contract"
    return None


def evaluate_real_write_capability(connector, attestation, target, platform_contract, *, session=None, clock=None, temp_root=None, preview_spec=None, forbidden_seams=None, process_spy=None):
    """Read one synthetic target and return only a non-executable capability preview."""
    del temp_root, forbidden_seams, process_spy
    if type(target) is not str or not target or type(session) is not str or not session:
        return _result("target-invalid")
    if not _plain_attestation(attestation, target, session):
        return _result("attestation-required")
    if not _contract_ok(platform_contract):
        return _result("connector-rejected")
    global _REENTRY
    if not _LOCK.acquire(blocking=False):
        _REENTRY = True
        return _result("reentry")
    _REENTRY = False
    try:
        timer = time.monotonic if clock is None else clock
        start = _clock(timer)
        if start is None:
            return _result("deadline")
        reader = _reader(connector)
        if reader is None:
            return _result("connector-rejected")
        try:
            observed = reader(connector, target)
        except KeyboardInterrupt:
            return _result("interrupted")
        except TimeoutError:
            return _result("deadline")
        except Exception:
            return _result("response-contract")
        if _REENTRY:
            return _result("reentry")
        end = _clock(timer)
        if end is None or end <= start or end - start > 50:
            return _result("deadline")
        failure = _response(observed)
        if failure:
            return _result(failure)
        preview = {"action_count": 1, "kind": "create_archive_target", "binding": "direct-child", "expected_state": "blank", "schema": "canonical-archive-v1", "executable": False}
        if not _preview_spec_ok(preview_spec, target):
            return _result("response-contract")
        return _result(None, status="preflight-complete", capabilities={kind: _LABELS for kind in _KINDS}, preview=preview, authority="none", forbidden_method_calls=0, write_authority=False)
    finally:
        _LOCK.release()
