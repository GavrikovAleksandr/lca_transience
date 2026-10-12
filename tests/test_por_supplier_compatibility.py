"""Importer decisions survive PoR linking without database or package writes."""
from copy import deepcopy
import ast
import csv
from functools import lru_cache
import hashlib
from importlib.util import find_spec
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import por_supplier_linking as linking
from inventory_supplier_compat import (
    install_inventory_supplier_compatibility, load_compatibility_rules,
)
from por_supplier_linking import (
    LINKING_POLICY_VERSION, PorLinkingPolicy, SupplierLinkingError,
    dataset_key, exchange_key, link_por_database,
)


ROOT = Path(__file__).resolve().parents[1]
PV_NAME = "electricity production, photovoltaic, commercial"
PV_MEDIUM = "electricity, medium voltage"
PV_LOW = "electricity, low voltage"
WIND_NAME = "electricity production, wind, >3MW turbine, onshore"
WIND_PRODUCT = "electricity, high voltage"
FIXED_NAME = "electricity supply, 50% solar and 50% wind, medium voltage, for DAC methanol (por)"
UNIT = "kilowatt hour"


def activity(name, product, location="MA", exchanges=None):
    return {"name": name, "reference product": product, "location": location,
            "unit": UNIT, "code": name + location, "exchanges": exchanges or []}


def feed(name, product, location="MA", amount=0.5, **extra):
    return {"name": name, "product": product, "location": location,
            "unit": UNIT, "amount": amount, "type": "technosphere", **extra}


def pv_rule(rules):
    candidates = [rule for rule in rules.rules
                  if PV_NAME in json.dumps(rule) and PV_MEDIUM in json.dumps(rule)]
    if len(candidates) != 1:
        raise AssertionError("Expected one declared commercial photovoltaic compatibility rule")
    return candidates[0]


def freeze_pv_decision(dataset, rules, target_product=PV_LOW):
    """Record the real importer's public receipt shape, without importing premise."""
    dataset["inventory supplier compatibility"] = {
        "schema_version": 1,
        "rules_sha256": rules.sha256,
        "inventory": "lci-itom_por.xlsx",
        "consumer_key": list(dataset_key(dataset)),
        "decisions": [{
            "rule": pv_rule(rules)["id"],
            "exchange_index": 0,
            "source_key": [PV_NAME, PV_MEDIUM, UNIT, "MA"],
            "target_key": [PV_NAME, target_product, UNIT, "MA"],
            "reason": "retained importer supplier decision",
            "fallback_sha256": None,
            "status": "changed" if target_product != PV_MEDIUM else "unchanged",
        }],
    }


def marker_for(policy):
    return {"linking_policy_version": LINKING_POLICY_VERSION,
            "policy_token": linking._register_policy(policy),
            "source_sha256": dict(policy.source_hashes),
            "config_path": str(policy.config_path) if policy.config_path else None,
            "inventory_path": str(policy.inventory_path) if policy.inventory_path else None,
            "compatibility_rules_path": str(policy.compatibility_rules.path)
            if policy.compatibility_rules is not None else None}


class ProtectedCompatibilityProfileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = load_compatibility_rules()

    def make_case(self, *, freeze=False, current_location="MA"):
        original_pv = feed(PV_NAME, PV_MEDIUM, **{
            "uncertainty type": 3, "loc": 0.5, "scale": 0.02,
            "minimum": 0, "maximum": None, "negative": False,
        })
        original_wind = feed(WIND_NAME, WIND_PRODUCT, "RoW")
        current_pv = {**original_pv, "product": PV_LOW,
                      "location": current_location, "input": ("old", "pv"),
                      "database": "old", "code": "pv"}
        current_wind = {**original_wind, "location": "NL"
                        if current_location == "NL" else "RoW"}
        bio = {"type": "biosphere", "amount": 0.003, "name": "Carbon dioxide, fossil"}
        production = {"type": "production", "amount": 1, "name": FIXED_NAME,
                      "location": "MA", "unit": UNIT}
        consumer = activity(FIXED_NAME, PV_MEDIUM,
                            exchanges=[current_pv, current_wind, bio, production])
        if freeze:
            freeze_pv_decision(consumer, self.rules)
        profiles = {dataset_key(consumer): [original_pv, original_wind]}
        policy = PorLinkingPolicy({}, profiles=profiles, compatibility_rules=self.rules)
        suppliers = [activity(PV_NAME, PV_LOW), activity(WIND_NAME, WIND_PRODUCT, "RoW")]
        if current_location == "NL":
            suppliers += [activity(PV_NAME, PV_LOW, "NL"),
                          activity(WIND_NAME, WIND_PRODUCT, "NL")]
        return [consumer, *suppliers], policy

    def test_mapped_profile_does_not_restore_missing_medium_voltage_solar(self):
        database, policy = self.make_case()
        before, source_before = deepcopy(database), deepcopy(policy.profiles)
        result, _, _ = link_por_database(database, policy)
        self.assertEqual(exchange_key(result[0]["exchanges"][0]),
                         (PV_NAME, PV_LOW, UNIT, "MA"))
        self.assertEqual(database, before)
        self.assertEqual(policy.profiles, source_before)
        self.assertEqual(result[0]["exchanges"][1:], before[0]["exchanges"][1:])
        repeated, _, summary = link_por_database(result, policy)
        self.assertEqual(repeated, result)
        self.assertEqual(summary["changed_exchanges"], 0)

    def test_frozen_importer_decision_survives_original_provider_reappearing(self):
        database, policy = self.make_case(freeze=True)
        database.append(activity(PV_NAME, PV_MEDIUM))
        result, _, _ = link_por_database(database, policy)
        self.assertEqual(exchange_key(result[0]["exchanges"][0]),
                         (PV_NAME, PV_LOW, UNIT, "MA"))
        self.assertEqual(result[0]["inventory supplier compatibility"],
                         database[0]["inventory supplier compatibility"])

    def test_protected_mix_restores_mapped_foreign_suppliers_and_keeps_shares(self):
        database, policy = self.make_case(freeze=True, current_location="NL")
        before = deepcopy(database)
        result, audit, summary = link_por_database(database, policy)
        exchanges = result[0]["exchanges"]
        self.assertEqual([exchange_key(exchange) for exchange in exchanges[:2]],
                         [(PV_NAME, PV_LOW, UNIT, "MA"),
                          (WIND_NAME, WIND_PRODUCT, UNIT, "RoW")])
        self.assertEqual([exchange["amount"] for exchange in exchanges[:2]], [0.5, 0.5])
        self.assertEqual(sum(exchange["amount"] for exchange in exchanges[:2]), 1)
        for key in ("amount", "unit", "uncertainty type", "loc", "scale",
                    "minimum", "maximum", "negative"):
            self.assertEqual(exchanges[0][key], before[0]["exchanges"][0][key])
        self.assertEqual(exchanges[2:], before[0]["exchanges"][2:])
        self.assertEqual(database, before)
        self.assertEqual(summary["changed_exchanges"], 2)
        self.assertEqual({row["reason"] for row in audit if row["status"] == "changed"},
                         {"restored_protected_source_supplier"})

    def test_fixed_mix_still_rejects_share_changes(self):
        database, policy = self.make_case(freeze=True)
        database[0]["exchanges"][0]["amount"] = 0.4
        with self.assertRaisesRegex(SupplierLinkingError, "share changed"):
            link_por_database(database, policy)

    def test_displaced_medium_voltage_nl_input_restores_frozen_low_voltage_ma(self):
        database, policy = self.make_case(freeze=True, current_location="NL")
        database[0]["exchanges"][0]["product"] = PV_MEDIUM
        database.append(activity(PV_NAME, PV_MEDIUM, "NL"))
        before, source_before = deepcopy(database), deepcopy(policy.profiles)
        receipt_before = deepcopy(database[0]["inventory supplier compatibility"])
        result, _, summary = link_por_database(database, policy)
        exchanges = result[0]["exchanges"]
        self.assertEqual([exchange_key(row) for row in exchanges[:2]],
                         [(PV_NAME, PV_LOW, UNIT, "MA"),
                          (WIND_NAME, WIND_PRODUCT, UNIT, "RoW")])
        self.assertEqual([row["amount"] for row in exchanges[:2]], [0.5, 0.5])
        for key in ("unit", "uncertainty type", "loc", "scale", "minimum", "maximum", "negative"):
            self.assertEqual(exchanges[0][key], before[0]["exchanges"][0][key])
        self.assertEqual(result[0]["inventory supplier compatibility"], receipt_before)
        self.assertEqual(exchanges[2:], before[0]["exchanges"][2:])
        self.assertEqual(database, before)
        self.assertEqual(policy.profiles, source_before)
        self.assertEqual(summary["changed_exchanges"], 2)

    def test_frozen_target_must_remain_unique_and_available_at_ma(self):
        for failure in ("missing", "duplicate", "only_nl"):
            with self.subTest(failure=failure):
                database, policy = self.make_case(freeze=True)
                if failure == "duplicate":
                    database.append(deepcopy(database[1]))
                else:
                    database.pop(1)
                    # The old exact voltage and a foreign low-voltage producer
                    # cannot replace a decision frozen at the MA supplier.
                    database.append(activity(PV_NAME, PV_MEDIUM))
                    if failure == "only_nl":
                        database.append(activity(PV_NAME, PV_LOW, "NL"))
                before = deepcopy(database)
                with self.assertRaises(SupplierLinkingError):
                    link_por_database(database, policy)
                self.assertEqual(database, before)

    def test_stale_frozen_rules_receipt_fails_before_linking(self):
        database, policy = self.make_case(freeze=True)
        database[0]["inventory supplier compatibility"]["rules_sha256"] = "0" * 64
        before = deepcopy(database)
        with self.assertRaises(SupplierLinkingError):
            link_por_database(database, policy)
        self.assertEqual(database, before)

    def test_frozen_fallback_is_not_promoted_when_native_candidates_reappear(self):
        database, policy = self.make_case(freeze=True)
        rule = pv_rule(self.rules)
        snapshot_path = (self.rules.path.parent / rule["fallback"]["path"]).resolve()
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        provider_key = dataset_key(rule["fallback"]["provider"])
        provider = next(data for data in snapshot["datasets"] if dataset_key(data) == provider_key)
        database[1] = provider
        database += [activity(PV_NAME, PV_MEDIUM), activity(PV_NAME, PV_LOW)]
        database[0]["exchanges"][0].update(name=provider_key[0], product=provider_key[1])
        decision = database[0]["inventory supplier compatibility"]["decisions"][0]
        decision.update(target_key=list(provider_key),
                        fallback_sha256=self.rules.checksums[str(snapshot_path)])
        before = deepcopy(database)
        result, _, _ = link_por_database(database, policy)
        self.assertEqual(exchange_key(result[0]["exchanges"][0]), provider_key)
        self.assertEqual([row["amount"] for row in result[0]["exchanges"][:2]], [0.5, 0.5])
        self.assertEqual(database, before)
        self.assertIs(result[1], provider)
        decision["fallback_sha256"] = "0" * 64
        with self.assertRaises(SupplierLinkingError):
            link_por_database(database, policy)

    def test_unselected_legacy_activity_with_the_same_supplier_is_untouched(self):
        consumer = activity("unrelated legacy activity", "unrelated product",
                            exchanges=[feed(PV_NAME, PV_MEDIUM)])
        database = [consumer, activity(PV_NAME, PV_LOW)]
        policy = PorLinkingPolicy({}, profiles={dataset_key(consumer):
                                              deepcopy(consumer["exchanges"])},
                                  compatibility_rules=self.rules)
        before = deepcopy(database)
        result, audit, summary = link_por_database(database, policy)
        self.assertEqual(result, before)
        self.assertIs(result[0], consumer)
        self.assertEqual(audit, [])
        self.assertEqual(summary["selected_activities"], 0)

    def test_manual_legacy_policy_without_compatibility_keeps_exact_profile(self):
        original = feed(PV_NAME, PV_MEDIUM)
        consumer = activity(FIXED_NAME, PV_MEDIUM, exchanges=[original])
        policy = PorLinkingPolicy({}, profiles={dataset_key(consumer): [deepcopy(original)]})
        result, audit, _ = link_por_database([consumer, activity(PV_NAME, PV_MEDIUM)], policy)
        self.assertEqual(result[0]["exchanges"], consumer["exchanges"])
        self.assertEqual(audit, [])


class CompatibilityCacheGuardTest(unittest.TestCase):
    def test_file_policy_tracks_rules_and_declared_fallback_sources(self):
        policy = PorLinkingPolicy.from_files()
        self.assertIsNotNone(policy.compatibility_rules)
        rules = policy.compatibility_rules
        self.assertIn(str(rules.path), policy.source_hashes)
        self.assertEqual(policy.source_hashes[str(rules.path)], rules.sha256)
        self.assertGreater(len(rules.source_paths), 1,
                           "Declared fallback datasets must be part of cache provenance")
        for name, digest in rules.checksums.items():
            self.assertEqual(policy.source_hashes[name], digest)

    def test_runtime_policy_token_does_not_bypass_marker_source_hashes(self):
        with tempfile.TemporaryDirectory(prefix="por_compat_guard_") as directory:
            source = Path(directory) / "inventory.xlsx"
            source.write_bytes(b"test inventory source")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            policy = PorLinkingPolicy({}, source_hashes={str(source): digest})
            marker = marker_for(policy)
            marker["source_sha256"][str(source)] = "0" * 64
            with self.assertRaises(SupplierLinkingError):
                linking._policy_for_marker(marker)

    def test_inventory_rules_and_fallback_changes_invalidate_cached_policy(self):
        for filename in ("inventory.xlsx", "compatibility_rules.yaml", "fallback.json"):
            with self.subTest(source=filename), \
                    tempfile.TemporaryDirectory(prefix="por_compat_guard_") as directory:
                source = Path(directory) / filename
                source.write_bytes(b"declared input")
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                policy = PorLinkingPolicy({}, source_hashes={str(source): digest})
                marker = marker_for(policy)
                self.assertIs(linking._policy_for_marker(marker), policy)
                source.write_bytes(b"changed declared input")
                with self.assertRaises(SupplierLinkingError):
                    linking._policy_for_marker(marker)

    def test_standard_export_support_installs_the_generic_importer_adapter(self):
        with patch("inventory_supplier_compat.install_inventory_supplier_compatibility") as adapter, \
                patch("premise_compat.install_premise_null_bounds_fix"), \
                patch("premise_compat.disable_legacy_por_regionalization"), \
                patch("premise_compat.install_premise_pathways_mapping_fix"), \
                patch("por_supplier_linking.install_por_export_geography_guard"):
            linking.configure_por_export_support(verbose=False)
        adapter.assert_called_once_with(verbose=False)

    def test_persisted_marker_reload_preserves_compatibility_sources(self):
        policy = PorLinkingPolicy.from_files()
        marker = marker_for(policy)
        with patch.dict(linking._POLICIES, {}, clear=True):
            reloaded = linking._policy_for_marker(marker)
        self.assertIsNot(reloaded, policy)
        self.assertEqual(reloaded.source_hashes, policy.source_hashes)
        self.assertEqual(reloaded.compatibility_rules.path, policy.compatibility_rules.path)
        self.assertEqual(reloaded.compatibility_rules.sha256, policy.compatibility_rules.sha256)


class NativeImporterSourceTest(unittest.TestCase):
    """Run installed old/new native methods without importing premise or Brightway."""

    @staticmethod
    def source_paths():
        package = find_spec("premise")
        if package is None:
            raise unittest.SkipTest("Installed premise source is required")
        package_path = Path(next(iter(package.submodule_search_locations)))
        paths = [package_path / "inventory_imports.py"]
        environment = package_path.parents[2]
        for name in ("premise_pathways", "premise_pathways_new"):
            sibling = environment.parent / name / "Lib/site-packages/premise/inventory_imports.py"
            if sibling.is_file() and sibling.resolve() not in {path.resolve() for path in paths}:
                paths.append(sibling)
        return paths

    @staticmethod
    def native_class(path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        wanted = {"BaseInventoryImport": {
            "add_product_field_to_exchanges", "correct_product_field", "_build_import_product_index"},
            "AdditionalInventory": {"prepare_inventory"}}
        classes = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name in wanted:
                node = deepcopy(node)
                node.body = [method for method in node.body if isinstance(method, ast.FunctionDef)
                             and method.name in wanted[node.name]]
                classes.append(node)
        if len(classes) != 2:
            raise AssertionError(f"Native importer API changed: {path}")

        def equals(field, value):
            return lambda record: record.get(field) == value

        def get_many(records, *predicates):
            return (record for record in records if all(predicate(record) for predicate in predicates))

        namespace = {"lru_cache": lru_cache, "uuid": uuid,
                     "ws": SimpleNamespace(equals=equals, get_many=get_many),
                     "migrate_import_db": lambda *args: None,
                     "remove_categories": lambda records: records,
                     "check_for_duplicate_datasets": lambda records: records,
                     "check_amount_format": lambda records: records}
        module = ast.Module(body=[ast.ImportFrom(module="__future__",
                                                names=[ast.alias(name="annotations")], level=0),
                                  *classes], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
        additional = namespace["AdditionalInventory"]
        for name in ("adapt_hydrogen_market_exchanges_for_legacy_versions",
                     "lower_case_technosphere_exchanges", "add_biosphere_links",
                     "remove_zero_amount_exchanges", "check_for_already_existing_datasets",
                     "display_unlinked_exchanges", "fill_dataset_data_gaps", "fill_data_gaps"):
            setattr(additional, name, lambda self, *args: None)
        return additional

    @staticmethod
    def instance(additional, consumer, providers):
        importer = additional.__new__(additional)
        importer.__dict__.update(
            database=providers, import_db=SimpleNamespace(data=[consumer]),
            path=Path("lci-itom_por.xlsx"), inventory_filename="lci-itom_por.xlsx",
            version_in="3.12", version_out="3.12", system_model="cutoff",
            list_unlinked=[], import_product_index={"by_fields": {}},
            database_product_index={
                "by_fields": {(data["name"], data["location"], data["unit"]): data["reference product"]
                              for data in providers},
                "by_fields_and_reference_product": {
                    (data["name"], data["location"], data["unit"], data["reference product"]):
                    data["reference product"] for data in providers},
            },
        )
        return importer

    def test_native_old_and_new_lookup_and_por_profile_use_the_same_decision(self):
        rules = load_compatibility_rules()
        for path in self.source_paths():
            with self.subTest(native_source=str(path)):
                additional = self.native_class(path)
                photovoltaic = feed(PV_NAME, PV_MEDIUM, **{"reference product": PV_MEDIUM})
                photovoltaic.pop("product")  # This is the workbook importer's native input shape.
                wind = feed(WIND_NAME, WIND_PRODUCT, "RoW", **{"reference product": WIND_PRODUCT})
                wind.pop("product")
                production = {"name": FIXED_NAME, "type": "production", "amount": 1,
                              "unit": UNIT, "location": "MA"}
                consumer = activity(FIXED_NAME, PV_MEDIUM,
                                    exchanges=[production, photovoltaic, wind])
                original_profile = deepcopy(consumer["exchanges"][1:])
                providers = [activity(PV_NAME, PV_LOW), activity(WIND_NAME, WIND_PRODUCT, "RoW")]
                providers_before = deepcopy(providers)
                baseline = self.instance(additional, deepcopy(consumer), providers)
                baseline.prepare_inventory()
                self.assertIsNone(baseline.import_db.data[0]["exchanges"][1]["product"])
                self.assertTrue(baseline.list_unlinked)

                native_lookup = additional.correct_product_field
                self.assertTrue(install_inventory_supplier_compatibility(additional, verbose=False))
                self.assertIs(additional.correct_product_field, native_lookup)
                importer = self.instance(additional, consumer, providers)
                self.assertIsNone(importer.prepare_inventory())  # Native return is retained.
                self.assertEqual(importer.list_unlinked, [])
                self.assertEqual(importer.correct_product_field((PV_NAME, "MA", UNIT, PV_LOW)), PV_LOW)
                self.assertEqual(exchange_key(consumer["exchanges"][1]), (PV_NAME, PV_LOW, UNIT, "MA"))
                decision = consumer["inventory supplier compatibility"]["decisions"][0]
                self.assertEqual((decision["exchange_index"], decision["profile_index"]), (1, 0))
                self.assertEqual(importer.inventory_supplier_compatibility_summary["changed_exchanges"], 1)
                self.assertEqual(providers, providers_before)

                policy = PorLinkingPolicy({}, profiles={dataset_key(consumer): original_profile},
                                          compatibility_rules=rules)
                result, _, _ = link_por_database([consumer, *providers], policy)
                self.assertEqual(exchange_key(result[0]["exchanges"][1]), (PV_NAME, PV_LOW, UNIT, "MA"))
                self.assertEqual([row["amount"] for row in result[0]["exchanges"][1:]], [0.5, 0.5])
                self.assertEqual(policy.profiles[dataset_key(consumer)], original_profile)

    def test_actual_fallback_biosphere_closure_links_with_both_native_methods(self):
        rules = load_compatibility_rules()
        snapshot_path = (rules.path.parent / pv_rule(rules)["fallback"]["path"]).resolve()
        snapshot_bytes = snapshot_path.read_bytes()
        source = json.loads(snapshot_bytes)["datasets"]
        self.assertEqual(len(source), 58)

        def bio_key(row):
            categories = row["categories"]
            if isinstance(categories, str):
                categories = categories.split("::")
            return (row["name"], categories[0].strip(),
                    categories[1] if len(categories) > 1 else "unspecified", row["unit"])

        biosphere_before = [row for data in source for row in data["exchanges"]
                            if row["type"] == "biosphere"]
        self.assertEqual(len({bio_key(row) for row in biosphere_before}), 103)
        self.assertTrue(any(row["name"] == "Heat, waste" and row["categories"] == ["air"]
                            for row in biosphere_before))
        other_before = [[deepcopy(row) for row in data["exchanges"] if row["type"] != "biosphere"]
                        for data in source]

        for path in self.source_paths():
            with self.subTest(native_source=str(path)):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                base = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                            and node.name == "BaseInventoryImport")
                method = next(node for node in base.body if isinstance(node, ast.FunctionDef)
                              and node.name == "add_biosphere_links")
                namespace = {}
                # Execute the unmodified native function independently of the
                # product-lookup fixture and its biosphere stub above.
                exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), namespace)
                flow_path = path.parent / "data/utils/export/flows_biosphere_312.csv"
                with flow_path.open(encoding="utf-8", newline="") as stream:
                    biosphere_dict = {tuple(row[:4]): row[-1] for row in csv.reader(stream)
                                      if len(row) == 5}
                linked = deepcopy(source)
                importer = SimpleNamespace(
                    import_db=SimpleNamespace(data=linked), biosphere_dict=biosphere_dict,
                    correspondence_bio_flows={}, path=Path("por_pv_ma.json"),
                    inventory_filename="por_pv_ma.json",
                )
                namespace["add_biosphere_links"](importer)
                biosphere_after = [row for data in linked for row in data["exchanges"]
                                   if row["type"] == "biosphere"]
                self.assertEqual(len(biosphere_after), len(biosphere_before))
                self.assertEqual({bio_key(row) for row in biosphere_after},
                                 {bio_key(row) for row in biosphere_before})
                self.assertEqual([[row for row in data["exchanges"] if row["type"] != "biosphere"]
                                  for data in linked], other_before)
                for before, after in zip(biosphere_before, biosphere_after):
                    self.assertEqual(after["amount"], before["amount"])
                    self.assertEqual(after["unit"], before["unit"])
                    self.assertEqual(after["input"], ("biosphere3", biosphere_dict[bio_key(after)]))
                self.assertFalse(any("delete" in row for data in linked for row in data["exchanges"]))
        self.assertEqual(snapshot_path.read_bytes(), snapshot_bytes)


if __name__ == "__main__":
    unittest.main()
