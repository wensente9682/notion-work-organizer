"""T6 fake-only red gate; every test names its invariant and attack clauses."""

import io
import json
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout


TARGET_CANARY = "synthetic-target-marker"
PRIVATE_CANARY = "synthetic-private-marker"


# Test name -> invariant -> every table attack clause / required evidence fields.
COVERAGE = {
    "confirmation_discovery": ("no-smoke-before-confirmation", ("missing-confirmation", "missing-target", "search-capability"), ("target_confirmed", "connector_calls_before_confirmation", "discovery_attempted")),
    "write_surface": ("read-only-surface", ("advertised-write", "write-attribute-trap", "callback-write"), ("read_calls", "write_calls", "adapter_surface_valid")),
    "authn_authz": ("authn-authz-distinct", ("unauthenticated", "forbidden", "metadata-only"), ("real_read_completed", "authn_ok", "authz_ok", "failure_class")),
    "bounds": ("bounded-request-response", ("oversized-sample", "pagination", "repeated-cursor", "cross-target", "no-progress"), ("target_count", "read_call_count", "sample_count", "limit_hit", "pagination_attempted")),
    "shape": ("allowed-shape-only", ("malformed", "ambiguous", "partial", "identity-drift"), ("response_kind", "shape_valid", "identity_stable", "partial_response", "parser_ok")),
    "dynamic": ("inert-deterministic-parser", ("getter", "iterator", "mapping", "repr-str"), ("plain_data_only", "parser_steps", "parser_budget_hit", "side_effect_calls")),
    "deadline": ("one-deadline-no-reentry", ("deadline", "phase-exception", "keyboard-interrupt", "recursive-reentry", "frozen-clock-no-progress"), ("deadline_budget_class", "timing_bucket", "deadline_exceeded", "completed_phase", "interrupted", "reentry_rejected", "read_cap_respected")),
    "target_exits": ("execution-record-only", ("argv", "environment", "cache", "state", "other-thread-artifact", "diagnostic"), ("target_lifetime", "execution_record_only", "persistent_copy_created_elsewhere", "reusable_authority_created", "forbidden_exit_matches")),
    "evidence": ("unlinkable-public-evidence", ("exact-count", "exact-timing", "exact-type", "stable-digest", "content-change"), ("evidence_schema_version", "count_cap_result", "response_kind_class", "timing_bucket", "stable_fingerprint_present")),
    "diagnostics": ("all-diagnostics-sanitized", ("raised", "chained", "repr", "str", "iterator", "subtest", "assertion-rendering", "stdout-stderr-traceback"), ("evidence_schema", "sensitive_value_matches", "stdout_bytes", "stderr_bytes", "diagnostic_exit_matches")),
    "success": ("success-no-authority", ("no-envelope", "no-profile-mutation", "no-readiness-claim", "second-call-no-inheritance"), ("write_authority_created", "profile_changed", "readiness_claimed", "scope_label")),
}


class ReadOnlyAdapter:
    def __init__(self, response=None, *, raise_error=None, counter=None):
        self.response = response
        self.raise_error = raise_error
        self.counter = counter if counter is not None else {"read": 0, "write": 0, "search": 0}

    def read_target(self, target):
        self.counter["read"] += 1
        if self.raise_error is not None:
            raise self.raise_error
        return self.response


class DiscoveryAdapter(ReadOnlyAdapter):
    def search(self):
        self.counter["search"] += 1
        return []


class MaliciousWriteAdapter(ReadOnlyAdapter):
    def write_target(self, value):
        self.counter["write"] += 1
        raise AssertionError("write must be unreachable")

    @property
    def write_callback(self):
        self.counter["write"] += 1
        raise AssertionError("write callback must be unreachable")


class WriteBase:
    def write_target(self, value):
        self.counter["write"] += 1
        raise AssertionError("write must be unreachable")


class InheritedWriteAdapter(WriteBase, ReadOnlyAdapter):
    def read_target(self, target):
        return ReadOnlyAdapter.read_target(self, target)


class InstanceWriteAdapter(ReadOnlyAdapter):
    def __init__(self, response):
        super().__init__(response)
        self.write_target = lambda: (_ for _ in ()).throw(AssertionError("unreachable"))

    def read_target(self, target):
        return ReadOnlyAdapter.read_target(self, target)


class PropertyDiscoveryAdapter(ReadOnlyAdapter):
    @property
    def search(self):
        self.counter["search"] += 1
        raise AssertionError("property must be unreachable")


class DynamicSurfaceAdapter(ReadOnlyAdapter):
    def __init__(self, response):
        super().__init__(response)
        self.counter["dynamic"] = 0

    def __getattr__(self, name):
        self.counter["dynamic"] += 1
        raise RuntimeError(PRIVATE_CANARY)


class DynamicTrap:
    def __init__(self, counter):
        self.counter = counter

    def __getattr__(self, name):
        self.counter["dynamic"] += 1
        raise RuntimeError(PRIVATE_CANARY)

    def __iter__(self):
        self.counter["dynamic"] += 1
        raise RuntimeError(PRIVATE_CANARY)

    def __repr__(self):
        self.counter["dynamic"] += 1
        raise RuntimeError(PRIVATE_CANARY)

    def __str__(self):
        self.counter["dynamic"] += 1
        raise RuntimeError(PRIVATE_CANARY)


class ExplosiveMapping(dict):
    def __init__(self, counter):
        super().__init__()
        self.counter = counter

    def items(self):
        self.counter["dynamic"] += 1
        raise RuntimeError(PRIVATE_CANARY)


class StringTrap(str):
    def __new__(cls, value, counter):
        value = super().__new__(cls, value)
        value.counter = counter
        return value

    def __eq__(self, value):
        self.counter["dynamic"] += 1
        raise RuntimeError(PRIVATE_CANARY)


class RequestKeyTrap(str):
    def __new__(cls, counter):
        value = super().__new__(cls, "workspace_confirmed")
        value.counter = counter
        value.armed = False
        return value

    def __hash__(self):
        self.counter["protocol"] += 1
        return hash("workspace_confirmed")

    def __eq__(self, value):
        self.counter["protocol"] += 1
        if self.armed:
            raise RuntimeError(PRIVATE_CANARY)
        return False


class RequestDictTrap(dict):
    def __init__(self, counter):
        super().__init__()
        self.counter = counter

    def items(self):
        self.counter["protocol"] += 1
        raise RuntimeError(PRIVATE_CANARY)


class FakeSinks:
    def __init__(self):
        self.argv = []
        self.environment = []
        self.cache = []
        self.state = []
        self.other_thread = []
        self.diagnostics = []

    def all_values(self):
        return self.argv + self.environment + self.cache + self.state + self.other_thread + self.diagnostics


def _request(**changes):
    request = {
        "workspace_confirmed": True,
        "target": TARGET_CANARY,
        "deadline_ms": 50,
        "sample_cap": 2,
        "read_cap": 1,
        "clock": lambda: 0,
        "sinks": FakeSinks(),
    }
    request.update(changes)
    return request


def _invoke(adapter, request):
    # Deliberately unresolved during T6-A. No test-only product skeleton.
    from new_system_connector_smoke import run_bounded_smoke

    return run_bounded_smoke(adapter, request)


class ConnectorSmokeRedGate(unittest.TestCase):
    def assertEvidence(self, result, coverage):
        self.assertTrue(set(COVERAGE[coverage][2]).issubset(result.evidence))

    def test_confirmation_discovery_zero_call(self):
        for name, request, adapter in (
            ("missing-confirmation", _request(workspace_confirmed=False), ReadOnlyAdapter()),
            ("missing-target", _request(target=None), ReadOnlyAdapter()),
            ("search-capability", _request(workspace_confirmed=False), DiscoveryAdapter()),
        ):
            with self.subTest(case=name):
                result = _invoke(adapter, request)
                self.assertEqual("confirmation-required", result.failure_class)
                self.assertEqual(0, adapter.counter["read"])
                self.assertEqual(0, adapter.counter["search"])
                self.assertFalse(result.evidence["target_confirmed"])
                self.assertEqual(0, result.evidence["connector_calls_before_confirmation"])
                self.assertFalse(result.evidence["discovery_attempted"])
                self.assertEvidence(result, "confirmation_discovery")

    def test_request_keys_are_materialized_before_any_lookup(self):
        counter = {"protocol": 0, "dynamic": 0}
        key = RequestKeyTrap(counter)
        request = {key: False}
        request.update(_request())
        key.armed = True
        counter["protocol"] = 0
        cases = (
            ("str-subclass-key", request),
            ("dict-subclass", RequestDictTrap(counter)),
            ("unknown-key", _request(unknown=DynamicTrap(counter))),
            ("nested-dynamic", _request(target=DynamicTrap(counter))),
        )
        for name, candidate in cases:
            with self.subTest(case=name):
                adapter = ReadOnlyAdapter()
                result = _invoke(adapter, candidate)
                self.assertEqual("confirmation-required", result.failure_class)
                self.assertEqual(0, adapter.counter["read"])
                self.assertEqual(0, counter["protocol"])
                self.assertEqual(0, counter["dynamic"])

    def test_write_surface_zero_write(self):
        adapter = MaliciousWriteAdapter({"kind": "ok", "target": TARGET_CANARY, "items": []})
        result = _invoke(adapter, _request())
        self.assertEqual("adapter-rejected", result.failure_class)
        self.assertEqual(0, adapter.counter["write"])
        self.assertEqual(0, result.evidence["read_calls"])
        self.assertEqual(0, result.evidence["write_calls"])
        self.assertFalse(result.evidence["adapter_surface_valid"])
        self.assertEvidence(result, "write_surface")

    def test_read_surface_rejects_inherited_instance_property_and_dynamic_offers_before_read(self):
        adapters = (
            InheritedWriteAdapter({"kind": "ok", "target": TARGET_CANARY, "items": []}),
            InstanceWriteAdapter({"kind": "ok", "target": TARGET_CANARY, "items": []}),
            PropertyDiscoveryAdapter({"kind": "ok", "target": TARGET_CANARY, "items": []}),
            DynamicSurfaceAdapter({"kind": "ok", "target": TARGET_CANARY, "items": []}),
        )
        for adapter in adapters:
            with self.subTest(adapter=type(adapter).__name__):
                result = _invoke(adapter, _request())
                self.assertEqual("adapter-rejected", result.failure_class)
                self.assertEqual(0, adapter.counter["read"])
                self.assertEqual(0, adapter.counter["write"])
                self.assertEqual(0, adapter.counter.get("dynamic", 0))

    def test_authentication_and_authorization_are_distinct(self):
        for name, response, expected, authn, authz in (
            ("unauthenticated", {"kind": "unauthenticated"}, "authentication", False, False),
            ("forbidden", {"kind": "forbidden"}, "authorization", True, False),
            ("metadata-only", {"kind": "metadata"}, "response-contract", False, False),
        ):
            with self.subTest(case=name):
                adapter = ReadOnlyAdapter(response)
                result = _invoke(adapter, _request())
                self.assertEqual(expected, result.failure_class)
                self.assertEqual(1, adapter.counter["read"])
                self.assertFalse(result.evidence["real_read_completed"])
                self.assertEqual(authn, result.evidence["authn_ok"])
                self.assertEqual(authz, result.evidence["authz_ok"])
                self.assertEvidence(result, "authn_authz")

    def test_bounds_cover_every_attack(self):
        cases = (
            ("oversized-sample", {"kind": "ok", "target": TARGET_CANARY, "items": [1, 2, 3]}, "resource-bound"),
            ("pagination", {"kind": "ok", "target": TARGET_CANARY, "items": [], "next_cursor": "next"}, "resource-bound"),
            ("repeated-cursor", {"kind": "ok", "target": TARGET_CANARY, "items": [], "cursor": "same", "next_cursor": "same"}, "resource-bound"),
            ("cross-target", {"kind": "ok", "target": "other-synthetic-target", "items": []}, "response-contract"),
            ("no-progress", {"kind": "ok", "target": TARGET_CANARY, "items": [], "next_cursor": "same"}, "resource-bound"),
        )
        for name, response, expected in cases:
            with self.subTest(case=name):
                adapter = ReadOnlyAdapter(response)
                result = _invoke(adapter, _request())
                self.assertEqual(expected, result.failure_class)
                self.assertLessEqual(adapter.counter["read"], 1)
                self.assertEqual("one", result.evidence["target_count"])
                self.assertEqual(1, result.evidence["read_call_count"])
                self.assertEqual("capped", result.evidence["sample_count"])
                self.assertEqual(expected == "resource-bound", result.evidence["limit_hit"])
                self.assertFalse(result.evidence["pagination_attempted"])
                self.assertEvidence(result, "bounds")

    def test_shape_cases_never_parse_success(self):
        cases = (
            ("malformed", {"items": []}),
            ("ambiguous", {"kind": "ok", "target": TARGET_CANARY, "items": [], "wrapper": "ambiguous"}),
            ("partial", {"kind": "ok", "target": TARGET_CANARY, "items": [], "partial": True}),
            ("identity-drift", {"kind": "ok", "target": "other-synthetic-target", "items": []}),
        )
        for name, response in cases:
            with self.subTest(case=name):
                result = _invoke(ReadOnlyAdapter(response), _request())
                self.assertEqual("response-contract", result.failure_class)
                self.assertFalse(result.evidence["parser_ok"])
                self.assertEqual("rejected", result.evidence["response_kind"])
                self.assertFalse(result.evidence["shape_valid"])
                self.assertFalse(result.evidence["identity_stable"])
                self.assertEvidence(result, "shape")

    def test_dynamic_object_side_effects_are_not_invoked(self):
        counter = {"dynamic": 0}
        for value in (DynamicTrap(counter), ExplosiveMapping(counter)):
            with self.subTest(case="dynamic-value"):
                adapter = ReadOnlyAdapter(value)
                result = _invoke(adapter, _request())
                self.assertEqual("response-contract", result.failure_class)
                self.assertEqual(0, counter["dynamic"])
                self.assertFalse(result.evidence["plain_data_only"])
                self.assertEqual("capped", result.evidence["parser_steps"])
                self.assertEqual(0, result.evidence["side_effect_calls"])
                self.assertEvidence(result, "dynamic")

    def test_recursive_plain_data_rejects_nested_dynamic_values_and_bounds(self):
        counter = {"dynamic": 0}
        cases = (
            ("dynamic", {"kind": "ok", "target": TARGET_CANARY, "items": [{"nested": DynamicTrap(counter)}]}, "response-contract", False),
            ("string-subclass", {"kind": "ok", "target": TARGET_CANARY, "items": [StringTrap("item", counter)]}, "response-contract", False),
            ("container", {"kind": "ok", "target": TARGET_CANARY, "items": list(range(17))}, "resource-bound", True),
            ("depth", {"kind": "ok", "target": TARGET_CANARY, "items": [[[[[0]]]]]}, "resource-bound", True),
            ("nodes", {"kind": "ok", "target": TARGET_CANARY, "items": [[0, 1, 2, 3] for _ in range(16)]}, "resource-bound", True),
        )
        for name, response, expected, budget_hit in cases:
            with self.subTest(case=name):
                result = _invoke(ReadOnlyAdapter(response), _request())
                self.assertEqual(expected, result.failure_class)
                self.assertFalse(result.evidence["parser_ok"])
                self.assertEqual(0, counter["dynamic"])
                self.assertEqual(budget_hit, result.evidence["limit_hit"])
                self.assertEqual(budget_hit, result.evidence["parser_budget_hit"])

    def test_result_evidence_is_immutable_and_public_copy_is_not_shared(self):
        result = _invoke(ReadOnlyAdapter({"kind": "ok", "target": TARGET_CANARY, "items": []}), _request())
        with self.assertRaises(TypeError):
            result.evidence["parser_ok"] = False
        first = result.to_public_dict()
        first["failure_class"] = "private"
        self.assertNotEqual("private", result.to_public_dict()["failure_class"])
        self.assertNotIn(TARGET_CANARY, repr(result) + json.dumps(result.to_public_dict(), sort_keys=True))

    def test_deadline_phase_interrupt_reentry_and_no_progress_have_one_read(self):
        cases = (
            ("deadline", _request(deadline_ms=0), ReadOnlyAdapter({"kind": "ok", "target": TARGET_CANARY, "items": []})),
            ("phase-exception", _request(), ReadOnlyAdapter(raise_error=RuntimeError(PRIVATE_CANARY))),
            ("keyboard-interrupt", _request(), ReadOnlyAdapter(raise_error=KeyboardInterrupt())),
            ("recursive-reentry", _request(reenter=True), ReadOnlyAdapter({"kind": "ok", "target": TARGET_CANARY, "items": []})),
            ("frozen-clock-no-progress", _request(clock=lambda: 0), ReadOnlyAdapter({"kind": "no-progress", "target": TARGET_CANARY, "items": []})),
        )
        for name, request, adapter in cases:
            with self.subTest(case=name):
                result = _invoke(adapter, request)
                self.assertIn(result.failure_class, {"deadline", "phase-exception", "interrupted", "reentry", "resource-bound"})
                self.assertLessEqual(adapter.counter["read"], 1)
                self.assertEqual(adapter.counter["read"] <= 1, result.evidence["read_cap_respected"])
                self.assertEvidence(result, "deadline")

    def test_monotonic_deadline_and_facts_are_measured_and_truthful(self):
        ticks = iter((0, 51))
        result = _invoke(
            ReadOnlyAdapter({"kind": "ok", "target": TARGET_CANARY, "items": []}),
            _request(deadline_ms=50, clock=lambda: next(ticks)),
        )
        self.assertEqual("deadline", result.failure_class)
        self.assertEqual(1, result.evidence["read_calls"])
        self.assertEqual("deadline-exceeded", result.evidence["timing_bucket"])
        self.assertTrue(result.evidence["deadline_exceeded"])
        self.assertEqual("read", result.evidence["completed_phase"])
        unconfirmed = _invoke(ReadOnlyAdapter(), _request(workspace_confirmed=False))
        self.assertFalse(unconfirmed.evidence["authn_ok"])
        self.assertFalse(unconfirmed.evidence["authz_ok"])
        self.assertEqual("not-measured", unconfirmed.evidence["timing_bucket"])
        unauthenticated = _invoke(ReadOnlyAdapter({"kind": "unauthenticated"}), _request())
        self.assertFalse(unauthenticated.evidence["authn_ok"])
        self.assertFalse(unauthenticated.evidence["authz_ok"])

    def test_deadline_is_checked_after_read_validation_and_before_success(self):
        ticks = iter((0, 10, 60))
        result = _invoke(
            ReadOnlyAdapter({"kind": "ok", "target": TARGET_CANARY, "items": []}),
            _request(deadline_ms=50, clock=lambda: next(ticks)),
        )
        self.assertEqual("deadline", result.failure_class)
        self.assertEqual(1, result.evidence["read_calls"])
        self.assertEqual("deadline-exceeded", result.evidence["timing_bucket"])
        self.assertFalse(result.evidence["parser_ok"])
        self.assertEqual("validated", result.evidence["completed_phase"])

    def test_real_reader_reentry_and_cross_instance_claim_allow_one_read(self):
        counter = {"read": 0, "write": 0, "search": 0}
        class ReenteringAdapter(ReadOnlyAdapter):
            def read_target(self, target):
                self.counter["read"] += 1
                self.inner = _invoke(self, _request())
                return {"kind": "ok", "target": target, "items": []}
        adapter = ReenteringAdapter(counter=counter)
        outer = _invoke(adapter, _request())
        self.assertIsNone(outer.failure_class)
        self.assertEqual("reentry", adapter.inner.failure_class)
        self.assertEqual(1, counter["read"])

    def test_cross_instance_concurrency_allows_only_one_reader(self):
        entered, release = threading.Event(), threading.Event()
        counter = {"read": 0, "write": 0, "search": 0}
        class BlockingAdapter(ReadOnlyAdapter):
            def read_target(self, target):
                self.counter["read"] += 1
                entered.set()
                release.wait(1)
                return {"kind": "ok", "target": target, "items": []}
        adapter = BlockingAdapter(counter=counter)
        first_result = []
        thread = threading.Thread(target=lambda: first_result.append(_invoke(adapter, _request())))
        thread.start()
        self.assertTrue(entered.wait(1))
        second = _invoke(adapter, _request())
        release.set()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual("reentry", second.failure_class)
        self.assertIsNone(first_result[0].failure_class)
        self.assertEqual(1, counter["read"])

    def test_target_exits_use_fake_sinks(self):
        sinks = FakeSinks()
        result = _invoke(ReadOnlyAdapter({"kind": "ok", "target": TARGET_CANARY, "items": []}), _request(sinks=sinks))
        self.assertFalse(any(TARGET_CANARY in str(value) for value in sinks.all_values()))
        self.assertFalse(result.evidence["reusable_authority_created"])
        self.assertEqual("call-only", result.evidence["target_lifetime"])
        self.assertTrue(result.evidence["execution_record_only"])
        self.assertFalse(result.evidence["persistent_copy_created_elsewhere"])
        self.assertEqual(0, result.evidence["forbidden_exit_matches"])
        self.assertEvidence(result, "target_exits")

    def test_untrusted_sinks_are_not_touched(self):
        class SinkTrap:
            def __init__(self):
                self.calls = 0
            def __getattr__(self, name):
                self.calls += 1
                raise RuntimeError(PRIVATE_CANARY)
        sinks = SinkTrap()
        result = _invoke(ReadOnlyAdapter({"kind": "ok", "target": TARGET_CANARY, "items": []}), _request(sinks=sinks))
        self.assertIsNone(result.failure_class)
        self.assertEqual(0, sinks.calls)

    def test_public_evidence_rejects_exact_target_fingerprints(self):
        attacks = (
            _request(target=TARGET_CANARY, exact_count=17),
            _request(target="other-synthetic-target", exact_timing=17, response_type="private"),
            _request(target="third-synthetic-target", stable_digest="synthetic-digest"),
        )
        for request in attacks:
            with self.subTest(case="target-derived-evidence"):
                result = _invoke(ReadOnlyAdapter({"kind": "ok", "target": request["target"], "items": []}), request)
                public = result.to_public_dict()
                self.assertNotIn("exact_count", public)
                self.assertFalse(result.evidence["stable_fingerprint_present"])
                self.assertEqual("v1", result.evidence["evidence_schema_version"])
                self.assertEqual("within-cap", result.evidence["count_cap_result"])
                self.assertEvidence(result, "evidence")

    def test_diagnostics_cover_raised_chained_and_dynamic_exits(self):
        dynamic_counter = {"dynamic": 0}
        chained = RuntimeError(PRIVATE_CANARY)
        raised = ValueError(PRIVATE_CANARY)
        raised.__cause__ = chained
        adapter = ReadOnlyAdapter(raise_error=raised)
        stdout, stderr = io.StringIO(), io.StringIO()
        with self.subTest(case="assertion-rendering"), redirect_stdout(stdout), redirect_stderr(stderr):
            result = _invoke(adapter, _request(diagnostic_value=DynamicTrap(dynamic_counter)))
        rendered = json.dumps(result.to_public_dict(), sort_keys=True)
        self.assertNotIn(PRIVATE_CANARY, rendered + stdout.getvalue() + stderr.getvalue())
        self.assertEqual(0, dynamic_counter["dynamic"])
        self.assertEqual("closed", result.evidence["evidence_schema"])
        self.assertEqual(0, result.evidence["sensitive_value_matches"])
        self.assertEqual(0, result.evidence["stdout_bytes"])
        self.assertEqual(0, result.evidence["stderr_bytes"])
        self.assertEqual(0, result.evidence["diagnostic_exit_matches"])
        self.assertEvidence(result, "diagnostics")

    def test_success_creates_no_authority_and_second_call_is_fresh(self):
        adapter = ReadOnlyAdapter({"kind": "ok", "target": TARGET_CANARY, "items": []})
        first = _invoke(adapter, _request())
        second = _invoke(adapter, _request())
        self.assertEqual("bounded-read-only-smoke", first.scope_label)
        self.assertFalse(first.evidence["write_authority_created"])
        self.assertFalse(first.evidence["profile_changed"])
        self.assertFalse(first.evidence["readiness_claimed"])
        self.assertFalse(second.evidence["reusable_authority_created"])
        self.assertTrue(first.evidence["real_read_completed"])
        self.assertTrue(first.evidence["authn_ok"])
        self.assertTrue(first.evidence["authz_ok"])
        self.assertFalse(first.evidence["write_authority_created"])
        self.assertEvidence(first, "success")


if __name__ == "__main__":
    unittest.main()
