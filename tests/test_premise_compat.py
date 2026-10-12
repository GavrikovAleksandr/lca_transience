"""Reproduce the traceback with the installed premise function, without DB writes."""
from __future__ import annotations

import ast
from copy import deepcopy
from importlib.util import find_spec
import math
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import contextlib
import io
import json

from premise_compat import install_premise_null_bounds_fix


def load_installed_relink():
    package = find_spec("premise")
    if package is None:
        raise unittest.SkipTest("Installed premise source is required")
    root = Path(next(iter(package.submodule_search_locations)))
    source = root / "external.py"
    parsed = ast.parse(source.read_text(encoding="utf-8"))
    scenario_class = next(node for node in parsed.body
                          if isinstance(node, ast.ClassDef) and node.name == "ExternalScenario")
    function = next(node for node in scenario_class.body
                    if isinstance(node, ast.FunctionDef) and node.name == "relink_to_new_datasets")
    vulnerable_fields = {node.left.value for node in ast.walk(function)
                         if isinstance(node, ast.Compare) and isinstance(node.left, ast.Constant)
                         and node.left.value in ("minimum", "maximum")
                         and len(node.ops) == 1 and isinstance(node.ops[0], ast.In)}
    if vulnerable_fields != {"minimum", "maximum"}:
        raise unittest.SkipTest("Installed premise does not have the two affected guards")

    # Load the actual pure Wurst search functions without importing package logs.
    wurst_package = find_spec("wurst")
    search_path = Path(next(iter(wurst_package.submodule_search_locations))) / "searching.py"
    search_tree = ast.parse(search_path.read_text(encoding="utf-8"))
    search_namespace = {"NoResults": LookupError, "MultipleResults": LookupError}
    definitions = [node for node in search_tree.body if isinstance(node, ast.FunctionDef)]
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(search_path), "exec"), search_namespace)
    search = SimpleNamespace(**{name: value for name, value in search_namespace.items() if callable(value)})

    namespace = {"math": math, "ws": search}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
    return namespace["relink_to_new_datasets"]


def make_scenario(exchange):
    class Scenario:
        relink_to_new_datasets = load_installed_relink()

        def __init__(self):
            self.database = [{
                "name": "PPP production", "reference product": "polypropylene",
                "location": "NL", "unit": "kilogram", "exchanges": [deepcopy(exchange)],
            }]

    return Scenario()


def relink(scenario, old_name, new_name, ratio=1.0):
    scenario.relink_to_new_datasets(
        replaces=[{"name": old_name, "product": "methanol"}],
        replaces_in=[{"location": "NL"}],
        new_name=new_name, new_ref="methanol", ratio=ratio,
        regions=["NL"], market_status={"NL": True},
    )


class PremiseNullBoundsTest(unittest.TestCase):
    def exchange(self, **extra):
        return {"name": "market for methanol", "product": "methanol", "location": "RER",
                "unit": "kilogram", "type": "technosphere", "amount": 2.0,
                "uncertainty type": 0, **extra}

    def test_original_chained_forecast_por_replacement_reproduces_typeerror(self):
        scenario = make_scenario(self.exchange())
        relink(scenario, "market for methanol", "market for methanol (SPS)")
        rewritten = scenario.database[0]["exchanges"][0]
        self.assertIsNone(rewritten["minimum"])
        self.assertIsNone(rewritten["maximum"])
        with self.assertRaisesRegex(TypeError, "NoneType"):
            relink(scenario, "market for methanol (SPS)", "market for methanol (por)")

    def test_fix_allows_chained_replacement_without_inventing_bounds(self):
        scenario = make_scenario(self.exchange())
        self.assertTrue(install_premise_null_bounds_fix(type(scenario), verbose=False))
        relink(scenario, "market for methanol", "market for methanol (SPS)")
        relink(scenario, "market for methanol (SPS)", "market for methanol (por)", ratio=1.5)
        rewritten = scenario.database[0]["exchanges"][0]
        self.assertEqual(rewritten["name"], "market for methanol (por)")
        self.assertEqual(rewritten["amount"], 3.0)
        self.assertIsNone(rewritten["minimum"])
        self.assertIsNone(rewritten["maximum"])

    def test_numeric_and_zero_bounds_keep_original_scaling(self):
        original = make_scenario(self.exchange(**{"uncertainty type": 4, "minimum": 0.0,
                                                "maximum": 4.0, "scale": 0.2}))
        fixed = make_scenario(self.exchange(**{"uncertainty type": 4, "minimum": 0.0,
                                             "maximum": 4.0, "scale": 0.2}))
        install_premise_null_bounds_fix(type(fixed), verbose=False)
        relink(original, "market for methanol", "market for methanol (SPS)", ratio=2.0)
        relink(fixed, "market for methanol", "market for methanol (SPS)", ratio=2.0)
        self.assertEqual(original.database, fixed.database)
        rewritten = fixed.database[0]["exchanges"][0]
        self.assertEqual(rewritten["minimum"], 0.0)
        self.assertEqual(rewritten["maximum"], 8.0)
        self.assertEqual(rewritten["scale"], 0.2)

    def test_null_maximum_and_existing_minimum(self):
        scenario = make_scenario(self.exchange(minimum=0.25, maximum=None))
        install_premise_null_bounds_fix(type(scenario), verbose=False)
        relink(scenario, "market for methanol", "market for methanol (por)", ratio=2.0)
        rewritten = scenario.database[0]["exchanges"][0]
        self.assertEqual(rewritten["minimum"], 0.5)
        self.assertIsNone(rewritten["maximum"])

    def test_installation_is_idempotent(self):
        scenario = make_scenario(self.exchange(minimum=None, maximum=None))
        self.assertTrue(install_premise_null_bounds_fix(type(scenario), verbose=False))
        patched = type(scenario).relink_to_new_datasets
        self.assertFalse(install_premise_null_bounds_fix(type(scenario), verbose=False))
        self.assertIs(type(scenario).relink_to_new_datasets, patched)


class ExportNotebookSafetyTest(unittest.TestCase):
    def run_export_cell(self, fail_update):
        root = Path(__file__).resolve().parents[1]
        notebook = json.loads((root / "1_export_packages.ipynb").read_text(encoding="utf-8"))
        source = next("".join(cell["source"]) for cell in notebook["cells"]
                      if cell["cell_type"] == "code" and "if create_dbs:" in "".join(cell["source"]))
        events = []

        class Registry(dict):
            def __delitem__(self, name):
                events.append(("delete", name))
                super().__delitem__(name)

        registry = Registry({"target_CL": {}, "target_OCE": {}, "remind_2050_other": {}})

        class Builder:
            def __init__(self, **kwargs):
                events.append(("construct", None))

            def update(self):
                events.append(("update", None))
                if fail_update:
                    raise RuntimeError("Synthetic update failure")

            def write_db_to_brightway(self, name):
                events.append(("write", tuple(name)))

        namespace = {
            "create_dbs": True, "pr": SimpleNamespace(NewDatabase=Builder),
            "bd": SimpleNamespace(databases=registry), "scenario_names": ["target_CL", "target_OCE"],
            "scenarios": [{"model": "remind", "pathway": "test", "year": 2050},
                          {"model": "remind", "pathway": "test", "year": 2050}],
            "DB_NAME_INIT": "test_source", "EI_VERSION": "3.12",
            "KEY_PREMISE": "test_placeholder", "BIOSPHERE_DB": "test_biosphere",
        }
        with patch("por_supplier_linking.configure_por_export_support",
                   side_effect=lambda: events.append(("install", None))), patch(
                       "por_supplier_linking.attach_por_supplier_linking",
                       side_effect=lambda *args, **kwargs: events.append(("attach", None))
                   ), contextlib.redirect_stdout(io.StringIO()):
            if fail_update:
                with self.assertRaisesRegex(RuntimeError, "Synthetic update failure"):
                    exec(compile(source, "export_notebook_cell", "exec"), namespace)
            else:
                exec(compile(source, "export_notebook_cell", "exec"), namespace)
        return registry, events

    def test_failed_update_preserves_existing_databases(self):
        registry, events = self.run_export_cell(fail_update=True)
        self.assertEqual(set(registry), {"target_CL", "target_OCE", "remind_2050_other"})
        self.assertEqual([event[0] for event in events], ["install", "construct", "attach", "update"])

    def test_success_replaces_only_named_targets_after_update(self):
        registry, events = self.run_export_cell(fail_update=False)
        self.assertEqual(set(registry), {"target_OCE", "remind_2050_other"})
        self.assertEqual([event[0] for event in events], ["install", "construct", "attach", "update", "delete", "write"])
        self.assertEqual(events[-1], ("write", ("target_CL",)))

    def test_package_reruns_receive_clean_inputs_after_a_brightway_update(self):
        root = Path(__file__).resolve().parents[1]
        notebook = json.loads((root / "1_export_packages.ipynb").read_text(encoding="utf-8"))
        cells = ["".join(c["source"]) for c in notebook["cells"] if c["cell_type"] == "code"]
        brightway = next(source for source in cells if "if create_dbs:" in source)
        package = next(source for source in cells if "if create_db_package:" in source)
        resource = object()
        definitions = [{"model": "remind", "pathway": "test", "year": 2050,
                        "external scenarios": [{"scenario": label, "data": resource}],
                        "applied functions": ["electricity", "external"],
                        "_inventory_checkpoint": "old_2050_cache"} for label in ("OCE", "CL")]
        packages = []
        class Builder:
            def __init__(self, scenarios, **kwargs):
                self.scenarios = scenarios
            def update(self):
                for item in self.scenarios:
                    item["applied functions"] = ["electricity", "external"]
                    item["_inventory_checkpoint"] = "new_runtime_cache"
            def write_db_to_brightway(self, **kwargs): pass
        class Package:
            def __init__(self, scenarios, years, **kwargs):
                self.requested_years = list(years)
                self.scenarios = [dict(item, year=year) for year in years for item in scenarios]
                self.datapackage = Builder(self.scenarios)
                packages.append(self)
            def create_datapackage(self, **kwargs):
                for item in self.scenarios:
                    self_check(item)
                self.datapackage.update()
        def self_check(item):
            self.assertNotIn("applied functions", item)
            self.assertNotIn("_inventory_checkpoint", item)
            self.assertIs(item["external scenarios"][0]["data"], resource)
        namespace = {"create_dbs": True, "create_db_package": True,
                     "pr": SimpleNamespace(NewDatabase=Builder, PathwaysDataPackage=Package),
                     "bd": SimpleNamespace(databases={}), "scenario_names": ["OCE", "CL"],
                     "scenarios": definitions, "DB_NAME_INIT": "test", "EI_VERSION": "3.12",
                     "KEY_PREMISE": "placeholder", "BIOSPHERE_DB": "test"}
        with patch("por_supplier_linking.configure_por_export_support"), patch("por_supplier_linking.attach_por_supplier_linking"), patch(
                "por_supplier_linking.attach_por_pathways_export"), patch("importlib.reload", side_effect=lambda module: module):
            exec(compile(brightway, "brightway_cell", "exec"), namespace)
            exec(compile(package, "package_cell", "exec"), namespace)
            exec(compile(package, "package_cell_repeat", "exec"), namespace)
        # The user may select two, six, or other years in the notebook. This
        # regression checks clean scenario expansion, not a fixed year policy.
        years = packages[0].requested_years
        self.assertTrue(years)
        self.assertEqual([len(p.scenarios) for p in packages], [len(definitions) * len(years)] * 2)
        self.assertEqual(packages[1].requested_years, years)
        self.assertEqual({item["year"] for item in packages[0].scenarios}, set(years))
        self.assertEqual(definitions[0]["_inventory_checkpoint"], "old_2050_cache")
        self.assertIsNot(packages[0].scenarios[0]["external scenarios"], definitions[0]["external scenarios"])


if __name__ == "__main__":
    unittest.main()
