"""Exact compatibility decisions and importer hooks without premise/Brightway."""
from collections.abc import Mapping
from copy import deepcopy
from functools import lru_cache
import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import yaml

import inventory_supplier_compat as compat
from supplier_resolution import dataset_key, exchange_key


def supplier(name="photovoltaic commercial", product="electricity, medium voltage", location="MA"):
    return {"name": name, "reference product": product, "unit": "kilowatt hour", "location": location,
            "exchanges": [{"name": name, "product": product, "unit": "kilowatt hour", "location": location,
                           "amount": 1, "type": "production"}]}


def feed(product="electricity, medium voltage", **extra):
    return {"name": "photovoltaic commercial", "reference product": product,
            "unit": "kilowatt hour", "location": "MA", "amount": 0.5,
            "type": "technosphere", "uncertainty type": 2, "loc": -0.7, "scale": 0.1,
            "minimum": 0, "maximum": 0.9, **extra}


def consumer(rows=None, **extra):
    return {"name": "electricity supply, MA for DAC methanol (por)",
            "reference product": "renewable electricity", "unit": "kilowatt hour", "location": "MA",
            "exchanges": rows if rows is not None else [feed()], **extra}


def configuration():
    high, low = supplier(), supplier(product="electricity, low voltage")
    fallback = supplier(name="photovoltaic Morocco (por)")
    return {"schema_version": 1, "rules": [{
        "id": "pv", "inventories": ["lci-itom_por.xlsx"],
        "consumers": [{"name_prefix": "electricity supply, ", "name_contains": "for DAC methanol (por)",
                       "unit": "kilowatt hour", "location": "MA"}],
        "exchange": {"name": high["name"], "reference_products": [high["reference product"], low["reference product"]],
                     "unit": "kilowatt hour", "location": "MA"},
        "candidates": [{**{field: data[field] for field in compat.IDENTITY_FIELDS}, "reason": reason}
                       for data, reason in ((high, "native medium"), (low, "declared low voltage"))],
        "fallback": {"path": "fallback.json", "provider": {field: fallback[field] for field in compat.IDENTITY_FIELDS}},
        "reason": "declared photovoltaic fallback",
    }]}


def fallback_document():
    dataset = supplier(name="photovoltaic Morocco (por)")
    dataset["exchanges"].append({"name": "test physical burden", "type": "biosphere", "unit": "kilogram",
                                 "categories": ["air"], "amount": 0.001})
    return {"schema_version": 1, "ecoinvent_version": "3.12", "system_model": "cutoff",
            "datasets": [dataset],
            "provenance": {"source_description": "explicit test snapshot", "source_path": "portable/original.json",
                           "source_sha256": "0" * 64, "assumptions": {"lossless interface": True}}}


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="inventory_compat_")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "rules.yaml"
        self.fallback = self.path.parent / "fallback.json"
        self.write_rules(configuration())

    def write_rules(self, value):
        self.path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
        return compat.load_compatibility_rules(self.path)

    def write_fallback(self, value=None):
        self.fallback.write_text(json.dumps(value or fallback_document()), encoding="utf-8")
        return compat.load_compatibility_rules(self.path)

    @staticmethod
    def index(*datasets):
        result = {}
        for data in datasets:
            result.setdefault(dataset_key(data), []).append(data)
        return result

    def test_ordered_exact_candidates_preserve_quantities_and_uncertainty(self):
        rules = compat.load_compatibility_rules(self.path)
        source = consumer()
        original = deepcopy(source)
        low = supplier(product="electricity, low voltage")
        high = supplier()
        result = compat.effective_profile(source, source["exchanges"], self.index(low), rules)
        self.assertEqual(exchange_key(result[0]), dataset_key(low))
        expected = deepcopy(source["exchanges"][0])
        expected["reference product"] = expected["product"] = "electricity, low voltage"
        self.assertEqual(result[0], expected)
        self.assertEqual(source, original)
        result = compat.effective_profile(source, source["exchanges"], self.index(low, high), rules)
        self.assertEqual(exchange_key(result[0]), dataset_key(high))

    def test_receipt_freezes_per_consumer_choice_and_rejects_missing_target(self):
        rules = compat.load_compatibility_rules(self.path)
        low, high = supplier(product="electricity, low voltage"), supplier()
        source = consumer()
        resolver = compat.InventoryCompatibilityResolver(self.index(low), rules)
        decision = resolver.resolve(source, source["exchanges"][0], exchange_index=0)
        source[compat.METADATA_KEY] = {"schema_version": 1, "rules_sha256": rules.sha256,
                                     "inventory": "lci-itom_por.xlsx", "consumer_key": list(dataset_key(source)),
                                     "decisions": [decision]}
        result = compat.effective_profile(source, source["exchanges"], self.index(low, high), rules)
        self.assertEqual(exchange_key(result[0]), dataset_key(low))
        with self.assertRaisesRegex(compat.InventoryCompatibilityError, "missing"):
            compat.effective_profile(source, source["exchanges"], self.index(high), rules)
        other = consumer()
        fresh = compat.effective_profile(other, other["exchanges"], self.index(low, high), rules)
        self.assertEqual(exchange_key(fresh[0]), dataset_key(high))

    def test_scope_and_invalid_geographic_or_unit_candidates(self):
        rules = compat.load_compatibility_rules(self.path)
        for changed in ({"location": "NL"}, {"name": "unrelated electricity supply"}, {"unit": "megajoule"}):
            source = consumer(**changed)
            self.assertEqual(compat.effective_profile(source, source["exchanges"], {}, rules), source["exchanges"])
        source = consumer()
        self.assertEqual(compat.effective_profile(source, source["exchanges"], {}, rules, inventory="another.xlsx"),
                         source["exchanges"])
        for field, value in (("location", "GLO"), ("unit", "megajoule")):
            data = configuration()
            data["rules"][0]["candidates"][1][field] = value
            with self.assertRaisesRegex(compat.InventoryCompatibilityError, "unit or geography"):
                self.write_rules(data)

    def test_duplicate_suppliers_and_overlapping_rules_fail(self):
        rules = compat.load_compatibility_rules(self.path)
        source = consumer()
        with self.assertRaisesRegex(compat.InventoryCompatibilityError, "Ambiguous exact"):
            compat.effective_profile(source, source["exchanges"], self.index(supplier(), supplier()), rules)
        data = configuration()
        data["rules"].append(deepcopy(data["rules"][0]))
        data["rules"][1]["id"] = "overlap"
        with self.assertRaisesRegex(compat.InventoryCompatibilityError, "Overlapping"):
            self.write_rules(data)

    def test_fallback_is_read_only_and_hashes_include_unused_snapshot(self):
        rules = self.write_fallback()
        self.assertEqual(set(rules.source_paths), {self.path.resolve(), self.fallback.resolve()})
        self.assertEqual(rules.checksums[str(self.fallback.resolve())], hashlib.sha256(self.fallback.read_bytes()).hexdigest())
        source = consumer()
        provider = fallback_document()["datasets"][0]
        index = self.index(provider)
        original_index = deepcopy(index)
        result = compat.effective_profile(source, source["exchanges"], index, rules)
        self.assertEqual(result[0]["name"], "photovoltaic Morocco (por)")
        self.assertEqual(index, original_index)
        with self.assertRaisesRegex(compat.InventoryCompatibilityError, "cannot insert fallback"):
            compat.effective_profile(source, source["exchanges"], {}, rules)
        self.fallback.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(compat.InventoryCompatibilityError, "changed"):
            compat.effective_profile(source, source["exchanges"], index, rules)

    def test_missing_fallback_is_only_needed_when_native_candidates_absent(self):
        rules = compat.load_compatibility_rules(self.path)
        source = consumer()
        result = compat.effective_profile(source, source["exchanges"], self.index(supplier()), rules)
        self.assertEqual(result[0]["name"], "photovoltaic commercial")
        with self.assertRaisesRegex(compat.InventoryCompatibilityError, "Missing required fallback"):
            compat.effective_profile(source, source["exchanges"], {}, rules)

    def test_lazy_mapping_never_iterates_database_index(self):
        target = supplier(product="electricity, low voltage")

        class LazyIndex(Mapping):
            def __iter__(self):
                raise AssertionError("Index must not be materialized")

            def __len__(self):
                raise AssertionError("Index must not be counted")

            def __getitem__(self, key):
                if key == dataset_key(target):
                    return [target]
                raise KeyError(key)

        source = consumer()
        result = compat.effective_profile(source, source["exchanges"], LazyIndex(), compat.load_compatibility_rules(self.path))
        self.assertEqual(exchange_key(result[0]), dataset_key(target))

    def mock_importer_class(self, *, modern=False):
        class Additional:
            def __init__(self, records, database, path="lci-itom_por.xlsx"):
                self.database = database
                self.import_db = SimpleNamespace(data=records)
                self.path = Path(path)
                self.version_out = "3.12"
                self.version_in = "3.12"
                self.system_model = "cutoff"
                self.events = []
                self.import_product_index = {"by_fields": {}} if modern else None

            def prepare_inventory(self, value=None, *, native_option=False):
                self.events.append(("migration", len(self.import_db.data), value, native_option))
                self.events.append(("biosphere", len(self.import_db.data)))
                self.add_product_field_to_exchanges()
                return "native return"

            def _build_import_product_index(self):
                return {"by_fields": {(row["name"], row["location"], row["unit"]): row["reference product"]
                                      for row in self.import_db.data}}

            @lru_cache
            def correct_product_field(self, key):
                for record in self.import_db.data + self.database:
                    if (record["name"], record["location"], record["unit"], record["reference product"]) == key:
                        return record["reference product"]
                raise KeyError(key)

            def add_product_field_to_exchanges(self):
                if modern:
                    self.import_product_index = self._build_import_product_index()
                for data in self.import_db.data:
                    for row in data["exchanges"]:
                        if row["type"] == "technosphere":
                            product = self.correct_product_field((row["name"], row["location"], row["unit"],
                                                                  row.get("reference product", row.get("product"))))
                            row["product"] = product
                self.events.append(("native lookup", len(self.import_db.data)))

        if not modern:
            del Additional._build_import_product_index
        return Additional

    def test_old_and_new_hooks_choose_before_native_lookup_and_preserve_api(self):
        for modern in (False, True):
            with self.subTest(modern=modern):
                additional = self.mock_importer_class(modern=modern)
                self.assertTrue(compat.install_inventory_supplier_compatibility(additional, rules_path=self.path, verbose=False))
                original_prepare = additional.prepare_inventory
                self.assertFalse(compat.install_inventory_supplier_compatibility(additional, rules_path=self.path, verbose=False))
                self.assertIs(additional.prepare_inventory, original_prepare)
                source = consumer()
                low = supplier(product="electricity, low voltage")
                database = [low]
                before = deepcopy(database)
                instance = additional([source], database)
                result = instance.prepare_inventory(7, native_option=True)
                self.assertEqual(result, "native return")
                self.assertEqual(instance.events[0], ("migration", 1, 7, True))
                self.assertEqual(exchange_key(source["exchanges"][0]), dataset_key(low))
                self.assertEqual(instance.inventory_supplier_compatibility_summary["changed_exchanges"], 1)
                self.assertEqual(source[compat.METADATA_KEY]["decisions"][0]["target_key"], list(dataset_key(low)))
                self.assertEqual(database, before)
                self.assertNotIn("_inventory_supplier_compatibility_context", instance.__dict__)

    def test_fallback_injected_before_migration_and_biosphere_then_index_rebuilt(self):
        self.write_fallback()
        additional = self.mock_importer_class(modern=True)
        compat.install_inventory_supplier_compatibility(additional, rules_path=self.path, verbose=False)
        source = consumer()
        instance = additional([source], [])
        instance.prepare_inventory()
        self.assertEqual(instance.events[:2], [("migration", 2, None, False), ("biosphere", 2)])
        self.assertEqual(instance.inventory_supplier_compatibility_summary["fallback_datasets_inserted"], 1)
        target = instance.import_db.data[1]
        self.assertEqual(exchange_key(source["exchanges"][0]), dataset_key(target))
        self.assertIn((target["name"], "MA", "kilowatt hour"), instance.import_product_index["by_fields"])
        decision = source[compat.METADATA_KEY]["decisions"][0]
        self.assertEqual(decision["fallback_sha256"], hashlib.sha256(self.fallback.read_bytes()).hexdigest())

    def test_unchanged_candidate_is_audited_and_unrelated_inventory_has_no_receipt(self):
        additional = self.mock_importer_class()
        compat.install_inventory_supplier_compatibility(additional, rules_path=self.path, verbose=False)
        source = consumer()
        instance = additional([source], [supplier()])
        instance.prepare_inventory()
        self.assertEqual(instance.inventory_supplier_compatibility_summary["unchanged_exchanges"], 1)
        unrelated = consumer()
        other = additional([unrelated], [supplier()], path="another.xlsx")
        other.prepare_inventory()
        self.assertNotIn(compat.METADATA_KEY, unrelated)

    def test_installation_remains_idempotent_after_module_reload(self):
        additional = self.mock_importer_class()
        compat.install_inventory_supplier_compatibility(additional, rules_path=self.path, verbose=False)
        wrapper = additional.prepare_inventory
        importlib.reload(compat)
        self.assertFalse(compat.install_inventory_supplier_compatibility(additional, rules_path=self.path, verbose=False))
        self.assertIs(additional.prepare_inventory, wrapper)
        instance = additional([consumer()], [supplier()])
        instance.prepare_inventory()
        self.assertEqual(instance.inventory_supplier_compatibility_summary["targeted_exchanges"], 1)

    def test_repeated_prepare_keeps_frozen_choice_when_preferred_provider_appears(self):
        additional = self.mock_importer_class()
        compat.install_inventory_supplier_compatibility(additional, rules_path=self.path, verbose=False)
        source = consumer()
        low = supplier(product="electricity, low voltage")
        instance = additional([source], [low])
        instance.prepare_inventory()
        receipt = deepcopy(source[compat.METADATA_KEY])
        instance.database.append(supplier())
        instance.prepare_inventory()
        self.assertEqual(exchange_key(source["exchanges"][0]), dataset_key(low))
        self.assertEqual(source[compat.METADATA_KEY], receipt)
        self.assertEqual(instance.inventory_supplier_compatibility_summary["unchanged_exchanges"], 1)
        self.assertEqual(receipt["decisions"][0]["amount_before"], 0.5)
        self.assertEqual(receipt["decisions"][0]["amount_after"], 0.5)
        self.assertEqual(receipt["decisions"][0]["unit"], "kilowatt hour")

    def test_receipt_cannot_be_transplanted_or_change_quantities(self):
        additional = self.mock_importer_class()
        compat.install_inventory_supplier_compatibility(additional, rules_path=self.path, verbose=False)
        source = consumer()
        high = supplier()
        instance = additional([source], [high])
        instance.prepare_inventory()
        rules = compat.load_compatibility_rules(self.path)
        other = deepcopy(source)
        other["name"] = "another process"
        with self.assertRaisesRegex(compat.InventoryCompatibilityError, "transplanted"):
            compat.effective_profile(other, source["exchanges"], self.index(high), rules)
        source[compat.METADATA_KEY]["decisions"][0]["amount_after"] = 0.4
        with self.assertRaisesRegex(compat.InventoryCompatibilityError, "quantities changed"):
            compat.effective_profile(source, source["exchanges"], self.index(high), rules)

    def test_fallback_rejects_production_only_or_version_migration(self):
        document = fallback_document()
        document["datasets"][0]["exchanges"] = document["datasets"][0]["exchanges"][:1]
        self.write_fallback(document)
        additional = self.mock_importer_class()
        compat.install_inventory_supplier_compatibility(additional, rules_path=self.path, verbose=False)
        instance = additional([consumer()], [])
        with self.assertRaisesRegex(compat.InventoryCompatibilityError, "no physical inventory burdens"):
            instance.prepare_inventory()
        self.assertEqual(len(instance.import_db.data), 1)
        self.write_fallback()
        instance.version_in = "3.9"
        with self.assertRaisesRegex(compat.InventoryCompatibilityError, "Unsupported fallback migration"):
            instance.prepare_inventory()
        self.assertEqual(len(instance.import_db.data), 1)

    def test_missing_fallback_dependency_does_not_mutate_imported_records(self):
        document = fallback_document()
        document["datasets"][0]["exchanges"].append({
            "name": "photovoltaic installation", "product": "photovoltaic installation",
            "unit": "unit", "location": "RER", "type": "technosphere", "amount": 0.001})
        self.write_fallback(document)
        additional = self.mock_importer_class()
        compat.install_inventory_supplier_compatibility(additional, rules_path=self.path, verbose=False)
        instance = additional([consumer()], [])
        before = deepcopy(instance.import_db.data)
        with self.assertRaisesRegex(compat.InventoryCompatibilityError, "dependency is missing"):
            instance.prepare_inventory()
        self.assertEqual(instance.import_db.data, before)

    def test_invalid_unused_fallback_is_not_selected_with_native_provider(self):
        self.fallback.write_text("{}", encoding="utf-8")
        additional = self.mock_importer_class()
        compat.install_inventory_supplier_compatibility(additional, rules_path=self.path, verbose=False)
        source = consumer()
        instance = additional([source], [supplier()])
        instance.prepare_inventory()
        self.assertEqual(len(instance.import_db.data), 1)
        self.assertEqual(instance.inventory_supplier_compatibility_summary["unchanged_exchanges"], 1)

    def test_non_electric_supplier_rule_has_no_special_case(self):
        data = configuration()
        rule = data["rules"][0]
        rule.pop("fallback")
        rule["consumers"] = [{"name": "hydrogen import", "reference product": "hydrogen",
                              "unit": "kilogram", "location": "ES"}]
        rule["exchange"] = {"name": "steam, obsolete", "reference_products": ["steam"],
                            "unit": "megajoule", "location": "ES"}
        rule["candidates"] = [{"name": "steam production", "reference product": "steam",
                               "unit": "megajoule", "location": "ES", "reason": "documented steam identity"}]
        rules = self.write_rules(data)
        source = {**rule["consumers"][0], "exchanges": [{"type": "technosphere", "name": "steam, obsolete",
                    "product": "steam", "unit": "megajoule", "location": "ES", "amount": 12.3}]}
        target = {**rule["candidates"][0], "exchanges": []}
        result = compat.effective_profile(source, source["exchanges"], self.index(target), rules)
        self.assertEqual(exchange_key(result[0]), dataset_key(target))
        self.assertEqual(result[0]["amount"], 12.3)

    def test_fallback_requires_declared_supported_system_model(self):
        for value in (None, "apos", 123):
            with self.subTest(model=value):
                document = fallback_document()
                if value is None:
                    document.pop("system_model")
                else:
                    document["system_model"] = value
                rules = self.write_fallback(document)
                with self.assertRaises(compat.InventoryCompatibilityError):
                    compat._read_fallback(rules.rules[0], rules)

    def test_model_mismatch_fails_before_insertion_or_native_migration(self):
        self.write_fallback()
        additional = self.mock_importer_class()
        compat.install_inventory_supplier_compatibility(additional, rules_path=self.path, verbose=False)
        instance = additional([consumer()], [])
        instance.system_model = "consequential"
        before = deepcopy(instance.import_db.data)
        with self.assertRaisesRegex(compat.InventoryCompatibilityError, "system model mismatch"):
            instance.prepare_inventory()
        self.assertEqual(instance.import_db.data, before)
        self.assertEqual(instance.events, [])

    def test_frozen_fallback_reuse_rechecks_versions_and_system_model(self):
        for field, value in (("system_model", "consequential"), ("version_in", "3.11"), ("version_out", "3.11")):
            with self.subTest(field=field):
                self.write_fallback()
                additional = self.mock_importer_class()
                compat.install_inventory_supplier_compatibility(additional, rules_path=self.path, verbose=False)
                instance = additional([consumer()], [])
                instance.prepare_inventory()
                before = deepcopy(instance.import_db.data)
                events_before = list(instance.events)
                setattr(instance, field, value)
                with self.assertRaises(compat.InventoryCompatibilityError):
                    instance.prepare_inventory()
                self.assertEqual(instance.import_db.data, before)
                self.assertEqual(instance.events, events_before)

    def test_declared_model_aliases_and_consequential_snapshot_are_supported(self):
        for source_model, target_model in (("cut-off", "cutoff"), ("cutoff", "cut_off"),
                                           ("consequential", "consequential")):
            with self.subTest(source=source_model, target=target_model):
                document = fallback_document()
                document["system_model"] = source_model
                self.write_fallback(document)
                additional = self.mock_importer_class()
                compat.install_inventory_supplier_compatibility(additional, rules_path=self.path, verbose=False)
                instance = additional([consumer()], [])
                instance.system_model = target_model
                instance.prepare_inventory()
                self.assertEqual(instance.inventory_supplier_compatibility_summary["fallback_datasets_inserted"], 1)


if __name__ == "__main__":
    unittest.main()
