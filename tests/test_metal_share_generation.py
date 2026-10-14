"""Share generation/source freshness fixtures; no installed-package writes."""
from copy import deepcopy
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import csv
import hashlib
import importlib
import io
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch
import yaml

from primary_vs_secondary_metals import fill_primary_vs_secondary_shares as generator
from primary_vs_secondary_metals import metals_primary_vs_secondary as extractor
from primary_vs_secondary_metals import share_files
from primary_vs_secondary_metals.share_files import (
    DATA_DIR, SCENARIO_FILES, ShareDataError, ShareDataConflictError,
    ensure_share_outputs, prepare_policy_share_data, load_yaml, validate_share_mapping,
)


def baseline_data():
    return {"copper": {"name": "market for copper", "reference product": "copper",
                        "shares": {"primary": {2020: .8, 2030: .7, 2050: .6},
                                   "secondary": {2020: .2, 2030: .3, 2050: .4}}},
            "ore": {"name": "market for ore", "reference product": "ore",
                     "shares": {"primary": {2020: 1., 2050: 1.}, "secondary": {2020: 0., 2050: 0.}}}}


class ShareGenerationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.baseline = self.directory / "metal_primary_secondary_shares.yaml"
        self.evidence = self.directory / "metal_2050_literature_evidence.csv"
        self.mapping = self.directory / "metal_product_family_mapping.csv"
        self.baseline.write_text(yaml.safe_dump(baseline_data(), sort_keys=False), encoding="utf-8")
        self.evidence.write_text("family,secondary_share_2050_conservative,secondary_share_2050_baseline,secondary_share_2050_optimistic,basis_type,confidence,source_short,source_url,notes\ncopper,0.3,0.5,0.8,direct,medium,source,url,note\nstructural,0,0,0,structural,high,source,,note\n", encoding="utf-8")
        self.mapping.write_text("product,family\ncopper,copper\nore,structural\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def ensure(self, **kwargs):
        return ensure_share_outputs(baseline_yaml=self.baseline, evidence_csv=self.evidence,
                                    mapping_csv=self.mapping, output_dir=self.directory, **kwargs)

    def test_three_named_files_alias_and_combined_are_correct_and_inputs_preserved(self):
        input_hashes = {path: path.read_bytes() for path in (self.baseline, self.evidence, self.mapping)}
        summary = self.ensure()
        self.assertEqual(summary["status"], "rebuilt")
        self.assertEqual(summary["product_count"], 2)
        expected = {"conservative": .3, "baseline": .5, "optimistic": .8}
        for case, amount in expected.items():
            data = load_yaml(summary["output_files"][case])
            self.assertEqual(data["copper"]["shares"]["secondary"][2050], amount)
            self.assertEqual(data["copper"]["shares"]["secondary"][2030], .3)
            self.assertEqual(data["ore"]["shares"]["secondary"][2050], 0)
            for entry in data.values():
                for year in entry["shares"]["primary"]:
                    self.assertAlmostEqual(entry["shares"]["primary"][year] + entry["shares"]["secondary"][year], 1)
        self.assertEqual(load_yaml(summary["baseline_alias"]), load_yaml(summary["output_files"]["baseline"]))
        combined = load_yaml(summary["combined_file"])
        self.assertEqual(set(combined["products"]), set(expected))
        for path, original in input_hashes.items():
            self.assertEqual(path.read_bytes(), original)
        manifest = json.loads(Path(summary["manifest_path"]).read_text())
        self.assertEqual(set(manifest["input_sha256"]), {self.baseline.name, self.evidence.name, self.mapping.name})
        for filename, expected_hash in manifest["output_sha256"].items():
            self.assertEqual(hashlib.sha256((self.directory / filename).read_bytes()).hexdigest(), expected_hash)

    def test_fresh_reuse_does_not_rewrite_any_output(self):
        first = self.ensure()
        times = {path: path.stat().st_mtime_ns for path in self.directory.iterdir()}
        second = self.ensure()
        self.assertEqual(second["status"], "reused")
        self.assertEqual(times, {path: path.stat().st_mtime_ns for path in self.directory.iterdir()})
        self.assertEqual(first["output_sha256"], second["output_sha256"])

    def test_missing_and_empty_files_rebuild_even_when_manifest_is_current(self):
        self.ensure()
        (self.directory / SCENARIO_FILES["conservative"]).unlink()
        (self.directory / SCENARIO_FILES["optimistic"]).write_text("{}\n")
        summary = self.ensure()
        self.assertEqual(len(summary["rebuilt_outputs"]), 2)
        self.assertEqual(len(load_yaml(self.directory / SCENARIO_FILES["optimistic"])), 2)

    def test_upstream_change_rebuilds_without_manual_edits(self):
        self.ensure()
        self.evidence.write_text(self.evidence.read_text().replace("copper,0.3,0.5,0.8", "copper,0.4,0.6,0.9"))
        summary = self.ensure()
        self.assertEqual(summary["status"], "rebuilt")
        self.assertEqual(load_yaml(summary["output_files"]["baseline"])["copper"]["shares"]["secondary"][2050], .6)

    def test_valid_manual_output_preserved_and_conflicts_after_upstream_change(self):
        first = self.ensure()
        case_file = Path(first["output_files"]["baseline"])
        edited = load_yaml(case_file)
        edited["copper"]["shares"]["secondary"][2050] = .65
        edited["copper"]["shares"]["primary"][2050] = .35
        case_file.write_text(yaml.safe_dump(edited, sort_keys=False))
        original = case_file.read_bytes()
        summary = self.ensure()
        self.assertEqual(summary["status"], "preserved_manual_outputs")
        self.assertEqual(case_file.read_bytes(), original)
        self.assertEqual(load_yaml(summary["baseline_alias"]), edited)
        self.assertEqual(load_yaml(summary["combined_file"])["products"]["baseline"], edited)
        self.assertEqual(self.ensure()["manual_outputs"], [case_file.name])
        self.evidence.write_text(self.evidence.read_text() + "\n")
        with self.assertRaisesRegex(ShareDataConflictError, "inputs changed"):
            self.ensure()
        self.assertEqual(case_file.read_bytes(), original)
        forced = self.ensure(force=True)
        self.assertEqual(forced["manual_outputs"], [])
        self.assertEqual(load_yaml(case_file)["copper"]["shares"]["secondary"][2050], .5)

    def test_no_manifest_migration_fills_legacy_empty_scenarios(self):
        for filename in SCENARIO_FILES.values():
            (self.directory / filename).write_text("{}\n")
        summary = self.ensure()
        self.assertEqual(len(summary["rebuilt_outputs"]), 3)

    def test_manual_identity_changes_are_not_accepted(self):
        first = self.ensure()
        path = Path(first["output_files"]["baseline"])
        data = load_yaml(path)
        data["copper"]["name"] = "wrong provider"
        path.write_text(yaml.safe_dump(data))
        with self.assertRaisesRegex(ShareDataError, "identity differs"):
            self.ensure()

    def test_changed_baseline_identity_regenerates_unedited_outputs(self):
        self.ensure()
        data = load_yaml(self.baseline)
        data["copper"]["name"] = "new market for copper"
        self.baseline.write_text(yaml.safe_dump(data))
        summary = self.ensure()
        self.assertEqual(load_yaml(summary["output_files"]["baseline"])["copper"]["name"], "new market for copper")

    def test_duplicate_and_missing_mapping_products_fail_without_fallback(self):
        original = self.mapping.read_text()
        for text in (original + "copper,copper\n", original.replace("copper,copper\n", ""), original.replace("copper,copper", "copper,unknown")):
            with self.subTest(text=text):
                self.mapping.write_text(text)
                with self.assertRaises(ShareDataError):
                    self.ensure()

    def test_duplicate_families_and_missing_evidence_columns_fail(self):
        original = self.evidence.read_text()
        self.evidence.write_text(original + original.splitlines()[1] + "\n")
        with self.assertRaisesRegex(ShareDataError, "duplicate family"):
            self.ensure()
        self.evidence.write_text("family,secondary_share_2050_baseline\ncopper,0.5\n")
        with self.assertRaisesRegex(ShareDataError, "columns"):
            self.ensure()

    def test_invalid_share_values_and_unbalanced_curves_fail(self):
        for value in (None, True, -0.1, 1.1, float("nan"), float("inf")):
            with self.subTest(value=value):
                data = baseline_data()
                data["copper"]["shares"]["secondary"][2050] = value
                with self.assertRaises(ShareDataError):
                    validate_share_mapping(data)
        data = baseline_data()
        data["copper"]["shares"]["primary"][2050] = .9
        with self.assertRaisesRegex(ShareDataError, "expected 1"):
            validate_share_mapping(data)

    def test_duplicate_yaml_keys_and_normalized_year_collisions_fail(self):
        self.baseline.write_text("copper: {}\ncopper: {}\n")
        with self.assertRaisesRegex(ShareDataError, "Duplicate YAML key"):
            load_yaml(self.baseline)
        data = baseline_data()
        data["copper"]["shares"]["primary"]["2020"] = .8
        with self.assertRaisesRegex(ShareDataError, "duplicate normalized"):
            validate_share_mapping(data)

    def test_default_paths_work_from_any_cwd_and_imports_are_safe(self):
        cwd = Path.cwd()
        try:
            os.chdir(self.directory)
            with patch.object(generator, "ensure_share_outputs", return_value={"status": "fixture"}) as ensure, redirect_stdout(io.StringIO()):
                generator.main([])
            self.assertEqual(ensure.call_args.kwargs["baseline_yaml"], DATA_DIR / self.baseline.name)
            with patch("openpyxl.load_workbook", side_effect=AssertionError("Import must not open workbook")):
                importlib.reload(extractor)
        finally:
            os.chdir(cwd)

    def test_unknown_cli_flags_fail_but_kernel_argument_is_ignored(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            generator.main(["--typo"])
        with patch("sys.argv", ["ipykernel_launcher.py", "--f=kernel.json"]), patch.object(generator, "ensure_share_outputs", return_value={}), redirect_stdout(io.StringIO()):
            generator.main()

    def test_package_output_directories_are_refused(self):
        with self.assertRaisesRegex(ShareDataError, "Installed-package writes"):
            ensure_share_outputs(output_dir=self.directory / "site-packages" / "premise" / "data" / "metals")

    def test_policy_wrapper_uses_policy_relative_custom_directory(self):
        policy_dir = self.directory / "configuration"
        policy_dir.mkdir()
        policy = policy_dir / "policy.yaml"
        policy.write_text("share_data:\n  directory: ..\n")
        summary = prepare_policy_share_data(policy)
        self.assertEqual(Path(summary["output_files"]["baseline"]).parent, self.directory.resolve())

    def test_manifest_is_portable_when_dataset_folder_is_copied(self):
        self.ensure()
        destination = self.directory / "clone"
        destination.mkdir()
        for path in list(self.directory.iterdir()):
            if path.is_file():
                shutil.copyfile(path, destination / path.name)
        summary = ensure_share_outputs(baseline_yaml=destination / self.baseline.name,
                                       evidence_csv=destination / self.evidence.name,
                                       mapping_csv=destination / self.mapping.name, output_dir=destination)
        self.assertEqual(summary["status"], "reused")

    def test_xlsx_mode_reads_only_and_extraction_has_one_authoritative_source(self):
        workbook = self.directory / "metal_primary_secondary_shares.xlsx"
        workbook.write_bytes(b"fixture workbook content")
        raw_workbook = workbook.read_bytes()
        extracted_data = baseline_data()
        with patch.object(extractor, "read_excel_shares", return_value=extracted_data):
            summary = self.ensure(source="xlsx", xlsx_path=workbook)
            timestamp = self.baseline.stat().st_mtime_ns
            again = self.ensure(source="xlsx", xlsx_path=workbook)
            self.assertEqual(again["status"], "reused")
            self.assertEqual(timestamp, self.baseline.stat().st_mtime_ns)
            modified = load_yaml(self.baseline)
            modified["copper"]["shares"]["secondary"][2050] = .3
            modified["copper"]["shares"]["primary"][2050] = .7
            self.baseline.write_text(yaml.safe_dump(modified))
            with self.assertRaisesRegex(ShareDataConflictError, "source='yaml'"):
                self.ensure(source="xlsx", xlsx_path=workbook)
            self.ensure(source="xlsx", xlsx_path=workbook, force=True)
        self.assertEqual(workbook.read_bytes(), raw_workbook)
        self.assertEqual(load_yaml(self.baseline), extracted_data)
        manifest = json.loads(Path(summary["manifest_path"]).read_text())
        self.assertIn(workbook.name, manifest["input_sha256"])

    def test_workbook_change_reextracts_when_intermediate_was_not_edited(self):
        workbook = self.directory / "metal_primary_secondary_shares.xlsx"
        workbook.write_bytes(b"workbook1")
        with patch.object(extractor, "read_excel_shares", return_value=baseline_data()):
            self.ensure(source="xlsx", xlsx_path=workbook)
        updated = baseline_data()
        updated["copper"]["shares"]["primary"][2020] = .7
        updated["copper"]["shares"]["secondary"][2020] = .3
        workbook.write_bytes(b"workbook2")
        with patch.object(extractor, "read_excel_shares", return_value=updated):
            summary = self.ensure(source="xlsx", xlsx_path=workbook)
        self.assertEqual(summary["status"], "rebuilt")
        self.assertEqual(load_yaml(self.baseline), updated)

    def test_invalid_nonempty_outputs_fail_unless_force_is_explicit(self):
        first = self.ensure()
        path = Path(first["output_files"]["conservative"])
        path.write_text("unexpected: {}\n")
        with self.assertRaises(ShareDataError):
            self.ensure()
        summary = self.ensure(force=True)
        self.assertEqual(len(load_yaml(path)), 2)
        self.assertEqual(summary["status"], "rebuilt")

    def test_concurrent_manual_output_edit_fails_before_any_publication(self):
        summary = self.ensure()
        target = Path(summary["output_files"]["baseline"])
        original_files = {path: path.read_bytes() for path in self.directory.iterdir() if path.is_file()}
        self.evidence.write_text(self.evidence.read_text() + "\n")
        modified = load_yaml(target)
        modified["copper"]["shares"]["secondary"][2050] = .66
        modified["copper"]["shares"]["primary"][2050] = .34
        manual_bytes = yaml.safe_dump(modified, sort_keys=False).encode()
        publish = share_files._publish
        def race(payloads, inputs, targets):
            target.write_bytes(manual_bytes)
            return publish(payloads, inputs, targets)
        with patch.object(share_files, "_publish", side_effect=race):
            with self.assertRaisesRegex(ShareDataConflictError, "output/manifest changed"):
                self.ensure()
        self.assertEqual(target.read_bytes(), manual_bytes)
        for path, previous in original_files.items():
            if path.resolve() not in (target.resolve(), self.evidence.resolve()):
                self.assertEqual(path.read_bytes(), previous)

    def test_previously_missing_target_created_concurrently_is_not_overwritten(self):
        target = self.directory / SCENARIO_FILES["baseline"]
        manual_bytes = yaml.safe_dump(baseline_data()).encode()
        publish = share_files._publish
        def race(payloads, inputs, targets):
            target.write_bytes(manual_bytes)
            return publish(payloads, inputs, targets)
        with patch.object(share_files, "_publish", side_effect=race):
            with self.assertRaisesRegex(ShareDataConflictError, "output/manifest changed"):
                self.ensure()
        self.assertEqual(target.read_bytes(), manual_bytes)
        self.assertFalse((self.directory / SCENARIO_FILES["conservative"]).exists())
        self.assertFalse((self.directory / "share_generation_manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
