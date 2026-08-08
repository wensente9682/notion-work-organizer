"""T10 Phase A red gate: synthetic-only real-write capability preflight."""

import contextlib
import ast
import copy
import dataclasses
import io
import importlib
import importlib.util
import json
import logging
import os
import sys
import tempfile
import traceback
import types
import unittest
from pathlib import Path
from unittest import mock

import adoption_profile
import new_system_approval
import new_system_recovery_journal
import new_system_write_coordinator
import new_system_real_write_preflight


TARGET_CANARY = "synthetic-t10-parent-canary"
SESSION_CANARY = "synthetic-t10-session-canary"
RAW_CANARY = "synthetic-raw-response-canary"
PRIVATE_CANARY = "synthetic-private-canary"
CAPABILITY_KINDS = ("database", "property", "view", "sort", "archive-target")
CAPABILITY_LABELS = ("tool-surface-present", "request-shape-ready", "real-write-unproven")
PLATFORM_CONTRACT = {
    "database": {"surface": "create_database", "request_schema": ("parent", "schema")},
    "property": {"surface": "configure_property", "request_schema": ("database", "property", "type")},
    "view": {"surface": "create_view", "request_schema": ("database", "view")},
    "sort": {"surface": "configure_view_sort", "request_schema": ("database", "view", "sort")},
    "archive-target": {"surface": "create_archive_target", "request_schema": ("container", "category", "schema")},
}


class FakeConnector:
    def __init__(self, response):
        self.response = response
        self.read_calls = 0
        self.write_calls = 0
        self.search_calls = 0

    def search(self):
        self.search_calls += 1
        return []

    def create_target(self, _request):
        self.write_calls += 1
        raise AssertionError("write must be unreachable")


class ReadOnlyFakeConnector:
    def __init__(self, response):
        self.response = response
        self.read_calls = 0
        self.targets = []

    def read_target(self, target):
        self.read_calls += 1
        self.targets.append(target)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


class ReentrantConnector(ReadOnlyFakeConnector):
    def __init__(self, response):
        super().__init__(response)
        self.nested = None

    def read_target(self, target):
        self.read_calls += 1
        self.nested = invoke(self, confirmation(), target=target)
        return self.response


class DynamicTrap:
    def __init__(self): self.calls = 0
    def __getattr__(self, _name): self.calls += 1; raise RuntimeError(PRIVATE_CANARY)
    def __iter__(self): self.calls += 1; raise RuntimeError(PRIVATE_CANARY)
    def __repr__(self): self.calls += 1; raise RuntimeError(PRIVATE_CANARY)
    def __str__(self): self.calls += 1; raise RuntimeError(PRIVATE_CANARY)


class FactString(str):
    pass


class ArmedKey(str):
    def __new__(cls, value, counter):
        item = super().__new__(cls, value); item.counter = counter; item.armed = False; return item
    def __hash__(self):
        if self.armed: self.counter["protocol"] += 1; raise RuntimeError(PRIVATE_CANARY)
        return str.__hash__(self)
    def __eq__(self, other):
        if self.armed: self.counter["protocol"] += 1; raise RuntimeError(PRIVATE_CANARY)
        return str.__eq__(self, other)


class DynamicConfirmationDict(dict):
    def __init__(self, value, counter): super().__init__(value); self.counter = counter
    def items(self): self.counter["protocol"] += 1; raise RuntimeError(PRIVATE_CANARY)


class DynamicPlain:
    def __init__(self, counter): self.counter = counter
    def __iter__(self): self.counter["protocol"] += 1; raise RuntimeError(PRIVATE_CANARY)
    def __repr__(self): self.counter["protocol"] += 1; raise RuntimeError(PRIVATE_CANARY)
    def __hash__(self): self.counter["protocol"] += 1; raise RuntimeError(PRIVATE_CANARY)


class ContractDict(dict):
    pass


class ContractList(list):
    pass


class ControlledClock:
    def __init__(self, values): self.values = iter(values)
    def __call__(self): return next(self.values)


class ClockAdvancingConnector(ReadOnlyFakeConnector):
    def __init__(self, response, advance):
        super().__init__(response)
        self.advance = advance

    def read_target(self, target):
        self.advance()
        return super().read_target(target)


class EventClock:
    def __init__(self, events, values=(0, 1, 2, 3)):
        self.events = events
        self.values = iter(values)
        self.calls = 0

    def __call__(self):
        self.events.append("start_check" if self.calls == 0 else "post_check")
        self.calls += 1
        return next(self.values)

    def during(self):
        self.events.append("during_check")
        self.calls += 1
        return next(self.values)


class EventConnector(ReadOnlyFakeConnector):
    def __init__(self, response, events, clock=None, during=False):
        super().__init__(response)
        self.events = events
        self.clock = clock
        self.during = during

    def read_target(self, target):
        self.read_calls += 1
        self.events.append("read_start")
        if self.during:
            self.clock.during()
            raise TimeoutError("synthetic timeout")
        if isinstance(self.response, BaseException): raise self.response
        value = self.response
        self.events.append("read_end")
        return value


class DangerousDescriptor:
    def __get__(self, instance, _owner):
        if instance is not None:
            object.__getattribute__(instance, "counter")["descriptor"] += 1
        raise RuntimeError(PRIVATE_CANARY)


class DangerousDataDescriptor(DangerousDescriptor):
    def __set__(self, _instance, _value):
        raise RuntimeError(PRIVATE_CANARY)


class SurfaceBase(ReadOnlyFakeConnector):
    discover = DangerousDescriptor()


class PropertySurfaceConnector(ReadOnlyFakeConnector):
    def __init__(self, response, counter):
        super().__init__(response)
        self.counter = counter

    @property
    def search(self):
        self.counter["property"] += 1
        raise RuntimeError(PRIVATE_CANARY)


class DataDescriptorSurfaceConnector(ReadOnlyFakeConnector):
    update = DangerousDataDescriptor()

    def __init__(self, response, counter):
        super().__init__(response)
        self.counter = counter


class DynamicSurfaceConnector(SurfaceBase):
    def __init__(self, response, counter):
        super().__init__(response)
        self.counter = counter

    def __getattr__(self, _name):
        self.counter["getattr"] += 1
        raise RuntimeError(PRIVATE_CANARY)


class InstanceSurfaceConnector(ReadOnlyFakeConnector):
    def __init__(self, response, counter):
        super().__init__(response)
        self.counter = counter
        self.search = DangerousDescriptor()
        self.write = DangerousDescriptor()
        self.create = DangerousDescriptor()
        self.update = DangerousDescriptor()
        self.delete = DangerousDescriptor()
        self.switch = DangerousDescriptor()


def confirmation(target=TARGET_CANARY, session=SESSION_CANARY, **changes):
    value = {
        "target_parent_attested": True,
        "creation_flow_attested": True,
        "dedicated_blank_parent": True,
        "not_t6": True,
        "target": target,
        "session": session,
    }
    value.update(changes)
    return value


def ready_response(**changes):
    value = {
        "kind": "page",
        "blank": True,
        "resources": [],
    }
    value.update(changes)
    return value


@dataclasses.dataclass(frozen=True)
class _ConstantResult:
    failure_class: str | None = None
    def to_public_dict(self): return {"status": "constant"}
    def __repr__(self): return "ConstantResult()"


@dataclasses.dataclass(frozen=True)
class _MissingImplementationResult:
    failure_class: str = "implementation-missing"
    def to_public_dict(self): return {"status": "implementation-missing"}
    def __repr__(self): return "MissingImplementationResult()"


@dataclasses.dataclass(frozen=True)
class _MutantResult:
    public: dict
    failure_class: str | None = None
    def to_public_dict(self): return dict(self.public)


def _happy_public():
    return {
        "status": "preflight-complete",
        "capabilities": {kind: CAPABILITY_LABELS for kind in CAPABILITY_KINDS},
        "preview": {"action_count": 1, "kind": "create_archive_target", "binding": "direct-child", "expected_state": "blank", "schema": "canonical-archive-v1", "executable": False},
        "authority": "none",
        "forbidden_method_calls": 0,
        "write_authority": False,
    }


def authority_free_source(source):
    forbidden = {"ApprovalLedger", "ApprovalEnvelope", "RecoveryJournal", "WriteCoordinator", "adoption_profile", "new_system_approval", "new_system_recovery_journal", "new_system_write_coordinator"}
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if any(part.name.split(".")[0] in forbidden for part in node.names): return False
        if isinstance(node, ast.Name) and node.id in forbidden: return False
    return True


@contextlib.contextmanager
def constant_shell():
    module = types.ModuleType("new_system_real_write_preflight")
    module.evaluate_real_write_capability = lambda *_args, **_kwargs: _ConstantResult()
    with mock.patch.dict(sys.modules, {module.__name__: module}):
        yield


@contextlib.contextmanager
def mutant_shell(kind):
    def evaluate(connector, _confirmation, _target, _contract, **kwargs):
        public = _happy_public()
        if kind == "privacy-clean":
            return _MutantResult({"status": "rejected"}, "response-contract")
        if kind.startswith("clock-"):
            clock = kwargs["clock"]
            if kind == "clock-during-clean":
                clock()
                try: connector.read_target(_target)
                except TimeoutError: return _MutantResult({"status": "rejected"}, "deadline")
            if kind == "clock-ok":
                clock(); connector.read_target(_target); clock()
            elif kind == "clock-no-start":
                connector.read_target(_target); clock()
            elif kind == "clock-no-post":
                clock(); connector.read_target(_target)
            elif kind == "clock-double-post":
                clock(); connector.read_target(_target); clock(); clock()
            elif kind == "clock-reversed":
                connector.read_target(_target); clock(); clock()
            return _MutantResult(public)
        if kind in {"preview-bind-ok", "preview-bind-authority"}:
            spec = kwargs["preview_spec"]
            public["preview"] = {"action_count": 1, "kind": "create_archive_target", "binding": "direct-child", "target": _target, "parent": spec["parent"], "expected_state": spec["blank_state"], "schema": spec["schema"], "executable": False}
        if kind == "preview-bind-authority":
            kwargs["forbidden_seams"]["approval"] += 1
        if kind == "authority-import":
            new_system_approval.ApprovalLedger()
            new_system_approval.ApprovalEnvelope()
            new_system_recovery_journal.RecoveryJournal(kwargs["temp_root"])
            new_system_write_coordinator.WriteCoordinator(object())
            adoption_profile.propose_adoption_profile()
        if kind == "labels": public["capabilities"]["database"] = ("real-write-proven",)
        if kind == "preview": public["preview"]["executable"] = True
        if kind == "surface": connector.read_target("synthetic-mutant")
        if kind.startswith("surface-"):
            name = {"surface-property": "search", "surface-data": "update", "surface-inherited": "discover", "surface-getattr": "unknown", "surface-instance": "search"}[kind]
            try:
                value = getattr(connector, name)
                if kind == "surface-instance": value.__get__(connector, type(connector))
            except Exception: pass
        if kind == "dynamic": public["diagnostic"] = PRIVATE_CANARY
        if kind == "privacy": public["response"] = RAW_CANARY
        if kind == "privacy-stdout": print(RAW_CANARY)
        if kind == "privacy-stderr": print(RAW_CANARY, file=sys.stderr)
        if kind == "privacy-logging": logging.getLogger("t10-red-gate").error(RAW_CANARY)
        if kind == "privacy-argv":
            before = list(sys.argv)
            try: sys.argv.append(RAW_CANARY); kwargs["process_spy"]["argv"] += 1
            finally: sys.argv[:] = before
        if kind == "privacy-env":
            before = dict(os.environ)
            try: os.environ["T10_MUTATED"] = RAW_CANARY; kwargs["process_spy"]["env"] += 1
            finally: os.environ.clear(); os.environ.update(before)
        if kind == "privacy-temp": Path(kwargs["temp_root"]).joinpath("leak").write_text(RAW_CANARY)
        if kind == "privacy-exception": raise RuntimeError(RAW_CANARY)
        return _MutantResult(public)
    module = types.ModuleType("new_system_real_write_preflight")
    module.evaluate_real_write_capability = evaluate
    with mock.patch.dict(sys.modules, {module.__name__: module}):
        yield


def invoke(connector, supplied_confirmation, target=TARGET_CANARY, contract=PLATFORM_CONTRACT, *, session=SESSION_CANARY, clock=None, temp_root=None, preview_spec=None, forbidden_seams=None, process_spy=None):
    if os.environ.get("T10_CONSTANT_SHELL") == "1":
        with constant_shell():
            from new_system_real_write_preflight import evaluate_real_write_capability
            return evaluate_real_write_capability(connector, supplied_confirmation, target, contract, session=session, clock=clock, temp_root=temp_root, preview_spec=preview_spec, forbidden_seams=forbidden_seams, process_spy=process_spy)
    mutant = os.environ.get("T10_MUTANT")
    if mutant in {"labels", "preview", "surface", "surface-property", "surface-data", "surface-inherited", "surface-getattr", "surface-instance", "dynamic", "privacy", "privacy-clean", "privacy-stdout", "privacy-stderr", "privacy-logging", "privacy-argv", "privacy-env", "privacy-temp", "privacy-exception", "clock-ok", "clock-no-start", "clock-no-post", "clock-double-post", "clock-reversed", "clock-during-clean", "preview-bind-ok", "preview-bind-authority", "authority-import"}:
        with mutant_shell(mutant):
            from new_system_real_write_preflight import evaluate_real_write_capability
            return evaluate_real_write_capability(connector, supplied_confirmation, target, contract, session=session, clock=clock, temp_root=temp_root, preview_spec=preview_spec, forbidden_seams=forbidden_seams, process_spy=process_spy)
    try:
        from new_system_real_write_preflight import evaluate_real_write_capability
    except ModuleNotFoundError:
        return _MissingImplementationResult()
    return evaluate_real_write_capability(connector, supplied_confirmation, target, contract, session=session, clock=clock, temp_root=temp_root, preview_spec=preview_spec, forbidden_seams=forbidden_seams, process_spy=process_spy)


class T10RealWritePreflightRedGate(unittest.TestCase):
    def test_guided_creation_attestation_prompt_is_fixed(self):
        self.assertEqual(
            "请确认：你提供的页面就是本次 New System creation 的目标父页面。确认本身不会修改 Notion；后续每个创建或修改动作仍会单独向你展示并要求批准。",
            new_system_real_write_preflight.TARGET_ATTESTATION_PROMPT,
        )

    def assert_closed_failure(self, result, expected):
        self.assertEqual(expected, result.failure_class)
        public = result.to_public_dict()
        self.assertEqual("rejected", public["status"])
        self.assertIn(expected, {"attestation-required", "target-invalid", "connector-rejected", "response-contract", "resource-bound", "deadline", "interrupted", "reentry", "access-denied"})

    def test_attestation_is_session_and_target_bound_before_read(self):
        cases = (
            ("missing-target-parent", confirmation(target_parent_attested=False), TARGET_CANARY, SESSION_CANARY, "attestation-required"),
            ("missing-creation-flow", confirmation(creation_flow_attested=False), TARGET_CANARY, SESSION_CANARY, "attestation-required"),
            ("not-dedicated", confirmation(dedicated_blank_parent=False), TARGET_CANARY, SESSION_CANARY, "attestation-required"),
            ("not-t6", confirmation(not_t6=False), TARGET_CANARY, SESSION_CANARY, "attestation-required"),
            ("target-changed", confirmation(target="synthetic-other"), TARGET_CANARY, SESSION_CANARY, "attestation-required"),
            ("session-changed", confirmation(session="synthetic-other-session"), TARGET_CANARY, SESSION_CANARY, "attestation-required"),
            ("multiple", confirmation(), [TARGET_CANARY, "synthetic-other"], SESSION_CANARY, "target-invalid"),
        )
        for name, supplied, target, session, expected in cases:
            with self.subTest(case=name):
                connector = ReadOnlyFakeConnector(ready_response())
                result = invoke(connector, supplied, target=target, session=session)
                self.assert_closed_failure(result, expected)
                self.assertEqual(0, connector.read_calls)

    def test_page_body_identity_like_data_is_never_an_identity_carrier(self):
        response = ready_response(resources=[{"body": TARGET_CANARY}])
        connector = ReadOnlyFakeConnector(response)
        result = invoke(connector, confirmation())
        self.assert_closed_failure(result, "response-contract")
        self.assertEqual(1, connector.read_calls)

    def test_confirmation_facts_and_platform_contract_are_exact_closed_plain_data(self):
        bad_facts = []
        for field in ("target_parent_attested", "creation_flow_attested", "dedicated_blank_parent", "not_t6", "target", "session"):
            missing = confirmation(); del missing[field]; bad_facts.append(("missing-" + field, missing))
            for value in (False, 1, "true", FactString("true"), lambda: True, [], {}):
                bad = confirmation(); bad[field] = value; bad_facts.append((field + "-" + type(value).__name__, bad))
        extra = confirmation(); extra["unexpected"] = True; bad_facts.append(("extra", extra))
        for name, facts in bad_facts:
            with self.subTest(facts=name):
                connector = ReadOnlyFakeConnector(ready_response())
                result = invoke(connector, facts)
                self.assert_closed_failure(result, "attestation-required")
                self.assertEqual(0, connector.read_calls)
        key_counter = {"protocol": 0}
        armed = ArmedKey("target_parent_attested", key_counter)
        bad_key = {armed: True, "creation_flow_attested": True, "dedicated_blank_parent": True, "not_t6": True, "target": TARGET_CANARY, "session": SESSION_CANARY}
        armed.armed = True
        for facts in (DynamicConfirmationDict(confirmation(), key_counter), bad_key):
            with self.subTest(container=type(facts).__name__):
                connector = ReadOnlyFakeConnector(ready_response())
                result = invoke(connector, facts)
                self.assert_closed_failure(result, "attestation-required")
                self.assertEqual(0, connector.read_calls)
        self.assertEqual(0, key_counter["protocol"])
        for kind in CAPABILITY_KINDS:
            mutations = []
            missing = copy.deepcopy(PLATFORM_CONTRACT); del missing[kind]; mutations.append(("missing", missing))
            wrong_name = copy.deepcopy(PLATFORM_CONTRACT); wrong_name[kind]["surface"] = "wrong_" + kind; mutations.append(("wrong-surface", wrong_name))
            wrong_schema = copy.deepcopy(PLATFORM_CONTRACT); wrong_schema[kind]["request_schema"] = ("wrong",); mutations.append(("wrong-schema", wrong_schema))
            dangerous = copy.deepcopy(PLATFORM_CONTRACT); dangerous[kind]["dangerous"] = "delete"; mutations.append(("dangerous-extra", dangerous))
            for mutation, contract in mutations:
                with self.subTest(kind=kind, mutation=mutation):
                    connector = ReadOnlyFakeConnector(ready_response())
                    result = invoke(connector, confirmation(), contract=contract)
                    self.assert_closed_failure(result, "connector-rejected")
                    self.assertEqual(0, connector.read_calls)
        counter = {"protocol": 0}
        invalid_contracts = (
            ContractDict(PLATFORM_CONTRACT),
            {**PLATFORM_CONTRACT, "database": ContractDict(PLATFORM_CONTRACT["database"])},
            {**PLATFORM_CONTRACT, "database": {"surface": FactString("create_database"), "request_schema": ("parent", "schema")}},
            {**PLATFORM_CONTRACT, "database": {"surface": "create_database", "request_schema": ContractList(["parent", "schema"])}},
            {**PLATFORM_CONTRACT, "database": {"surface": "create_database", "request_schema": (DynamicPlain(counter),)}},
        )
        for contract in invalid_contracts:
            with self.subTest(contract_type=type(contract).__name__):
                connector = ReadOnlyFakeConnector(ready_response())
                result = invoke(connector, confirmation(), contract=contract)
                self.assert_closed_failure(result, "connector-rejected")
                self.assertEqual(0, connector.read_calls)
        self.assertEqual(0, counter["protocol"])

    def test_connector_surface_rejects_discovery_and_write_offers_before_read(self):
        counter = {"descriptor": 0, "getattr": 0, "property": 0}
        for name, connector in (
            ("class-search-create", FakeConnector(ready_response())),
            ("property", PropertySurfaceConnector(ready_response(), counter)),
            ("data-descriptor", DataDescriptorSurfaceConnector(ready_response(), counter)),
            ("inherited-descriptor-and-getattr", DynamicSurfaceConnector(ready_response(), counter)),
            ("instance-dangerous-names", InstanceSurfaceConnector(ready_response(), counter)),
        ):
            with self.subTest(case=name):
                result = invoke(connector, confirmation())
                self.assert_closed_failure(result, "connector-rejected")
                self.assertEqual(0, connector.read_calls)
        self.assertEqual({"descriptor": 0, "getattr": 0, "property": 0}, counter)

    def test_surface_mutant_cannot_hide_a_read_behind_success_status(self):
        connector = ReadOnlyFakeConnector(ready_response())
        result = invoke(connector, confirmation())
        self.assertEqual("preflight-complete", result.to_public_dict()["status"])
        self.assertEqual(1, connector.read_calls)

    def test_surface_protocol_touch_mutants_have_observable_zero_side_effect_oracles(self):
        counter = {"descriptor": 0, "getattr": 0, "property": 0}
        selector = os.environ.get("T10_MUTANT")
        connector = {
            "surface-property": PropertySurfaceConnector(ready_response(), counter),
            "surface-data": DataDescriptorSurfaceConnector(ready_response(), counter),
            "surface-inherited": DynamicSurfaceConnector(ready_response(), counter),
            "surface-getattr": DynamicSurfaceConnector(ready_response(), counter),
            "surface-instance": InstanceSurfaceConnector(ready_response(), counter),
        }.get(selector, ReadOnlyFakeConnector(ready_response()))
        result = invoke(connector, confirmation())
        self.assertEqual("preflight-complete", result.to_public_dict()["status"])
        self.assertEqual({"descriptor": 0, "getattr": 0, "property": 0}, counter)

    def test_one_bounded_read_never_discovers_paginates_retries_or_switches_target(self):
        for name, response, expected in (
            ("unrelated", ready_response(resources=["unrelated"]), "response-contract"),
            ("pagination", ready_response(next_cursor="again"), "response-contract"),
            ("partial", ready_response(partial=True), "response-contract"),
        ):
            with self.subTest(case=name):
                connector = ReadOnlyFakeConnector(response)
                result = invoke(connector, confirmation())
                self.assert_closed_failure(result, expected)
                self.assertEqual(1, connector.read_calls)

    def test_access_identity_blank_conflict_and_malformed_are_closed(self):
        cases = (
            ("unauthenticated", ready_response(access="unauthenticated"), "access-denied"),
            ("unauthorized", ready_response(access="forbidden"), "access-denied"),
            ("ambiguous-carrier", ready_response(identity_candidates=["a", "b"]), "response-contract"),
            ("not-blank", ready_response(blank=False), "response-contract"),
            ("conflict", ready_response(resources=["conflict"]), "response-contract"),
            ("undeclared-raw", ready_response(raw=RAW_CANARY), "response-contract"),
            ("malformed", {"kind": "page"}, "response-contract"),
        )
        for name, response, expected in cases:
            with self.subTest(case=name):
                connector = ReadOnlyFakeConnector(response)
                result = invoke(connector, confirmation())
                self.assert_closed_failure(result, expected)
                self.assertEqual(1, connector.read_calls)

    def test_capability_labels_are_exact_and_make_no_write_claim(self):
        connector = ReadOnlyFakeConnector(ready_response())
        result = invoke(connector, confirmation())
        public = result.to_public_dict()
        self.assertIsNone(result.failure_class)
        self.assertEqual("preflight-complete", public["status"])
        self.assertEqual(set(CAPABILITY_KINDS), set(public["capabilities"]))
        for kind in CAPABILITY_KINDS:
            self.assertEqual(CAPABILITY_LABELS, tuple(public["capabilities"][kind]))
        self.assertNotIn("actual-write", json.dumps(public, sort_keys=True))
        self.assertFalse(public["write_authority"])
        self.assertEqual(1, connector.read_calls)

    def test_preview_is_one_exact_nonexecutable_archive_target_without_authority(self):
        connector = ReadOnlyFakeConnector(ready_response())
        result = invoke(connector, confirmation())
        public = result.to_public_dict()
        self.assertEqual("preflight-complete", public["status"])
        self.assertEqual({
            "action_count": 1,
            "kind": "create_archive_target",
            "binding": "direct-child",
            "expected_state": "blank",
            "schema": "canonical-archive-v1",
            "executable": False,
        }, public["preview"])
        self.assertEqual("none", public["authority"])
        self.assertEqual(0, public["forbidden_method_calls"])
        self.assertEqual(1, connector.read_calls)

    def test_preview_binds_each_target_parent_blank_state_and_schema_without_authority_side_effect(self):
        cases = (
            ("synthetic-target-a", {"parent": "synthetic-target-a", "blank_state": "blank", "schema": "canonical-archive-v1"}),
            ("synthetic-target-b", {"parent": "synthetic-target-b", "blank_state": "blank", "schema": "canonical-archive-v1"}),
        )
        for target, spec in cases:
            with self.subTest(target=target):
                spies = {name: 0 for name in ("approval", "envelope", "journal", "coordinator", "t9", "profile")}
                connector = ReadOnlyFakeConnector(ready_response())
                result = invoke(connector, confirmation(target=target), target=target, preview_spec=spec, forbidden_seams=spies)
                self.assertEqual("preflight-complete", result.to_public_dict()["status"])
                self.assertEqual([target], connector.targets)
                public = repr(result) + json.dumps(result.to_public_dict(), sort_keys=True) + json.dumps(dataclasses.asdict(result), sort_keys=True)
                self.assertNotIn(target, public)
                self.assertEqual({
                    "action_count": 1,
                    "kind": "create_archive_target",
                    "binding": "direct-child",
                    "expected_state": "blank",
                    "schema": "canonical-archive-v1",
                    "executable": False,
                }, result.to_public_dict()["preview"])
                self.assertEqual({name: 0 for name in spies}, spies)

    def test_preview_spec_rejects_noncanonical_or_unbound_values_before_publication(self):
        cases = (
            ("different-parent", {"parent": "synthetic-other-parent", "blank_state": "blank", "schema": "canonical-archive-v1"}),
            ("nonblank", {"parent": TARGET_CANARY, "blank_state": "not-blank", "schema": "canonical-archive-v1"}),
            ("noncanonical-schema", {"parent": TARGET_CANARY, "blank_state": "blank", "schema": "synthetic-schema-canary"}),
        )
        for name, spec in cases:
            with self.subTest(case=name):
                connector = ReadOnlyFakeConnector(ready_response())
                result = invoke(connector, confirmation(), preview_spec=spec)
                self.assert_closed_failure(result, "response-contract")
                self.assertEqual(1, connector.read_calls)

    def test_real_authority_import_and_construction_seams_are_never_touched(self):
        calls = {name: 0 for name in ("ledger", "envelope", "journal", "coordinator", "profile")}
        def spy(name):
            def called(*_args, **_kwargs): calls[name] += 1; return object()
            return called
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(new_system_approval, "ApprovalLedger", side_effect=spy("ledger")), \
             mock.patch.object(new_system_approval, "ApprovalEnvelope", side_effect=spy("envelope")), \
             mock.patch.object(new_system_recovery_journal, "RecoveryJournal", side_effect=spy("journal")), \
             mock.patch.object(new_system_write_coordinator, "WriteCoordinator", side_effect=spy("coordinator")), \
             mock.patch.object(adoption_profile, "propose_adoption_profile", side_effect=spy("profile")):
            result = invoke(ReadOnlyFakeConnector(ready_response()), confirmation(), temp_root=root)
        self.assertEqual("preflight-complete", result.to_public_dict()["status"])
        self.assertEqual({name: 0 for name in calls}, calls)

    def test_evaluator_cached_authority_aliases_and_ast_are_denied(self):
        clean = "def evaluate_real_write_capability(connector, confirmation, target, contract, **kwargs):\n    return None\n"
        alias_mutant = "from new_system_approval import ApprovalLedger as Ledger\ndef evaluate_real_write_capability():\n    return Ledger()\n"
        self.assertTrue(authority_free_source(clean))
        self.assertFalse(authority_free_source(alias_mutant))
        module = types.ModuleType("synthetic_t10_authority_alias")
        exec(alias_mutant, module.__dict__)
        calls = {"ledger": 0}
        with mock.patch.object(module, "Ledger", side_effect=lambda: calls.__setitem__("ledger", calls["ledger"] + 1)):
            module.evaluate_real_write_capability()
        self.assertEqual(1, calls["ledger"])
        importlib_spec = importlib.util.find_spec("new_system_real_write_preflight")
        if importlib_spec is None or importlib_spec.origin is None:
            self.fail("T10 evaluator module is missing")
        source = Path(importlib_spec.origin).read_text(encoding="utf-8")
        self.assertTrue(authority_free_source(source))
        evaluator = importlib.import_module("new_system_real_write_preflight")
        forbidden = {"ApprovalLedger", "ApprovalEnvelope", "RecoveryJournal", "WriteCoordinator", "adoption_profile", "new_system_approval", "new_system_recovery_journal", "new_system_write_coordinator"}
        self.assertTrue(forbidden.isdisjoint(vars(evaluator)))

    def test_shape_deadline_interruption_and_reentry_fail_without_second_read(self):
        cases = (
            ("oversize", ready_response(resources=[0] * 17), confirmation(), None, "resource-bound"),
            ("deadline", ready_response(), confirmation(), ControlledClock((0, 0)), "deadline"),
            ("interrupt", KeyboardInterrupt(), confirmation(), None, "interrupted"),
        )
        for name, response, supplied, clock, expected in cases:
            with self.subTest(case=name):
                connector = ReadOnlyFakeConnector(response)
                result = invoke(connector, supplied, clock=clock)
                self.assert_closed_failure(result, expected)
                self.assertLessEqual(connector.read_calls, 1)
        connector = ReentrantConnector(ready_response())
        result = invoke(connector, confirmation())
        self.assert_closed_failure(result, "reentry")
        self.assertEqual("reentry", connector.nested.failure_class)
        self.assertEqual(1, connector.read_calls)

    def test_monotonic_deadline_is_checked_before_during_and_after_the_single_read(self):
        cases = (
            ("read-over-budget", ControlledClock((0, 51)), "deadline"),
            ("clock-backwards", ControlledClock((10, 9)), "deadline"),
            ("frozen-no-progress", ControlledClock((0, 0)), "deadline"),
        )
        for name, clock, expected in cases:
            with self.subTest(case=name):
                connector = ClockAdvancingConnector(ready_response(), lambda: None)
                result = invoke(connector, confirmation(), clock=clock)
                self.assert_closed_failure(result, expected)
                self.assertEqual(1, connector.read_calls)

    def test_deadline_uses_relative_elapsed_time_not_absolute_clock_value(self):
        short = ReadOnlyFakeConnector(ready_response())
        result = invoke(short, confirmation(), clock=ControlledClock((100, 101)))
        self.assertEqual("preflight-complete", result.to_public_dict()["status"])
        self.assertEqual(1, short.read_calls)
        over_budget = ReadOnlyFakeConnector(ready_response())
        result = invoke(over_budget, confirmation(), clock=ControlledClock((100, 151)))
        self.assert_closed_failure(result, "deadline")
        self.assertEqual(1, over_budget.read_calls)

    def test_clock_event_order_is_start_then_one_read_then_post(self):
        events = []
        clock = EventClock(events)
        connector = EventConnector(ready_response(), events, clock=clock)
        result = invoke(connector, confirmation(), clock=clock)
        self.assertEqual("preflight-complete", result.to_public_dict()["status"])
        self.assertEqual(["start_check", "read_start", "read_end", "post_check"], events)
        self.assertEqual(1, connector.read_calls)

    def test_read_during_timeout_and_post_read_timeout_have_distinct_event_oracles(self):
        during_events = []; during_clock = EventClock(during_events, (0, 51))
        during = EventConnector(ready_response(), during_events, clock=during_clock, during=True)
        result = invoke(during, confirmation(), clock=during_clock)
        self.assert_closed_failure(result, "deadline")
        self.assertEqual(["start_check", "read_start", "during_check"], during_events)
        self.assertEqual(1, during.read_calls)
        after_events = []; after_clock = EventClock(after_events, (0, 51))
        after = EventConnector(ready_response(), after_events, clock=after_clock)
        result = invoke(after, confirmation(), clock=after_clock)
        self.assert_closed_failure(result, "deadline")
        self.assertEqual(["start_check", "read_start", "read_end", "post_check"], after_events)
        self.assertEqual(1, after.read_calls)

    def test_during_timeout_clean_control_records_one_attempt_without_post_check(self):
        events = []; clock = EventClock(events, (0, 51)); connector = EventConnector(ready_response(), events, clock=clock, during=True)
        result = invoke(connector, confirmation(), clock=clock)
        self.assert_closed_failure(result, "deadline")
        self.assertEqual(["start_check", "read_start", "during_check"], events)
        self.assertEqual(1, connector.read_calls)

    def test_response_resource_boundaries_are_distinct_from_later_semantic_rejection(self):
        cases = (
            ("container-allowed", ready_response(resources=[0] * 16), False),
            ("container-plus-one", ready_response(resources=[0] * 17), True),
            ("string-allowed", ready_response(resources=["x" * 128]), False),
            ("string-plus-one", ready_response(resources=["x" * 129]), True),
            ("total-allowed", ready_response(resources=["x" * 128, "y" * 128]), False),
            ("total-plus-one", ready_response(resources=["x" * 128, "y" * 129]), True),
            ("depth-allowed", ready_response(resources=[[[[0]]]]), False),
            ("depth-plus-one", ready_response(resources=[[[[[0]]]]]), True),
        )
        for name, response, limit_hit in cases:
            with self.subTest(case=name):
                connector = ReadOnlyFakeConnector(response)
                result = invoke(connector, confirmation())
                self.assertEqual("resource-bound" if limit_hit else "response-contract", result.failure_class)
                self.assertEqual(1, connector.read_calls)

    def test_dynamic_request_is_inert_and_rejected_before_read(self):
        trap = DynamicTrap()
        connector = ReadOnlyFakeConnector(trap)
        result = invoke(connector, confirmation(), target=trap)
        self.assert_closed_failure(result, "target-invalid")
        self.assertEqual(0, trap.calls)
        self.assertEqual(0, connector.read_calls)

    def test_dynamic_response_and_exception_are_inert_and_sanitized(self):
        trap = DynamicTrap()
        connector = ReadOnlyFakeConnector(trap)
        result = invoke(connector, confirmation())
        self.assert_closed_failure(result, "response-contract")
        self.assertEqual(0, trap.calls)
        self.assertEqual(1, connector.read_calls)
        connector = ReadOnlyFakeConnector(RuntimeError(PRIVATE_CANARY))
        result = invoke(connector, confirmation())
        text = repr(result) + json.dumps(result.to_public_dict(), sort_keys=True)
        self.assert_closed_failure(result, "response-contract")
        self.assertNotIn(PRIVATE_CANARY, text)
        self.assertEqual(1, connector.read_calls)

    def test_dynamic_mutant_cannot_hide_in_a_successful_public_result(self):
        result = invoke(ReadOnlyFakeConnector(ready_response()), confirmation())
        public = repr(result) + json.dumps(result.to_public_dict(), sort_keys=True)
        self.assertEqual("preflight-complete", result.to_public_dict()["status"])
        self.assertNotIn(PRIVATE_CANARY, public)

    def test_all_public_diagnostic_exits_are_target_and_raw_response_free(self):
        connector = ReadOnlyFakeConnector(RuntimeError(RAW_CANARY))
        stream = io.StringIO(); logger = logging.getLogger("t10-red-gate"); root = logging.getLogger(); handler = logging.StreamHandler(stream); root.addHandler(handler)
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.dict(os.environ, {"T10_ENV_CANARY": PRIVATE_CANARY}), mock.patch.object(sys, "argv", [*sys.argv, TARGET_CANARY]):
            before = sorted(path.relative_to(temp_dir).as_posix() for path in Path(temp_dir).rglob("*"))
            env_before = dict(os.environ); argv_before = list(sys.argv)
            try:
                with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                    result = invoke(connector, confirmation(), temp_root=temp_dir)
            finally:
                root.removeHandler(handler)
            nested = json.dumps(dataclasses.asdict(result), sort_keys=True)
            public = repr(result) + json.dumps(result.to_public_dict(), sort_keys=True) + nested
            self.assert_closed_failure(result, "response-contract")
            after = sorted(path.relative_to(temp_dir).as_posix() for path in Path(temp_dir).rglob("*"))
            self.assertEqual(before, after)
            self.assertEqual(env_before, dict(os.environ))
            self.assertEqual(argv_before, sys.argv)
            exits = public + stream.getvalue()
            for secret in (TARGET_CANARY, RAW_CANARY, PRIVATE_CANARY):
                self.assertNotIn(secret, exits)

    def test_privacy_mutant_cannot_hide_raw_response_in_a_successful_result(self):
        result = invoke(ReadOnlyFakeConnector(ready_response()), confirmation())
        public = repr(result) + json.dumps(result.to_public_dict(), sort_keys=True) + json.dumps(dataclasses.asdict(result), sort_keys=True)
        self.assertEqual("preflight-complete", result.to_public_dict()["status"])
        self.assertNotIn(RAW_CANARY, public)

    def test_privacy_channel_mutants_leave_no_output_or_process_or_temp_trace(self):
        stream = io.StringIO(); root = logging.getLogger(); handler = logging.StreamHandler(stream); root.addHandler(handler)
        with tempfile.TemporaryDirectory() as temp_dir, contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            env_before = dict(os.environ); argv_before = list(sys.argv); files_before = list(Path(temp_dir).rglob("*")); caught = ""; status = "exception"; process_spy = {"argv": 0, "env": 0}
            try:
                result = invoke(ReadOnlyFakeConnector(ready_response()), confirmation(), temp_root=temp_dir, process_spy=process_spy)
                public = repr(result) + json.dumps(result.to_public_dict(), sort_keys=True) + json.dumps(dataclasses.asdict(result), sort_keys=True)
                status = result.to_public_dict()["status"]
            except Exception:
                public = ""; caught = traceback.format_exc()
            finally:
                root.removeHandler(handler)
            self.assertEqual(env_before, dict(os.environ))
            self.assertEqual(argv_before, sys.argv)
            self.assertEqual({"argv": 0, "env": 0}, process_spy)
            self.assertEqual(files_before, list(Path(temp_dir).rglob("*")))
            self.assertNotIn(RAW_CANARY, public + caught + stream.getvalue())
            self.assertEqual("preflight-complete", status)

    def test_constant_shell_is_behaviorally_red_without_infrastructure_errors(self):
        with constant_shell():
            connector = ReadOnlyFakeConnector(ready_response())
            result = invoke(connector, confirmation())
            self.assertNotEqual("preflight-complete", result.to_public_dict()["status"])
            self.assertEqual(0, connector.read_calls)


if __name__ == "__main__":
    unittest.main()
