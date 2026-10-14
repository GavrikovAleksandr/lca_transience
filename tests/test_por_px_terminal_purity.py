"""Purity correction conserves local supply and preserves original ITOM data."""
import importlib.util
from pathlib import Path
import unittest

import pandas as pd
import yaml

from por_px_terminal_purity import (
    TERMINAL_RAW, EXTRACTION_GROSS, TERMINAL_PURE_LOCAL, EXTRACTION_NONTERMINAL,
    DERIVED_VARIABLES, annual_raw_px_use, reconcile_px_terminal_purity,
    verify_px_extraction_yield,
    validate_px_source_flows,
)

ROOT = Path(__file__).resolve().parents[1]


class ImportPathwaySelectionTest(unittest.TestCase):
    def test_terminal_is_a_distinct_identifier_not_a_substring(self):
        from por_supplier_linking import _import_pathway
        for variable in (
            "Intermediate Product|benzene|benzene_terminal_BAT",
            "Material Input|methanol|DAC_methanol_terminal_BATplus",
            TERMINAL_PURE_LOCAL,
            "Intermediate Product|p-xylene|terminal",
        ):
            with self.subTest(variable=variable):
                self.assertTrue(_import_pathway("route", {"production volume": {"variable": variable}}))
        for variable in (
            EXTRACTION_NONTERMINAL,
            "Intermediate Product|benzene|nonterminal_supply",
            "Intermediate Product|p-xylene|terminalization",
        ):
            with self.subTest(variable=variable):
                self.assertFalse(_import_pathway("route", {"production volume": {"variable": variable}}))

    def test_other_explicit_import_signals_remain_protected(self):
        from por_supplier_linking import _import_pathway
        entries = (
            ("route", {"production volume": {"variable": "Trade|To PoR|raw_para_xylene"}}),
            ("route", {"production volume": {"variable": "Trade|To PoR|raw_para_xylene|nonterminal_supply"}}),
            ("raw_para_xylene_trade_import_eu", {}),
            ("route", {"ecoinvent alias": {"name": "p-xylene, imported from EU (por)"}}),
        )
        for alias, entry in entries:
            with self.subTest(alias=alias, entry=entry):
                self.assertTrue(_import_pathway(alias, entry))


def fixture(terminal=80., domestic=20., extraction=90., exports=10., other=0., imported=0.):
    values = [(TERMINAL_RAW, terminal), (EXTRACTION_GROSS, extraction),
              ("Intermediate Product|raw_para_xylene|cracker", domestic),
              ("Trade|To PoR|raw_para_xylene", imported),
              ("Trade|From PoR|raw_para_xylene", -exports),
              ("Trade|To PoR|para_xylene", 17.),
              ("Final Product|polypropylene|PP", 1000.)]
    frame = pd.DataFrame([{"model": "Petchem", "scenario": "OCE", "region": "NL",
                           "variables": variable, "unit": "kt/yr", "2045": amount}
                          for variable, amount in values])
    uses = pd.DataFrame([{"model": "Petchem", "scenario": "OCE", "region": "NL", "year": 2045,
                         "technology": technology, "amount": amount}
                        for technology, amount in (("para_xylene_extraction", extraction),
                                                  ("para_xylene_terminal_BAT", other))])
    return frame, uses


class PurityReconciliationTests(unittest.TestCase):
    def test_proportional_export_split_and_no_double_count(self):
        source, uses = fixture()
        before = source.copy(deep=True)
        result, audit = reconcile_px_terminal_purity(source, uses)
        derived = result.loc[result.variables.isin(DERIVED_VARIABLES)].set_index("variables")
        self.assertAlmostEqual(derived.loc[TERMINAL_PURE_LOCAL, "2045"], 72.)
        self.assertAlmostEqual(derived.loc[EXTRACTION_NONTERMINAL, "2045"], 18.)
        self.assertAlmostEqual(derived["2045"].sum(), 90.)
        self.assertEqual(audit[0]["terminal_other_use_or_export"], 8.)
        self.assertEqual(audit[0]["local_supply_difference"], 0.)
        pd.testing.assert_frame_equal(source, before)
        pd.testing.assert_frame_equal(result.loc[~result.variables.isin(DERIVED_VARIABLES)].reset_index(drop=True), before)

    def test_no_terminal_all_terminal_and_zero_pool(self):
        for t, d, e, ex, expected in ((0., 100., 90., 10., (0., 90.)),
                                      (100., 0., 90., 10., (90., 0.)),
                                      (0., 0., 0., 0., (0., 0.)),
                                      (100., 0., 0., 100., (0., 0.))):
            with self.subTest(terminal=t, extraction=e):
                source, uses = fixture(t, d, e, ex)
                result, _ = reconcile_px_terminal_purity(source, uses)
                rows = result.set_index("variables")
                self.assertAlmostEqual(rows.loc[TERMINAL_PURE_LOCAL, "2045"], expected[0])
                self.assertAlmostEqual(rows.loc[EXTRACTION_NONTERMINAL, "2045"], expected[1])

    def test_terminal_mode2_other_use_not_subtracted_twice(self):
        source, uses = fixture(0., 100., 30., 0., other=70.)
        result, audit = reconcile_px_terminal_purity(source, uses)
        self.assertEqual(audit[0]["other_raw_use_including_terminal_exports"], 70.)
        self.assertEqual(result.set_index("variables").loc[EXTRACTION_NONTERMINAL, "2045"], 30.)

    def test_idempotent_and_conflicting_derived_rows_rejected(self):
        source, uses = fixture()
        first, audit = reconcile_px_terminal_purity(source, uses)
        second, repeated = reconcile_px_terminal_purity(first, uses)
        pd.testing.assert_frame_equal(first, second)
        self.assertEqual(audit, repeated)
        first.loc[first.variables.eq(TERMINAL_PURE_LOCAL), "2045"] = 999.
        with self.assertRaisesRegex(ValueError, "Conflicting pre-existing"):
            reconcile_px_terminal_purity(first, uses)

    def test_bad_units_duplicates_missing_values_and_balance_fail_closed(self):
        for edit in ("unit", "duplicate", "missing", "negative", "infinite", "balance", "yield"):
            source, uses = fixture()
            if edit == "unit": source.loc[0, "unit"] = "kt"
            elif edit == "duplicate": source = pd.concat([source, source.iloc[:1]], ignore_index=True)
            elif edit == "missing": source.loc[0, "2045"] = float("nan")
            elif edit == "negative": source.loc[0, "2045"] = -80.
            elif edit == "infinite": source.loc[0, "2045"] = float("inf")
            elif edit == "balance": source.loc[2, "2045"] = 99.
            elif edit == "yield": uses.loc[0, "amount"] = 89.
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                reconcile_px_terminal_purity(source, uses)

    def test_nonunit_extraction_yield_rejected(self):
        keys = {"REGION": "West", "TECHNOLOGY": "para_xylene_extraction", "MODE_OF_OPERATION": "1", "YEAR": 2045}
        inputs = pd.DataFrame([{**keys, "PRODUCT": "raw_para_xylene", "InputActivityRatio": 1.}])
        outputs = pd.DataFrame([{**keys, "PRODUCT": "para_xylene", "OutputActivityRatio": 1.}])
        verify_px_extraction_yield(inputs, outputs)
        outputs.loc[0, "OutputActivityRatio"] = .9
        with self.assertRaisesRegex(ValueError, "1:1"):
            verify_px_extraction_yield(inputs, outputs)

    def test_new_source_purity_schema_is_not_silently_ignored(self):
        source, uses = fixture()
        source.loc[0, "variables"] = "Intermediate Product|para_xylene|para_xylene_terminal_BAT"
        with self.assertRaisesRegex(ValueError, "already labels"):
            reconcile_px_terminal_purity(source, uses)

    def test_missing_original_flow_cannot_be_coerced_to_zero(self):
        table = pd.DataFrame({"PRODUCT": ["raw_para_xylene", "other"], "Transport": [float("nan"), float("nan")]})
        with self.assertRaises(ValueError):
            validate_px_source_flows(table, "Transport")
        table.loc[0, "Transport"] = 0.
        validate_px_source_flows(table, "Transport")


class RealPoRPurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("px_converter", ROOT / "0_convert_ITOM_por_to_IAMC.py")
        cls.converter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.converter)
        cls.config = yaml.safe_load((ROOT / "configuration_file/config_itom_por.yaml").read_text(encoding="utf-8"))
        cls.data = pd.read_csv(ROOT / "scenario_data/scenario_data_itom_por.csv")
        cls.uses = []
        for scenario, filename in cls.converter.SCENARIO_INPUTS.items():
            sheets = pd.read_excel(ROOT / "scenario_data" / filename, sheet_name=["use_volume", "input_factors", "output_factors"])
            verify_px_extraction_yield(sheets["input_factors"], sheets["output_factors"])
            cls.uses.append(annual_raw_px_use(sheets["use_volume"], model="Petchem", scenario=scenario,
                                            region_map=cls.converter.DEFAULT_REGION_MAP))
        cls.reconciled, cls.audit = reconcile_px_terminal_purity(cls.data, pd.concat(cls.uses, ignore_index=True))

    def test_all14_context_years_conserve_supply(self):
        self.assertEqual(len(self.audit), 14)
        for row in self.audit:
            self.assertAlmostEqual(row["terminal_local_purified"] + row["extraction_nonterminal"], row["extraction_original"])
            self.assertGreaterEqual(row["terminal_other_use_or_export"], 0.)
            self.assertLessEqual(row["terminal_local_purified"], row["terminal_gross"] + 1e-8)

    def test_oce2045_terminal_larger_than_local_use_does_not_make_negative_extraction(self):
        row = next(r for r in self.audit if r["scenario"] == "OCE" and r["year"] == 2045)
        self.assertAlmostEqual(row["terminal_gross"], 1036.284733537952)
        self.assertAlmostEqual(row["terminal_local_purified"], 761.432100106011, places=7)
        self.assertAlmostEqual(row["extraction_nonterminal"], 13.567899893989, places=7)
        self.assertAlmostEqual(row["terminal_other_use_or_export"], 274.852633431941, places=7)

    def test_config_uses_only_derived_supplies_and_retains_domestic_purification(self):
        paths = self.config["production pathways"]
        self.assertNotIn("raw_para_xylene_terminal_import_row", paths)
        self.assertEqual(paths["para_xylene_terminal_import_row"]["production volume"]["variable"], TERMINAL_PURE_LOCAL)
        self.assertEqual(paths["para_xylene_from_raw_upgrading"]["production volume"]["variable"], EXTRACTION_NONTERMINAL)
        upgrade = next(m for m in self.config["markets"] if m["name"] == "p-xylene production, from raw p-xylene upgrading (por)")
        market = next(m for m in self.config["markets"] if m["name"] == "market for p-xylene (por)")
        self.assertNotIn("para_xylene_terminal_import_row", upgrade["includes"])
        self.assertIn("para_xylene_terminal_import_row", market["includes"])
        self.assertIn("para_xylene_from_raw_upgrading", market["includes"])
        self.assertIn("raw_para_xylene_trade_import_eu", upgrade["includes"])
        self.assertEqual([a["amount"] for a in upgrade["add"]], [.055555556, .8])

    def test_purified_wrapper_is_import_protected_and_no_extra_energy(self):
        from por_supplier_linking import PorLinkingPolicy
        policy = PorLinkingPolicy.from_files()
        pair = ("p-xylene, purified, imported from outside EU (por)", "p-xylene")
        self.assertIn(pair, policy.import_pairs)
        matches = [(key, flows) for key, flows in policy.profiles.items() if key[:2] == pair]
        self.assertEqual(len(matches), 1)
        key, flows = matches[0]
        self.assertEqual(len(flows), 1)
        self.assertEqual((flows[0]["name"], flows[0]["product"], flows[0]["location"], flows[0]["amount"]),
                         ("market for p-xylene", "p-xylene", "RoW", 1))
        self.assertEqual(policy.role({"name": pair[0], "reference product": pair[1], "unit": "kilogram", "location": "NL"}), "import")

        # A local copy/import wrapper must never feed itself through the new
        # PoR market, nor switch its outside-EU source to an NL supplier.
        from por_supplier_linking import link_por_database, exchange_key
        def activity(name, location, exchanges=None):
            return {"name": name, "reference product": "p-xylene", "unit": "kilogram",
                    "location": location, "code": name + location, "exchanges": exchanges or []}
        imported = activity(pair[0], "NL", [{"name": "market for p-xylene", "product": "p-xylene",
                                            "unit": "kilogram", "location": "NL", "type": "technosphere", "amount": 1.}])
        database = [imported, activity("market for p-xylene", "RoW"),
                    activity("market for p-xylene", "NL"), activity("market for p-xylene (por)", "NL")]
        linked, _, _ = link_por_database(database, policy)
        self.assertEqual(exchange_key(linked[0]["exchanges"][0]), ("market for p-xylene", "p-xylene", "kilogram", "RoW"))

    def test_constructed_extraction_is_not_a_protected_import(self):
        from copy import deepcopy
        from por_supplier_linking import PorLinkingPolicy, link_por_database
        policy = PorLinkingPolicy.from_files()
        name = "p-xylene production, from raw p-xylene upgrading (por)"
        pair = (name, "p-xylene")
        recipe = next(m for m in self.config["markets"] if m["name"] == name)
        raw = self.config["production pathways"][recipe["includes"][0]]["ecoinvent alias"]

        def activity(name, product, unit="kilogram", location="NL", exchanges=None):
            return {"name": name, "reference product": product, "unit": unit,
                    "location": location, "code": name + location, "exchanges": exchanges or []}

        exchanges = [{"name": raw["name"], "product": raw["reference product"], "unit": "kilogram",
                      "location": "NL", "type": "technosphere", "amount": 1.}]
        providers = [activity(raw["name"], raw["reference product"])]
        for addition, unit in zip(recipe["add"], ("kilowatt hour", "megajoule")):
            exchanges.append({"name": addition["name"], "product": addition["reference product"],
                              "unit": unit, "location": "RoW", "type": "technosphere",
                              "amount": addition["amount"]})
            providers.extend(activity(addition["name"], addition["reference product"], unit, location)
                             for location in ("RoW", "NL"))
        extraction = activity(name, pair[1], exchanges=exchanges)
        self.assertNotIn(pair, policy.import_pairs)
        self.assertIn(pair, policy.domestic_pairs)
        self.assertEqual(policy.role(extraction), "constructed_market")
        # premise constructs this activity from YAML; no imported workbook
        # recipe should be requested. Its raw mix remains unchanged while
        # auxiliary energy links use the available local suppliers.
        self.assertFalse(any(key[:2] == pair for key in policy.profiles))
        before = deepcopy(extraction)
        linked, _, _ = link_por_database([extraction, *providers], policy)
        self.assertEqual(extraction, before)
        self.assertEqual(linked[0]["exchanges"][0], before["exchanges"][0])
        self.assertEqual([e["location"] for e in linked[0]["exchanges"][1:]], ["NL", "NL"])
        self.assertEqual([e["amount"] for e in linked[0]["exchanges"]], [1., .055555556, .8])
        again, _, summary = link_por_database(linked, policy)
        self.assertEqual(again, linked)
        self.assertEqual(summary["changed_exchanges"], 0)


if __name__ == "__main__":
    unittest.main()
