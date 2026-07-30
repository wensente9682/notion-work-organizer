"""Local, bounded, read-only connector-smoke seam (no real connector calls)."""

from __future__ import annotations

from dataclasses import dataclass
from types import FunctionType, MappingProxyType
import threading
from typing import Mapping


_CLAIM = threading.Lock()
_MAX_DEPTH = 4
_MAX_NODES = 64
_MAX_CONTAINER = 16
_REQUEST_KEYS = frozenset({
    "workspace_confirmed", "target", "deadline_ms", "sample_cap", "read_cap",
    "clock", "reenter", "sinks",
})
_EVIDENCE_FIELDS = (
    "target_confirmed", "connector_calls_before_confirmation", "discovery_attempted",
    "read_calls", "write_calls", "adapter_surface_valid", "real_read_completed",
    "authn_ok", "authz_ok", "failure_class", "target_count", "read_call_count",
    "sample_count", "limit_hit", "pagination_attempted", "response_kind",
    "shape_valid", "identity_stable", "partial_response", "parser_ok",
    "plain_data_only", "parser_steps", "parser_budget_hit", "side_effect_calls",
    "deadline_budget_class", "timing_bucket", "deadline_exceeded", "completed_phase",
    "interrupted", "reentry_rejected", "read_cap_respected", "target_lifetime",
    "execution_record_only", "persistent_copy_created_elsewhere",
    "reusable_authority_created", "forbidden_exit_matches", "evidence_schema_version",
    "count_cap_result", "response_kind_class", "stable_fingerprint_present",
    "evidence_schema", "sensitive_value_matches", "stdout_bytes", "stderr_bytes",
    "diagnostic_exit_matches", "write_authority_created", "profile_changed",
    "readiness_claimed", "scope_label",
)


@dataclass(frozen=True)
class SmokeResult:
    failure_class: str | None
    evidence: Mapping[str, object]
    scope_label: str = "bounded-read-only-smoke"

    def to_public_dict(self) -> dict[str, object]:
        return {
            "outcome": "success" if self.failure_class is None else "failed",
            "failure_class": self.failure_class,
            "scope_label": self.scope_label,
            "timing_bucket": self.evidence["timing_bucket"],
            "count_cap_result": self.evidence["count_cap_result"],
        }


def _evidence(failure: str | None, *, reads: int = 0, confirmed: bool = False,
              phase: str = "unstarted", interrupted: bool = False, reentry: bool = False,
              limit: bool = False, shape: bool = False, identity: bool = False,
              partial: bool = False, parser: bool = False, measured: bool = False,
              surface: bool = True, authn: bool = False, authz: bool = False,
              parser_budget: bool = False) -> Mapping[str, object]:
    values = {field: False for field in _EVIDENCE_FIELDS}
    success = failure is None
    values.update({
        "target_confirmed": confirmed,
        "connector_calls_before_confirmation": reads if not confirmed else 0,
        "discovery_attempted": False,
        "read_calls": reads,
        "write_calls": 0,
        "adapter_surface_valid": surface,
        "real_read_completed": success,
        "authn_ok": authn,
        "authz_ok": authz,
        "failure_class": failure or "none",
        "target_count": "one",
        "read_call_count": reads,
        "sample_count": "capped",
        "limit_hit": limit,
        "pagination_attempted": False,
        "response_kind": "allowed" if shape else "rejected",
        "shape_valid": shape,
        "identity_stable": identity,
        "partial_response": partial,
        "parser_ok": parser,
        "plain_data_only": shape,
        "parser_steps": "capped",
        "parser_budget_hit": parser_budget,
        "side_effect_calls": 0,
        "deadline_budget_class": "bounded" if measured else "unmeasured",
        "timing_bucket": "within-budget" if success and measured else (
            "deadline-exceeded" if failure == "deadline" else "not-measured"),
        "deadline_exceeded": failure == "deadline",
        "completed_phase": phase,
        "interrupted": interrupted,
        "reentry_rejected": reentry,
        "read_cap_respected": reads <= 1,
        "target_lifetime": "call-only",
        "execution_record_only": True,
        "persistent_copy_created_elsewhere": False,
        "reusable_authority_created": False,
        "forbidden_exit_matches": 0,
        "evidence_schema_version": "v1",
        "count_cap_result": "within-cap" if not limit else "cap-hit",
        "response_kind_class": "structural",
        "stable_fingerprint_present": False,
        "evidence_schema": "closed",
        "sensitive_value_matches": 0,
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "diagnostic_exit_matches": 0,
        "write_authority_created": False,
        "profile_changed": False,
        "readiness_claimed": False,
        "scope_label": "bounded-read-only-smoke",
    })
    return MappingProxyType(values)


def _result(failure: str | None, **facts: object) -> SmokeResult:
    return SmokeResult(failure, _evidence(failure, **facts))


def _plain_request(request: object) -> dict[str, object] | None:
    if type(request) is not dict:
        return None
    try:
        plain: dict[str, object] = {}
        for key, value in dict.items(request):
            if type(key) is not str or key not in _REQUEST_KEYS:
                return None
            plain[key] = value
        return plain
    except BaseException:
        return None


def _valid_request(request: object) -> tuple[dict[str, object] | None, bool, bool, FunctionType | None]:
    plain = _plain_request(request)
    if plain is None:
        return None, False, False, None
    confirmed = plain.get("workspace_confirmed") is True
    target = plain.get("target")
    valid_target = type(target) is str and bool(target.strip())
    if type(plain.get("deadline_ms")) is not int or plain["deadline_ms"] < 0:
        return plain, False, confirmed, None
    if type(plain.get("sample_cap")) is not int or not 0 < plain["sample_cap"] <= _MAX_CONTAINER:
        return plain, False, confirmed, None
    if type(plain.get("read_cap")) is not int or plain["read_cap"] != 1:
        return plain, False, confirmed, None
    clock = plain.get("clock")
    return plain, valid_target, confirmed, clock if type(clock) is FunctionType else None


def _read_only_method(adapter: object) -> FunctionType | None:
    cls = type(adapter)
    try:
        hierarchy = type.__getattribute__(cls, "__mro__")
        instance = object.__getattribute__(adapter, "__dict__")
    except BaseException:
        return None
    if type(instance) is not dict:
        return None
    forbidden = ("search", "discover", "query", "write", "create", "update", "move", "comment", "duplicate", "callback")
    for namespace in (*hierarchy, instance):
        try:
            if isinstance(namespace, type):
                names = type.__getattribute__(namespace, "__dict__").keys()
            else:
                names = dict.keys(namespace)
        except BaseException:
            return None
        for name in names:
            if type(name) is not str:
                return None
            dynamic_hook = name in {"__getattr__", "__getattribute__"} and namespace is not object
            if dynamic_hook or any(word in name.lower() for word in forbidden):
                return None
    try:
        method = type.__getattribute__(cls, "__dict__").get("read_target")
    except BaseException:
        return None
    return method if type(method) is FunctionType else None


def _plain(value: object, depth: int = 0, nodes: list[int] | None = None) -> str:
    nodes = [0] if nodes is None else nodes
    nodes[0] += 1
    if depth > _MAX_DEPTH or nodes[0] > _MAX_NODES:
        return "resource-bound"
    value_type = type(value)
    if value is None or value_type in {str, bool, int}:
        return "ok"
    if value_type is list:
        if len(value) > _MAX_CONTAINER:
            return "resource-bound"
        for item in list.__iter__(value):
            result = _plain(item, depth + 1, nodes)
            if result != "ok":
                return result
        return "ok"
    if value_type is dict:
        if len(value) > _MAX_CONTAINER:
            return "resource-bound"
        for key, item in dict.items(value):
            if type(key) is not str:
                return "response-contract"
            result = _plain(item, depth + 1, nodes)
            if result != "ok":
                return result
        return "ok"
    return "response-contract"


def _elapsed(clock: FunctionType | None, started: int | None) -> int | None:
    if clock is None or started is None:
        return None
    try:
        ended = clock()
    except BaseException:
        return None
    if type(ended) is not int or ended < started:
        return None
    return ended - started


def _started(clock: FunctionType | None) -> int | None:
    if clock is None:
        return None
    try:
        value = clock()
    except BaseException:
        return None
    return value if type(value) is int else None


def run_bounded_smoke(adapter: object, request: object) -> SmokeResult:
    """One claimed local read path. The adapter is a test double in this stage."""
    request, valid_target, confirmed, clock = _valid_request(request)
    confirmed = confirmed and valid_target
    if request is None or not confirmed:
        return _result("confirmation-required")
    if not _CLAIM.acquire(blocking=False):
        return _result("reentry", confirmed=True, phase="claimed", reentry=True)
    try:
        started = _started(clock)
        if started is None:
            return _result("phase-exception", confirmed=True, phase="claimed")
        if request["deadline_ms"] == 0:
            return _result("deadline", confirmed=True, phase="claimed", measured=True)
        if request.get("reenter") is True:
            return _result("reentry", confirmed=True, phase="claimed", reentry=True)
        reader = _read_only_method(adapter)
        if reader is None:
            return _result("adapter-rejected", confirmed=True, phase="claimed", surface=False)
        try:
            response = reader(adapter, request["target"])
        except KeyboardInterrupt:
            return _result("interrupted", confirmed=True, reads=1, phase="read", interrupted=True)
        except BaseException:
            return _result("phase-exception", confirmed=True, reads=1, phase="read")
        elapsed = _elapsed(clock, started)
        if elapsed is None:
            return _result("phase-exception", confirmed=True, reads=1, phase="read")
        if elapsed >= request["deadline_ms"]:
            return _result("deadline", confirmed=True, reads=1, phase="read", measured=True)
        plain = _plain(response)
        elapsed = _elapsed(clock, started)
        if elapsed is None:
            return _result("phase-exception", confirmed=True, reads=1, phase="validated")
        if elapsed >= request["deadline_ms"]:
            return _result("deadline", confirmed=True, reads=1, phase="validated", measured=True)
        if plain != "ok":
            limit = plain == "resource-bound"
            return _result(plain, confirmed=True, reads=1, phase="validated", measured=True,
                           limit=limit, parser_budget=limit)
        kind = response.get("kind")
        if kind == "unauthenticated":
            return _result("authentication", confirmed=True, reads=1, phase="read", measured=True)
        if kind == "forbidden":
            return _result("authorization", confirmed=True, reads=1, phase="read", measured=True, authn=True)
        if kind == "no-progress":
            return _result("resource-bound", confirmed=True, reads=1, phase="read", measured=True, limit=True)
        if kind != "ok":
            return _result("response-contract", confirmed=True, reads=1, phase="read", measured=True)
        target, items = response.get("target"), response.get("items")
        if type(target) is not str or target != request["target"]:
            return _result("response-contract", confirmed=True, reads=1, phase="validated", measured=True)
        if type(items) is not list or response.get("wrapper") == "ambiguous" or response.get("partial") is True:
            return _result("response-contract", confirmed=True, reads=1, phase="validated", measured=True, partial=response.get("partial") is True)
        if len(items) > request["sample_cap"] or response.get("next_cursor") is not None:
            return _result("resource-bound", confirmed=True, reads=1, phase="validated", measured=True, limit=True)
        elapsed = _elapsed(clock, started)
        if elapsed is None:
            return _result("phase-exception", confirmed=True, reads=1, phase="validated")
        if elapsed >= request["deadline_ms"]:
            return _result("deadline", confirmed=True, reads=1, phase="validated", measured=True)
        return _result(None, confirmed=True, reads=1, phase="validated", measured=True,
                       shape=True, identity=True, parser=True, authn=True, authz=True)
    finally:
        _CLAIM.release()
