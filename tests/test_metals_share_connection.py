"""File-based numerical policy connection tests, with isolated share inputs."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

import yaml

from metals_scenario_policy import MetalPolicyError, MetalSteelPolicy, StaleMetalPolicyError, plan_database
from tests.test_metals_scenario_policy import configuration, inventory, market


class ShareConnectionTests(unittest.TestCase):
    def setUp(self):
        self.directory_context = tempfile.TemporaryDirectory()
        self.directory = Path(self.directory_context.name)
        self.addCleanup(self.directory_context.cleanup)
        self.policy_path = self.directory / "policy.yaml"
        self.input_path = self.directory / "source.yaml"
        self.selected_path = self.directory / "scenario.yaml"
        self.steel_path = self.directory / "steel.yaml"
        self.config = configuration()
        self.config["share_data"] = {
            "directory": ".",
            "nonferrous": {"reference": {"file": "source.yaml", "year": 2020},
                           "high_recycling": {"file": "scenario.yaml"}},
        }
        self.records = {"copper, cathode": {"name": "market for copper, cathode", "reference product": "copper, cathode",
                                            "shares": {"primary": {2020: 0.8, 2050: 0.75}, "secondary": {2020: 0.2, 2050: 0.25}}}}
        self.write(self.input_path, self.records)
        self.write(self.selected_path, self.records)
        self.write(self.steel_path, {"schema_version": 1, "products": {"steel": {"high_recycling": {2025: 0, 2050: 0.8}}}})
        self.write(self.policy_path, self.config)

    def write(self, path, value):
        path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")

    def selected(self, **kwargs):
        return MetalSteelPolicy.from_file(self.policy_path, **kwargs)

    def test_selected_file_replaces_inline_numbers_and_drives_plan(self):
        selected = self.selected(nonferrous_case="high_recycling")
        plan = plan_database(inventory(market()), selected, scenario={"year": 2050})
        self.assertAlmostEqual(plan.audit[0]["applied_secondary_share"], 0.25)
        self.assertEqual(plan.audit[0]["share_data_path"], str(self.selected_path.resolve()))
        self.assertEqual(plan.audit[0]["share_data_targets"], {2020: 0.2, 2050: 0.25})
        self.assertEqual(selected.describe()["share_data_sources"][0]["source_product"], "copper, cathode")
        self.assertNotIn(str(self.steel_path.resolve()), selected.source_hashes)

    def test_direct_selected_file_edit_changes_new_plan_and_invalidates_old_policy(self):
        old = self.selected(nonferrous_case="high_recycling")
        revised = deepcopy(self.records)
        revised["copper, cathode"]["shares"]["secondary"][2050] = 0.55
        revised["copper, cathode"]["shares"]["primary"][2050] = 0.45
        self.write(self.selected_path, revised)
        with self.assertRaises(StaleMetalPolicyError):
            old.verify_sources()
        new = self.selected(nonferrous_case="high_recycling")
        self.assertNotEqual(old.fingerprint, new.fingerprint)
        self.assertAlmostEqual(plan_database(inventory(market()), new, scenario={"year": 2050}).audit[0]["applied_secondary_share"], 0.55)

    def test_native_baseline_never_applies_generated_baseline_numbers(self):
        self.selected_path.unlink()
        self.input_path.unlink()
        self.steel_path.unlink()
        selected = self.selected()
        self.assertTrue(selected.baseline)
        self.assertEqual(set(selected.source_hashes), {str(self.policy_path.resolve())})
        self.assertEqual(plan_database(inventory(market()), selected, scenario={"year": 2050}).changes, ())

    def test_reference_reads_original_year_and_not_generated_future(self):
        revised = deepcopy(self.records)
        revised["copper, cathode"]["shares"]["secondary"][2020] = 0.17
        revised["copper, cathode"]["shares"]["primary"][2020] = 0.83
        self.write(self.input_path, revised)
        selected = self.selected(nonferrous_case="reference")
        plan = plan_database(inventory(market()), selected, scenario={"year": 2050})
        self.assertAlmostEqual(plan.audit[0]["applied_secondary_share"], 0.17)
        self.assertEqual(selected.describe()["share_data_sources"][0]["reference_year"], 2020)

    def test_missing_empty_wrong_grade_or_unbalanced_source_fails(self):
        variants = ({}, {"copper, cathode": {**self.records["copper, cathode"], "reference product": "copper, anode"}},
                    {"copper, cathode": {**self.records["copper, cathode"], "unit": "tonne"}},
                    {"copper, cathode": {**self.records["copper, cathode"], "shares": {"primary": {2020: 0.8}, "secondary": {2020: 0.3}}}},
                    {"copper, cathode": {**self.records["copper, cathode"], "shares": {"primary": {2020: 0.8}, "secondary": {2050: 0.2}}}})
        for document in variants:
            with self.subTest(document=document):
                self.write(self.selected_path, document)
                with self.assertRaises(MetalPolicyError):
                    self.selected(nonferrous_case="high_recycling")
        self.selected_path.unlink()
        with self.assertRaisesRegex(MetalPolicyError, "ensure_share_outputs"):
            self.selected(nonferrous_case="high_recycling")

    def test_legacy_steel_share_data_rejected_before_reading_any_graph(self):
        self.config["share_data"]["steel"] = {"file": "does-not-exist.yaml", "key": "steel"}
        self.write(self.policy_path, self.config)
        with self.assertRaisesRegex(MetalPolicyError, "endogenous to IAM/EDM-I"):
            self.selected()
        with self.assertRaisesRegex(MetalPolicyError, "endogenous to IAM/EDM-I"):
            self.selected(steel_case="high_recycling")

    def test_manifest_and_upstream_inputs_are_freshness_dependencies(self):
        digest = lambda path: sha256(path.read_bytes()).hexdigest()
        manifest = {"schema_version": 1, "generator_version": 1,
                    "input_sha256": {str(self.input_path.resolve()): digest(self.input_path)},
                    "output_sha256": {self.selected_path.name: digest(self.selected_path)}}
        manifest_path = self.directory / "share_generation_manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        selected = self.selected(nonferrous_case="high_recycling")
        self.assertIn(str(manifest_path.resolve()), selected.source_hashes)
        self.assertIn(str(self.input_path.resolve()), selected.source_hashes)
        self.assertTrue(selected.describe()["share_data_sources"][0]["matches_generated_manifest"])
        self.input_path.write_text(self.input_path.read_text() + "\n# revised\n", encoding="utf-8")
        with self.assertRaises(StaleMetalPolicyError):
            selected.verify_sources()
        with self.assertRaisesRegex(StaleMetalPolicyError, "ensure_share_outputs"):
            self.selected(nonferrous_case="high_recycling")

    def test_manual_selected_output_can_be_read_with_manifest_receipt(self):
        manifest = {"schema_version": 1, "input_sha256": {}, "output_sha256": {"scenario.yaml": "earlier-generation"}}
        (self.directory / "share_generation_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        selected = self.selected(nonferrous_case="high_recycling")
        self.assertFalse(selected.describe()["share_data_sources"][0]["matches_generated_manifest"])
        self.assertEqual(selected.rules[0]["targets"]["high_recycling"], self.records["copper, cathode"]["shares"]["secondary"])

    def test_manifest_input_paths_are_resolved_portably(self):
        manifest = {"schema_version": 1, "input_sha256": {"source.yaml": sha256(self.input_path.read_bytes()).hexdigest()},
                    "output_sha256": {"scenario.yaml": sha256(self.selected_path.read_bytes()).hexdigest()}}
        (self.directory / "share_generation_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        selected = self.selected(nonferrous_case="high_recycling")
        self.assertIn(str(self.input_path.resolve()), selected.source_hashes)

    def test_only_supported_configured_material_subset_loaded(self):
        self.records["copper ore"] = {"name": "market for copper ore", "reference product": "copper ore", "shares": "not interpreted"}
        self.write(self.selected_path, self.records)
        selected = self.selected(nonferrous_case="high_recycling")
        self.assertEqual(len(selected.describe()["share_data_sources"]), 1)

    def test_duplicate_source_keys_never_silently_overwrite_assumptions(self):
        documents = (
            "copper, cathode: {}\ncopper, cathode: {}\n",
            "copper, cathode:\n  name: market for copper, cathode\n  reference product: copper, cathode\n  shares:\n    primary: {2020: 0.8, 2050: 0.75}\n    secondary: {2020: 0.2, 2050: 0.25, 2050: 0.7}\n",
        )
        for document in documents:
            with self.subTest(document=document):
                self.selected_path.write_text(document, encoding="utf-8")
                with self.assertRaisesRegex(MetalPolicyError, "duplicate key"):
                    self.selected(nonferrous_case="high_recycling")
        self.policy_path.write_text("schema_version: 1\nschema_version: 2\n", encoding="utf-8")
        with self.assertRaisesRegex(MetalPolicyError, "duplicate key"):
            self.selected()

    def test_source_rounding_tolerance_matches_generator(self):
        revised = deepcopy(self.records)
        revised["copper, cathode"]["shares"]["primary"][2050] = 0.7499995
        self.write(self.selected_path, revised)
        self.assertAlmostEqual(self.selected(nonferrous_case="high_recycling").rules[0]["targets"]["high_recycling"][2050], 0.25)


if __name__ == "__main__":
    unittest.main()
