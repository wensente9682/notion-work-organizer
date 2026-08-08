"""Opaque, single-use acknowledgement for one connector call boundary."""

from __future__ import annotations

import threading
from types import FunctionType, MappingProxyType


_OUTCOMES = {"success", "changed", "no-op", "partial", "ambiguous"}


class _Handoff:
    __slots__ = ("_owner", "_nonce")

    def __init__(self, owner: object, nonce: object):
        self._owner = owner
        self._nonce = nonce


class _Acknowledgement:
    __slots__ = ("_owner", "_nonce")

    def __init__(self, owner: object, nonce: object):
        self._owner = owner
        self._nonce = nonce


class ConnectorEntryOutcome:
    __slots__ = ("_public", "_attempted", "_entry", "_outcome")

    def __init__(self, attempted: bool, outcome: str, *, entry: str | None = None):
        self._attempted = attempted
        self._outcome = outcome
        entry_value = entry or ("invoked" if attempted else "not-invoked")
        self._entry = entry_value
        self._public = MappingProxyType({
            "status": "closed",
            "entry": entry_value,
            "attempted": attempted,
            "outcome": outcome,
        })

    def to_public_dict(self) -> dict[str, object]:
        return dict(self._public)

    def __repr__(self) -> str:
        return "ConnectorEntryOutcome(status='closed')"


def _hex(value: object, size: int) -> bool:
    return type(value) is str and len(value) == size and all(
        char in "0123456789abcdef" for char in value
    )


class ConnectorEntryBridge:
    """One lifecycle: handoff -> acknowledgement -> connector boundary -> outcome.

    Acknowledgement alone never means that the connector was invoked.  The bridge
    records entry immediately before dispatching the connector callable.
    """

    __slots__ = (
        "_digest", "_attempt", "_owner", "_nonce", "_lock", "_state",
        "_revoked", "_attempted", "_outcome",
    )

    def __init__(self, action_digest: object, attempt_fingerprint: object):
        if not _hex(action_digest, 64) or not _hex(attempt_fingerprint, 32):
            raise ValueError("invalid binding")
        self._digest = action_digest
        self._attempt = attempt_fingerprint
        self._owner = object()
        self._nonce = object()
        self._lock = threading.Lock()
        self._state = "new"
        self._revoked = False
        self._attempted = False
        self._outcome = "not-invoked"

    def __copy__(self):
        raise TypeError("bridge capability is not copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("bridge capability is not copyable")

    def __reduce_ex__(self, _protocol):
        raise TypeError("bridge capability is not serializable")

    def handoff(self, action_digest: object, attempt_fingerprint: object):
        with self._lock:
            if (
                self._state != "new"
                or self._revoked
                or type(action_digest) is not str
                or type(attempt_fingerprint) is not str
                or action_digest != self._digest
                or attempt_fingerprint != self._attempt
            ):
                self._revoked = True
                return None
            self._state = "handed-off"
            return _Handoff(self._owner, self._nonce)

    def acknowledge(self, handoff: object):
        with self._lock:
            if (
                self._state != "handed-off"
                or self._revoked
                or type(handoff) is not _Handoff
                or handoff._owner is not self._owner
                or handoff._nonce is not self._nonce
            ):
                self._revoked = True
                return None
            self._state = "acknowledged"
            return _Acknowledgement(self._owner, self._nonce)

    def invoke(self, acknowledgement: object, method: object, request: object):
        with self._lock:
            if (
                self._state != "acknowledged"
                or self._revoked
                or type(acknowledgement) is not _Acknowledgement
                or acknowledgement._owner is not self._owner
                or acknowledgement._nonce is not self._nonce
                or type(method) is not FunctionType
            ):
                self._revoked = True
                return None
            self._state = "entered"
            self._attempted = True
        try:
            outcome = method(request)
        except BaseException:
            outcome = "unknown"
        with self._lock:
            if self._state != "entered":
                outcome = "unknown"
            self._state = "closed"
            self._outcome = (
                outcome if type(outcome) is str and outcome in _OUTCOMES else "unknown"
            )
            return ConnectorEntryOutcome(True, self._outcome)

    def close(self) -> ConnectorEntryOutcome:
        with self._lock:
            if self._state != "closed":
                self._state = "closed"
            if not self._attempted:
                self._outcome = "not-invoked"
            elif self._outcome == "not-invoked":
                self._outcome = "unknown"
            return ConnectorEntryOutcome(self._attempted, self._outcome)


class ConnectorEntryWireBridge:
    """Fail-closed marker for the unsupported cross-isolate transport.

    A byte stream cannot prove that a Codex connector tool was entered.  Until
    the host supplies a trusted tool-entry receipt, no transcript is consumed
    and this boundary can never report an attempted or successful write.
    """

    __slots__ = ("_read_response", "_lock", "_used")

    def __init__(self, input_stream: object, output_stream: object, read_response: object = None):
        del input_stream, output_stream
        self._read_response = read_response
        self._lock = threading.Lock()
        self._used = False

    def read_canonical(self, _request: object):
        return self._read_response

    def __copy__(self):
        raise TypeError("wire bridge is not copyable")

    def __deepcopy__(self, _memo):
        raise TypeError("wire bridge is not copyable")

    def __reduce_ex__(self, _protocol):
        raise TypeError("wire bridge is not serializable")

    def execute(
        self,
        request: object,
        action_digest: object,
        attempt_fingerprint: object,
    ) -> ConnectorEntryOutcome:
        del request, action_digest, attempt_fingerprint
        with self._lock:
            if self._used:
                return ConnectorEntryOutcome(False, "bridge-unsupported", entry="unknown")
            self._used = True
        return ConnectorEntryOutcome(False, "bridge-unsupported", entry="unknown")

    def close(self) -> ConnectorEntryOutcome:
        return ConnectorEntryOutcome(False, "bridge-unsupported", entry="unknown")
