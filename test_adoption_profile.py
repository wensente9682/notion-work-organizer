import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from adopt_inspector import AdoptionReport, CompatibilityCheck, inspect_existing_system
from adoption_profile import (
    ProfileError,
    freeze_inspection_evidence,
    persist_approved_adoption_profile,
    propose_adoption_profile,
)
from test_adopt_inspector import (
    FakeReader,
    ready_source,
    request as inspector_request,
)


def ready_report(profile=None):
    return inspect_existing_system(
        FakeReader(),
        inspector_request(profile or private_profile()),
    )


def partial_report(profile=None):
    source = ready_source()
    source["properties"].pop("Next time")
    return inspect_existing_system(
        FakeReader(source=source),
        inspector_request(profile or private_profile()),
    )


def private_profile():
    return {
        "mode": "real",
        "source_database_id": "source-reference",
        "archive_tables_page_id": "archive-container-reference",
        "field_mapping": {
            "task": "Work",
            "done": "Complete",
            "category": "Area",
            "takeaway": "Learning",
            "improvement": "Next time",
        },
        "archive_tables": {"research": "research"},
        "total": {
            "view_id": "ordered-view-reference",
            "fields": {
                "done": "Complete",
                "categories": "Area",
                "timeboxing": "Blocks",
                "date_anchor": "Date marker",
            },
        },
    }


def evidence_for(
    profile=None,
    *,
    report=None,
    inspected_at=90,
    accept_partial=False,
):
    inspected = profile or private_profile()
    confirmed = json.loads(json.dumps(inspected))
    return freeze_inspection_evidence(
        report or ready_report(inspected),
        confirmed,
        inspected_at=inspected_at,
        accept_partial=accept_partial,
    )


class AdoptionProfileTests(unittest.TestCase):
    def test_inspector_report_cannot_be_rebound_to_another_valid_profile(self):
        report = inspect_existing_system(FakeReader(), inspector_request())
        first = private_profile()
        second = private_profile()
        second["source_database_id"] = "different-source-reference"

        freeze_inspection_evidence(
            report,
            first,
            inspected_at=90,
        )
        with self.assertRaises(ProfileError):
            freeze_inspection_evidence(
                report,
                second,
                inspected_at=90,
            )

    def test_ready_report_cannot_authorize_different_valid_bindings(self):
        inspected = private_profile()
        confirmed = private_profile()
        evidence = freeze_inspection_evidence(
            ready_report(inspected),
            confirmed,
            inspected_at=90,
        )
        different = private_profile()
        different["source_database_id"] = "different-source-reference"

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / ".todo_archive" / "real_profile.json"
            with self.assertRaises(ProfileError):
                propose_adoption_profile(
                    evidence,
                    different,
                    destination,
                    now=lambda: 100,
                    ignore_checker=lambda path: True,
                )
            self.assertFalse(destination.parent.exists())

    def test_parent_directory_identity_change_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repository"
            root.mkdir()
            destination = root / ".todo_archive" / "real_profile.json"
            destination.parent.mkdir(mode=0o700)
            proposal = propose_adoption_profile(
                evidence_for(),
                private_profile(),
                destination,
                now=lambda: 100,
                ignore_checker=lambda path: True,
            )
            destination.parent.rename(root / ".todo_archive-original")
            destination.parent.mkdir(mode=0o700)
            sentinel = destination.parent / "sentinel"
            sentinel.write_text("unchanged", encoding="utf-8")

            with self.assertRaises(ProfileError):
                persist_approved_adoption_profile(
                    proposal,
                    proposal.approval_phrase,
                    now=lambda: 101,
                )
            self.assertFalse(destination.exists())
            self.assertEqual("unchanged", sentinel.read_text(encoding="utf-8"))

    def test_exact_approval_atomically_writes_private_profile_with_restrictive_permissions(self):
        profile = private_profile()
        private_values = tuple(
            value
            for value in (
                profile["source_database_id"],
                profile["archive_tables_page_id"],
                profile["total"]["view_id"],
                *profile["field_mapping"].values(),
                *profile["archive_tables"].keys(),
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / ".todo_archive" / "real_profile.json"
            proposal = propose_adoption_profile(
                evidence_for(profile),
                profile,
                destination,
                now=lambda: 100,
                ignore_checker=lambda path: True,
            )

            for private in (*private_values, str(destination)):
                self.assertNotIn(private, proposal.summary)
                self.assertNotIn(private, proposal.approval_phrase)

            result = persist_approved_adoption_profile(
                proposal,
                proposal.approval_phrase,
                now=lambda: 101,
            )

            self.assertEqual("ready", result.status)
            self.assertNotIn(str(destination), result.summary)
            self.assertEqual(profile, json.loads(destination.read_text(encoding="utf-8")))
            self.assertEqual(0o700, destination.parent.stat().st_mode & 0o777)
            self.assertEqual(0o600, destination.stat().st_mode & 0o777)
            self.assertEqual([], [name for name in os.listdir(destination.parent) if name != destination.name])

    def test_rejection_stale_evidence_and_expired_approval_do_not_write(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / ".todo_archive" / "real_profile.json"
            stale = evidence_for(inspected_at=0)
            with self.assertRaises(ProfileError):
                propose_adoption_profile(
                    stale,
                    private_profile(),
                    destination,
                    now=lambda: 1000,
                    ignore_checker=lambda path: True,
                )
            self.assertFalse(destination.parent.exists())

            proposal = propose_adoption_profile(
                evidence_for(),
                private_profile(),
                destination,
                now=lambda: 100,
                ignore_checker=lambda path: True,
            )
            with self.assertRaises(ProfileError):
                persist_approved_adoption_profile(
                    proposal,
                    "reject",
                    now=lambda: 101,
                )
            self.assertFalse(destination.exists())
            with self.assertRaises(ProfileError):
                persist_approved_adoption_profile(
                    proposal,
                    proposal.approval_phrase,
                    now=lambda: 1000,
                )
            self.assertFalse(destination.exists())

    def test_partial_report_requires_explicit_acceptance_and_incompatible_never_proceeds(self):
        incompatible = AdoptionReport(
            status="not-ready",
            checks=(CompatibilityCheck("view.order", "incompatible", "stop"),),
            next_action="stop",
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / ".todo_archive" / "real_profile.json"
            for report, accept_partial in (
                (partial_report(), False),
                (incompatible, True),
            ):
                with self.subTest(report=report, accept_partial=accept_partial):
                    with self.assertRaises(ProfileError):
                        evidence_for(
                            report=report,
                            accept_partial=accept_partial,
                        )

            partial_evidence = evidence_for(
                report=partial_report(),
                accept_partial=True,
            )
            proposal = propose_adoption_profile(
                partial_evidence,
                private_profile(),
                destination,
                now=lambda: 100,
                ignore_checker=lambda path: True,
            )
            self.assertIn("accepted partial", proposal.summary)

    def test_external_conflict_and_atomic_replace_failure_preserve_existing_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / ".todo_archive" / "real_profile.json"
            destination.parent.mkdir(mode=0o700)
            destination.write_text('{"existing":"first"}\n', encoding="utf-8")
            destination.chmod(0o600)
            proposal = propose_adoption_profile(
                evidence_for(),
                private_profile(),
                destination,
                now=lambda: 100,
                ignore_checker=lambda path: True,
            )
            destination.write_text('{"external":"change"}\n', encoding="utf-8")

            with self.assertRaises(ProfileError):
                persist_approved_adoption_profile(
                    proposal,
                    proposal.approval_phrase,
                    now=lambda: 101,
                )
            self.assertEqual('{"external":"change"}\n', destination.read_text(encoding="utf-8"))

            second = propose_adoption_profile(
                evidence_for(),
                private_profile(),
                destination,
                now=lambda: 100,
                ignore_checker=lambda path: True,
            )
            with (
                patch("adoption_profile.os.replace", side_effect=OSError),
                self.assertRaises(ProfileError),
            ):
                persist_approved_adoption_profile(
                    second,
                    second.approval_phrase,
                    now=lambda: 101,
                )
            self.assertEqual('{"external":"change"}\n', destination.read_text(encoding="utf-8"))
            self.assertEqual([destination.name], os.listdir(destination.parent))

    def test_proposal_is_single_use_and_binds_frozen_content_and_ignore_state(self):
        profile = private_profile()
        checks = iter((True, True))
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / ".todo_archive" / "real_profile.json"
            proposal = propose_adoption_profile(
                evidence_for(profile),
                profile,
                destination,
                now=lambda: 100,
                ignore_checker=lambda path: next(checks),
            )
            profile["source_database_id"] = "changed-after-proposal"
            persist_approved_adoption_profile(
                proposal,
                proposal.approval_phrase,
                now=lambda: 101,
            )
            saved = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual("source-reference", saved["source_database_id"])
            with self.assertRaises(ProfileError):
                persist_approved_adoption_profile(
                    proposal,
                    proposal.approval_phrase,
                    now=lambda: 102,
                )

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / ".todo_archive" / "real_profile.json"
            checks = iter((True, False))
            proposal = propose_adoption_profile(
                evidence_for(),
                private_profile(),
                destination,
                now=lambda: 100,
                ignore_checker=lambda path: next(checks),
            )
            with self.assertRaises(ProfileError):
                persist_approved_adoption_profile(
                    proposal,
                    proposal.approval_phrase,
                    now=lambda: 101,
                )
            self.assertFalse(destination.exists())

    def test_approval_rejects_proposal_state_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / ".todo_archive" / "real_profile.json"
            proposal = propose_adoption_profile(
                evidence_for(),
                private_profile(),
                destination,
                now=lambda: 100,
                ignore_checker=lambda path: True,
            )
            proposal._content = b'{"changed":"after approval"}\n'

            with self.assertRaises(ProfileError):
                persist_approved_adoption_profile(
                    proposal,
                    proposal.approval_phrase,
                    now=lambda: 101,
                )
            self.assertFalse(destination.exists())

    def test_ignore_check_failure_is_sanitized(self):
        def unavailable(path):
            raise RuntimeError(f"private path: {path}")

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / ".todo_archive" / "real_profile.json"
            with self.assertRaises(ProfileError) as raised:
                propose_adoption_profile(
                    evidence_for(),
                    private_profile(),
                    destination,
                    now=lambda: 100,
                    ignore_checker=unavailable,
                )
            self.assertEqual(
                "private adoption profile operation failed safely",
                str(raised.exception),
            )
            self.assertNotIn(str(destination), str(raised.exception))


if __name__ == "__main__":
    unittest.main()
