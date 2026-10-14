"""Notebook controls/export integration without constructing real LCA databases."""
from __future__ import annotations

import contextlib
import ast
from importlib.util import find_spec
import io
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def notebook_cells():
    notebook = json.loads((ROOT / "1_export_packages.ipynb").read_text(encoding="utf-8"))
    return ["".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code"]


class MetalsNotebookTest(unittest.TestCase):
    def test_installed_matrix_writer_keeps_policy_supply_coefficients(self):
        from metals_scenario_policy import MetalSteelPolicy, apply_database, plan_database
        spec = find_spec("premise")
        if spec is None:
            self.skipTest("Installed premise source unavailable")
        source = Path(next(iter(spec.submodule_search_locations))) / "export.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        export_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Export")
        writer = next(node for node in export_class.body
                      if isinstance(node, ast.FunctionDef) and node.name == "create_A_matrix_coordinates")
        native_geography = next(node for node in tree.body
                                if isinstance(node, ast.FunctionDef) and node.name == "check_geographical_linking")
        index_key = lambda dataset: (dataset["name"], dataset["reference product"], dataset["unit"], dataset["location"])
        namespace = {"create_index_of_A_matrix": lambda database: {index_key(ds): i for i, ds in enumerate(database)}}
        exec(compile(ast.Module(body=[writer, native_geography], type_ignores=[]), str(source), "exec"), namespace)
        product = "copper, cathode"
        def dataset(name, location):
            return {"name": name, "reference product": product, "unit": "kilogram", "location": location, "exchanges": []}
        market = dataset("market for copper, cathode", "World")
        primary, secondary = dataset("copper production, primary", "RoW"), dataset("treatment of copper scrap, refining", "RER")
        for provider, amount, kind in ((market, 1.0, "production"), (primary, 0.8, "technosphere"), (secondary, 0.2, "technosphere")):
            market["exchanges"].append({"name": provider["name"], "product": product, "unit": "kilogram",
                                        "location": provider["location"], "amount": amount, "type": kind,
                                        "uncertainty type": 0})
        policy = MetalSteelPolicy.from_mapping({
            "schema_version": 1, "nonferrous_cases": {"high_recycling": {"mode": "target"}},
            "markets": [{"id": "copper", "sector": "nonferrous", "name": market["name"],
                         "reference product": product, "locations": ["World"],
                         "targets": {"high_recycling": {2050: 0.45}}}],
        }, nonferrous_case="high_recycling")
        database = [market, primary, secondary]
        revised = apply_database(database, plan_database(database, policy, scenario={"year": 2050}), policy=policy)
        prepared = namespace["check_geographical_linking"]({"database": revised}, [])
        coordinates = namespace["create_A_matrix_coordinates"](SimpleNamespace(db=prepared["database"]))
        coefficients = {row[1]: row[2] for row in coordinates if row[0] == 0}
        self.assertAlmostEqual(coefficients[0], 1.0)
        self.assertAlmostEqual(coefficients[1], 0.55)
        self.assertAlmostEqual(coefficients[2], 0.45)
        self.assertEqual(market["exchanges"][1]["amount"], 0.8)

    def test_controls_load_native_baseline_and_allow_configured_cases(self):
        source = next(text for text in notebook_cells() if "NONFERROUS_METALS_SCENARIO =" in text)
        controls = source.split("sps_op =", 1)[0]
        namespace = {}
        with patch("primary_vs_secondary_metals.share_files.prepare_policy_share_data",
                   return_value={"status": "fixture_read_only", "product_count": 103}) as prepare, contextlib.redirect_stdout(io.StringIO()):
            exec(compile(controls, "metals_notebook_controls", "exec"), namespace)
            policy = namespace["selected_metal_steel_policy"]
            self.assertEqual((policy.nonferrous_case, policy.steel_case), ("baseline", "baseline"))
            self.assertEqual(policy.case_suffix, "")
            self.assertTrue(namespace["METALS_POLICY_FILE"].is_absolute())
            namespace["NONFERROUS_METALS_SCENARIO"] = "high_recycling"
            selected = namespace["load_selected_metal_steel_policy"]()
            self.assertEqual((selected.nonferrous_case, selected.steel_case), ("high_recycling", "baseline"))
            self.assertEqual(selected.case_suffix, "__metals-high_recycling")
            self.assertEqual(prepare.call_args.kwargs, {"source": "yaml", "force": False})
            namespace["NONFERROUS_METALS_SCENARIO"] = "not_a_case"
            with self.assertRaises((ValueError, RuntimeError)):
                namespace["load_selected_metal_steel_policy"]()

    def test_no_exogenous_steel_control_or_rules(self):
        import yaml
        self.assertNotIn("STEEL_SCENARIO", "\n".join(notebook_cells()))
        config = yaml.safe_load((ROOT / "configuration_file/metals_scenario_policy.yaml").read_text(encoding="utf-8"))
        self.assertNotIn("steel_cases", config)
        self.assertNotIn("steel", config["share_data"])
        self.assertTrue(all(rule["sector"] == "nonferrous" for rule in config["markets"]))
        self.assertFalse((ROOT / "primary_vs_secondary_metals/scenario_data/steel_secondary_share_scenarios.yaml").exists())

    def test_notebook_prepares_real_named_files_and_live_edits_change_planned_supply(self):
        import yaml
        from metals_scenario_policy import MetalSteelPolicy, StaleMetalPolicyError, plan_database
        source = next(text for text in notebook_cells() if "NONFERROUS_METALS_SCENARIO =" in text)
        controls = source.split("sps_op =", 1)[0]
        source_dir = ROOT / "primary_vs_secondary_metals/scenario_data"
        with tempfile.TemporaryDirectory(prefix="connected_metal_shares_") as directory:
            directory = Path(directory)
            for filename in ("metal_primary_secondary_shares.yaml", "metal_2050_literature_evidence.csv",
                             "metal_product_family_mapping.csv"):
                shutil.copyfile(source_dir / filename, directory / filename)
            config = yaml.safe_load((ROOT / "configuration_file/metals_scenario_policy.yaml").read_text(encoding="utf-8"))
            config["share_data"]["directory"] = str(directory)
            policy_path = directory / "policy.yaml"
            policy_path.write_text(yaml.safe_dump(config), encoding="utf-8")
            namespace = {}
            # Import/validate the notebook's controls without any production-file writes.
            with patch("primary_vs_secondary_metals.share_files.prepare_policy_share_data",
                       return_value={"status": "fixture_read_only", "product_count": 103}), contextlib.redirect_stdout(io.StringIO()):
                exec(compile(controls, "metals_notebook_controls", "exec"), namespace)
            namespace["METALS_POLICY_FILE"] = policy_path
            namespace["NONFERROUS_METALS_SCENARIO"] = "high_recycling"
            with contextlib.redirect_stdout(io.StringIO()):
                original_policy = namespace["load_selected_metal_steel_policy"]()
            selected_path = directory / "primary_secondary_split_optimistic.yaml"
            selected = yaml.safe_load(selected_path.read_text(encoding="utf-8"))
            self.assertEqual(len(selected), 103)
            selected["copper, cathode"]["shares"]["secondary"][2050] = 0.60
            selected["copper, cathode"]["shares"]["primary"][2050] = 0.40
            selected_path.write_text(yaml.safe_dump(selected), encoding="utf-8")
            with self.assertRaises(StaleMetalPolicyError):
                original_policy.verify_sources()
            with contextlib.redirect_stdout(io.StringIO()):
                edited_policy = namespace["load_selected_metal_steel_policy"]()
            def activity(name, location, exchanges=None):
                return {"name": name, "reference product": "copper, cathode", "unit": "kilogram",
                        "location": location, "exchanges": exchanges or []}
            market = activity("market for copper, cathode", "World", [
                {"name": "market for copper, cathode", "product": "copper, cathode", "unit": "kilogram", "location": "World", "type": "production", "amount": 1},
                {"name": "copper production, primary", "product": "copper, cathode", "unit": "kilogram", "location": "RoW", "type": "technosphere", "amount": 0.8},
                {"name": "treatment of copper scrap, refining", "product": "copper, cathode", "unit": "kilogram", "location": "RER", "type": "technosphere", "amount": 0.2},
            ])
            database = [market, activity("copper production, primary", "RoW"),
                        activity("treatment of copper scrap, refining", "RER")]
            plan = plan_database(database, edited_policy, scenario={"year": 2050})
            copper = next(row for row in plan.audit if row["rule_id"] == "copper_cathode")
            self.assertAlmostEqual(copper["applied_secondary_share"], 0.60)
            self.assertEqual(Path(copper["share_data_path"]).resolve(), selected_path.resolve())
            # Native baseline is not the generated file named baseline.
            native = MetalSteelPolicy.from_file(policy_path)
            self.assertEqual(plan_database(database, native, scenario={"year": 2050}).changes, ())

    def run_export(self, *, nonbaseline=False, fail=False):
        events = []
        suffix = "__metals-high_recycling" if nonbaseline else ""
        policy = SimpleNamespace(case_suffix=suffix)
        registry = {"target_OCE": {}, "target_OCE" + suffix: {}, "unrelated": {}}
        definitions = [{"model": "remind", "pathway": "test", "year": 2050,
                        "external scenarios": [{"scenario": label, "data": object()}],
                        "metal steel policy": {"old": True}, "_inventory_checkpoint": "stale"}
                       for label in ("OCE", "CL")]

        class Builder:
            def __init__(self, scenarios, **options):
                self.scenarios = scenarios
                events.append(("construct", scenarios))

            def update(self):
                events.append(("update",))
                if fail:
                    raise RuntimeError("synthetic update failure")

            def write_db_to_brightway(self, **options):
                events.append(("brightway", options["name"]))

        class Package:
            def __init__(self, scenarios, years, **options):
                self.datapackage = Builder([dict(item, year=year) for year in years for item in scenarios])
                self.classification_stats = {}
                self.classification_audit = []

            def create_datapackage(self, **options):
                self.datapackage.update()
                events.append(("package", options["name"]))

        namespace = dict(create_dbs=True, create_db_package=True, scenarios=definitions,
                         scenario_names=["target_OCE", "target_CL"],
                         pr=SimpleNamespace(NewDatabase=Builder, PathwaysDataPackage=Package),
                         bd=SimpleNamespace(databases=registry), DB_NAME_INIT="fixture", EI_VERSION="3.12",
                         KEY_PREMISE="placeholder", BIOSPHERE_DB="fixture_bio", Path=Path,
                         PATHWAYS_PACKAGE_BASENAME="remind-transience_por",
                         load_selected_metal_steel_policy=lambda: policy,
                         pd=SimpleNamespace(DataFrame=lambda rows: SimpleNamespace(head=lambda count: rows)),
                         display=lambda rows: None)
        cells = notebook_cells()
        brightway = next(text for text in cells if "if create_dbs:" in text)
        package = next(text for text in cells if "if create_db_package:" in text)

        def attach_metal(builder, **options):
            self.assertIs(options["policy"], policy)
            events.append(("metals", options["stage"]))

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch("importlib.reload", side_effect=lambda module: module))
            stack.enter_context(patch("por_supplier_linking.configure_por_export_support"))
            stack.enter_context(patch("metals_policy_workflow.attach_metal_steel_policy", side_effect=attach_metal))
            stack.enter_context(patch("metals_policy_workflow.attach_metal_steel_package_provenance",
                                      side_effect=lambda *args, **options: events.append(("provenance",))))
            stack.enter_context(patch("por_supplier_linking.attach_por_supplier_linking",
                                      side_effect=lambda *args, **options: events.append(("por", "brightway"))))
            stack.enter_context(patch("por_supplier_linking.attach_por_pathways_export",
                                      side_effect=lambda *args, **options: events.append(("por", "pathways"))))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            if fail:
                with self.assertRaisesRegex(RuntimeError, "synthetic update failure"):
                    exec(compile(brightway, "metal_brightway", "exec"), namespace)
            else:
                exec(compile(brightway, "metal_brightway", "exec"), namespace)
                exec(compile(package, "metal_package", "exec"), namespace)
        return events, registry, definitions

    def test_both_export_paths_use_same_policy_before_supplier_linking(self):
        events, _, original = self.run_export()
        labels = [entry[0] for entry in events]
        self.assertEqual(labels, ["construct", "metals", "por", "update", "brightway",
                                  "construct", "metals", "provenance", "por", "update", "package"])
        self.assertEqual(events[4][1], ["target_OCE"])
        self.assertEqual(events[-1][1], "remind-transience_por")
        for _, scenarios in [event for event in events if event[0] == "construct"]:
            self.assertTrue(scenarios)
            self.assertTrue(all("metal steel policy" not in item and "_inventory_checkpoint" not in item
                                for item in scenarios))
        self.assertEqual(original[0]["metal steel policy"], {"old": True})

    def test_nonbaseline_names_do_not_replace_baseline_database(self):
        events, registry, _ = self.run_export(nonbaseline=True)
        self.assertIn("target_OCE", registry)
        self.assertIn("unrelated", registry)
        suffix = "__metals-high_recycling"
        self.assertEqual(next(event[1] for event in events if event[0] == "brightway"), ["target_OCE" + suffix])
        self.assertEqual(events[-1][1], "remind-transience_por" + suffix)

    def test_failed_update_does_not_delete_any_database(self):
        _, registry, _ = self.run_export(fail=True)
        self.assertIn("target_OCE", registry)
        self.assertIn("unrelated", registry)


if __name__ == "__main__":
    unittest.main()
