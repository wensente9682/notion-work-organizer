"""Synthetic-only canonical action adapter for one fail-closed write attempt."""

from __future__ import annotations

import hashlib
import inspect
import json
import threading
from collections.abc import Mapping
from types import FunctionType, MappingProxyType

from new_system_write_coordinator import ReadFailure
from new_system_approval import PreviewInputError, canonical_action_bytes
from new_system_connector_entry_bridge import (
    ConnectorEntryBridge,
    ConnectorEntryOutcome,
    ConnectorEntryWireBridge,
)


_MAX_DEPTH = 4
_MAX_NODES = 64
_MAX_CONTAINER = 16
_MAX_INT_ABS = 1_000_000_000
_MAX_STRING_BYTES = 128
_MAX_TOTAL_BYTES = 256
_MAX_CANONICAL_BYTES = 768
_SCHEMAS = {
    "create_database": ({"parent"}, {"schema"}),
    "configure_property": ({"database", "property"}, {"type"}),
    "configure_view_sort": ({"database", "view"}, {"sort"}),
    "create_archive_container": ({"parent"}, {"title"}),
    "create_archive_target": ({"container", "category"}, {"display_name", "schema"}),
}


def _public(
    phase: str,
    failure: str | None = None,
    *,
    read_attempted: bool = False,
    write_attempted: bool = False,
) -> Mapping[str, object]:
    return MappingProxyType({
        "phase": phase,
        "failure_class": failure or "none",
        "evidence_schema": "closed",
        "read_attempted": read_attempted,
        "write_attempted": write_attempted,
        "read_calls": "at-most-one",
        "write_calls": "at-most-one",
    })


class _AdapterFailure(ReadFailure):
    def __init__(
        self,
        reason: str,
        *,
        phase: str = "claim",
        read_attempted: bool = False,
        write_attempted: bool = False,
    ):
        super().__init__(reason)
        object.__setattr__(
            self,
            "evidence",
            _public(phase, reason, read_attempted=read_attempted, write_attempted=write_attempted),
        )

    def to_public_dict(self) -> dict[str, object]:
        return dict(self.evidence)

    def __repr__(self) -> str:
        return "AdapterFailure(status='failed')"


class _AdapterRead(Mapping):
    __slots__ = ("_data", "evidence")

    def __init__(self, data: dict[str, object]):
        self._data = MappingProxyType(data)
        self.evidence = _public("read", read_attempted=True)

    def __getitem__(self, key: str) -> object:
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def to_public_dict(self) -> dict[str, object]:
        return dict(self.evidence)

    def __repr__(self) -> str:
        return "AdapterRead(status='read-completed')"


class _AdapterWrite:
    __slots__ = ("_attempted", "_outcome", "_entry")

    def __init__(self, attempted: bool, outcome: str, entry: str | None = None):
        self._attempted = attempted
        self._outcome = outcome
        self._entry = entry or ("invoked" if attempted else "not-invoked")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "status": "write-result",
            "connector_write_attempted": self._attempted,
            "connector_entry": self._entry,
            "outcome": self._outcome,
        }

    def __repr__(self) -> str:
        return "AdapterWrite(status='closed')"


def _plain(
    value: object,
    depth: int = 0,
    nodes: list[int] | None = None,
    byte_count: list[int] | None = None,
):
    nodes = [0] if nodes is None else nodes
    byte_count = [0] if byte_count is None else byte_count
    nodes[0] += 1
    if depth > _MAX_DEPTH or nodes[0] > _MAX_NODES:
        return "limit", None
    kind = type(value)
    if value is None or kind is bool:
        return "ok", value
    if kind is str:
        if not _charge_string(value, byte_count):
            return "limit", None
        return "ok", value
    if kind is int:
        return ("ok", value) if abs(value) <= _MAX_INT_ABS else ("limit", None)
    if kind is list:
        if len(value) > _MAX_CONTAINER:
            return "limit", None
        output = []
        for item in list.__iter__(value):
            status, child = _plain(item, depth + 1, nodes, byte_count)
            if status != "ok":
                return status, None
            output.append(child)
        return "ok", output
    if kind is dict:
        if len(value) > _MAX_CONTAINER:
            return "limit", None
        output: dict[str, object] = {}
        for key, item in dict.items(value):
            if type(key) is not str:
                return "invalid", None
            if not _charge_string(key, byte_count):
                return "limit", None
            status, child = _plain(item, depth + 1, nodes, byte_count)
            if status != "ok":
                return status, None
            output[key] = child
        return "ok", output
    return "invalid", None


def _charge_string(value: str, byte_count: list[int]) -> bool:
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        return False
    byte_count[0] += size
    return size <= _MAX_STRING_BYTES and byte_count[0] <= _MAX_TOTAL_BYTES


def _action(action: object):
    kind = dict.get(action, "kind") if type(action) is dict else None
    if type(action) is dict and type(kind) is not str:
        return None
    if kind == "create_archive_target":
        try:
            canonical = canonical_action_bytes(action)
        except (PreviewInputError, TypeError, ValueError, OverflowError):
            return None
        if len(canonical) > _MAX_CANONICAL_BYTES:
            return None
        canonical_value = json.loads(canonical)
        bounded_input = {
            "kind": canonical_value["kind"],
            "target": canonical_value["target"],
            "payload": {"display_name": canonical_value["payload"]["display_name"]},
        }
        status, _ = _plain(bounded_input)
        if status != "ok":
            return None
        return hashlib.sha256(canonical).hexdigest(), canonical_value
    status, value = _plain(action)
    if status != "ok" or type(value) is not dict or set(value) != {"kind", "target", "payload"}:
        return None
    kind, target, payload = value["kind"], value["target"], value["payload"]
    schema = _SCHEMAS.get(kind) if type(kind) is str else None
    if schema is None or type(target) is not dict or type(payload) is not dict:
        return None
    target_keys, payload_keys = schema
    if set(target) != target_keys or set(payload) != payload_keys:
        return None
    if not target or not payload or any(not isinstance(item, str) or not item.strip() for item in target.values()):
        return None
    if kind in {"create_database", "create_archive_target"}:
        if type(payload["schema"]) is not dict or not payload["schema"]:
            return None
        if kind == "create_archive_target" and (
            type(payload["display_name"]) is not str
            or not payload["display_name"].strip()
        ):
            return None
    elif any(type(item) is not str or not item.strip() for item in payload.values()):
        return None
    try:
        canonical = canonical_action_bytes(value)
    except (PreviewInputError, TypeError, ValueError, OverflowError):
        return None
    if len(canonical) > _MAX_CANONICAL_BYTES:
        return None
    return hashlib.sha256(canonical).hexdigest(), value


def _connector_request(action: dict[str, object]) -> dict[str, object]:
    if action["kind"] != "create_archive_target":
        return action
    return action["connector_projection"]["request"]


def _connector_method(connector: object, name: str) -> FunctionType | None:
    try:
        for cls in type.__getattribute__(type(connector), "__mro__"):
            method = type.__getattribute__(cls, "__dict__").get(name)
            if type(method) is FunctionType:
                return method
    except BaseException:
        return None
    return None


def _connector_write_signature(method: object) -> bool:
    if type(method) is not FunctionType:
        return False
    try:
        code = method.__code__
        forbidden = inspect.CO_VARARGS | inspect.CO_VARKEYWORDS | inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR
        return (
            code.co_argcount == 2
            and code.co_kwonlyargcount == 0
            and not (code.co_flags & forbidden)
        )
    except BaseException:
        return False


class CanonicalConnectorActionAdapter:
    """A small local adapter; its connector is a synthetic test double only."""

    def __init__(self, connector: object):
        self._connector = connector
        self._lock = threading.Lock()
        self._read_digest: str | None = None
        self._read_data: dict[str, object] | None = None
        self._read_claimed = False
        self._write_used = False
        self._write_inflight = False
        self._authority_revoked = False

    def __copy__(self):
        raise TypeError("adapter capability is not copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("adapter capability is not copyable")

    def __reduce_ex__(self, _protocol):
        raise TypeError("adapter capability is not serializable")

    def read_exact(self, action: object):
        with self._lock:
            checked = _action(action)
            if checked is None:
                if (self._read_claimed and not self._write_used) or self._write_inflight:
                    self._authority_revoked = True
                return _AdapterFailure("invalid-action")
            digest, action_value = checked
            request = _connector_request(action_value)
            if self._read_claimed:
                self._authority_revoked = True
                return _AdapterFailure("read-terminal")
            self._read_claimed = True
        method = _connector_method(self._connector, "read_canonical")
        if method is None:
            return _AdapterFailure("read-unavailable")
        try:
            response = method(self._connector, request)
        except BaseException:
            return _AdapterFailure("read-unavailable", phase="read", read_attempted=True)
        status, plain = _plain(response)
        if (
            status != "ok"
            or type(plain) is not dict
            or not plain
            or plain.get("kind") == "no-progress"
        ):
            return _AdapterFailure(
                "read-unusable" if status != "limit" else "read-limited",
                phase="read",
                read_attempted=True,
            )
        with self._lock:
            self._read_digest = digest
            self._read_data = plain
        return _AdapterRead(plain)

    def write_once(self, action: object):
        return self._write_once_evidenced(action)._outcome

    def _write_once_evidenced(
        self,
        action: object,
        *,
        attempt_fingerprint: object = "0" * 32,
    ) -> _AdapterWrite:
        with self._lock:
            checked = _action(action)
            if checked is None:
                if (self._read_claimed and not self._write_used) or self._write_inflight:
                    self._authority_revoked = True
                return _AdapterWrite(False, "unknown")
            digest, action_value = checked
            request = _connector_request(action_value)
            if self._write_inflight:
                self._authority_revoked = True
                return _AdapterWrite(False, "unknown")
            if self._write_used or self._authority_revoked or self._read_digest != digest or self._read_data is None:
                if self._read_claimed and not self._write_used:
                    self._authority_revoked = True
                return _AdapterWrite(False, "unknown")
            self._write_used = True
            self._write_inflight = True
        method = _connector_method(self._connector, "write_canonical")
        wire = type(self._connector) is ConnectorEntryWireBridge
        if method is None and not wire:
            with self._lock:
                self._write_inflight = False
            return _AdapterWrite(False, "unknown")
        if not wire and not _connector_write_signature(method):
            with self._lock:
                self._write_inflight = False
            return _AdapterWrite(False, "not-invoked")
        bridge = None
        try:
            if wire:
                evidence = ConnectorEntryWireBridge.execute(
                    self._connector, request, digest, attempt_fingerprint
                )
            else:
                bridge = ConnectorEntryBridge(digest, attempt_fingerprint)
                handoff = bridge.handoff(digest, attempt_fingerprint)
                acknowledgement = bridge.acknowledge(handoff)
                evidence = bridge.invoke(
                    acknowledgement,
                    lambda supplied: method(self._connector, supplied),
                    request,
                )
            attempted = type(evidence) is ConnectorEntryOutcome and evidence._attempted is True
            outcome = evidence._outcome if type(evidence) is ConnectorEntryOutcome else "unknown"
            entry = evidence._entry if type(evidence) is ConnectorEntryOutcome else "unknown"
        except BaseException:
            try:
                evidence = (
                    bridge.close() if type(bridge) is ConnectorEntryBridge
                    else ConnectorEntryWireBridge.close(self._connector) if wire
                    else None
                )
            except BaseException:
                evidence = None
            attempted = type(evidence) is ConnectorEntryOutcome and evidence._attempted is True
            outcome = "unknown" if attempted else "not-invoked"
            entry = "invoked" if attempted else "not-invoked"
        with self._lock:
            self._write_inflight = False
            if self._authority_revoked:
                return _AdapterWrite(attempted, "unknown" if attempted else "not-invoked", entry)
        if entry == "unknown":
            return _AdapterWrite(False, "unknown", "unknown")
        if not attempted:
            return _AdapterWrite(False, "not-invoked")
        closed = outcome if type(outcome) is str and outcome in {"success", "changed", "no-op", "partial", "ambiguous"} else "unknown"
        return _AdapterWrite(True, closed)
