"""Classification preservation and matrix coverage, without production LCA data."""
from copy import deepcopy
import ast
import csv
import gc
import io
from importlib.util import find_spec, module_from_spec, spec_from_file_location
import json
from pathlib import Path
import pickle
import tempfile
import sys
from types import ModuleType, SimpleNamespace
from typing import Any, MutableMapping
import unittest
from unittest.mock import patch
import zipfile

import yaml

from por_classifications import (
    ClassificationError, capture_scenario_classifications,
    prepare_pathways_classifications, write_classification_snapshot, _project_reference_records,
)


def activity(name="chemical production", product="chemical", unit="kilogram", location="NL", classifications=None):
    return {"name": name, "reference product": product, "unit": unit, "location": location,
            "classifications": classifications or [],
            "exchanges": [{"type": "production", "amount": 1, "uncertainty type": 0}]}


def builder(*databases, **kwargs):
    return SimpleNamespace(scenarios=[{"model": "remind", "pathway": "test", "year": 2025 + 5 * index,
                                      "database": database} for index, database in enumerate(databases)], **kwargs)


class MetadataOnlyStore:
    """Any attempted full activity materialization is a test failure."""
    def __init__(self, database):
        self._state = SimpleNamespace(activities={index: deepcopy(row) for index, row in enumerate(database)})
        self.generation = 3

    def _iter_storage_activities(self):
        for index, row in self._state.activities.items():
            yield index, row, (100 + index,)

    def activity(self, index):
        raise AssertionError("Exchange payloads must not be materialized")

    def materialize(self, **kwargs):
        raise AssertionError("Whole inventory must not be materialized")


class ClassificationCaptureTest(unittest.TestCase):
    def capture(self, value, reference=(), **kwargs):
        return capture_scenario_classifications(value, rules_path=None, reference_func=lambda: reference, **kwargs)

    def test_preserves_source_metadata_and_original_inventory_values(self):
        database = [activity(classifications=[("CPC", "34710: Polymers of ethylene, in primary forms"),
                                              ("ISIC rev.4 ecoinvent", "2013:Manufacture of plastics")]),
                    activity("missing", "missing")]
        before = deepcopy(database)
        snapshot = self.capture(builder(database))
        self.assertEqual(database, before)
        self.assertEqual(snapshot.classifications[("chemical production", "chemical")]["CPC"],
                         "34710: Polymers of ethylene, in primary forms")
        self.assertEqual(snapshot.classifications[("missing", "missing")],
                         {"CPC": "undefined", "ISIC rev.4 ecoinvent": "undefined"})
        self.assertEqual(snapshot.stats["activity_count"], 2)
        self.assertEqual(snapshot.stats["unique_pairs"], 2)
        self.assertEqual(snapshot.stats["undefined_pairs"]["CPC"], 1)

    def test_native_store_and_source_reference_use_metadata_only(self):
        store = MetadataOnlyStore([activity("chemical production (por)")])
        source = MetadataOnlyStore([activity(classifications={"CPC": "341: Chemical products"})])
        definition = {"_inventory_store": store, "year": 2050}
        value = SimpleNamespace(scenarios=[definition], _source_inventory_store=source,
                                _inventory_api_active=True)
        before = deepcopy(store._state.activities)
        snapshot = self.capture(value)
        self.assertEqual(snapshot.classifications[("chemical production (por)", "chemical")]["CPC"], "341: Chemical products")
        self.assertEqual(store._state.activities, before)
        self.assertEqual(definition, {"_inventory_store": store, "year": 2050})
        self.assertIn("normalized_reference", snapshot.stats["status_counts"])
        store.generation += 1
        with self.assertRaisesRegex(ClassificationError, "inventory changed"):
            snapshot.verify_sources()

    def test_lazy_activity_sidecar_is_read_without_installing_metadata_overlay(self):
        class LazyActivity(dict):
            _deleted = set()
            def _premise_common_value(self, key):
                return dict.get(self, key)
            def _metadata(self):
                return {"classifications": [("CPC", "341: Chemical products")],
                        "comment": "source metadata"}
        store = MetadataOnlyStore([])
        row = LazyActivity(name="chemical production", unit="kilogram", location="NL")
        row["reference product"] = "chemical"
        store._state.activities = {0: row}
        snapshot = self.capture(SimpleNamespace(scenarios=[{"_inventory_store": store}], _inventory_api_active=True))
        self.assertEqual(snapshot.classifications[("chemical production", "chemical")]["CPC"], "341: Chemical products")
        self.assertNotIn("classifications", dict(row))

    def test_checkpoint_wins_over_stale_handoff_without_mutating_scenario(self):
        proper = MetadataOnlyStore([activity(classifications={"CPC": "341: Chemical products"})])
        stale = MetadataOnlyStore([activity("wrong", "wrong")])
        definition = {"_inventory_checkpoint": "fixture.inventory-store", "_inventory_export_handoff": stale}
        api = SimpleNamespace(InventoryStore=SimpleNamespace(open=lambda path: proper))
        with patch("por_classifications._inventory_store_api", return_value=api):
            snapshot = self.capture(SimpleNamespace(scenarios=[definition]))
        self.assertNotIn(("wrong", "wrong"), snapshot.classifications)
        self.assertEqual(set(definition), {"_inventory_checkpoint", "_inventory_export_handoff"})

    def test_legacy_cache_load_is_non_consuming_and_keeps_original_scenario(self):
        definition = {"database filepath": "cache.sqlite", "year": 2050}
        calls = []
        def loader(**kwargs):
            calls.append(kwargs)
            kwargs["scenario"]["database"] = [activity(classifications={"CPC": "341: Chemical products"})]
            return kwargs["scenario"]
        snapshot = self.capture(SimpleNamespace(scenarios=[definition]), load_database_func=loader)
        self.assertFalse(calls[0]["delete"])
        self.assertFalse(calls[0]["load_metadata"])
        self.assertEqual(definition, {"database filepath": "cache.sqlite", "year": 2050})
        self.assertEqual(snapshot.stats["unique_pairs"], 1)

    def test_exact_reference_and_suffix_reference_require_correct_product_and_unit(self):
        reference = [activity(classifications={"CPC": "341: Chemical products"})]
        database = [activity("chemical production (retrofit) (por)"),
                    activity("chemical production (por)", "different product"),
                    activity("chemical production, imported", unit="megajoule"),
                    activity("unknown production (por)")]
        snapshot = self.capture(builder(database), reference)
        self.assertEqual(snapshot.classifications[(database[0]["name"], "chemical")]["CPC"], "341: Chemical products")
        for row in database[1:]:
            self.assertEqual(snapshot.classifications[(row["name"], row["reference product"])]["CPC"], "undefined")

    def test_unitless_catalogue_only_resolves_exact_names(self):
        reference = {("chemical production", "chemical"): {"CPC": "341: Chemical products"}}
        snapshot = self.capture(builder([activity(), activity("chemical production (por)")]), reference)
        self.assertEqual(snapshot.classifications[("chemical production", "chemical")]["CPC"], "341: Chemical products")
        self.assertEqual(snapshot.classifications[("chemical production (por)", "chemical")]["CPC"], "undefined")

    def test_known_categories_are_shared_with_identical_unclassified_copies(self):
        snapshot = self.capture(builder([activity(classifications={"CPC": "341: Chemical products"})],
                                        [activity(location="DE")]))
        self.assertEqual(snapshot.stats["unique_pairs"], 1)
        self.assertEqual(snapshot.stats["activity_count"], 2)
        self.assertEqual(snapshot.classifications[("chemical production", "chemical")]["CPC"], "341: Chemical products")

    def test_conflicting_categories_across_geographies_and_units_remain_audited_undefined(self):
        for second in (activity(location="DE", classifications={"CPC": "999: Other"}),
                       activity(unit="megajoule", classifications={"CPC": "999: Other"})):
            with self.subTest(second=second):
                snapshot = self.capture(builder([activity(classifications={"CPC": "341: Chemical products"}), second]))
                self.assertEqual(snapshot.classifications[("chemical production", "chemical")]["CPC"], "undefined")
                self.assertEqual(snapshot.stats["conflicts"], 1)
                note = next(row for row in snapshot.audit if row["classification_system"] == "CPC")
                self.assertIn("341: Chemical products", note["reason"])
                self.assertIn("999: Other", note["reason"])

    def test_unrelated_ambiguous_reference_does_not_stop_complete_capture(self):
        references = [activity("unrelated", "heat", classifications={"CPC": "17100: Electricity"}),
                      activity("unrelated", "heat", classifications={"CPC": "17300: Steam"})]
        snapshot = self.capture(builder([activity()]), references)
        self.assertEqual(snapshot.stats["unique_pairs"], 1)
        self.assertEqual(snapshot.stats["ambiguous_reference_entries"], 1)
        self.assertEqual(snapshot.stats["conflicts"], 0)

    def test_ambiguous_exact_reference_is_undefined_unless_actual_source_resolves_it(self):
        references = [activity(classifications={"CPC": "17100: Electricity"}),
                      activity(classifications={"CPC": "17300: Steam"})]
        snapshot = self.capture(builder([activity()]), references)
        self.assertEqual(snapshot.classifications[("chemical production", "chemical")]["CPC"], "undefined")
        self.assertEqual(snapshot.stats["conflicts"], 1)
        original = builder([activity(classifications={"CPC": "341: Chemical products"})])
        resolved = self.capture(original, references)
        self.assertEqual(resolved.classifications[("chemical production", "chemical")]["CPC"], "341: Chemical products")

    def test_official_code_description_variants_do_not_create_false_conflicts(self):
        snapshot = self.capture(builder([activity(classifications={"CPC": "34740: Full polymer description"}),
                                         activity(location="DE", classifications={"CPC": "34740: Truncated polymer"})]))
        self.assertEqual(snapshot.stats["conflicts"], 0)
        self.assertTrue(snapshot.classifications[("chemical production", "chemical")]["CPC"].startswith("34740:"))

    def test_invalid_identity_and_malformed_classification_metadata_fail(self):
        with self.assertRaisesRegex(ClassificationError, "incomplete"):
            self.capture(builder([activity(product="")]))
        with self.assertRaisesRegex(ClassificationError, "Invalid classification metadata"):
            self.capture(builder([activity(classifications=[("CPC",)])]))

    def test_reported_petroleum_gypsum_duplicate_codes_are_audited_not_fatal(self):
        codes = [("CPC", "33500: Petroleum jelly; paraffin wax"),
                 ("CPC", "15200: Gypsum; anhydrite; limestone flux"),
                 ("CPC", "33500: Petroleum jelly; paraffin wax"),
                 ("ISIC", "1920:Manufacture of refined petroleum products")]
        database = [activity("calcined petroleum coke production", "gypsum, mineral", location="CN", classifications=codes)]
        before = deepcopy(database)
        snapshot = self.capture(builder(database))
        pair = (database[0]["name"], database[0]["reference product"])
        self.assertEqual(snapshot.classifications[pair]["CPC"], "undefined")
        self.assertEqual(snapshot.classifications[pair]["ISIC rev.4 ecoinvent"], codes[-1][1])
        self.assertEqual(snapshot.stats["conflicts"], 1)
        row = next(row for row in snapshot.audit if row["classification_system"] == "CPC")
        self.assertEqual(row["status"], "conflicting_metadata")
        self.assertIn(codes[0][1], row["reason"])
        self.assertIn(codes[1][1], row["reason"])
        self.assertEqual(database, before)

    def test_duplicate_native_source_and_scenario_metadata_remain_metadata_only(self):
        dataset = activity(classifications=[("CPC", "33500: Wax"), ("CPC", "15200: Gypsum"),
                                             ("ISIC", "1920: Refining")])
        store = MetadataOnlyStore([dataset])
        before = deepcopy(store._state.activities)
        snapshot = self.capture(SimpleNamespace(scenarios=[{"_inventory_store": store}],
                                               _source_inventory_store=store, _inventory_api_active=True))
        self.assertEqual(snapshot.classifications[(dataset["name"], dataset["reference product"])]["CPC"], "undefined")
        self.assertEqual(store._state.activities, before)
        self.assertEqual(store.generation, 3)

    def test_duplicate_reference_lists_and_mapping_values_do_not_resolve_ambiguity(self):
        codes = [("CPC", "33500: Wax"), ("CPC", "15200: Gypsum")]
        for reference in ([activity(classifications=codes)],
                          {("chemical production", "chemical", "kilogram"): codes}):
            with self.subTest(reference=reference):
                snapshot = self.capture(builder([activity()]), reference)
                self.assertEqual(snapshot.classifications[("chemical production", "chemical")]["CPC"], "undefined")
                self.assertEqual(snapshot.stats["conflicts"], 1)
                row = next(row for row in snapshot.audit if row["classification_system"] == "CPC")
                self.assertIn("33500: Wax", row["reason"])
                self.assertIn("15200: Gypsum", row["reason"])

    def test_single_record_same_numeric_code_keeps_first_description(self):
        snapshot = self.capture(builder([activity(classifications=[("CPC", "34740: Full description"),
                                                                   ("CPC", "34740: Abbreviated")])]))
        self.assertEqual(snapshot.classifications[("chemical production", "chemical")]["CPC"], "34740: Full description")
        self.assertEqual(snapshot.stats["conflicts"], 0)


class ClassificationRulesAndExportTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def rules(self, entries):
        path = self.directory / "rules.yaml"
        # Fixture generation only; no user file is edited by the tests.
        path.write_text(yaml.safe_dump({"schema_version": 1, "rules": entries}), encoding="utf-8")
        return path

    def rule(self, **updates):
        return {"id": "foreground", "match": {"name_prefix": "chemical", "ref_product": "chemical", "unit": "kilogram"},
                "classifications": {"CPC": "341: Chemical products", "PoR activity group": "Chemical production"},
                "status": "provisional", "source": "reviewed test reference", "reason": "Explicit foreground proxy.", **updates}

    def test_rules_fill_only_missing_metadata_and_preserve_custom_provenance(self):
        rules = self.rules([self.rule()])
        snapshot = capture_scenario_classifications(
            builder([activity(classifications={"CPC": "34710: Source code"})]), rules,
            reference_func=lambda: ())
        classes = snapshot.classifications[("chemical production", "chemical")]
        self.assertEqual(classes["CPC"], "34710: Source code")
        self.assertEqual(classes["PoR activity group"], "Chemical production")
        note = next(row for row in snapshot.audit if row["classification_system"] == "PoR activity group")
        self.assertEqual(note["status"], "provisional")
        self.assertEqual(note["rule_id"], "foreground")
        rules.write_text("schema_version: 1\nrules: []\n", encoding="utf-8")
        with self.assertRaisesRegex(ClassificationError, "source changed"):
            write_classification_snapshot(SimpleNamespace(), snapshot, self.directory / "classifications.csv")

    def test_proxy_reference_is_audited_and_a_missing_reference_remains_undefined(self):
        rules = self.rules([self.rule(classifications={}, reference={"name": "reference production", "ref_product": "reference", "unit": "kilogram"})])
        snapshot = capture_scenario_classifications(builder([activity()]), rules,
                    reference_func=lambda: [activity("reference production", "reference", classifications={"CPC": "341: Chemical products"})])
        note = next(row for row in snapshot.audit if row["classification_system"] == "CPC")
        self.assertEqual(note["reference_name"], "reference production")
        self.assertEqual(note["rule_id"], "foreground")
        missing = capture_scenario_classifications(builder([activity()]), rules, reference_func=lambda: ())
        self.assertEqual(missing.classifications[("chemical production", "chemical")]["CPC"], "undefined")
        self.assertIn("unavailable_rule_reference", missing.stats["status_counts"])

    def test_matching_conflicting_rules_fail(self):
        rules = self.rules([self.rule(), self.rule(id="second", classifications={"CPC": "999: Different"})])
        with self.assertRaisesRegex(ClassificationError, "Conflicting matching"):
            capture_scenario_classifications(builder([activity()]), rules, reference_func=lambda: ())

    def test_export_has_populated_resource_and_audit_with_all_unknown_pairs(self):
        snapshot = capture_scenario_classifications(builder([activity(), activity("missing", "missing")]),
                                                    rules_path=None, reference_func=lambda: ())
        package = SimpleNamespace()
        audit_dir = self.directory / "audit"
        path = write_classification_snapshot(package, snapshot, self.directory / "classifications/classifications.csv", audit_dir=audit_dir)
        with path.open(newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 4)
        self.assertEqual({(row["name"], row["reference product"]) for row in rows}, set(snapshot.classifications))
        self.assertEqual(package.classification_stats["undefined_pairs"]["CPC"], 2)
        report = json.loads((audit_dir / "classification_audit.json").read_text(encoding="utf-8"))
        self.assertEqual(report["snapshot_sha256"], snapshot.snapshot_sha256)
        self.assertFalse(list(path.parent.glob("*.tmp")))

    def test_export_after_matrix_store_generation_changes_uses_frozen_metadata(self):
        store = MetadataOnlyStore([activity(classifications={"CPC": "341: Chemical products"})])
        snapshot = capture_scenario_classifications(SimpleNamespace(scenarios=[{"_inventory_store": store}]),
                                                    rules_path=None, reference_func=lambda: ())
        store.generation += 1  # Native matrix export can advance or release a store.
        path = write_classification_snapshot(SimpleNamespace(), snapshot, self.directory / "classifications.csv",
                                             audit_dir=self.directory / "audit")
        self.assertIn("341: Chemical products", path.read_text(encoding="utf-8"))
        snapshot.classifications[("chemical production", "chemical")]["CPC"] = "999: Changed"
        with self.assertRaisesRegex(ClassificationError, "snapshot changed"):
            write_classification_snapshot(SimpleNamespace(), snapshot, path, audit_dir=self.directory / "audit")

    def test_reviewed_portable_reference_metadata_is_hashed_without_live_database(self):
        path = self.directory / "references.json"
        path.write_text(json.dumps({"schema_version": 1, "source_inventory_version": "3.12", "datasets": [
            activity("reviewed reference", "reviewed product", classifications={"CPC": "341: Chemical products"})]}), encoding="utf-8")
        rows, hashes = _project_reference_records(path)
        self.assertEqual(rows[0]["classifications"], {"CPC": "341: Chemical products"})
        self.assertEqual(set(hashes), {str(path.resolve())})
        self.assertNotIn("exchanges", rows[0])
        with patch("por_classifications._bundled_reference_records", return_value=([], {})), patch(
                "por_classifications._project_reference_records", return_value=(rows, hashes)):
            snapshot = capture_scenario_classifications(builder([activity("reviewed reference", "reviewed product")]), rules_path=None)
        self.assertEqual(snapshot.classifications[("reviewed reference", "reviewed product")]["CPC"], "341: Chemical products")
        self.assertEqual(snapshot.source_hashes, hashes)

    def test_legacy_trimmed_metadata_sidecar_preserves_actual_classifications_and_cache(self):
        metadata_path = self.directory / "source metadata.pickle"
        metadata = {("chemical production", "chemical", "NL"): {"classifications": [("CPC", "34710: Authentic legacy code")],
                    "comment": "Additional source metadata", "irrelevant": {"large": [1, 2, 3]}}}
        metadata_path.write_bytes(pickle.dumps(metadata))
        original_bytes = metadata_path.read_bytes()
        database = [activity()]
        original_database = deepcopy(database)
        definition = {"database": database, "database metadata cache filepath": metadata_path}
        snapshot = capture_scenario_classifications(SimpleNamespace(scenarios=[definition]), rules_path=None,
                        reference_func=lambda: [activity(classifications={"CPC": "99999: Wrong fallback"})])
        self.assertEqual(snapshot.classifications[("chemical production", "chemical")]["CPC"], "34710: Authentic legacy code")
        self.assertEqual(database, original_database)
        self.assertEqual(metadata_path.read_bytes(), original_bytes)
        self.assertEqual(set(snapshot.source_hashes), {str(metadata_path.resolve())})
        self.assertNotIn("irrelevant", snapshot.classifications[("chemical production", "chemical")])

    def test_conflicting_legacy_sidecars_are_audited_undefined(self):
        first, second = self.directory / "source.pickle", self.directory / "inventories.pickle"
        first.write_bytes(pickle.dumps({("chemical production", "chemical", "NL"): {"classifications": {"CPC": "17100: Electricity"}}}))
        second.write_bytes(pickle.dumps({("chemical production", "chemical", "NL"): {"classifications": {"CPC": "17300: Steam"}}}))
        definition = {"database": [activity()], "database metadata cache filepath": first, "inventories metadata cache filepath": second}
        snapshot = capture_scenario_classifications(SimpleNamespace(scenarios=[definition]), rules_path=None,
                                                    reference_func=lambda: ())
        self.assertEqual(snapshot.classifications[("chemical production", "chemical")]["CPC"], "undefined")
        self.assertEqual(snapshot.stats["conflicts"], 1)

    def test_duplicate_entries_in_one_legacy_sidecar_keep_conflicts_with_undefined_own_label(self):
        path = self.directory / "source.pickle"
        path.write_bytes(pickle.dumps({("chemical production", "chemical", "NL"): {
            "classifications": [("CPC", "33500: Wax"), ("CPC", "15200: Gypsum"), ("ISIC", "1920: Refining")]
        }}))
        before = path.read_bytes()
        for own in ({"CPC": "undefined"}, [("CPC", "33500: Wax"), ("CPC", "15200: Gypsum")]):
            with self.subTest(own=own):
                database = [activity(classifications=own)]
                original = deepcopy(database)
                definition = {"database": database, "database metadata cache filepath": path}
                snapshot = capture_scenario_classifications(SimpleNamespace(scenarios=[definition]), rules_path=None,
                                                           reference_func=lambda: ())
                self.assertEqual(snapshot.classifications[("chemical production", "chemical")]["CPC"], "undefined")
                self.assertEqual(snapshot.stats["conflicts"], 1)
                self.assertEqual(database, original)
        self.assertEqual(path.read_bytes(), before)

    def test_authored_rule_duplicate_codes_still_fail(self):
        path = self.rules([self.rule(classifications=[("CPC", "33500: Wax"), ("CPC", "15200: Gypsum")])])
        with self.assertRaisesRegex(ClassificationError, "Conflicting CPC"):
            capture_scenario_classifications(builder([activity()]), path, reference_func=lambda: ())

    def matrix_text(self):
        return "name;reference product;unit;location;index\nchemical production;chemical;kilogram;NL;0\nmissing;missing;kilogram;DE;1\nchemical production;chemical;kilogram;DE;2\n"

    def test_header_empty_legacy_zip_repaired_in_memory_without_source_modification(self):
        path = self.directory / "package.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("classifications/classifications.csv", "name,reference product,classification_system,classification_code\n")
            archive.writestr("inventories/remind/test/2025/A_matrix_index.csv", self.matrix_text())
        before = path.read_bytes()
        package = SimpleNamespace(classifications={}, classification_system="CPC")
        snapshot = prepare_pathways_classifications(package, path, rules_path=None,
            reference_func=lambda: {("chemical production", "chemical"): {"CPC": "341: Chemical products"}},
            audit_dir=self.directory / "audit")
        self.assertEqual(package.classifications, {("chemical production", "chemical"): "341: Chemical products",
                                                   ("missing", "missing"): "undefined"})
        self.assertEqual(package.reverse_classifications["undefined"], [("missing", "missing")])
        self.assertEqual(snapshot.stats["activity_count"], 3)
        self.assertEqual(snapshot.stats["unique_pairs"], 2)
        self.assertEqual(path.read_bytes(), before)

    def test_existing_selected_categories_preserved_and_matrix_sources_checked(self):
        path = self.directory / "A_matrix_index.csv"
        path.write_text(self.matrix_text(), encoding="utf-8")
        package = SimpleNamespace(classifications={("chemical production", "chemical"): "Chemicals"},
                                  classification_system="CPC", filepaths=[str(path)])
        snapshot = prepare_pathways_classifications(package, rules_path=None, reference_func=lambda: ())
        self.assertEqual(package.classifications[("chemical production", "chemical")], "Chemicals")
        self.assertIn("unclassified", package.reverse_classifications)
        path.write_text(self.matrix_text() + "another;another;kilogram;NL;3\n", encoding="utf-8")
        with self.assertRaisesRegex(ClassificationError, "source changed"):
            snapshot.verify_sources()

    def test_native_installed_fallback_is_not_treated_as_declared_package_metadata(self):
        index = self.directory / "A_matrix_index.csv"
        index.write_text(self.matrix_text(), encoding="utf-8")
        classifications = self.directory / "classifications.csv"
        classifications.write_text("name,reference product,classification_system,classification_code\n", encoding="utf-8")
        resource = SimpleNamespace(source=str(classifications))
        package = SimpleNamespace(classifications={("chemical production", "chemical"): "17100: First installed fallback"},
                                  classification_system="CPC", filepaths=[str(index)],
                                  data=SimpleNamespace(get_resource=lambda name: resource))
        references = [activity(classifications={"CPC": "17100: Electricity"}),
                      activity(classifications={"CPC": "17300: Steam"})]
        snapshot = prepare_pathways_classifications(package, rules_path=None, reference_func=lambda: references)
        self.assertEqual(package.classifications[("chemical production", "chemical")], "undefined")
        self.assertEqual(snapshot.stats["conflicts"], 1)
        self.assertIn(str(classifications.resolve()), snapshot.source_hashes)

    def test_all_declared_package_systems_survive_selected_category_mapping(self):
        index = self.directory / "A_matrix_index.csv"
        index.write_text(self.matrix_text(), encoding="utf-8")
        content = ("name,reference product,classification_system,classification_code\n"
                   "chemical production,chemical,CPC,111: Declared CPC\n"
                   "chemical production,chemical,ISIC rev.4 ecoinvent,999: Declared ISIC\n"
                   "chemical production,chemical,PoR activity category,Native group\n")
        resource = SimpleNamespace(source="archive-member.csv", raw_read=lambda: content)
        for selected, expected_cpc in (("111: Declared CPC", "111: Declared CPC"), ("Mapped group", "Mapped group")):
            with self.subTest(selected=selected):
                package = SimpleNamespace(classifications={("chemical production", "chemical"): selected}, classification_system="CPC",
                                          filepaths=[str(index)], data=SimpleNamespace(get_resource=lambda name: resource))
                prepare_pathways_classifications(package, rules_path=None,
                    reference_func=lambda: [activity(classifications={"CPC": "222: Fallback CPC", "ISIC rev.4 ecoinvent": "222: Fallback ISIC"})])
                details = package.classification_details[("chemical production", "chemical")]
                self.assertEqual(details["CPC"], expected_cpc)
                self.assertEqual(details["ISIC rev.4 ecoinvent"], "999: Declared ISIC")
                self.assertEqual(details["PoR activity category"], "Native group")

    def test_declared_package_undefined_remains_undefined_and_raw_resource_is_verified(self):
        index = self.directory / "A_matrix_index.csv"
        index.write_text(self.matrix_text(), encoding="utf-8")
        content = ["name,reference product,classification_system,classification_code\nchemical production,chemical,CPC,undefined\n"]
        resource = SimpleNamespace(source="archive-member.csv", raw_read=lambda: content[0].encode("utf-8"))
        package = SimpleNamespace(classifications={("chemical production", "chemical"): "17100: Installed fallback"},
                                  classification_system="CPC", filepaths=[str(index)],
                                  data=SimpleNamespace(get_resource=lambda name: resource))
        snapshot = prepare_pathways_classifications(package, rules_path=None,
            reference_func=lambda: [activity(classifications={"CPC": "341: Chemical products"})])
        self.assertEqual(package.classifications[("chemical production", "chemical")], "undefined")
        self.assertIn("package_undefined", snapshot.stats["status_counts"])
        content[0] += "missing,missing,CPC,341: Changed\n"
        with self.assertRaisesRegex(ClassificationError, "source changed"):
            snapshot.verify_sources()

    def test_legacy_package_conflicting_rows_are_audited_without_dropping_activity(self):
        path = self.directory / "legacy.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("A_matrix_index.csv", self.matrix_text())
            archive.writestr("classifications.csv", "name,reference product,classification_system,classification_code\n"
                             "chemical production,chemical,CPC,33500: Wax\n"
                             "chemical production,chemical,CPC,15200: Gypsum\n"
                             "chemical production,chemical,ISIC,1920: Refining\n")
        before = path.read_bytes()
        package = SimpleNamespace(classifications={("chemical production", "chemical"): "33500: Wax"}, classification_system="CPC")
        snapshot = prepare_pathways_classifications(package, path, rules_path=None,
                                                   reference_func=lambda: [activity(classifications={"CPC": "15200: Gypsum"})])
        self.assertEqual(package.classifications[("chemical production", "chemical")], "undefined")
        self.assertEqual(snapshot.classifications[("chemical production", "chemical")]["ISIC rev.4 ecoinvent"], "1920: Refining")
        row = next(row for row in snapshot.audit if row["classification_system"] == "CPC" and row["name"] == "chemical production")
        self.assertIn("33500: Wax", row["reason"])
        self.assertIn("15200: Gypsum", row["reason"])
        self.assertIn(("chemical production", "chemical"), package.reverse_classifications["undefined"])
        self.assertEqual(path.read_bytes(), before)

    def test_reviewed_ambiguity_blocks_sparse_unitless_fallback(self):
        index = self.directory / "A_matrix_index.csv"
        index.write_text(self.matrix_text(), encoding="utf-8")
        references = [{"name": "chemical production", "reference product": "chemical", "unit": "", "classifications": {"CPC": "17100: Electricity"}},
                      {"name": "chemical production", "reference product": "chemical", "unit": "kilogram", "classifications": {},
                       "ambiguous_systems": {"CPC": ["17100: Electricity", "17300: Steam"]}}]
        package = SimpleNamespace(classifications={}, classification_system="CPC", filepaths={"remind": {"test": [str(index)]}})
        snapshot = prepare_pathways_classifications(package, rules_path=None, reference_func=lambda: references)
        self.assertEqual(package.classifications[("chemical production", "chemical")], "undefined")
        self.assertEqual(snapshot.stats["conflicts"], 1)

    def test_missing_matrix_resources_fail_closed(self):
        with self.assertRaisesRegex(ClassificationError, "No A_matrix_index"):
            prepare_pathways_classifications(SimpleNamespace(classifications={}), rules_path=None, reference_func=lambda: ())


class NativeClassificationStoreIntegrationTest(unittest.TestCase):
    """Read actual native sidecars without importing Brightway or premise init."""
    @classmethod
    def setUpClass(cls):
        package = find_spec("premise")
        if package is None:
            raise unittest.SkipTest("premise source unavailable")
        directory = Path(next(iter(package.submodule_search_locations)))
        if not (directory / "inventory_store.py").is_file():
            raise unittest.SkipTest("Older premise has no native inventory store")
        package_name = "_por_classification_native_fixture"
        package_module = ModuleType(package_name)
        package_module.__path__ = [str(directory)]
        cleanup_module = ModuleType(package_name + ".cache_cleanup")
        cleanup_module.mark_checkpoint = lambda path, **kwargs: None
        names = [package_name, cleanup_module.__name__, package_name + ".inventory_copy", package_name + ".inventory_store",
                 package_name + ".filesystem_constants", package_name + ".utils"]
        cls.addClassCleanup(lambda: [sys.modules.pop(name, None) for name in names])
        sys.modules[package_name] = package_module
        sys.modules[cleanup_module.__name__] = cleanup_module
        for name in ("inventory_copy", "inventory_store"):
            spec = spec_from_file_location(package_name + "." + name, directory / (name + ".py"))
            module = module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
        cls.api = module
        constants = ModuleType(package_name + ".filesystem_constants")
        constants.DATA_DIR = directory / "data"
        sys.modules[constants.__name__] = constants
        # Cache normalization is pure metadata logic. Load just these functions
        # and constants rather than premise.utils and its Brightway initializer.
        import numpy as np
        import pandas as pd
        utils = ModuleType(package_name + ".utils")
        utils.__dict__.update(Any=Any, MutableMapping=MutableMapping, np=np, pd=pd)
        source = ast.parse((directory / "utils.py").read_text(encoding="utf-8"))
        constants = {"_SCENARIO_TRIMMED_DATASET_FIELDS", "_SCENARIO_TRIMMED_EXCHANGE_FIELDS", "_CACHE_VALUE_CONTAINER_TYPES", "_CACHE_VALUE_INTEGER_TYPES"}
        functions = {"_has_cache_value", "_scenario_metadata_value_is_restored", "_normalize_scenario_cache_activity",
                     "_scenario_cache_exchange_field_is_restored", "_normalize_scenario_cache_exchange"}
        nodes = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in functions
                 or isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in constants for target in node.targets)]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "classification_cache_normalization", "exec"), utils.__dict__)
        sys.modules[utils.__name__] = utils

    def test_actual_compact_checkpoint_preserves_classifications_without_exchange_materialization(self):
        data = [activity(classifications={"CPC": "341: Chemical products", "ISIC rev.4 ecoinvent": "2011:Manufacture of basic chemicals"}),
                activity("missing", "missing")]
        store = self.api.create_inventory_store(data, backend="compact", scenario_identity="test")
        with tempfile.TemporaryDirectory() as directory:
            checkpoint = store.checkpoint(Path(directory) / "fixture.inventory-store")
            opened = self.api.InventoryStore.open(checkpoint)
            generation = opened.generation
            with patch.object(opened, "activity", side_effect=AssertionError("Full exchanges forbidden")), patch.object(
                    opened, "materialize", side_effect=AssertionError("Full inventory forbidden")):
                snapshot = capture_scenario_classifications(SimpleNamespace(scenarios=[{"_inventory_store": opened}]),
                                                            rules_path=None, reference_func=lambda: ())
            self.assertEqual(snapshot.classifications[("chemical production", "chemical")]["CPC"], "341: Chemical products")
            self.assertEqual(snapshot.classifications[("missing", "missing")]["CPC"], "undefined")
            self.assertEqual(opened.generation, generation)
            del opened
            gc.collect()  # Release Windows memory maps before fixture cleanup.


if __name__ == "__main__":
    unittest.main()
