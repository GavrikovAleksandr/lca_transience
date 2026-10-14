"""Pure inventory tests: no premise imports, actual databases or LCI edits."""
from copy import deepcopy
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from metals_scenario_policy import (
    MetalSteelPolicy, MetalPolicyError, MissingMetalSupplierError,
    StaleMetalPolicyError, apply_database, classify_supply,
    is_policy_market, plan_database,
)


def configuration():
    cases = {"baseline": {"mode": "native"}, "reference": {"mode": "reference"},
             "conservative": {"mode": "minimum"}, "high_recycling": {"mode": "minimum"},
             "primary_only": {"mode": "target"}, "custom": {"mode": "target"}}
    return {"schema_version": 1, "nonferrous_cases": deepcopy(cases), "markets": [
        {"id": "copper", "sector": "nonferrous", "name": "market for copper, cathode",
         "reference product": "copper, cathode", "unit": "kilogram", "locations": ["World"],
         "reference_shares": 0.2, "targets": {"conservative": {2025: 0.1, 2050: 0.3},
         "high_recycling": {2025: 0.2, 2050: 0.6}, "custom": {2025: 0.0, 2050: 1.0}}}
    ]}


def exchange(name, amount, product="copper, cathode", unit="kilogram", location="World", **extra):
    return dict(name=name, amount=amount, product=product, unit=unit, location=location, type="technosphere", **extra)


def supplier(exc, code):
    return {"name": exc["name"], "reference product": exc["product"], "unit": exc["unit"],
            "location": exc["location"], "database": "updated", "code": code}


def market(exchanges=None, *, steel=False, location="World"):
    product = "steel, low-alloyed" if steel else "copper, cathode"
    name = f"market for {product}"
    if exchanges is None:
        exchanges = [exchange("copper production, primary", 0.8),
                     exchange("treatment of copper scrap, refining", 0.2)]
    return {"name": name, "reference product": product, "unit": "kilogram", "location": location,
            "exchanges": [{"name": name, "amount": 1, "product": product,
                           "unit": "kilogram", "location": location, "type": "production"}] + exchanges}


def inventory(ds):
    return [ds] + [supplier(exc, f"provider{index}") for index, exc in enumerate(ds["exchanges"])
                   if exc.get("type") == "technosphere"]


def policy(case="baseline", steel_case="baseline", config=None):
    return MetalSteelPolicy.from_mapping(config or configuration(), nonferrous_case=case, steel_case=steel_case)


class MetalPolicyTests(unittest.TestCase):
    def test_native_baseline_is_exact_noop_even_anomalous(self):
        ds = market([exchange("copper production, primary", 0.7), exchange("treatment of copper scrap, refining", 0.1)])
        db = inventory(ds)
        before = deepcopy(db)
        selected = policy()
        plan = plan_database(db, selected, scenario={"year": 2050})
        output = apply_database(db, plan, policy=selected)
        self.assertEqual(db, before)
        self.assertEqual(output, before)
        self.assertIs(output[0], db[0])
        self.assertEqual(plan.changes, ())
        self.assertIn("do not sum", plan.audit[0]["warnings"])
        self.assertTrue(plan.stats["native_baseline"])

    def test_native_baseline_does_not_build_unused_supplier_index_or_consume_source(self):
        class ForbiddenSource:
            def __iter__(self):
                raise AssertionError("Native baseline must not load unused source recipes")
        with patch("metals_scenario_policy._supplier_index", side_effect=AssertionError("Unused supplier index")):
            plan = plan_database(inventory(market()), policy(), scenario={"year": 2050}, source_database=ForbiddenSource())
        self.assertEqual(plan.changes, ())

    def test_already_satisfied_case_does_not_build_supplier_index(self):
        with patch("metals_scenario_policy._supplier_index", side_effect=AssertionError("Unused supplier index")):
            plan = plan_database(inventory(market()), policy("high_recycling"), scenario={"year": 2025})
        self.assertEqual(plan.changes, ())

    def test_interpolation_and_preservation_of_nonmaterial_exchanges(self):
        ds = market()
        service = exchange("transport, freight", 0.13, product="transport", unit="ton kilometer")
        negative = exchange("treatment of copper coproduct", -0.2)
        biosphere = {"name": "Carbon dioxide, fossil", "type": "biosphere", "amount": 0.08}
        ds["exchanges"].extend((service, negative, biosphere))
        db = inventory(ds)
        selected = policy("high_recycling")
        plan = plan_database(db, selected, scenario={"year": 2037.5})
        out = apply_database(db, plan, policy=selected)
        self.assertAlmostEqual(out[0]["exchanges"][1]["amount"], 0.6)
        self.assertAlmostEqual(out[0]["exchanges"][2]["amount"], 0.4)
        self.assertEqual(out[0]["exchanges"][3:], ds["exchanges"][3:])
        self.assertEqual(db[0]["exchanges"][1]["amount"], 0.8)
        self.assertIs(out[1], db[1])

    def test_reference_uses_verified_reference_share(self):
        ds = market([exchange("copper production, primary", 0.65), exchange("treatment of copper scrap, refining", 0.35)])
        selected = policy("reference")
        out = apply_database(inventory(ds), plan_database(inventory(ds), selected, scenario={"year": 2050}), policy=selected)
        self.assertAlmostEqual(out[0]["exchanges"][2]["amount"], 0.2)

    def test_minimum_never_lowers_native_recycling(self):
        ds = market([exchange("copper production, primary", 0.1), exchange("treatment of copper scrap, refining", 0.9)])
        plan = plan_database(inventory(ds), policy("high_recycling"), scenario={"year": 2050})
        self.assertEqual(plan.changes, ())
        self.assertEqual(plan.audit[0]["status"], "already_satisfied")

    def test_primary_only_retains_zero_rows_and_no_negative_credit(self):
        ds = market()
        plan = plan_database(inventory(ds), policy("primary_only"), scenario={"year": 2050})
        out = apply_database(inventory(ds), plan)
        self.assertEqual(len(out[0]["exchanges"]), len(ds["exchanges"]))
        self.assertEqual(out[0]["exchanges"][1]["amount"], 1)
        self.assertEqual(out[0]["exchanges"][2]["amount"], 0)

    def test_custom_full_secondary_target(self):
        ds = market()
        out = apply_database(inventory(ds), plan_database(inventory(ds), policy("custom"), scenario={"year": 2050}))
        self.assertEqual(out[0]["exchanges"][1]["amount"], 0)
        self.assertEqual(out[0]["exchanges"][2]["amount"], 1)

    def test_absent_secondary_recipe_fails_actionably(self):
        ds = market([exchange("copper production, primary", 1)])
        with self.assertRaisesRegex(MissingMetalSupplierError, "no positive recipe"):
            plan_database(inventory(ds), policy("high_recycling"), scenario={"year": 2050})

    def test_missing_primary_recipe_fails(self):
        ds = market([exchange("treatment of copper scrap, refining", 1)])
        with self.assertRaisesRegex(MissingMetalSupplierError, "primary"):
            plan_database(inventory(ds), policy("primary_only"), scenario={"year": 2050})

    def test_verified_source_recipe_only_available_updated_provider(self):
        current = market([exchange("copper production, primary", 1)])
        reference = market()
        db = inventory(current) + [supplier(reference["exchanges"][2], "new_secondary")]
        db[-1]["database"] = "prospective"
        reference["exchanges"][2]["input"] = ("old", "old_secondary")
        selected = policy("high_recycling")
        plan = plan_database(db, selected, scenario={"year": 2050}, source_database=[reference])
        out = apply_database(db, plan)
        self.assertEqual(len(out[0]["exchanges"]), 3)
        self.assertEqual(out[0]["exchanges"][-1]["input"], ("prospective", "new_secondary"))
        self.assertAlmostEqual(out[0]["exchanges"][-1]["amount"], 0.6)
        self.assertEqual(plan.audit[0]["recipe_source_market"], (reference["name"], reference["reference product"], "kilogram", "World"))
        self.assertEqual(len(plan.audit[0]["recipe_source_exchanges_sha256"]), 64)
        with self.assertRaisesRegex(MissingMetalSupplierError, "absent from"):
            plan_database(inventory(current), selected, scenario={"year": 2050}, source_database=[reference])

    def test_reference_country_never_chosen_implicitly(self):
        current = market([exchange("copper production, primary", 1)])
        reference = market(location="GLO")
        db = inventory(current) + [supplier(reference["exchanges"][2], "secondary")]
        with self.assertRaises(MissingMetalSupplierError):
            plan_database(db, policy("high_recycling"), scenario={"year": 2050}, source_database=[reference])
        config = configuration()
        config["markets"][0]["reference_locations"] = ["GLO"]
        self.assertEqual(len(plan_database(db, policy("high_recycling", config=config), scenario={"year": 2050}, source_database=[reference]).changes), 1)

    def test_exogenous_steel_route_classification_is_removed(self):
        rule = {"sector": "steel", "reference product": "steel, low-alloyed"}
        with self.assertRaisesRegex(MetalPolicyError, "endogenous"):
            classify_supply(exchange("steel production, electric, scrap", 1, product="steel, low-alloyed"), rule)

    def test_steel_override_selectors_rejected(self):
        for selected in ("conservative", "high_recycling", "reference", "primary_only", "custom"):
            with self.subTest(selected=selected):
                with self.assertRaisesRegex(MetalPolicyError, "endogenous to IAM/EDM-I"):
                    policy(steel_case=selected)

    def test_world_steel_aggregate_regionweights_preserved(self):
        ds = market([exchange("market for steel, low-alloyed", 0.4, product="steel, low-alloyed", location="EUR"),
                     exchange("market for steel, low-alloyed", 0.6, product="steel, low-alloyed", location="CHA")], steel=True)
        plan = plan_database(inventory(ds), policy("high_recycling"), scenario={"year": 2050})
        self.assertEqual(plan.changes, ())
        self.assertEqual(plan.stats["matched_markets"], 0)
        out = apply_database(inventory(ds), plan)
        self.assertIs(out[0], ds)
        self.assertEqual(out[0]["exchanges"], ds["exchanges"])
        self.assertEqual(policy().describe()["steel_owner"], "endogenous IAM/EDM-I")

    def test_region_specific_target_leaves_unlisted_regions_native(self):
        def regional(location):
            return market([exchange("copper production, primary", 0.8, location=location),
                           exchange("treatment of copper scrap, refining", 0.2, location=location)], location=location)
        europe, africa = regional("EUR"), regional("AFR")
        db = inventory(europe) + inventory(africa)
        config = configuration()
        config["markets"][0]["locations"] = ["*"]
        config["markets"][0]["targets"]["custom"] = {"EUR": {2025: 0.2, 2050: 0.7}}
        selected = policy("custom", config=config)
        plan = plan_database(db, selected, scenario={"year": 2050})
        out = apply_database(db, plan)
        self.assertEqual(plan.changed_indices, (0,))
        self.assertAlmostEqual(out[0]["exchanges"][2]["amount"], 0.7)
        self.assertIs(out[3], africa)
        self.assertEqual(plan.audit[1]["status"], "region_not_targeted")

    def test_external_steel_model_cannot_be_overridden_twice(self):
        with self.assertRaisesRegex(MetalPolicyError, "endogenous"):
            plan_database([], policy(steel_case="high_recycling"), scenario={"year": 2050, "_external_steel": True})
        self.assertEqual(plan_database([], policy(), scenario={"year": 2050, "_external_steel": True}).changes, ())

    def test_consequential_baseline_allowed_but_recycling_override_rejected(self):
        context = {"year": 2050, "system_model": "consequential"}
        self.assertEqual(plan_database([], policy(), scenario=context).changes, ())
        with self.assertRaisesRegex(MetalPolicyError, "marginal suppliers"):
            plan_database([], policy("high_recycling"), scenario=context)

    def test_legacy_steel_rule_or_case_config_rejected(self):
        config = configuration()
        config["markets"].append({"id": "steel", "sector": "steel", "name": "market for steel, low-alloyed",
                                  "reference product": "steel, low-alloyed", "unit": "kilogram"})
        with self.assertRaisesRegex(MetalPolicyError, "endogenous"):
            policy(config=config)
        config["markets"][-1]["sector"] = "nonferrous"
        with self.assertRaisesRegex(MetalPolicyError, "endogenous"):
            policy(config=config)
        config = configuration()
        config["steel_cases"] = {"baseline": {"mode": "native"}, "high_recycling": {"mode": "minimum"}}
        with self.assertRaisesRegex(MetalPolicyError, "endogenous"):
            policy(config=config)
        config = configuration()
        config["steel_source"] = "external-target-file.yaml"
        with self.assertRaisesRegex(MetalPolicyError, "endogenous"):
            policy(config=config)

    def test_steel_names_and_supply_aliases_cannot_bypass_endogenous_scope(self):
        for name, product, supply_products in (
            ("market for reinforcing steel", "reinforcing steel", None),
            ("market for stainless steel", "stainless steel", None),
            ("market for reinforcement material", "reinforcement material", ["steel, low-alloyed"]),
            ("market for generic material", "generic material", ["carbon steel", "recovered metal"]),
        ):
            with self.subTest(name=name, product=product):
                config = configuration()
                rule = config["markets"][0]
                rule.update(name=name, **{"reference product": product})
                if supply_products:
                    rule["supply_products"] = supply_products
                with self.assertRaisesRegex(MetalPolicyError, "endogenous"):
                    policy(config=config)
                with self.assertRaisesRegex(MetalPolicyError, "endogenous"):
                    classify_supply(exchange("steel production", 1, product=product), rule)
        # Word boundaries avoid interpreting unrelated metal names as steel.
        config = configuration()
        config["markets"][0]["name"] = "market for copper, steelmaking-grade additive"
        self.assertTrue(policy(config=config).baseline)

    def test_mixed_units_not_rescaled(self):
        ds = market()
        ds["exchanges"].append(exchange("copper, service", 42, unit="megajoule"))
        out = apply_database(inventory(ds), plan_database(inventory(ds), policy("high_recycling"), scenario={"year": 2050}))
        self.assertEqual(out[0]["exchanges"][-1]["amount"], 42)

    def test_explicit_grade_conversion_required(self):
        ds = market([exchange("copper production, primary", 0.8, product="copper, anode"),
                     exchange("treatment of copper scrap, refining", 0.2)])
        with self.assertRaisesRegex(MetalPolicyError, "expected at least 1 kg"):
            plan_database(inventory(ds), policy("high_recycling"), scenario={"year": 2050})
        config = configuration()
        config["markets"][0]["supply_products"] = ["copper, cathode", "copper, anode"]
        self.assertEqual(len(plan_database(inventory(ds), policy("high_recycling", config=config), scenario={"year": 2050}).changes), 1)

    def test_uncertainty_none_bounds_normal_and_lognormal_scaling(self):
        ds = market([exchange("copper production, primary", 0.8, **{"uncertainty type": 3, "loc": 0.8, "scale": 0.04, "minimum": None}),
                     exchange("treatment of copper scrap, refining", 0.2, **{"uncertainty type": 2, "loc": math.log(0.2), "scale": 0.1, "maximum": 0.3})])
        out = apply_database(inventory(ds), plan_database(inventory(ds), policy("high_recycling"), scenario={"year": 2050}))
        a, b = out[0]["exchanges"][1:]
        self.assertIsNone(a["minimum"])
        self.assertAlmostEqual(a["loc"], 0.4)
        self.assertAlmostEqual(a["scale"], 0.02)
        self.assertAlmostEqual(b["loc"], math.log(0.6))
        self.assertEqual(b["scale"], 0.1)
        self.assertAlmostEqual(b["maximum"], 0.9)

    def test_old_new_uncertainty_fields_preserved_when_native(self):
        ds = market()
        ds["exchanges"][1].update({"minimum": None, "maximum": None, "loc": None, "scale": None})
        out = apply_database(inventory(ds), plan_database(inventory(ds), policy(), scenario={"year": 2050}))
        self.assertEqual(out[0], ds)

    def test_dimensionless_or_discrete_uncertainty_not_silently_scaled(self):
        for uncertainty in (6, 7, 10, 13, 99):
            with self.subTest(uncertainty=uncertainty):
                ds = market([exchange("copper production, primary", 0.8,
                                      **{"uncertainty type": uncertainty, "loc": 0.8, "scale": 2, "shape": 3}),
                             exchange("treatment of copper scrap, refining", 0.2)])
                with self.assertRaisesRegex(MetalPolicyError, "Cannot safely rescale uncertainty"):
                    plan_database(inventory(ds), policy("high_recycling"), scenario={"year": 2050})
                self.assertEqual(ds["exchanges"][1]["scale"], 2)
                self.assertEqual(plan_database(inventory(ds), policy(), scenario={"year": 2050}).changes, ())

    def test_zeroing_unsupported_uncertainty_is_deterministic(self):
        ds = market([exchange("copper production, primary", 0.8),
                     exchange("treatment of copper scrap, refining", 0.2,
                              **{"uncertainty type": 10, "loc": 0.2, "scale": 2, "shape": 3})])
        out = apply_database(inventory(ds), plan_database(inventory(ds), policy("primary_only"), scenario={"year": 2050}))
        zero = out[0]["exchanges"][2]
        self.assertEqual(zero["amount"], 0)
        self.assertEqual(zero["uncertainty type"], 0)
        self.assertNotIn("shape", zero)

    def test_dimensionful_shape_families_keep_shape_and_scale_units(self):
        for uncertainty in (8, 9, 11, 12):
            with self.subTest(uncertainty=uncertainty):
                ds = market([exchange("copper production, primary", 0.8,
                                      **{"uncertainty type": uncertainty, "loc": 0.8, "scale": 0.04, "shape": 0 if uncertainty == 11 else 3}),
                             exchange("treatment of copper scrap, refining", 0.2)])
                out = apply_database(inventory(ds), plan_database(inventory(ds), policy("high_recycling"), scenario={"year": 2050}))
                first = out[0]["exchanges"][1]
                self.assertAlmostEqual(first["loc"], 0.4)
                self.assertAlmostEqual(first["scale"], 0.02)
                self.assertEqual(first["shape"], ds["exchanges"][1]["shape"])

    def test_distribution_losses_gross_supply_and_metadata_preserved(self):
        ds = market([exchange("copper production, primary", 0.808,
                              **{"uncertainty type": 3, "loc": 0.808, "scale": 0.0404, "minimum": None}),
                     exchange("treatment of copper scrap, refining", 0.202)])
        ds["distribution loss"] = 0.01
        selected = policy("high_recycling")
        plan = plan_database(inventory(ds), selected, scenario={"year": 2050})
        out = apply_database(inventory(ds), plan)
        revised = out[0]
        self.assertAlmostEqual(revised["exchanges"][1]["amount"], 0.404)
        self.assertAlmostEqual(revised["exchanges"][2]["amount"], 0.606)
        self.assertEqual(revised["exchanges"][0]["amount"], 1)
        self.assertEqual(revised["distribution loss"], 0.01)
        self.assertAlmostEqual(revised["exchanges"][1]["scale"], 0.0202)
        self.assertIsNone(revised["exchanges"][1]["minimum"])
        self.assertAlmostEqual(plan.audit[0]["applied_supply_total"], 1.01)
        self.assertAlmostEqual(plan.audit[0]["applied_secondary_share"], 0.6)
        self.assertAlmostEqual(ds["exchanges"][1]["amount"], 0.808)

    def test_missing_material_cannot_be_normalised_into_loss_assumption(self):
        ds = market([exchange("copper production, primary", 0.7),
                     exchange("treatment of copper scrap, refining", 0.2)])
        with self.assertRaisesRegex(MetalPolicyError, "Resolve missing supply routes"):
            plan_database(inventory(ds), policy("high_recycling"), scenario={"year": 2050})

    def test_explicit_policy_requires_one_kilogram_output_even_if_share_satisfied(self):
        ds = market()
        ds["exchanges"][0]["amount"] = 2
        self.assertEqual(plan_database(inventory(ds), policy(), scenario={"year": 2025}).changes, ())
        with self.assertRaisesRegex(MetalPolicyError, "1 kg reference-production"):
            plan_database(inventory(ds), policy("high_recycling"), scenario={"year": 2025})

    def test_stale_inventory_or_plan_rejected_transactionally(self):
        ds = market()
        db = inventory(ds)
        plan = plan_database(db, policy("high_recycling"), scenario={"year": 2050})
        db[0]["exchanges"][1]["amount"] = 0.7
        with self.assertRaises(StaleMetalPolicyError):
            apply_database(db, plan)
        self.assertEqual(db[0]["exchanges"][2]["amount"], 0.2)
        plan.changes[0].new_exchanges[1]["amount"] = 8
        with self.assertRaisesRegex(StaleMetalPolicyError, "modified"):
            plan.verify()

    def test_mutated_policy_rejected(self):
        selected = policy()
        selected.configuration["markets"][0]["reference_shares"] = 0.4
        with self.assertRaises(StaleMetalPolicyError):
            selected.verify_sources()

    def test_baseline_name_reserved_for_native_quantities_and_output_names(self):
        for sector in ("nonferrous",):
            for mode in ("reference", "minimum", "target"):
                with self.subTest(sector=sector, mode=mode):
                    config = configuration()
                    config[f"{sector}_cases"]["baseline"]["mode"] = mode
                    with self.assertRaisesRegex(MetalPolicyError, "reserved for mode: native"):
                        policy(config=config)

    def test_policy_file_freshness(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "policy.yaml"
            import yaml
            path.write_text(yaml.safe_dump(configuration()), encoding="utf-8")
            selected = MetalSteelPolicy.from_file(path)
            path.write_text(path.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
            with self.assertRaises(StaleMetalPolicyError):
                selected.verify_sources()

    def test_invalid_cases_ores_and_targets_fail_closed(self):
        with self.assertRaisesRegex(MetalPolicyError, "Unknown"):
            policy("not-a-case")
        config = configuration()
        config["markets"][0]["reference product"] = "copper concentrate"
        with self.assertRaisesRegex(MetalPolicyError, "concentrates"):
            policy(config=config)
        config = configuration()
        config["markets"][0]["targets"]["high_recycling"] = {2025: 0.2, 2050: 1.2}
        with self.assertRaisesRegex(MetalPolicyError, "in \\[0, 1\\]"):
            plan_database(inventory(market()), policy("high_recycling", config=config), scenario={"year": 2050})

    def test_invalid_curves_rejected_before_inventory_update(self):
        for specification in ({2025: 0.2, 2050: float("nan")}, {"EUR": {2050: 2}},
                              {2025: 0.2, "EUR": 0.4}, {"EUR": {"World": 0.4}}, {}):
            with self.subTest(specification=specification):
                config = configuration()
                config["markets"][0]["targets"]["high_recycling"] = specification
                with self.assertRaises(MetalPolicyError):
                    policy(config=config)
        config = configuration()
        config["markets"][0]["targets"]["typo"] = 0.4
        with self.assertRaisesRegex(MetalPolicyError, "unknown case"):
            policy(config=config)

    def test_exact_market_match_not_downstream_metal_application(self):
        ds = market()
        selected = policy()
        self.assertTrue(is_policy_market(ds, selected))
        self.assertFalse(is_policy_market({**ds, "name": "copper wire production"}, selected))
        self.assertEqual(selected.case_suffix, "")
        self.assertIn("__metals-high_recycling", policy("high_recycling").case_suffix)

    def test_repo_yaml_loads_with_dated_provenance(self):
        selected = MetalSteelPolicy.from_file()
        selected.verify_sources()
        self.assertTrue(selected.baseline)
        self.assertGreaterEqual(len(selected.rules), 5)
        self.assertNotIn("__steel", policy("high_recycling").case_suffix)

    def test_baseline_missing_market_audited(self):
        plan = plan_database([], policy(), scenario={"year": 2050})
        self.assertEqual(plan.stats["matched_markets"], 0)
        self.assertTrue(all(row["status"] == "market_not_present" for row in plan.audit))

    def test_invalid_native_amount_is_warning_not_failure(self):
        ds = market([exchange("copper production, primary", None)])
        plan = plan_database([ds], policy(), scenario={"year": 2050})
        self.assertEqual(plan.changes, ())
        self.assertIn("Invalid", plan.audit[0]["warnings"])

    def test_supplier_absent_or_ambiguous_never_silently_substituted(self):
        ds = market()
        with self.assertRaisesRegex(MissingMetalSupplierError, "absent"):
            plan_database([ds], policy("high_recycling"), scenario={"year": 2050})
        db = inventory(ds)
        db.append(deepcopy(db[-1]))
        with self.assertRaisesRegex(MissingMetalSupplierError, "ambiguous"):
            plan_database(db, policy("high_recycling"), scenario={"year": 2050})


if __name__ == "__main__":
    unittest.main()
