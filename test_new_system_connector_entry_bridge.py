"""Synthetic contract tests for connector-entry acknowledgement."""

import copy
import io
import json
import threading
import unittest

from new_system_connector_entry_bridge import ConnectorEntryBridge, ConnectorEntryWireBridge


DIGEST = "a" * 64
ATTEMPT = "b" * 32
CANARY = "private-connector-value"


class ConnectorEntryBridgeTests(unittest.TestCase):
    def bridge(self):
        return ConnectorEntryBridge(DIGEST, ATTEMPT)

    def test_eof_before_handoff_or_ack_is_truthful_not_invoked(self):
        untouched = self.bridge()
        self.assertEqual(
            {"status": "closed", "entry": "not-invoked", "attempted": False, "outcome": "not-invoked"},
            untouched.close().to_public_dict(),
        )
        after_handoff = self.bridge()
        after_handoff.handoff(DIGEST, ATTEMPT)
        self.assertFalse(after_handoff.close().to_public_dict()["attempted"])

    def test_ack_is_not_entry_and_lost_ack_cannot_claim_attempt(self):
        subject = self.bridge()
        handoff = subject.handoff(DIGEST, ATTEMPT)
        subject.acknowledge(handoff)
        self.assertEqual("not-invoked", subject.close().to_public_dict()["entry"])

    def test_exact_ack_then_connector_return_or_exception_records_entry(self):
        for returned, expected in (("success", "success"), ("partial", "partial"), (RuntimeError(CANARY), "unknown")):
            with self.subTest(expected=expected):
                subject = self.bridge()
                ack = subject.acknowledge(subject.handoff(DIGEST, ATTEMPT))
                calls = []
                def invoke(_request):
                    calls.append("entered")
                    if isinstance(returned, BaseException):
                        raise returned
                    return returned
                result = subject.invoke(ack, invoke, {})
                self.assertEqual(["entered"], calls)
                self.assertEqual(True, result.to_public_dict()["attempted"])
                self.assertEqual(expected, result.to_public_dict()["outcome"])

    def test_wrong_duplicate_late_dynamic_and_cross_bridge_capabilities_fail_closed(self):
        first = self.bridge(); second = self.bridge()
        handoff = first.handoff(DIGEST, ATTEMPT)
        self.assertIsNone(first.handoff(DIGEST, ATTEMPT))
        self.assertIsNone(first.handoff("c" * 64, ATTEMPT))
        self.assertIsNone(second.acknowledge(handoff))
        ack = first.acknowledge(handoff)
        self.assertIsNone(first.acknowledge(handoff))
        self.assertIsNone(first.invoke(object(), lambda _: "success", {}))
        self.assertEqual("not-invoked", first.close().to_public_dict()["entry"])
        self.assertIsNone(first.invoke(ack, lambda _: "success", {}))

    def test_concurrent_or_reentrant_invoke_has_at_most_one_connector_entry(self):
        subject = self.bridge(); ack = subject.acknowledge(subject.handoff(DIGEST, ATTEMPT))
        entered = threading.Event(); release = threading.Event(); calls = []
        def invoke(_request):
            calls.append("entered"); entered.set(); release.wait(1); return "success"
        results = []
        worker = threading.Thread(target=lambda: results.append(subject.invoke(ack, invoke, {})))
        worker.start(); self.assertTrue(entered.wait(1))
        self.assertIsNone(subject.invoke(ack, invoke, {}))
        release.set(); worker.join(1)
        self.assertEqual(["entered"], calls)
        self.assertTrue(results[0].to_public_dict()["attempted"])

    def test_close_after_entry_but_before_result_is_attempted_unknown(self):
        subject = self.bridge(); ack = subject.acknowledge(subject.handoff(DIGEST, ATTEMPT))
        entered = threading.Event(); release = threading.Event()
        def invoke(_request): entered.set(); release.wait(1); return "success"
        completed = []
        worker = threading.Thread(target=lambda: completed.append(subject.invoke(ack, invoke, {})))
        worker.start(); self.assertTrue(entered.wait(1))
        interim = subject.close().to_public_dict()
        self.assertTrue(interim["attempted"])
        self.assertEqual("unknown", interim["outcome"])
        release.set(); worker.join(1)
        self.assertEqual("unknown", completed[0].to_public_dict()["outcome"])

    def test_binding_and_resource_boundaries_reject_without_entry(self):
        invalid = (
            ("a" * 63, ATTEMPT), ("a" * 65, ATTEMPT),
            (DIGEST, "b" * 31), (DIGEST, "b" * 33),
            ("A" * 64, ATTEMPT), (DIGEST, "g" * 32),
        )
        for digest, attempt in invalid:
            with self.subTest(digest=len(digest), attempt=len(attempt)):
                with self.assertRaises(ValueError): ConnectorEntryBridge(digest, attempt)

        for changed in (("c" * 64, ATTEMPT), (DIGEST, "d" * 32)):
            with self.subTest(changed=changed[0] != DIGEST):
                subject = self.bridge()
                self.assertIsNone(subject.handoff(*changed))
                self.assertFalse(subject.close().to_public_dict()["attempted"])

    def test_capability_is_not_copyable_and_public_result_is_sanitized(self):
        subject = self.bridge()
        with self.assertRaises(TypeError): copy.copy(subject)
        public = json.dumps(subject.close().to_public_dict(), sort_keys=True)
        self.assertNotIn(DIGEST, public); self.assertNotIn(ATTEMPT, public); self.assertNotIn(CANARY, public)


class ConnectorEntryWireBridgeTests(unittest.TestCase):
    class TrapStream:
        def __getattr__(self, _name):
            raise AssertionError("unsupported wire path must not touch streams")

    def test_preloaded_success_transcript_has_zero_authority_and_zero_attempt(self):
        transcript = b'{"type":"connector-entry-ack","outcome":"success"}\n'
        subject = ConnectorEntryWireBridge(io.BytesIO(transcript), io.BytesIO())
        result = subject.execute({}, DIGEST, ATTEMPT).to_public_dict()
        self.assertEqual((False, "unknown", "bridge-unsupported"),
                         (result["attempted"], result["entry"], result["outcome"]))

    def test_unsupported_wire_never_reads_writes_or_waits(self):
        subject = ConnectorEntryWireBridge(self.TrapStream(), self.TrapStream())
        result = subject.execute({}, DIGEST, ATTEMPT).to_public_dict()
        self.assertFalse(result["attempted"])
        self.assertEqual("bridge-unsupported", result["outcome"])

    def test_wire_capability_is_single_use_noncopyable_and_sanitized(self):
        subject = ConnectorEntryWireBridge(self.TrapStream(), self.TrapStream())
        with self.assertRaises(TypeError): copy.copy(subject)
        first = subject.execute({}, DIGEST, ATTEMPT).to_public_dict()
        second = subject.execute({}, DIGEST, ATTEMPT).to_public_dict()
        self.assertEqual("unknown", first["entry"])
        self.assertEqual("unknown", second["entry"])
        public = json.dumps(first) + json.dumps(second)
        self.assertNotIn(DIGEST, public); self.assertNotIn(ATTEMPT, public)


if __name__ == "__main__":
    unittest.main()
