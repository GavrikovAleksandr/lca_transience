"""Exercise concrete Pathways instances through update and native mapping export."""
from __future__ import annotations

import ast
import csv
from copy import deepcopy
from importlib.util import find_spec
import json
from pathlib import Path
from types import MethodType, SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

import por_supplier_linking as linking


def installed_mapper():
    """Load the installed writer without importing premise or a Brightway project."""
    package = find_spec("premise")
    if package is None:
        raise unittest.SkipTest("Installed premise source is required")
    source = Path(next(iter(package.submodule_search_locations))) / "pathways.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == "PathwaysDataPackage")
    method = next(node for node in cls.body
                  if isinstance(node, ast.FunctionDef) and node.name == "_add_variables_mapping")
    namespace = {"Path": Path, "json": json, "yaml": yaml}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)
    return namespace["_add_variables_mapping"]


def activity(name="steel activity", product="steel", **extra):
    return {"name": name, "reference product": product, "unit": "kilogram", **extra}


def diagnostics():
    return {
        "material decisions": [{"activity": activity(), "status": "changed"}],
        "material update metrics": {"compiled pairs": 1, "executed pairs": 1},
    }


class PathwaysInstanceExportTest(unittest.TestCase):
    def package(self, mappings, *, clear_providers=(), instance_mapper=False):
        events = []
        scenarios = [{"model": "remind", "pathway": "SSP2-PkBudg1000", "year": 2050}
                     for _ in mappings]

        class Builder:
            def __init__(self):
                self.scenarios = scenarios

            def update(self, *args, **kwargs):
                events.append("update")
                for scenario, mapping in zip(self.scenarios, mappings):
                    scenario["mapping"] = mapping
                    scenario["database"] = [{"name": "source activity", "reference product": "source product",
                                             "unit": "kilogram", "location": "NL",
                                             "classifications": [("CPC", "test source classification")]}]
                return "native update result"

            def write_db_to_matrices(self, **kwargs):
                events.append("matrices")
                for provider in clear_providers:
                    provider.clear()

        mapper = installed_mapper()

        class PublicAliasPackage:
            _add_variables_mapping = mapper

            def __init__(self):
                self.datapackage = Builder()
                self.variables_name_change = {}

            def create_datapackage(self, name="synthetic", contributors=None, transformations=None):
                if transformations:
                    self.datapackage.update(transformations)
                else:
                    self.datapackage.update()
                self._export_datapackage(name=name, contributors=contributors)

            def _export_datapackage(self, name, contributors=None):
                events.append("export")
                self.datapackage.write_db_to_matrices(filepath="unused")
                self.variables_name_change = {}
                events.append("mapper")
                self._add_variables_mapping()
                self._add_classifications_file()
                events.append("completed")

        package = PublicAliasPackage()
        if instance_mapper:
            def override(self):
                events.append("instance mapper")
                return mapper(self)

            package._add_variables_mapping = MethodType(override, package)
        return package, events, mapper

    def attach(self, package):
        return linking.attach_por_pathways_export(
            package, policy=SimpleNamespace(verify_sources=Mock()),
            install_support=False, verbose=False,
        )

    def export(self, package, events, *, post_link=None):
        def linked(builder, **kwargs):
            events.append("linked")
            if post_link is not None:
                post_link(builder)
            return []

        with tempfile.TemporaryDirectory(prefix="por_pathways_instance_") as directory, patch.object(
            Path, "cwd", return_value=Path(directory)
        ), patch.object(linking, "apply_por_supplier_linking", side_effect=linked):
            package.create_datapackage()
            with (Path(directory) / "pathways_temp/classifications/classifications.csv").open(
                encoding="utf-8", newline=""
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertTrue(rows, "The package must not contain a header-only classification resource")
            self.assertEqual(set(rows[0]), {"name", "reference product", "classification_system", "classification_code"})
            self.assertTrue((package.classification_audit_path / "classification_audit.json").is_file())
            self.assertNotIn("pathways_temp", package.classification_audit_path.parts)
            return yaml.safe_load((Path(directory) / "pathways_temp/mapping/mapping.yaml").read_text())

    def test_classification_resource_survives_matrix_inventory_release(self):
        package, events, _ = self.package([{"metals": {"steel": [activity()]}}])
        native_matrices = package.datapackage.write_db_to_matrices

        def matrices_release_inventory(**kwargs):
            native_matrices(**kwargs)
            for scenario in package.datapackage.scenarios:
                scenario["database"].clear()
                scenario["database"] = None

        package.datapackage.write_db_to_matrices = matrices_release_inventory
        self.attach(package)
        self.export(package, events)
        self.assertIsNone(package.datapackage.scenarios[0]["database"])
        self.assertEqual(package.classifications[("source activity", "source product")]["CPC"],
                         "test source classification")

    def test_replaced_classification_guard_is_rejected_before_update(self):
        package, events, _ = self.package([{"metals": {"steel": [activity()]}}])
        self.attach(package)
        package._add_classifications_file = lambda: None
        with self.assertRaisesRegex(linking.SupplierLinkingError, "classification export guard was replaced"):
            package.create_datapackage()
        self.assertEqual(events, [])

    def test_public_alias_instance_exports_diagnostics_and_keeps_all_real_providers(self):
        fuel_a = activity("fuel A", "fuel", lhv=42.0, location="NL", exchanges=[{"amount": 1}])
        fuel_b = activity("fuel B", "fuel", lhv=40.0, location="RER")
        metal = activity()
        first = {"fuels": {"fuel": [fuel_a, fuel_a]},
                 "metals": {"steel": [metal], **diagnostics()}}
        second = {"fuels": {"fuel": [fuel_a, fuel_b]},
                  "external_0": {"PoR output": [activity("PoR activity", "PoR product")]}}
        before = deepcopy([first, second])
        package, events, mapper = self.package([first, second])
        self.assertFalse(getattr(type(package)._add_variables_mapping, "__por_pathways_mapping_fix__", False))
        self.assertTrue(self.attach(package))
        exported = self.export(package, events)
        self.assertCountEqual(exported["SE - fuels - fuel"]["dataset"], [
            activity("fuel A", "fuel", lhv=42.0), activity("fuel B", "fuel", lhv=40.0),
        ])
        self.assertEqual(exported["SE - metals - steel"]["dataset"], [activity()])
        self.assertEqual(exported["EXT - 0 - PoR output"]["dataset"], [activity("PoR activity", "PoR product")])
        self.assertEqual(set(exported), {"SE - fuels - fuel", "SE - metals - steel", "EXT - 0 - PoR output"})
        self.assertEqual([first, second], before)
        self.assertIs(package.datapackage.scenarios[0]["mapping"], first)
        self.assertIs(package.datapackage.scenarios[1]["mapping"], second)
        self.assertIs(first["fuels"]["fuel"][0], fuel_a)
        self.assertIs(type(package)._add_variables_mapping, mapper)
        self.assertEqual(events, ["update", "linked", "export", "matrices", "mapper", "completed"])
        self.assertEqual(package.classification_stats["unique_pairs"], 1)
        self.assertEqual(package.classifications[("source activity", "source product")]["CPC"],
                         "test source classification")

    def test_activity_only_legacy_mapping_keeps_lhv_and_variable_names(self):
        mapping = {"electricity": {"power": [activity("power activity", "power", lhv=0.0)]},
                   "final energy": {"heat": [activity("heat", "heat")]}}
        package, events, _ = self.package([mapping])
        self.assertTrue(self.attach(package))
        exported = self.export(package, events)
        self.assertEqual(exported["SE - electricity - power"]["dataset"], [activity("power activity", "power", lhv=0.0)])
        self.assertEqual(package.variables_name_change["power"], "SE - electricity - power")
        self.assertEqual(package.variables_name_change["heat"], "FE - final energy - heat")
        self.assertIs(package.datapackage.scenarios[0]["mapping"], mapping)

    def test_instance_method_override_is_wrapped_and_called_once(self):
        mapping = {"metals": {"steel": [activity()], **diagnostics()}}
        package, events, _ = self.package([mapping], instance_mapper=True)
        original_override = package._add_variables_mapping
        self.assertTrue(self.attach(package))
        self.assertIsNot(package._add_variables_mapping, original_override)
        exported = self.export(package, events)
        self.assertEqual(exported["SE - metals - steel"]["dataset"], [activity()])
        self.assertEqual(events.count("instance mapper"), 1)
        self.assertIs(package.datapackage.scenarios[0]["mapping"], mapping)

    def test_malformed_genuine_provider_fails_before_any_matrix_export(self):
        for missing in ("name", "reference product", "unit"):
            with self.subTest(missing=missing):
                provider = activity()
                del provider[missing]
                mapping = {"electricity": {"power": [provider]}}
                package, events, _ = self.package([mapping])
                self.assertTrue(self.attach(package))
                with self.assertRaises(ValueError) as error:
                    self.export(package, events)
                message = str(error.exception)
                for context in ("electricity", "power", missing):
                    self.assertIn(context, message)
                self.assertEqual(events, ["update", "linked"])
                self.assertIs(package.datapackage.scenarios[0]["mapping"], mapping)

    def test_diagnostic_key_in_external_sector_is_validated_as_a_real_variable(self):
        mapping = {"external_0": {"material decisions": [{"unit": "kilogram"}]},
                   "metals": diagnostics()}
        package, events, _ = self.package([mapping])
        self.assertTrue(self.attach(package))
        with self.assertRaisesRegex(ValueError, "external_0"):
            self.export(package, events)
        self.assertNotIn("matrices", events)
        self.assertIs(package.datapackage.scenarios[0]["mapping"], mapping)

    def test_metadata_snapshot_survives_native_matrix_export_clearing_providers(self):
        fuel = activity("fuel A", "fuel", lhv=42.0, exchanges=[{"amount": 1}])
        mapping = {"fuels": {"fuel": [fuel]}, "metals": diagnostics()}
        saved_diagnostics = deepcopy(mapping["metals"])
        package, events, _ = self.package([mapping], clear_providers=[fuel])
        self.assertTrue(self.attach(package))
        exported = self.export(package, events)
        self.assertEqual(exported["SE - fuels - fuel"]["dataset"], [activity("fuel A", "fuel", lhv=42.0)])
        self.assertEqual(fuel, {})
        self.assertIs(package.datapackage.scenarios[0]["mapping"], mapping)
        self.assertIs(mapping["fuels"]["fuel"][0], fuel)
        self.assertEqual(mapping["metals"], saved_diagnostics)

    def test_snapshot_is_captured_after_supplier_linking_finishes(self):
        provider = activity("original provider", "steel")
        mapping = {"steel": {"steel output": [provider]}}
        package, events, _ = self.package([mapping], clear_providers=[provider])
        self.assertTrue(self.attach(package))
        exported = self.export(package, events, post_link=lambda builder: provider.update(name="linked provider"))
        self.assertEqual(exported["SE - steel - steel output"]["dataset"], [activity("linked provider", "steel")])

    def test_idempotent_attachment_runs_one_native_update_and_one_export(self):
        mapping = {"steel": {"steel output": [activity()]}}
        package, events, _ = self.package([mapping])
        self.assertTrue(self.attach(package))
        wrapped_update = package.datapackage.update
        wrapped_mapper = package._add_variables_mapping
        self.assertFalse(self.attach(package))
        self.assertIs(package.datapackage.update, wrapped_update)
        self.assertIs(package._add_variables_mapping, wrapped_mapper)
        self.export(package, events)
        self.assertEqual(events, ["update", "linked", "export", "matrices", "mapper", "completed"])

    def test_original_mapping_is_restored_when_native_mapper_raises(self):
        mapping = {"metals": {"steel": [activity()], **diagnostics()}}
        package, events, _ = self.package([mapping])

        def failing_mapper(self):
            self.assertion_mapping = self.datapackage.scenarios[0]["mapping"]
            raise OSError("synthetic mapping write failure")

        package._add_variables_mapping = MethodType(failing_mapper, package)
        self.assertTrue(self.attach(package))
        with self.assertRaisesRegex(OSError, "synthetic mapping write failure"):
            self.export(package, events)
        self.assertNotIn("material decisions", package.assertion_mapping["metals"])
        self.assertIs(package.datapackage.scenarios[0]["mapping"], mapping)
        self.assertIn("material decisions", mapping["metals"])


if __name__ == "__main__":
    unittest.main()
