"""Initial, connector-free New System setup action."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import html
import json
import re
import threading
from types import MappingProxyType
from pathlib import Path

from new_system_approval import (
    ApprovalLedger, AttemptClaim, AuthorizationDecision, _ClaimDecision,
    canonical_action_bytes, canonical_expected_state_bytes,
)
from new_system_write_coordinator import consume_source_execution
from new_system_recovery_journal import RecoveryJournal
from new_system_blueprint import ARCHIVE_PROPERTIES, SOURCE_PROPERTIES, canonical_blueprint, validate_canonical_blueprint
from new_system_finalizer import NewSystemFinalizer


_FIELDS = (
    ("Task", "TITLE"),
    ("Done", "CHECKBOX"),
    ("Takeaway", "RICH_TEXT"),
    ("Improvement", "RICH_TEXT"),
    ("Work Date", "DATE"),
    ("Time Blocks", "RICH_TEXT"),
)
_ARCHIVE_SCHEMA = {"Task": "title", "Takeaway": "rich_text", "Improvement": "rich_text"}


@dataclass(frozen=True, init=False)
class InitialSetupState:
    target_page_id: str
    categories: tuple[str, ...]
    phase: str = "pre-source"
    source_database_id: str | None = None
    source_data_source_id: str | None = None
    action_digest: str | None = None
    result_status: str | None = None
    archives: tuple[tuple[str, str, str], ...] = ()
    profile_content: bytes | None = None

    def __init__(self, *args, **kwargs):
        raise TypeError("setup state is backend-owned")

    def __copy__(self):
        raise TypeError("setup state is not copyable")

    def __deepcopy__(self, memo):
        raise TypeError("setup state is not copyable")

    def __reduce_ex__(self, protocol):
        raise TypeError("setup state is not serializable")

    @classmethod
    def _new(cls, target_page_id, categories, phase="pre-source", source_database_id=None, source_data_source_id=None, action_digest=None, result_status=None, archives=(), profile_content=None):
        value = object.__new__(cls)
        for name, item in (("target_page_id", target_page_id), ("categories", categories), ("phase", phase), ("source_database_id", source_database_id), ("source_data_source_id", source_data_source_id), ("action_digest", action_digest), ("result_status", result_status), ("archives", archives), ("profile_content", profile_content)):
            object.__setattr__(value, name, item)
        return value


def begin(target_page_id: object, categories: object) -> InitialSetupState:
    if type(target_page_id) is not str or not target_page_id.strip() or type(categories) not in {list, tuple}:
        raise ValueError("target page and confirmed categories are required")
    values = tuple(item.strip() for item in categories if type(item) is str and item.strip())
    if not values or len(values) != len(categories) or len(set(values)) != len(values):
        raise ValueError("categories must be unique non-empty text")
    return InitialSetupState._new(target_page_id.strip(), values)


def from_verified_source(
    target_page_id: object, categories: object, source_database_id: object, source_data_source_id: object,
) -> InitialSetupState:
    initial = begin(target_page_id, categories)
    source_database_key = _identity_key(source_database_id)
    source_data_source_key = _identity_key(source_data_source_id)
    target_key = _identity_key(target_page_id)
    if (
        not _valid_database_id(source_database_id) or not _valid_data_source_id(source_data_source_id)
        or source_database_key == source_data_source_key
        or target_key is not None and target_key in {source_database_key, source_data_source_key}
    ):
        raise ValueError("verified source identities are required")
    return InitialSetupState._new(initial.target_page_id, initial.categories, "source-created", source_database_id, source_data_source_id)


def _schema(categories: tuple[str, ...]) -> str:
    options = ", ".join("'" + value.replace("'", "''") + "'" for value in categories)
    fields = [f'"{name}" {kind}' for name, kind in _FIELDS]
    fields.insert(2, f'"Category" SELECT({options})')
    return "CREATE TABLE (" + ", ".join(fields) + ")"


def next_action(state: object) -> dict[str, object] | None:
    if type(state) is not InitialSetupState:
        return None
    if state.phase == "pre-source":
        return {
        "kind": "create_database",
        "target": {"page_id": state.target_page_id},
        "payload": {
            "request": {
                "parent": {"page_id": state.target_page_id},
                "title": "Daily Work",
                "schema": _schema(state.categories),
            }
        },
        }
    if state.phase != "source-created" or len(state.archives) >= len(state.categories):
        return None
    category = state.categories[len(state.archives)]
    archive_action = {
        "kind": "create_archive_target",
        "target": {"container": state.target_page_id, "category": category},
        "payload": {"display_name": category, "schema": dict(_ARCHIVE_SCHEMA)},
    }
    return json.loads(canonical_action_bytes(archive_action))


def preview(state: object, action: object) -> str:
    if type(state) is not InitialSetupState or action != next_action(state):
        raise ValueError("exact action required")
    if action["kind"] == "create_archive_target":
        category = action["target"]["category"]
        return f"Create the “{category}” archive database in the confirmed dedicated page.\nFields: Task, Takeaway, Improvement."
    try:
        request = action["payload"]["request"]
        parent = action["target"]["page_id"]
        schema = request["schema"]
        if action["kind"] != "create_database" or request["parent"] != {"page_id": parent} or request["title"] != "Daily Work" or type(schema) is not str:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError("exact action required") from None
    return (
        "Create the “Daily Work” database in the confirmed blank/dedicated page.\n"
        "Fields: Task, Done, Category, Takeaway, Improvement, Work Date, Time Blocks.\n"
        f"Current Category options: {', '.join(state.categories)}."
    )


def _digest(action: object) -> str:
    return hashlib.sha256(canonical_action_bytes(action)).hexdigest()


def expected_state(state: object, action: object) -> dict[str, object]:
    if type(state) is not InitialSetupState or action != next_action(state):
        raise ValueError("exact action required")
    if action["kind"] == "create_archive_target":
        observed = {
            "parent": state.target_page_id,
            "source": {"database_id": state.source_database_id, "data_source_id": state.source_data_source_id},
            "archive_count": len(state.archives),
            "next_category": action["target"]["category"],
        }
        if state.archives:
            observed["archives"] = [{"category": category, "database_id": database_id, "data_source_id": data_source_id} for category, database_id, data_source_id in state.archives]
        return observed
    return {"blank": True, "parent": state.target_page_id}


def _build_source_dispatch_channel():
    lock = threading.Lock()
    registry: dict[object, bytes] = {}
    request_claimed: set[object] = set()
    ingested: set[object] = set()

    class SourceDispatch:
        __slots__ = ("__token",)

        def __init__(self):
            object.__setattr__(self, "_SourceDispatch__token", object())

        def __setattr__(self, name: str, value: object) -> None:
            raise AttributeError("immutable source dispatch")

        def __copy__(self):
            raise TypeError("source dispatch is not copyable")

        def __deepcopy__(self, memo):
            raise TypeError("source dispatch is not copyable")

        def __reduce_ex__(self, protocol):
            raise TypeError("source dispatch is not serializable")

    def frozen(value: object) -> object:
        if type(value) is dict:
            return MappingProxyType({key: frozen(child) for key, child in value.items()})
        if type(value) is list:
            return tuple(frozen(child) for child in value)
        return value

    def prepare(ledger: object, envelope: object, state: object, action: object, observed: object):
        if type(ledger) is not ApprovalLedger or type(state) is not InitialSetupState or state.phase != "pre-source" or action != next_action(state):
            return None
        expected = expected_state(state, action)
        claimed = ledger.claim_for_write(envelope)
        if (
            type(claimed) is not _ClaimDecision
            or type(claimed.claim) is not AttemptClaim
            or type(claimed.action_digest) is not str
            or type(claimed.attempt_fingerprint) is not str
            or type(claimed.action_canonical) is not bytes
            or type(claimed.expected_state_canonical) is not bytes
        ):
            return None
        try:
            matches = (
                claimed.action_canonical == canonical_action_bytes(action)
                and claimed.expected_state_canonical == canonical_expected_state_bytes(expected)
                and observed == expected
            )
        except (TypeError, ValueError, UnicodeError):
            matches = False
        decision = ledger.finalize_write_attempt(
            claimed.claim, action, observed if matches else expected, interrupted=not matches
        )
        if type(decision) is not AuthorizationDecision or not matches or decision.authorized is not True:
            return None
        payload = {
            "action": action,
            "expected_state": expected,
            "action_digest": claimed.action_digest,
            "attempt_fingerprint": claimed.attempt_fingerprint,
            "request": action["payload"]["request"],
        }
        try:
            snapshot = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError):
            return None
        dispatch = SourceDispatch()
        with lock:
            registry[dispatch] = snapshot
        return dispatch

    def request(dispatch: object):
        with lock:
            if type(dispatch) is not SourceDispatch or dispatch in request_claimed or dispatch in ingested:
                return None
            snapshot = registry.get(dispatch)
            if snapshot is None:
                return None
            request_claimed.add(dispatch)
        try:
            payload = json.loads(snapshot)
            request_value = payload["request"]
        except (TypeError, ValueError, KeyError):
            return None
        return frozen(request_value)

    def identifiers(dispatch: object):
        with lock:
            if type(dispatch) is not SourceDispatch or dispatch in ingested:
                return None
            snapshot = registry.get(dispatch)
        try:
            payload = json.loads(snapshot)
            digest, attempt = payload["action_digest"], payload["attempt_fingerprint"]
        except (TypeError, ValueError, KeyError):
            return None
        return (digest, attempt) if type(digest) is str and type(attempt) is str else None

    def ingest(state: object, action: object, dispatch: object, raw_result: object):
        if type(state) is not InitialSetupState:
            return InitialSetupState._new("invalid", ("invalid",), "write-unresolved")
        if state.phase != "pre-source" or action != next_action(state) or type(dispatch) is not SourceDispatch:
            return InitialSetupState._new(state.target_page_id, state.categories, "write-unresolved")
        with lock:
            snapshot = registry.get(dispatch)
            if snapshot is None or dispatch not in request_claimed or dispatch in ingested:
                return InitialSetupState._new(state.target_page_id, state.categories, "write-unresolved")
            try:
                payload = json.loads(snapshot)
                digest = _digest(action)
                valid = (
                    payload.get("action") == action
                    and payload.get("expected_state") == expected_state(state, action)
                    and payload.get("action_digest") == digest
                    and type(payload.get("attempt_fingerprint")) is str
                    and payload["request"] == action["payload"]["request"]
                )
            except (TypeError, ValueError, KeyError, UnicodeError):
                valid = False
                digest = _digest(action)
            if not valid:
                return InitialSetupState._new(state.target_page_id, state.categories, "write-unresolved", action_digest=digest, result_status="unresolved")
            ingested.add(dispatch)
        if type(raw_result) is not dict:
            status = "unknown"
        else:
            status = raw_result.get("status")
        if status == "success":
            ids = (raw_result.get("database_id"), raw_result.get("data_source_id")) if type(raw_result) is dict else ()
            invalid = {"TODO", "TBD", "UNKNOWN", "?"}
            if (
                len(ids) != 2 or any(type(value) is not str or not value or value != value.strip() or value.upper() in invalid for value in ids)
                or ids[0] == ids[1]
            ):
                status = "unresolved"
            else:
                return InitialSetupState._new(state.target_page_id, state.categories, "source-created", ids[0], ids[1], digest, "completed")
        if status in {"changed", "no-op", "failure", "failed"}:
            return InitialSetupState._new(state.target_page_id, state.categories, "write-failed", action_digest=digest, result_status="write-failed")
        return InitialSetupState._new(state.target_page_id, state.categories, "write-unresolved", action_digest=digest, result_status="unresolved")

    return prepare, request, identifiers, ingest


prepare_source_dispatch, source_dispatch_request, _source_dispatch_identifiers, ingest_source_dispatch = _build_source_dispatch_channel()
del _build_source_dispatch_channel


@dataclass(frozen=True)
class JournalSourceDispatch:
    request_json: str
    action_digest: str
    attempt_fingerprint: str


def _journal_facts(digest: str, attempt: str, phase: str) -> dict[str, object]:
    return {
        "action_digest": digest,
        "attempt_fingerprint": attempt,
        "read_attempted": phase in {"read-completed", "write-ready"},
        "write_attempted": False,
        "write_started": False,
    }


_NOTION_PAGE_ID = re.compile(r"^[0-9a-fA-F]{32}$")
_NOTION_UUID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
_DATABASE_BLOCK = re.compile(
    r'<database\s+url="\{\{https://app\.notion\.com/p/([0-9a-fA-F]{32})\}\}"[^>]*>(.{0,4096}?)</database>',
    re.DOTALL,
)
_DATABASE_URL = re.compile(r'<database\s+url="\{\{https://app\.notion\.com/p/([0-9a-fA-F]{32})\}\}"')
_DATABASE_TOKEN = re.compile(r'\{\{https://app\.notion\.com/p/([0-9a-fA-F]{32})\}\}')
_PARENT_URL = re.compile(r'<parent-page\s+url="https://app\.notion\.com/p/([0-9a-fA-F]{32})"')
_DATA_SOURCE_URL = re.compile(r'<data-source\s+url="\{\{collection://([0-9a-fA-F-]{36})\}\}"')
_DATA_SOURCE_TOKEN = re.compile(r'\{\{collection://([0-9a-fA-F-]{36})\}\}')


def _valid_database_id(value: object) -> bool:
    return _identity_key(value) is not None


def _valid_data_source_id(value: object) -> bool:
    return _identity_key(value) is not None


def _identity_key(value: object) -> str | None:
    if type(value) is not str:
        return None
    compact = value.replace("-", "").lower()
    return compact if _NOTION_PAGE_ID.fullmatch(compact) is not None else None


def _identities_are_unique(state: InitialSetupState, database_id: object, data_source_id: object, *, include_source: bool) -> bool:
    database_key = _identity_key(database_id)
    data_source_key = _identity_key(data_source_id)
    if database_key is None or data_source_key is None or database_key == data_source_key:
        return False
    used = set()
    target_key = _identity_key(state.target_page_id)
    if target_key is not None:
        used.add(target_key)
    if include_source:
        used.update({_identity_key(state.source_database_id), _identity_key(state.source_data_source_id)})
    for _, existing_database, existing_source in state.archives:
        used.update({_identity_key(existing_database), _identity_key(existing_source)})
    used.discard(None)
    return database_key not in used and data_source_key not in used



def _normalize_created_result(raw_result: object, state: InitialSetupState, title: str, *, allow_structured: bool = False) -> tuple[str, tuple[str, str] | None]:
    if type(raw_result) is dict and type(raw_result.get("status")) is str:
        if allow_structured and raw_result["status"] == "success" and set(raw_result) == {"status", "database_id", "data_source_id"}:
            return "success", (raw_result["database_id"], raw_result["data_source_id"])
        if raw_result["status"] != "success" and set(raw_result) == {"status"}:
            return raw_result["status"], None
        return "unknown", None
    if type(raw_result) is not dict or set(raw_result) != {"result"} or type(raw_result["result"]) is not str:
        return "unknown", None
    text = raw_result["result"]
    if len(text) > 8192:
        return "unknown", None
    text = html.unescape(text)
    if (
        not text.startswith("Created database:") or text.count("<database") != 1
        or text.count("</database>") != 1 or len(_PARENT_URL.findall(text)) != 1
    ):
        return "unknown", None
    blocks = _DATABASE_BLOCK.findall(text)
    if len(blocks) != 1:
        return "unknown", None
    database, block = blocks[0]
    if re.search(r"The title of this Database is:\s*" + re.escape(title) + r"(?=\s|[.<])", block) is None:
        return "unknown", None
    databases, sources = _DATABASE_TOKEN.findall(text), _DATA_SOURCE_TOKEN.findall(text)
    parents, block_sources = _PARENT_URL.findall(block), _DATA_SOURCE_URL.findall(block)
    expected_parent = state.target_page_id.replace("-", "").lower()
    if (
        not _NOTION_PAGE_ID.fullmatch(expected_parent)
        or len(databases) != 1 or len(sources) != 1 or len(parents) != 1 or len(block_sources) != 1
        or databases[0].lower() != database.lower() or sources[0].lower() != block_sources[0].lower()
        or parents[0].lower() != expected_parent or not _NOTION_UUID.fullmatch(sources[0])
    ):
        return "unknown", None
    return "success", (database.lower(), sources[0].lower())


def _normalize_source_create_result(raw_result: object, state: InitialSetupState) -> tuple[str, tuple[str, str] | None]:
    return _normalize_created_result(raw_result, state, "Daily Work", allow_structured=True)


def prepare_journal_source_dispatch(
    ledger: object, envelope: object, state: object, action: object,
    observed: object, journal: object,
) -> JournalSourceDispatch | None:
    if type(journal) is not RecoveryJournal:
        return None
    dispatch = prepare_source_dispatch(ledger, envelope, state, action, observed)
    identifiers = _source_dispatch_identifiers(dispatch)
    if identifiers is None:
        return None
    digest, attempt = identifiers
    for phase in ("prepared", "read-completed", "write-ready"):
        result = journal.transition(phase, _journal_facts(digest, attempt, phase)).to_public_dict()
        if type(result) is not dict or result.get("status") != "accepted":
            return None
    request = source_dispatch_request(dispatch)
    if request is None:
        return None
    try:
        request_json = json.dumps(action["payload"]["request"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError, UnicodeError):
        return None
    return JournalSourceDispatch(request_json, digest, attempt)


def ingest_journal_source_dispatch(
    state: object, action: object, journal: object, action_digest: object,
    attempt_fingerprint: object, raw_result: object,
) -> InitialSetupState:
    if type(state) is not InitialSetupState:
        return InitialSetupState._new("invalid", ("invalid",), "write-unresolved")
    if (
        state.phase != "pre-source" or action != next_action(state)
        or type(journal) is not RecoveryJournal or action_digest != _digest(action)
        or type(attempt_fingerprint) is not str
    ):
        return InitialSetupState._new(state.target_page_id, state.categories, "write-unresolved")
    status, identities = _normalize_source_create_result(raw_result, state)
    terminal = "outcome-unknown"
    result_phase = "write-unresolved"
    if status == "success":
        invalid = {"TODO", "TBD", "UNKNOWN", "?"}
        if (
            all(type(value) is str and value and value == value.strip() and value.upper() not in invalid for value in identities)
            and identities[0] != identities[1]
        ):
            terminal, result_phase = "confirmed-applied", "source-created"
    elif status in {"changed", "no-op", "failure", "failed"}:
        terminal, result_phase = "confirmed-not-applied", "write-failed"
    elif status == "partial":
        terminal = "possible-partial"
    result = journal.consume_source_dispatch(action_digest, attempt_fingerprint, terminal).to_public_dict()
    if type(result) is not dict or result.get("status") != "accepted":
        return InitialSetupState._new(state.target_page_id, state.categories, "write-unresolved")
    if result_phase == "source-created":
        return InitialSetupState._new(state.target_page_id, state.categories, "source-created", identities[0], identities[1], action_digest, "completed")
    if result_phase == "write-failed":
        return InitialSetupState._new(state.target_page_id, state.categories, "write-failed", action_digest=action_digest, result_status="write-failed")
    return InitialSetupState._new(state.target_page_id, state.categories, "write-unresolved", action_digest=action_digest, result_status="unresolved")


def prepare_journal_archive_dispatch(
    ledger: object, envelope: object, state: object, action: object,
    observed: object, journal: object,
) -> JournalSourceDispatch | None:
    if (
        type(ledger) is not ApprovalLedger or type(journal) is not RecoveryJournal
        or type(state) is not InitialSetupState or state.phase != "source-created"
        or action != next_action(state)
    ):
        return None
    expected = expected_state(state, action)
    claimed = ledger.claim_for_write(envelope)
    if (
        type(claimed) is not _ClaimDecision or type(claimed.claim) is not AttemptClaim
        or type(claimed.action_digest) is not str or type(claimed.attempt_fingerprint) is not str
        or claimed.action_canonical != canonical_action_bytes(action)
        or claimed.expected_state_canonical != canonical_expected_state_bytes(expected)
        or observed != expected
    ):
        return None
    decision = ledger.finalize_write_attempt(claimed.claim, action, observed)
    if type(decision) is not AuthorizationDecision or decision.authorized is not True:
        return None
    for phase in ("prepared", "read-completed", "write-ready"):
        result = journal.transition(phase, _journal_facts(claimed.action_digest, claimed.attempt_fingerprint, phase)).to_public_dict()
        if type(result) is not dict or result.get("status") != "accepted":
            return None
    try:
        request_json = json.dumps(json.loads(canonical_action_bytes(action))["connector_projection"]["request"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (KeyError, TypeError, ValueError, UnicodeError):
        return None
    return JournalSourceDispatch(request_json, claimed.action_digest, claimed.attempt_fingerprint)


def profile_for(state: object) -> dict[str, object] | None:
    if (
        type(state) is not InitialSetupState or state.phase != "setup-complete"
        or len(state.archives) != len(state.categories) or type(state.profile_content) is not bytes
    ):
        return None
    try:
        profile = json.loads(state.profile_content)
        return profile if profile_is_valid(profile) else None
    except (TypeError, ValueError, UnicodeError):
        return None


def _generate_profile(state: InitialSetupState, archives: tuple[tuple[str, str, str], ...]) -> bytes | None:
    archive_ids = {category: database_id for category, database_id, _ in archives}
    blueprint = canonical_blueprint(archive_container_id=state.target_page_id, archive_databases=archive_ids)
    blueprint["source"]["id"] = state.source_database_id
    blueprint["view"]["id"] = state.source_data_source_id
    result = NewSystemFinalizer(
        Path(".todo_archive") / "new_system_profile.json", ignore_checker=lambda _: True,
    ).generate_profile(blueprint)
    return result.content if result.status == "ready" and type(result.content) is bytes else None


def profile_is_valid(profile: object) -> bool:
    if type(profile) is not dict:
        return False
    try:
        source, view, container, archives = profile["source"], profile["view"], profile["archive_container"], profile["archives"]
        blueprint = {
            "source": {"id": source["id"], "properties": dict(source["properties"]), "category_options": source["categories"]},
            "view": {"id": view["id"], "configured": True, "sort": dict(view["sort"])},
            "archive_container": {"id": container["id"], "approved": True},
            "archives": {category: {"id": archive["id"], "parent_id": archive["parent_id"], "category": archive["category"], "properties": dict(archive["properties"])} for category, archive in archives.items()},
        }
        return validate_canonical_blueprint(blueprint).status == "ready"
    except (KeyError, TypeError, ValueError):
        return False


def ingest_journal_archive_dispatch(
    state: object, action: object, journal: object, action_digest: object,
    attempt_fingerprint: object, raw_result: object,
) -> InitialSetupState:
    if (
        type(state) is not InitialSetupState or state.phase != "source-created"
        or action != next_action(state) or type(journal) is not RecoveryJournal
        or action_digest != _digest(action) or type(attempt_fingerprint) is not str
    ):
        return InitialSetupState._new(state.target_page_id, state.categories, "write-unresolved", state.source_database_id, state.source_data_source_id, archives=state.archives, profile_content=state.profile_content) if type(state) is InitialSetupState else InitialSetupState._new("invalid", ("invalid",), "write-unresolved")
    category = action["target"]["category"]
    status, identities = _normalize_created_result(raw_result, state, category)
    terminal, phase = "outcome-unknown", "write-unresolved"
    if status == "success" and identities is not None:
        database_id, data_source_id = identities
        if _identities_are_unique(state, database_id, data_source_id, include_source=True):
            terminal, phase = "confirmed-applied", "source-created"
    elif status in {"changed", "no-op", "failure", "failed"}:
        terminal, phase = "confirmed-not-applied", "write-failed"
    elif status == "partial":
        terminal = "possible-partial"
    result = journal.consume_source_dispatch(action_digest, attempt_fingerprint, terminal).to_public_dict()
    if type(result) is not dict or result.get("status") != "accepted":
        return InitialSetupState._new(state.target_page_id, state.categories, "write-unresolved", state.source_database_id, state.source_data_source_id, archives=state.archives, profile_content=state.profile_content)
    if phase != "source-created":
        return InitialSetupState._new(state.target_page_id, state.categories, phase, state.source_database_id, state.source_data_source_id, action_digest, phase, state.archives, state.profile_content)
    updated = state.archives + ((category, database_id, data_source_id),)
    completed = len(updated) == len(state.categories)
    return InitialSetupState._new(
        state.target_page_id, state.categories, "archives-created" if completed else "source-created",
        state.source_database_id, state.source_data_source_id, action_digest, "completed", updated, None,
    )


def ingest_execution(state: object, action: object, execution: object) -> InitialSetupState:
    if type(state) is not InitialSetupState:
        return InitialSetupState._new("invalid", ("invalid",), "write-unresolved")
    if state.phase != "pre-source" or action != next_action(state):
        return InitialSetupState._new(state.target_page_id, state.categories, "write-unresolved")
    digest = _digest(action)
    claim = consume_source_execution(execution, digest)
    if claim is None or not claim:
        return InitialSetupState._new(state.target_page_id, state.categories, "write-unresolved", action_digest=digest, result_status="unresolved")
    if claim[0] == "source-created":
        return InitialSetupState._new(state.target_page_id, state.categories, "source-created", claim[1], claim[2], digest, "completed")
    if claim[0] == "write-failed":
        return InitialSetupState._new(state.target_page_id, state.categories, "write-failed", action_digest=digest, result_status="write-failed")
    return InitialSetupState._new(state.target_page_id, state.categories, "write-unresolved", action_digest=digest, result_status="unresolved")
