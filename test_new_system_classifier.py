import json
import unittest
from dataclasses import asdict

from new_system_blueprint import canonical_blueprint
from new_system_classifier import classify_new_system


class NewSystemClassifierTests(unittest.TestCase):
    def canonical(self):
        return canonical_blueprint(
            archive_container_id="archive-container",
            archive_databases={"Research": "archive-research"},
        )

    def test_empty_snapshot_is_blank(self):
        result = classify_new_system({})

        self.assertEqual("blank", result.status)
        self.assertEqual((), result.evidence)

    def test_canonical_snapshot_is_ready_to_preview(self):
        result = classify_new_system(self.canonical())

        self.assertEqual("ready-to-preview", result.status)
        self.assertEqual((), result.evidence)

    def test_missing_source_field_is_partially_present(self):
        snapshot = self.canonical()
        del snapshot["source"]["properties"]["Takeaway"]

        result = classify_new_system(snapshot)

        self.assertEqual("partially present", result.status)
        self.assertEqual("missing", result.evidence[0].kind)
        self.assertEqual("source.property-type", result.evidence[0].code)
        self.assertEqual("source.properties.Takeaway", result.evidence[0].path)
        self.assertEqual(
            "add required source property",
            result.evidence[0].remediation,
        )

    def test_missing_category_archive_is_partially_present(self):
        snapshot = self.canonical()
        del snapshot["archives"]["Research"]

        result = classify_new_system(snapshot)

        self.assertEqual("partially present", result.status)
        self.assertEqual("missing", result.evidence[0].kind)
        self.assertEqual("archive.missing", result.evidence[0].code)
        self.assertEqual("archives[*]", result.evidence[0].path)
        self.assertEqual(
            "add required category archive",
            result.evidence[0].remediation,
        )

    def test_wrong_sort_is_incompatible(self):
        snapshot = self.canonical()
        snapshot["view"]["sort"]["direction"] = "ascending"

        result = classify_new_system(snapshot)

        self.assertEqual("incompatible", result.status)
        self.assertEqual("incompatible", result.evidence[0].kind)
        self.assertEqual("view.sort", result.evidence[0].code)
        self.assertEqual("view.sort.direction", result.evidence[0].path)
        self.assertEqual(
            "restore canonical view sort",
            result.evidence[0].remediation,
        )

    def test_non_child_archive_is_incompatible(self):
        snapshot = self.canonical()
        snapshot["archives"]["Research"]["parent_id"] = "other-parent"

        result = classify_new_system(snapshot)

        self.assertEqual("incompatible", result.status)
        self.assertEqual("archive.parent", result.evidence[0].code)
        self.assertEqual("archives[*].parent_id", result.evidence[0].path)

    def test_incompatible_source_and_archive_schema_are_incompatible(self):
        cases = (
            ("source", "source.property-type", "source.properties.Category"),
            ("archive", "archive.schema", "archives[*].properties"),
        )
        for scope, code, path in cases:
            with self.subTest(scope=scope):
                snapshot = self.canonical()
                if scope == "source":
                    snapshot["source"]["properties"]["Category"] = "status"
                else:
                    snapshot["archives"]["Research"]["properties"]["Takeaway"] = (
                        "number"
                    )

                result = classify_new_system(snapshot)

                self.assertEqual("incompatible", result.status)
                self.assertEqual(code, result.evidence[0].code)
                self.assertEqual(path, result.evidence[0].path)

    def test_wrong_category_binding_is_incompatible(self):
        snapshot = self.canonical()
        snapshot["archives"]["Research"]["category"] = "private-category"

        result = classify_new_system(snapshot)

        self.assertEqual("incompatible", result.status)
        self.assertEqual("archive.category", result.evidence[0].code)
        self.assertEqual("archives[*].category", result.evidence[0].path)

    def test_unrelated_resource_fails_closed(self):
        result = classify_new_system(
            {"unrelated_resources": [{"id": "private-unrelated-id"}]}
        )

        self.assertEqual("incompatible", result.status)
        self.assertEqual("discovery.unrelated", result.evidence[0].code)
        self.assertEqual("discovery.unrelated_resources", result.evidence[0].path)

    def test_ambiguous_candidate_fails_closed(self):
        result = classify_new_system(
            {"ambiguous_candidates": [{"id": "private-candidate-id"}]}
        )

        self.assertEqual("incompatible", result.status)
        self.assertEqual("discovery.ambiguous", result.evidence[0].code)
        self.assertEqual("discovery.ambiguous_candidates", result.evidence[0].path)

    def test_mixed_missing_and_incompatible_is_incompatible(self):
        snapshot = self.canonical()
        del snapshot["source"]["properties"]["Takeaway"]
        snapshot["view"]["sort"]["direction"] = "ascending"

        result = classify_new_system(snapshot)

        self.assertEqual("incompatible", result.status)
        self.assertEqual(
            {"missing", "incompatible"},
            {item.kind for item in result.evidence},
        )

    def test_public_result_is_sanitized(self):
        canaries = (
            "PRIVATE_DATABASE_ID",
            "https://notion.invalid/private-page",
            "PRIVATE_RAW_TASK_TEXT",
            "PRIVATE_CATEGORY",
        )
        snapshot = canonical_blueprint(
            archive_container_id=canaries[0],
            archive_databases={canaries[3]: "PRIVATE_ARCHIVE_ID"},
        )
        snapshot["archives"][canaries[3]]["parent_id"] = canaries[1]
        snapshot["raw_page"] = {
            "url": canaries[1],
            "task": canaries[2],
        }

        result = classify_new_system(snapshot)
        public_output = repr(result) + json.dumps(asdict(result), sort_keys=True)

        self.assertEqual("incompatible", result.status)
        for canary in (*canaries, "PRIVATE_ARCHIVE_ID"):
            self.assertNotIn(canary, public_output)
        self.assertNotIn("actual", public_output)
        self.assertNotIn("expected", public_output)

    def test_undeclared_top_level_resource_fails_closed(self):
        for snapshot in (
            {**self.canonical(), "raw_page": {"task": "PRIVATE_TASK"}},
            {"raw_page": {"task": "PRIVATE_TASK"}},
        ):
            with self.subTest(canonical="source" in snapshot):
                result = classify_new_system(snapshot)

                self.assertEqual("incompatible", result.status)
                self.assertEqual("snapshot.undeclared", result.evidence[0].code)
                self.assertEqual("snapshot[*]", result.evidence[0].path)
                self.assertEqual(
                    "remove undeclared snapshot resource",
                    result.evidence[0].remediation,
                )

    def test_canonical_resources_must_be_singular_mappings(self):
        for key in ("source", "view", "archive_container", "archives"):
            with self.subTest(key=key):
                snapshot = self.canonical()
                snapshot[key] = [snapshot[key]]

                result = classify_new_system(snapshot)

                self.assertEqual("incompatible", result.status)
                self.assertEqual("snapshot.resource-shape", result.evidence[0].code)
                self.assertEqual(f"snapshot.{key}", result.evidence[0].path)
                self.assertEqual(
                    "provide one mapping for canonical resource",
                    result.evidence[0].remediation,
                )

    def test_discovery_metadata_rejects_every_non_sequence_shape(self):
        for key in ("unrelated_resources", "ambiguous_candidates"):
            for value in ({}, "", None):
                with self.subTest(key=key, value=value):
                    snapshot = self.canonical()
                    snapshot[key] = value

                    result = classify_new_system(snapshot)

                    self.assertEqual("incompatible", result.status)
                    self.assertEqual(
                        "discovery.metadata-shape",
                        result.evidence[0].code,
                    )
                    self.assertEqual(f"discovery.{key}", result.evidence[0].path)

    def test_discovery_sequences_are_shape_checked_without_truthiness(self):
        for empty in ([], ()):
            with self.subTest(empty=type(empty).__name__):
                snapshot = self.canonical()
                snapshot["unrelated_resources"] = empty
                snapshot["ambiguous_candidates"] = empty

                result = classify_new_system(snapshot)

                self.assertEqual("ready-to-preview", result.status)

        snapshot = self.canonical()
        snapshot["ambiguous_candidates"] = [{"id": "PRIVATE_ID"}]
        del snapshot["source"]["properties"]["Takeaway"]

        result = classify_new_system(snapshot)

        self.assertEqual("incompatible", result.status)
        self.assertEqual("discovery.ambiguous", result.evidence[0].code)

    def test_nested_containers_must_be_mappings_when_present(self):
        cases = (
            (
                "source-properties",
                lambda snapshot: snapshot["source"].__setitem__("properties", []),
                "snapshot.source.properties",
            ),
            (
                "view-sort",
                lambda snapshot: snapshot["view"].__setitem__("sort", []),
                "snapshot.view.sort",
            ),
            (
                "archive-child",
                lambda snapshot: snapshot["archives"].__setitem__("Research", []),
                "snapshot.archives[*]",
            ),
            (
                "archive-properties",
                lambda snapshot: snapshot["archives"]["Research"].__setitem__(
                    "properties", []
                ),
                "snapshot.archives[*].properties",
            ),
        )
        for name, mutate, path in cases:
            with self.subTest(name=name):
                snapshot = self.canonical()
                mutate(snapshot)

                result = classify_new_system(snapshot)

                self.assertEqual("incompatible", result.status)
                self.assertEqual("snapshot.nested-shape", result.evidence[0].code)
                self.assertEqual(path, result.evidence[0].path)
                self.assertEqual(
                    "provide one mapping for nested canonical resource",
                    result.evidence[0].remediation,
                )

    def test_missing_nested_containers_remain_missing(self):
        cases = (
            lambda snapshot: snapshot["source"].__delitem__("properties"),
            lambda snapshot: snapshot["view"].__delitem__("sort"),
            lambda snapshot: snapshot["archives"]["Research"].__delitem__(
                "properties"
            ),
        )
        for mutate in cases:
            with self.subTest(mutate=mutate):
                snapshot = self.canonical()
                mutate(snapshot)

                result = classify_new_system(snapshot)

                self.assertEqual("partially present", result.status)
                self.assertEqual(
                    {"missing"},
                    {item.kind for item in result.evidence},
                )


if __name__ == "__main__":
    unittest.main()
