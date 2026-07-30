"""Pure-local one-action preview and single-use approval for New System."""

from __future__ import annotations

import hashlib
import json
import math
import threading
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


_PLACEHOLDERS = {"TODO", "TBD", "PLACEHOLDER", "UNKNOWN", "?"}


class PreviewInputError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

    def __repr__(self) -> str:
        return f"PreviewInputError(code={self.code!r})"


def _is_placeholder(value: str) -> bool:
    return not value.strip() or value.strip().upper() in _PLACEHOLDERS


def _normalize(value: object, error_code: str) -> object:
    if value is None:
        raise PreviewInputError(error_code)
    if isinstance(value, str):
        if _is_placeholder(value):
            raise PreviewInputError(error_code)
        return value
    if isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PreviewInputError(error_code)
        return value
    if isinstance(value, Mapping):
        if not value or not all(isinstance(key, str) and key.strip() for key in value):
            raise PreviewInputError(error_code)
        return {key: _normalize(child, error_code) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        if not value:
            raise PreviewInputError(error_code)
        return [_normalize(child, error_code) for child in value]
    raise PreviewInputError(error_code)


def _canonical_bytes(value: object, error_code: str) -> bytes:
    try:
        return json.dumps(
            _normalize(value, error_code),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except PreviewInputError:
        raise
    except (TypeError, ValueError):
        raise PreviewInputError(error_code) from None


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


def _summary() -> dict[str, object]:
    return {
        "action_count": 1,
        "target_bound": True,
        "payload_bound": True,
    }


class GuidedAction:
    __slots__ = ("_canonical", "_digest")

    def __init__(self, canonical: bytes, digest: str):
        self._canonical = canonical
        self._digest = digest

    @classmethod
    def from_mapping(cls, action: object) -> GuidedAction:
        if not isinstance(action, Mapping) or set(action) != {"kind", "target", "payload"}:
            raise PreviewInputError("action.invalid")
        kind = action["kind"]
        target = action["target"]
        payload = action["payload"]
        if (
            not isinstance(kind, str)
            or _is_placeholder(kind)
            or not isinstance(target, (str, Mapping))
            or not target
            or not isinstance(payload, Mapping)
            or not payload
        ):
            raise PreviewInputError("action.invalid")
        canonical = _canonical_bytes(action, "action.not-canonical")
        return cls(canonical, _digest(canonical))

    @property
    def digest(self) -> str:
        return self._digest

    def review(self) -> object:
        return _freeze(json.loads(self._canonical))

    def to_public_dict(self) -> dict[str, object]:
        return {"digest": self._digest, **_summary()}

    def __repr__(self) -> str:
        return f"GuidedAction({self.to_public_dict()!r})"


class ActionPreview:
    __slots__ = (
        "_action",
        "_state_digest",
        "_binding_digest",
        "_trusted",
    )

    def __init__(
        self,
        action: GuidedAction,
        state_digest: str,
        binding_digest: str,
    ):
        self._action = action
        self._state_digest = state_digest
        self._binding_digest = binding_digest
        self._trusted = False

    @property
    def status(self) -> str:
        return "preview-ready"

    @property
    def action_digest(self) -> str:
        return self._action.digest

    @property
    def expected_state_digest(self) -> str:
        return self._state_digest

    @property
    def binding_digest(self) -> str:
        return self._binding_digest

    def review_action(self) -> object:
        return self._action.review()

    def to_public_dict(self) -> dict[str, object]:
        summary = self._action.to_public_dict()
        summary.pop("digest")
        return {
            "status": self.status,
            "action_digest": self.action_digest,
            "expected_state_digest": self.expected_state_digest,
            "binding_digest": self.binding_digest,
            **summary,
        }

    def __repr__(self) -> str:
        return f"ActionPreview({self.to_public_dict()!r})"


def preview_next_action(action: object, expected_state: object) -> ActionPreview:
    guided_action = GuidedAction.from_mapping(action)
    if not isinstance(expected_state, Mapping) or not expected_state:
        raise PreviewInputError("expected-state.invalid")
    state_digest = _digest(_canonical_bytes(expected_state, "expected-state.invalid"))
    binding = _digest(f"{guided_action.digest}:{state_digest}".encode("ascii"))
    return ActionPreview(guided_action, state_digest, binding)


@dataclass(frozen=True, eq=False)
class ApprovalEnvelope:
    """Opaque, in-memory capability; no bearer material is public."""

    def __copy__(self) -> ApprovalEnvelope:
        return self

    def __deepcopy__(self, memo: object) -> ApprovalEnvelope:
        return self

    def to_public_dict(self) -> dict[str, str]:
        return {"status": "approval-issued"}

    def __repr__(self) -> str:
        return "ApprovalEnvelope(status='approval-issued')"


@dataclass(frozen=True)
class AuthorizationDecision:
    status: str
    code: str
    authorized: bool

    def to_public_dict(self) -> dict[str, object]:
        return {"status": self.status, "code": self.code, "authorized": self.authorized}


class ApprovalLedger:
    def __init__(self) -> None:
        self._previews: dict[ActionPreview, tuple[str, str, str, bytes, bool]] = {}
        self._approvals: dict[ApprovalEnvelope, tuple[str, bool]] = {}
        self._lock = threading.Lock()

    def preview(self, action: object, expected_state: object) -> ActionPreview:
        preview = preview_next_action(action, expected_state)
        with self._lock:
            preview._trusted = True
            self._previews[preview] = (
                preview.binding_digest,
                preview.action_digest,
                preview.expected_state_digest,
                preview._action._canonical,
                False,
            )
        return preview

    def issue(self, preview: object, *, accepted: bool) -> ApprovalEnvelope | None:
        if not isinstance(preview, ActionPreview):
            return None
        with self._lock:
            record = self._previews.get(preview)
            if (
                record is None
                or not preview._trusted
                or record[4]
                or record[0] != preview.binding_digest
                or record[1] != preview.action_digest
                or record[2] != preview.expected_state_digest
                or record[3] != preview._action._canonical
            ):
                return None
            self._previews[preview] = (*record[:4], True)
            if accepted is not True:
                return None
            envelope = ApprovalEnvelope()
            self._approvals[envelope] = (record[0], False)
            return envelope

    def consume(
        self,
        envelope: object,
        action: object,
        expected_state: object,
        *,
        interrupted: bool = False,
    ) -> AuthorizationDecision:
        if not isinstance(envelope, ApprovalEnvelope):
            return AuthorizationDecision("denied", "approval.required", False)
        with self._lock:
            record = self._approvals.get(envelope)
            if record is None:
                return AuthorizationDecision("denied", "approval.unknown", False)
            binding_digest, consumed = record
            if consumed:
                return AuthorizationDecision("denied", "approval.consumed", False)
            self._approvals[envelope] = (binding_digest, True)
        if interrupted:
            return AuthorizationDecision("denied", "approval.interrupted", False)
        try:
            attempted = preview_next_action(action, expected_state).binding_digest
        except (PreviewInputError, TypeError, ValueError):
            attempted = None
        if attempted != binding_digest:
            return AuthorizationDecision("denied", "approval.binding-mismatch", False)
        return AuthorizationDecision("authorized", "approval.authorized", True)

    def __repr__(self) -> str:
        return "ApprovalLedger()"
