"""Metals workflow fixtures: no production premise/Brightway initialization."""
from copy import deepcopy
from importlib.util import find_spec, module_from_spec, spec_from_file_location
from pathlib import Path
import ast
import gc
import json
import logging
import os
import pickle
import sys
import tempfile
import time
from types import MappingProxyType, ModuleType, SimpleNamespace
from typing import Any, MutableMapping
import unittest
from unittest.mock import patch
from dataclasses import replace

from metals_scenario_policy import MetalSteelPolicy, MissingMetalSupplierError
from metals_policy_workflow import (
    MetalPolicyWorkflowError, apply_metal_steel_policy, attach_metal_steel_policy,
    attach_metal_steel_package_provenance, verify_metal_steel_policy,
)


def fixture_policy(nonferrous="baseline"):
    return MetalSteelPolicy.from_mapping({
        "schema_version": 1,
        "nonferrous_cases": {"baseline": {"mode": "native"}, "high": {"mode": "minimum"},
                              "reference": {"mode": "reference"}, "primary_only": {"mode": "target"}},
        "markets": [{"id": "copper", "sector": "nonferrous", "name": "market for copper, cathode",
                     "reference product": "copper, cathode", "unit": "kilogram", "locations": ["World"],
                     "secondary": {"name_contains": ["treatment of copper scrap"]},
                     "targets": {"high": {2025: .2, 2050: .6}, "primary_only": 0.0}, "reference_shares": .2}],
    }, nonferrous_case=nonferrous)


def fixture_database(secondary=.2):
    product = "copper, cathode"
    names = ("market for copper, cathode", "copper production", "treatment of copper scrap, refining", "unrelated process")
    datasets = []
    for index, name in enumerate(names):
        reference = product if index < 3 else "other product"
        datasets.append({"name": name, "reference product": reference, "unit": "kilogram", "location": "World",
                         "database": "fixture", "code": f"code{index}",
                         "exchanges": [{"name": name, "product": reference, "unit": "kilogram", "location": "World",
                                        "type": "production", "amount": 1}]})
    for index, amount in ((1, 1 - secondary), (2, secondary)):
        datasets[0]["exchanges"].append({"name": names[index], "product": product, "unit": "kilogram",
                                          "location": "World", "type": "technosphere", "amount": amount,
                                          "input": ("fixture", f"code{index}"), "uncertainty type": 0})
    return datasets


def scenario(**storage):
    return {"model": "remind", "pathway": "test", "year": 2050,
            "external scenarios": [{"scenario": "CL"}], "applied functions": ["metals", "external"], **storage}


class FakeStore:
    """ID-aware fixture whose full graph cannot be materialized."""
    def __init__(self, database, generation=0, scenario_identity="CL"):
        self._state = SimpleNamespace(activities={}, activity_exchanges={}, exchanges={})
        self.generation, self.scenario_identity = generation, scenario_identity
        self.reads, self.forks, self.checkpoints = [], 0, 0
        self._next_exchange_id = 0
        for activity_id, dataset in enumerate(deepcopy(database)):
            self._state.activities[activity_id] = {key: value for key, value in dataset.items() if key != "exchanges"}
            self._state.activity_exchanges[activity_id] = []
            for exchange in dataset["exchanges"]:
                self._add(activity_id, exchange)

    def _add(self, activity_id, exchange):
        exchange_id = self._next_exchange_id
        self._next_exchange_id += 1
        self._state.exchanges[exchange_id] = deepcopy(exchange)
        self._state.activity_exchanges[activity_id].append(exchange_id)

    def _iter_storage_activities(self):
        for activity_id, metadata in self._state.activities.items():
            yield activity_id, metadata, ()

    def iter_activity_ids(self):
        return iter(self._state.activities)

    def activity(self, activity_id):
        def to_dict():
            self.reads.append(activity_id)
            return {**deepcopy(self._state.activities[activity_id]),
                    "exchanges": [deepcopy(self._state.exchanges[index]) for index in self._state.activity_exchanges[activity_id]]}
        return SimpleNamespace(to_dict=to_dict)

    def materialize(self, **kwargs):
        raise AssertionError("Full background materialization is forbidden")

    def fork(self, scenario_identity=None):
        self.forks += 1
        clone = FakeStore([], self.generation, scenario_identity)
        clone._state = deepcopy(self._state)
        clone._next_exchange_id = self._next_exchange_id
        return clone

    def transaction(self, label):
        store = self
        class Transaction:
            def __enter__(self): return self
            def __exit__(self, error, *_):
                if error is None: store.generation += 1
            def patch_exchange(self, exchange_id, updates, delete_fields=(), *, activity_id=None):
                exchange = deepcopy(store._state.exchanges[exchange_id])
                exchange.update(deepcopy(updates))
                for field in delete_fields: exchange.pop(field, None)
                store._state.exchanges[exchange_id] = exchange
            def add_exchange(self, activity_id, exchange): store._add(activity_id, exchange)
        return Transaction()

    def checkpoint(self, path):
        self.checkpoints += 1
        path = Path(path)
        path.mkdir(parents=True)
        (path / "fixture.pickle").write_bytes(pickle.dumps((self._state, self.generation, self._next_exchange_id)))
        return path

    @classmethod
    def open(cls, path):
        state, generation, next_id = pickle.loads((Path(path) / "fixture.pickle").read_bytes())
        result = cls([], generation)
        result._state, result._next_exchange_id = state, next_id
        return result


def fake_api():
    return SimpleNamespace(InventoryStore=FakeStore,
                           _hydrate_scenario_mapping=lambda mapping, activities: {"rebound": activities[0]},
                           _compact_scenario_mapping=lambda mapping, store, checkpoint: {"checkpoint": checkpoint, **mapping})


def immutable_report_fixture(label):
    """Match the native report's nested immutable payloads in older versions."""
    identities = frozenset({("market for copper, cathode", "copper, cathode", "World"),
                            ("copper production", "copper, cathode", "World")})
    return SimpleNamespace(scenario_identity=("remind", label, 2050, ("CL",)),
                           actual=MappingProxyType({"cycle_identities": frozenset({identities}),
                                                    "nested": MappingProxyType({"counts": MappingProxyType({"targets": 1})})}))


class WorkflowTest(unittest.TestCase):
    def apply(self, builder, directory, policy=None, **kwargs):
        with patch("metals_policy_workflow._inventory_store_api", return_value=fake_api()):
            return apply_metal_steel_policy(builder, policy=policy or fixture_policy(),
                                             report_dir=Path(directory) / "reports", verbose=False, **kwargs)

    def test_native_baseline_uses_only_metadata_and_matches_plain_inventory_audit(self):
        from metals_policy_workflow import _stage_native
        from metals_scenario_policy import plan_database
        data = fixture_database()
        original = FakeStore(data)
        original._iter_storage_activities = lambda: self.fail("Do not copy background exchange IDs")
        original.materialize = lambda **kwargs: self.fail("No full inventory materialization")
        definition = scenario(_inventory_store=original)
        builder = SimpleNamespace(scenarios=[definition])
        selected = fixture_policy()
        expected = plan_database(data, selected, scenario=definition)
        with tempfile.TemporaryDirectory() as directory, patch("metals_policy_workflow._inventory_store_api", return_value=fake_api()), \
                patch("metals_scenario_policy._supplier_index", side_effect=AssertionError("Native baseline needs no supplier index")):
            working, actual, summary, receipts = _stage_native(builder, definition, selected, None, Path(directory))
        self.assertEqual(actual.audit, expected.audit)
        self.assertEqual(actual.stats, expected.stats)
        self.assertEqual(set(original.reads), {0})
        self.assertIs(working["_inventory_store"], original)
        self.assertEqual(summary["metadata_activity_count"], len(data))
        self.assertEqual(summary["selected_activity_count"], 1)
        self.assertEqual(set(summary["phase_seconds"]), {"checkpoint_acquisition", "identity_scan", "policy_planning"})
        self.assertTrue(all(value >= 0 for value in summary["phase_seconds"].values()))
        self.assertEqual(receipts, [])

    def test_native_baseline_scans_but_does_not_materialize_unmatched_background_markets(self):
        from metals_policy_workflow import _stage_native
        original = FakeStore(fixture_database()[1:])
        original._iter_storage_activities = lambda: self.fail("Metadata scan must not access exchange IDs")
        definition = scenario(_inventory_store=original)
        with tempfile.TemporaryDirectory() as directory, patch("metals_policy_workflow._inventory_store_api", return_value=fake_api()):
            _, plan, summary, _ = _stage_native(SimpleNamespace(scenarios=[definition]), definition, fixture_policy(), None, Path(directory))
        self.assertEqual(plan.stats["matched_markets"], 0)
        self.assertEqual(summary["selected_activity_count"], 0)
        self.assertEqual(original.reads, [])

    def test_update_capture_reuses_small_native_baseline_plan_without_second_inventory_read(self):
        from metals_policy_workflow import _open_store
        original = FakeStore(fixture_database())
        definition = scenario()
        class Builder:
            def __init__(self):
                self.scenarios = [definition]
                self._validation_reports = {"original": immutable_report_fixture("CL")}
            def _store_updated_scenario(self, scenario_definition):
                scenario_definition["_inventory_store"] = original
                return original
            def update(self):
                self._store_updated_scenario(definition)
                return "updated"
        builder = Builder()
        publisher = builder._store_updated_scenario
        registry = builder._validation_reports
        report = registry["original"]
        with tempfile.TemporaryDirectory() as directory, patch("metals_policy_workflow._inventory_store_api", return_value=fake_api()), \
                patch("metals_policy_workflow._open_store", wraps=_open_store) as opens:
            attach_metal_steel_policy(builder, policy=fixture_policy(), report_dir=Path(directory), verbose=False)
            self.assertEqual(builder.update(), "updated")
            self.assertEqual(opens.call_count, 1)
        self.assertEqual(original.reads, [0])
        self.assertEqual(original.forks, 0)
        self.assertEqual(builder._store_updated_scenario, publisher)
        self.assertFalse(hasattr(builder, "_repo_native_store_capture"))
        self.assertIs(builder._validation_reports, registry)
        self.assertIs(registry["original"], report)
        self.assertTrue(builder.metal_steel_policy_reports[0]["baseline_capture_reused"])
        self.assertEqual(builder._metal_steel_policy_nonferrous_case, "baseline")

    def test_stale_prepared_baseline_is_reaudited_against_current_live_store(self):
        from metals_policy_workflow import _stage_native
        from native_inventory_access import checkpoint_token
        original = FakeStore(fixture_database())
        definition = scenario(_inventory_store=original)
        selected = fixture_policy()
        builder = SimpleNamespace(scenarios=[definition])
        with tempfile.TemporaryDirectory() as directory, patch("metals_policy_workflow._inventory_store_api", return_value=fake_api()):
            _, plan, summary, _ = _stage_native(builder, definition, selected, None, Path(directory))
            prepared = {id(definition): {"plan": plan, "summary": summary, "checkpoint_token": checkpoint_token(definition)}}
            original.generation += 1
            reports = apply_metal_steel_policy(builder, policy=selected, report_dir=Path(directory),
                                              prepared_baselines=prepared, verbose=False)
        self.assertEqual(original.reads, [0, 0])
        self.assertNotIn("baseline_capture_reused", reports[0])

    def test_native_baseline_preserves_exact_store_and_generation_no_fork_dump_or_certification(self):
        original = FakeStore(fixture_database())
        definition = scenario(_inventory_store=original, _validation_report={"store_generation": 0})
        builder = SimpleNamespace(scenarios=[definition],
                                  _certify_scenario_store=lambda *args, **kwargs: self.fail("Baseline must not certify"))
        with tempfile.TemporaryDirectory() as directory:
            reports = self.apply(builder, directory)
            self.assertTrue(Path(reports[0]["report_path"]).is_file())
        self.assertIs(definition["_inventory_store"], original)
        self.assertEqual((original.generation, original.forks, original.checkpoints), (0, 0, 0))
        self.assertEqual(set(original.reads), {0})
        self.assertEqual(reports[0]["changed_markets"], 0)
        self.assertEqual(definition["_validation_report"], {"store_generation": 0})

    def test_baseline_preserves_live_report_proxy_identity_without_deepcopy(self):
        for native in (False, True):
            with self.subTest(native=native):
                previous = immutable_report_fixture("CL")
                registry = {("remind", "CL", 2050, ("CL",)): previous}
                proxy = previous.actual["nested"]["counts"]
                storage = {"_inventory_store": FakeStore(fixture_database())} if native else {"database": fixture_database()}
                definition = scenario(**storage)
                builder = SimpleNamespace(scenarios=[definition], _validation_reports=registry)
                with tempfile.TemporaryDirectory() as directory:
                    self.apply(builder, directory, load_database_func=lambda **kwargs: kwargs["scenario"],
                               dump_database_func=lambda value: value)
                self.assertIs(builder._validation_reports, registry)
                self.assertIs(next(iter(registry.values())), previous)
                self.assertIs(previous.actual["nested"]["counts"], proxy)

    def test_nonbaseline_replaces_registry_entries_without_copying_existing_report_proxies(self):
        previous, untouched = immutable_report_fixture("CL"), immutable_report_fixture("other")
        registry = {"CL": previous, "other": untouched}
        replacement = immutable_report_fixture("CL updated")
        definition = scenario(_inventory_store=FakeStore(fixture_database()))
        builder = SimpleNamespace(scenarios=[definition], _validation_reports=registry)
        def certify(working, store, **kwargs):
            self.assertIs(builder._validation_reports["CL"], previous)
            builder._validation_reports["CL"] = replacement
            working["_validation_report"] = {"store_generation": store.generation}
        builder._certify_scenario_store = certify
        validation = SimpleNamespace(ValidationIntent=SimpleNamespace,
                                     inventory_baseline_snapshot=lambda store: ({}, frozenset()),
                                     inventory_cycle_signatures=lambda store: frozenset())
        with tempfile.TemporaryDirectory() as directory, patch("metals_policy_workflow._inventory_validation_api", return_value=validation):
            self.apply(builder, directory, fixture_policy("high"))
        self.assertIs(builder._validation_reports, registry)
        self.assertIs(registry["CL"], replacement)
        self.assertIs(registry["other"], untouched)
        self.assertEqual(previous.actual["nested"]["counts"]["targets"], 1)

    def test_failure_restores_registry_object_and_original_report_associations(self):
        previous = {name: immutable_report_fixture(name) for name in ("CL", "OCE", "unrelated")}
        registry = dict(previous)
        stores = [FakeStore(fixture_database()), FakeStore(fixture_database())]
        definitions = [scenario(_inventory_store=store) for store in stores]
        definitions[0]["pathway"], definitions[1]["pathway"] = "CL", "OCE"
        builder = SimpleNamespace(scenarios=definitions, _validation_reports=registry)
        calls = []
        def certify(working, store, **kwargs):
            identity = working["pathway"]
            calls.append(identity)
            builder._validation_reports[identity] = immutable_report_fixture(identity + " updated")
            builder._validation_reports["new association"] = immutable_report_fixture("new")
            if identity == "OCE":
                # Even a registry rebind must restore the initial registry,
                # whose associations were already mutated by the first pass.
                builder._validation_reports = dict(builder._validation_reports)
                raise RuntimeError("later certification failed")
            working["_validation_report"] = {"store_generation": store.generation}
        builder._certify_scenario_store = certify
        validation = SimpleNamespace(ValidationIntent=SimpleNamespace,
                                     inventory_baseline_snapshot=lambda store: ({}, frozenset()),
                                     inventory_cycle_signatures=lambda store: frozenset())
        with tempfile.TemporaryDirectory() as directory, patch("metals_policy_workflow._inventory_validation_api", return_value=validation):
            with self.assertRaisesRegex(RuntimeError, "later certification failed"):
                self.apply(builder, directory, fixture_policy("high"))
        self.assertEqual(calls, ["CL", "OCE"])
        self.assertIs(builder._validation_reports, registry)
        self.assertEqual(set(registry), set(previous))
        for key, report in previous.items():
            self.assertIs(registry[key], report)
        for definition, store in zip(definitions, stores):
            self.assertIs(definition["_inventory_store"], store)
            self.assertEqual(store.generation, 0)
            self.assertNotIn("metal steel policy", definition)

    def test_native_checkpoint_baseline_retains_old_checkpoint_and_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            original = FakeStore(fixture_database()).checkpoint(Path(directory) / "original")
            handoff = FakeStore([])
            definition = scenario(_inventory_checkpoint=original, _inventory_export_handoff=handoff)
            self.apply(SimpleNamespace(scenarios=[definition]), directory)
            self.assertEqual(definition["_inventory_checkpoint"], original)
            self.assertIs(definition["_inventory_export_handoff"], handoff)
            self.assertTrue(original.exists())

    def test_native_baseline_export_detects_a_stale_certificate_without_mutation(self):
        original = FakeStore(fixture_database())
        definition = scenario(_inventory_store=original, _validation_report={"store_generation": 0})
        builder = SimpleNamespace(scenarios=[definition])
        with tempfile.TemporaryDirectory() as directory:
            self.apply(builder, directory)
        original.generation = 1
        with patch("metals_policy_workflow._inventory_store_api", return_value=fake_api()):
            with self.assertRaisesRegex(MetalPolicyWorkflowError, "certificate is stale"):
                verify_metal_steel_policy(builder, fixture_policy())
        self.assertEqual(original.forks, 0)

    def test_native_override_forks_and_preserves_all_original_ids(self):
        original = FakeStore(fixture_database())
        definition = scenario(_inventory_store=original)
        original_state = deepcopy(original._state)
        with tempfile.TemporaryDirectory() as directory:
            reports = self.apply(SimpleNamespace(scenarios=[definition]), directory, fixture_policy("high"))
        staged = definition["_inventory_store"]
        self.assertIsNot(staged, original)
        self.assertEqual(original._state, original_state)
        self.assertEqual(original.generation, 0)
        self.assertEqual(staged._state.activity_exchanges, original._state.activity_exchanges)
        self.assertEqual([exc["amount"] for exc in staged.activity(0).to_dict()["exchanges"]], [1, .4, .6])
        self.assertEqual(set(original.reads), {0})
        self.assertEqual(reports[0]["changed_markets"], 1)

    def test_override_checkpoint_rebinds_mapping_and_removes_old_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            original = FakeStore(fixture_database()).checkpoint(Path(directory) / "original")
            definition = scenario(_inventory_checkpoint=original, _inventory_export_handoff=FakeStore([]),
                                  mapping={"old": "reference"})
            self.apply(SimpleNamespace(scenarios=[definition]), directory, fixture_policy("high"))
            self.assertNotEqual(definition["_inventory_checkpoint"], original)
            self.assertNotIn("_inventory_export_handoff", definition)
            self.assertNotIn("_inventory_store", definition)
            self.assertEqual(definition["mapping"]["checkpoint"], definition["_inventory_checkpoint"])
            new = FakeStore.open(definition["_inventory_checkpoint"])
            self.assertEqual(new.activity(0).to_dict()["exchanges"][2]["amount"], .6)
            self.assertEqual(FakeStore.open(original).activity(0).to_dict()["exchanges"][2]["amount"], .2)

    def test_later_scenario_failure_publishes_none_and_preserves_builder_reports(self):
        good, bad = FakeStore(fixture_database()), FakeStore(fixture_database()[:2])
        definitions = [scenario(_inventory_store=good), scenario(_inventory_store=bad)]
        builder = SimpleNamespace(scenarios=definitions, _validation_reports={"old": "untouched"})
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(MissingMetalSupplierError):
                self.apply(builder, directory, fixture_policy("high"))
        self.assertIs(definitions[0]["_inventory_store"], good)
        self.assertIs(definitions[1]["_inventory_store"], bad)
        self.assertFalse(any("metal steel policy" in entry for entry in definitions))
        self.assertEqual(builder._validation_reports, {"old": "untouched"})

    def test_stale_native_certificate_is_not_reused_after_override(self):
        definition = scenario(_inventory_store=FakeStore(fixture_database()), _validation_report={"store_generation": 0})
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(MetalPolicyWorkflowError, "certificate"):
                self.apply(SimpleNamespace(scenarios=[definition]), directory, fixture_policy("high"))
        self.assertEqual(definition["_inventory_store"].generation, 0)

    def test_legacy_baseline_never_dumps_or_deletes_and_retains_exact_pointer(self):
        data = fixture_database()
        path = Path("existing_source.pickle")
        definition = scenario(**{"database filepath": path})
        calls = []
        def loader(**kwargs):
            calls.append(kwargs)
            return {**kwargs["scenario"], "database": data}
        with tempfile.TemporaryDirectory() as directory:
            reports = self.apply(SimpleNamespace(scenarios=[definition]), directory,
                                 load_database_func=loader,
                                 dump_database_func=lambda *_: self.fail("Baseline must not dump"))
        self.assertEqual(definition["database filepath"], path)
        self.assertNotIn("database", definition)
        self.assertFalse(calls[0]["delete"])
        self.assertEqual(calls[0]["original_database"], [])
        self.assertEqual(reports[0]["inventory_storage"], "legacy_cache")

    def test_legacy_override_persists_new_cache_preserving_original(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "old.pickle"
            original.write_bytes(pickle.dumps(fixture_database()))
            definition = scenario(**{"database filepath": original})
            def loader(**kwargs):
                result = kwargs["scenario"]
                result["database"] = pickle.loads(Path(result["database filepath"]).read_bytes())
                return result
            def dumper(working):
                destination = Path(directory) / "new.pickle"
                destination.write_bytes(pickle.dumps(working.pop("database")))
                working["database filepath"] = destination
            self.apply(SimpleNamespace(scenarios=[definition]), directory, fixture_policy("high"),
                       load_database_func=loader, dump_database_func=dumper)
            self.assertTrue(original.exists())
            self.assertNotEqual(definition["database filepath"], original)
            self.assertEqual(pickle.loads(original.read_bytes())[0]["exchanges"][2]["amount"], .2)
            self.assertEqual(pickle.loads(definition["database filepath"].read_bytes())[0]["exchanges"][2]["amount"], .6)

    def test_hook_idempotence_switch_rejection_order_and_export_guard(self):
        definition = scenario(database=fixture_database())
        events = []
        builder = SimpleNamespace(scenarios=[definition], update=lambda **kw: events.append(kw) or "native result",
                                  write_db_to_brightway=lambda **kw: events.append("export") or "exported")
        policy = fixture_policy("high")
        with tempfile.TemporaryDirectory() as directory:
            arguments = dict(policy=policy, report_dir=Path(directory), verbose=False,
                             load_database_func=lambda **kw: kw["scenario"], dump_database_func=lambda value: value)
            self.assertTrue(attach_metal_steel_policy(builder, **arguments))
            self.assertFalse(attach_metal_steel_policy(builder, **arguments))
            with self.assertRaisesRegex(MetalPolicyWorkflowError, "switch"):
                attach_metal_steel_policy(builder, policy=fixture_policy(), verbose=False)
            self.assertEqual(builder.update(persist=False), "native result")
            self.assertEqual(builder.write_db_to_brightway(name="fixture"), "exported")
            definition["database"][0]["exchanges"][2]["amount"] = .4
            with self.assertRaisesRegex(MetalPolicyWorkflowError, "market changed"):
                builder.write_db_to_brightway(name="fixture")
        self.assertEqual(events, [{"persist": False}, "export"])
        linked_builder = SimpleNamespace(_por_supplier_linking_attached=True)
        with self.assertRaisesRegex(MetalPolicyWorkflowError, "before PoR"):
            attach_metal_steel_policy(linked_builder, policy=fixture_policy(), verbose=False)

    def test_source_capture_loads_market_only_before_native_update_releases_reference(self):
        source = FakeStore(fixture_database())
        definition = scenario(_inventory_store=FakeStore(fixture_database()))
        builder = SimpleNamespace(_source_inventory_store=source, scenarios=[definition], update=lambda: None)
        with tempfile.TemporaryDirectory() as directory:
            attach_metal_steel_policy(builder, policy=fixture_policy("high"), report_dir=Path(directory), verbose=False)
        self.assertEqual(set(source.reads), {0})
        self.assertEqual(source.forks, 0)

    def test_endogenous_external_steel_coexists_with_nonferrous_sensitivity(self):
        forms = [
            {"external scenarios": [{"data": SimpleNamespace(descriptor={"name": "edm-i"})}]},
            {"external scenarios": [{"data": "datapackage_edm_i.json"}]},
            {"steel sector owner": "external"},
            {"sector_owners": {"steel": "edm-i"}},
            {"external_steel": True},
            {"_external_steel": True},
        ]
        for owner in forms:
            with self.subTest(owner=owner):
                definition = scenario(database=fixture_database())
                definition.update(owner)
                builder = SimpleNamespace(scenarios=[definition], update=lambda: None)
                self.assertTrue(attach_metal_steel_policy(builder, policy=fixture_policy("high"), verbose=False))

    def test_nonferrous_override_preserves_endogenous_steel_dataset_and_amounts(self):
        steel = {"name": "market for steel, low-alloyed", "reference product": "steel, low-alloyed",
                 "unit": "kilogram", "location": "EUR", "exchanges": [
                     {"name": "steel production, converter", "type": "technosphere", "product": "steel, low-alloyed",
                      "unit": "kilogram", "location": "EUR", "amount": .3},
                     {"name": "steel production, electric", "type": "technosphere", "product": "steel, low-alloyed",
                      "unit": "kilogram", "location": "EUR", "amount": .7}]}
        data = fixture_database() + [steel]
        before = deepcopy(steel)
        builder = SimpleNamespace(scenarios=[scenario(database=data)], update=lambda: None)
        with tempfile.TemporaryDirectory() as directory:
            reports = self.apply(builder, directory, fixture_policy("high"),
                                 load_database_func=lambda **kwargs: kwargs["scenario"], dump_database_func=lambda value: value)
        self.assertIs(builder.scenarios[0]["database"][-1], steel)
        self.assertEqual(steel, before)
        self.assertEqual(reports[0]["steel_owner"], "endogenous")
        self.assertNotIn("steel_case", reports[0])
        self.assertTrue(all(rule["sector"] == "nonferrous" for rule in reports[0]["supported_market_rules"]))

    def test_consequential_and_substitution_allow_only_native_baseline(self):
        for system_model in ("consequential", "substitution"):
            with self.subTest(system_model=system_model):
                builder = SimpleNamespace(scenarios=[scenario(database=fixture_database())], system_model=system_model,
                                          update=lambda: self.fail("Unsupported policy must fail before update"))
                with self.assertRaisesRegex(MetalPolicyWorkflowError, "support cutoff/apos"):
                    attach_metal_steel_policy(builder, policy=fixture_policy("high"), verbose=False)
                self.assertTrue(attach_metal_steel_policy(builder, policy=fixture_policy(), verbose=False))

    def test_baseline_warnings_and_declared_scope_are_in_reports(self):
        data = fixture_database()
        data[0]["exchanges"][1]["amount"] = 1.0
        policy_configuration = deepcopy(fixture_policy().configuration)
        policy_configuration["excluded_scope"] = [{"products": ["ore"], "reason": "Outside refined market scope"}]
        policy = MetalSteelPolicy.from_mapping(policy_configuration)
        builder = SimpleNamespace(scenarios=[scenario(database=data)])
        with tempfile.TemporaryDirectory() as directory:
            reports = self.apply(builder, directory, policy,
                                 load_database_func=lambda **kwargs: kwargs["scenario"], dump_database_func=lambda value: value)
        self.assertEqual(reports[0]["warning_rows"], 1)
        self.assertEqual(reports[0]["excluded_scope"], policy_configuration["excluded_scope"])
        self.assertEqual(reports[0]["policy_description"]["nonferrous_case"], policy.nonferrous_case)
        self.assertEqual(reports[0]["policy_description"]["steel_owner"], "endogenous")
        self.assertNotIn("steel_case", reports[0]["policy_description"])
        self.assertIs(builder.scenarios[0]["database"], data)

    def test_package_provenance_is_json_only_written_before_native_archive_build(self):
        policy = fixture_policy()
        report = {"policy_fingerprint": policy.fingerprint, "changed_markets": 0}
        observed = []
        package = SimpleNamespace(datapackage=SimpleNamespace(metal_steel_policy_reports=[report]))
        package._build_datapackage = lambda **kw: observed.append(json.loads(Path("pathways_temp/metal_steel_policy.json").read_text())) or "archive"
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                self.assertTrue(attach_metal_steel_package_provenance(package, policy, verbose=False))
                self.assertFalse(attach_metal_steel_package_provenance(package, policy, verbose=False))
                self.assertEqual(package._build_datapackage(name="fixture"), "archive")
                self.assertEqual(list(Path("pathways_temp").iterdir()), [Path("pathways_temp/metal_steel_policy.json")])
            finally:
                os.chdir(original_cwd)
        self.assertEqual(observed[0]["nonferrous_case"], "baseline")
        self.assertEqual(observed[0]["steel_owner"], "endogenous")
        self.assertNotIn("steel_case", observed[0])
        self.assertEqual(observed[0]["scenario_reports"], [report])
        self.assertEqual(observed[0]["configuration_snapshot"], json.loads(json.dumps(policy.configuration, default=str)))

    def test_missing_updated_inventory_does_not_fall_back_to_reference(self):
        builder = SimpleNamespace(scenarios=[scenario()], database=fixture_database())
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(MetalPolicyWorkflowError, "Updated inventory"):
                self.apply(builder, directory,
                           load_database_func=lambda **kwargs: self.fail("No source fallback"), dump_database_func=lambda value: value)


class NativeWorkflowIntegrationTest(unittest.TestCase):
    """Import native storage/validation files under an isolated fixture package."""
    @classmethod
    def setUpClass(cls):
        package = find_spec("premise")
        if package is None:
            raise unittest.SkipTest("premise source unavailable")
        cls.directory = Path(next(iter(package.submodule_search_locations)))
        if not (cls.directory / "inventory_store.py").is_file():
            raise unittest.SkipTest("Older premise uses legacy caches; tested above")
        cls.package_name = "_metals_native_fixture"
        module = ModuleType(cls.package_name)
        module.__path__ = [str(cls.directory)]
        cleanup = ModuleType(cls.package_name + ".cache_cleanup")
        cleanup.mark_checkpoint = lambda *_args, **_kwargs: None
        sys.modules[module.__name__], sys.modules[cleanup.__name__] = module, cleanup
        cls.module_names = [module.__name__, cleanup.__name__]
        for name in ("inventory_copy", "inventory_store"):
            spec = spec_from_file_location(cls.package_name + "." + name, cls.directory / (name + ".py"))
            loaded = module_from_spec(spec)
            sys.modules[spec.name] = loaded
            cls.module_names.append(spec.name)
            spec.loader.exec_module(loaded)
        cls.api = loaded
        constants = ModuleType(cls.package_name + ".filesystem_constants")
        constants.DATA_DIR = cls.directory / "data"
        sys.modules[constants.__name__] = constants
        cls.module_names.append(constants.__name__)
        spec = spec_from_file_location(cls.package_name + ".validation_framework", cls.directory / "validation_framework.py")
        cls.validation = module_from_spec(spec)
        sys.modules[spec.name] = cls.validation
        cls.module_names.append(spec.name)
        spec.loader.exec_module(cls.validation)
        import numpy as np
        import pandas as pd
        utils = ModuleType(cls.package_name + ".utils")
        utils.__dict__.update(Any=Any, MutableMapping=MutableMapping, np=np, pd=pd)
        tree = ast.parse((cls.directory / "utils.py").read_text(encoding="utf-8"))
        constants = {"_SCENARIO_TRIMMED_DATASET_FIELDS", "_SCENARIO_TRIMMED_EXCHANGE_FIELDS", "_CACHE_VALUE_CONTAINER_TYPES", "_CACHE_VALUE_INTEGER_TYPES"}
        functions = {"_has_cache_value", "_scenario_metadata_value_is_restored", "_normalize_scenario_cache_activity",
                     "_scenario_cache_exchange_field_is_restored", "_normalize_scenario_cache_exchange"}
        nodes = [node for node in tree.body if (isinstance(node, ast.FunctionDef) and node.name in functions)
                 or (isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in constants for target in node.targets))]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "metals_native_normalization", "exec"), utils.__dict__)
        sys.modules[utils.__name__] = utils
        cls.module_names.append(utils.__name__)

    @classmethod
    def tearDownClass(cls):
        for name in cls.module_names:
            sys.modules.pop(name, None)

    def validating_builder(self, definitions):
        tree = ast.parse((self.directory / "new_database.py").read_text(encoding="utf-8"))
        new_database = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "NewDatabase")
        fn = next(node for node in new_database.body if isinstance(node, ast.FunctionDef) and node.name == "_certify_scenario_store")
        namespace = vars(self.validation).copy()
        namespace.update(InventoryStore=self.api.InventoryStore, time=time, replace=replace,
                         logger=logging.getLogger("metals_native_fixture"))
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "native_metals_certify", "exec"), namespace)
        builder = SimpleNamespace(scenarios=definitions, version="3.12", system_model="cutoff", _validation_enabled=True,
                                  _scenario_identity=lambda definition: "CL", _validation_source_fingerprint=lambda: "source",
                                  _validation_iam_fingerprint=lambda definition: "IAM",
                                  _ensure_validation_baseline_cycles=lambda: frozenset())
        builder._certify_scenario_store = lambda *args, **kwargs: namespace["_certify_scenario_store"](builder, *args, **kwargs)
        return builder

    def native_immutable_report(self, identity="CL", generation=0):
        """Use the real frozen class which reproduces the reported failure."""
        cycle = (("market for copper, cathode", "copper, cathode", "World"),
                 ("copper production", "copper, cathode", "World"))
        rule = self.validation.ValidationRuleResult(
            rule_id="FIXTURE.IMMUTABLE_NATIVE_REPORT", severity="error", applicability="applicable",
            checked_object_count=1, expected={"scope": {"targets": 1}},
            actual={"cycle_identities": (cycle,), "nested": {"supplier_counts": {"total": 2}}})
        return self.validation.ValidationReport(
            scenario_identity=("remind", identity, 2050, ("CL",)), store_generation=generation,
            ruleset_version=self.validation.VALIDATION_RULESET_VERSION,
            certificate_key="fixture_previous_" + identity, rule_results=(rule,))

    def test_actual_native_report_proxy_survives_audit_only_baseline(self):
        store = self.api.create_inventory_store(fixture_database(), scenario_identity="CL")
        previous = self.native_immutable_report()
        registry = {"CL": previous}
        actual_proxy = previous.rule_results[0].actual["nested"]["supplier_counts"]
        self.assertIsInstance(actual_proxy, MappingProxyType)
        # This isolated fixture demonstrates why copying the registry's values
        # failed in the user's actual report; the workflow must not do this.
        with self.assertRaisesRegex(TypeError, "mappingproxy"):
            deepcopy(registry)
        definition = scenario(_inventory_store=store, _validation_report=previous.to_dict())
        builder = SimpleNamespace(scenarios=[definition], _validation_reports=registry)
        with tempfile.TemporaryDirectory() as directory, patch("metals_policy_workflow._inventory_store_api", return_value=self.api):
            apply_metal_steel_policy(builder, policy=fixture_policy(), report_dir=Path(directory), verbose=False)
        self.assertIs(builder._validation_reports, registry)
        self.assertIs(registry["CL"], previous)
        self.assertIs(previous.rule_results[0].actual["nested"]["supplier_counts"], actual_proxy)
        self.assertIs(definition["_inventory_store"], store)
        self.assertEqual(store.generation, 0)

    def test_actual_native_certificate_replaces_changed_report_without_copying_previous(self):
        store = self.api.create_inventory_store(fixture_database(), scenario_identity="CL")
        previous, untouched = self.native_immutable_report(), self.native_immutable_report("other")
        registry = {"CL": previous, "other": untouched}
        previous_payload = json.dumps(previous.to_dict(), sort_keys=True)
        actual_proxy = previous.rule_results[0].actual["nested"]["supplier_counts"]
        definition = scenario(_inventory_store=store, _validation_report=previous.to_dict())
        builder = self.validating_builder([definition])
        builder._validation_reports = registry
        with tempfile.TemporaryDirectory() as directory, patch("metals_policy_workflow._inventory_store_api", return_value=self.api), patch(
                "metals_policy_workflow._inventory_validation_api", return_value=self.validation):
            apply_metal_steel_policy(builder, policy=fixture_policy("high"), report_dir=Path(directory), verbose=False)
        self.assertIs(builder._validation_reports, registry)
        self.assertIsNot(registry["CL"], previous)
        self.assertIs(registry["other"], untouched)
        self.assertIs(previous.rule_results[0].actual["nested"]["supplier_counts"], actual_proxy)
        self.assertEqual(json.dumps(previous.to_dict(), sort_keys=True), previous_payload)
        self.assertEqual(registry["CL"].store_generation, definition["_inventory_store"].generation)
        self.assertFalse(registry["CL"].errors)

    def test_real_native_report_registry_rolls_back_if_reports_fail_after_certification(self):
        stores = [self.api.create_inventory_store(fixture_database(), scenario_identity=identity) for identity in ("CL", "OCE")]
        previous = {identity: self.native_immutable_report(identity) for identity in ("CL", "OCE", "unrelated")}
        registry = dict(previous)
        definitions = [scenario(_inventory_store=store, _validation_report=previous[identity].to_dict())
                       for identity, store in zip(("CL", "OCE"), stores)]
        for identity, definition in zip(("CL", "OCE"), definitions):
            definition["pathway"] = identity
        builder = self.validating_builder(definitions)
        builder._scenario_identity = lambda definition: definition["pathway"]
        builder._validation_reports = registry
        certified = []
        def fail_audits(*args, **kwargs):
            for identity in ("CL", "OCE"):
                self.assertIsNot(builder._validation_reports[identity], previous[identity])
                self.assertEqual(builder._validation_reports[identity].store_generation, 1)
                certified.append(identity)
            raise RuntimeError("audit publication failed")
        with tempfile.TemporaryDirectory() as directory, patch("metals_policy_workflow._inventory_store_api", return_value=self.api), patch(
                "metals_policy_workflow._inventory_validation_api", return_value=self.validation), patch(
                    "metals_policy_workflow._write_reports", side_effect=fail_audits):
            with self.assertRaisesRegex(RuntimeError, "audit publication failed"):
                apply_metal_steel_policy(builder, policy=fixture_policy("high"), report_dir=Path(directory), verbose=False)
        self.assertEqual(certified, ["CL", "OCE"])
        self.assertIs(builder._validation_reports, registry)
        self.assertEqual(set(registry), set(previous))
        for identity, report in previous.items():
            self.assertIs(registry[identity], report)
        for identity, definition, store in zip(("CL", "OCE"), definitions, stores):
            self.assertIs(definition["_inventory_store"], store)
            self.assertEqual(store.generation, 0)
            self.assertEqual(definition["_validation_report"], previous[identity].to_dict())
            self.assertNotIn("metal steel policy", definition)

    def test_real_native_certification_keeps_original_and_current_ids(self):
        original = self.api.create_inventory_store(fixture_database(), scenario_identity="CL")
        ids = tuple(original._state.activity_exchanges[0])
        definition = scenario(_inventory_store=original)
        builder = self.validating_builder([definition])
        with tempfile.TemporaryDirectory() as directory, patch("metals_policy_workflow._inventory_store_api", return_value=self.api), patch(
                "metals_policy_workflow._inventory_validation_api", return_value=self.validation):
            reports = apply_metal_steel_policy(builder, policy=fixture_policy("high"), report_dir=Path(directory), verbose=False)
            self.assertTrue(verify_metal_steel_policy(builder, fixture_policy("high")))
        staged = definition["_inventory_store"]
        report = self.validation.ValidationReport.from_dict(definition["_validation_report"])
        self.assertFalse(report.errors)
        self.assertEqual(report.store_generation, staged.generation)
        self.assertEqual(tuple(staged._state.activity_exchanges[0]), ids)
        self.assertEqual(original.activity(0).to_dict()["exchanges"][2]["amount"], .2)
        self.assertEqual(staged.activity(0).to_dict()["exchanges"][2]["amount"], .6)
        self.assertEqual(reports[0]["native_validation_targets"], 1)

    def test_real_native_baseline_is_byte_equivalent_without_fork_or_recertification(self):
        store = self.api.create_inventory_store(fixture_database(), scenario_identity="CL")
        fingerprints = self.validation.inventory_activity_fingerprints(store)
        definition = scenario(_inventory_store=store, _validation_report={"store_generation": store.generation})
        builder = SimpleNamespace(scenarios=[definition],
                                  _certify_scenario_store=lambda *args, **kwargs: self.fail("Native baseline must not recertify"))
        with tempfile.TemporaryDirectory() as directory, patch("metals_policy_workflow._inventory_store_api", return_value=self.api), patch.object(
                self.api.InventoryStore, "fork", side_effect=AssertionError("Native baseline must not fork")):
            apply_metal_steel_policy(builder, policy=fixture_policy(), report_dir=Path(directory), verbose=False)
        self.assertIs(definition["_inventory_store"], store)
        self.assertEqual(store.generation, 0)
        self.assertEqual(self.validation.inventory_activity_fingerprints(store), fingerprints)

    def test_real_checkpoint_recipe_and_mapping_survive_reload(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            store = self.api.create_inventory_store(fixture_database(), scenario_identity="CL")
            original = store.checkpoint(Path(directory) / "source.inventory-store")
            mapping = self.api._compact_scenario_mapping({"Petchem": {"variable": [store._state.activities[0]]}}, store, original)
            definition = scenario(_inventory_checkpoint=original, _inventory_export_handoff=store, mapping=mapping)
            builder = self.validating_builder([definition])
            with patch("metals_policy_workflow._inventory_store_api", return_value=self.api), patch(
                    "metals_policy_workflow._inventory_validation_api", return_value=self.validation):
                apply_metal_steel_policy(builder, policy=fixture_policy("high"), report_dir=Path(directory) / "reports", verbose=False)
                self.assertTrue(verify_metal_steel_policy(builder, fixture_policy("high")))
            self.assertTrue(original.exists())
            reference = definition["mapping"]["Petchem"]["variable"][0]
            self.assertEqual(reference._resolver.checkpoint, definition["_inventory_checkpoint"])
            reopened = self.api.InventoryStore.open(definition["_inventory_checkpoint"])
            self.assertEqual(reopened.activity(0).to_dict()["exchanges"][2]["amount"], .6)
            self.assertEqual(self.api.InventoryStore.open(original).activity(0).to_dict()["exchanges"][2]["amount"], .2)
            del reopened, reference, definition, builder, store, mapping
            gc.collect()

    def test_real_native_seed_append_preserves_existing_exchange_ids(self):
        data = fixture_database(secondary=0)
        data[0]["exchanges"].pop()
        source = fixture_database()
        store = self.api.create_inventory_store(data, scenario_identity="CL")
        original_ids = tuple(store._state.activity_exchanges[0])
        definition = scenario(_inventory_store=store)
        builder = self.validating_builder([definition])
        with tempfile.TemporaryDirectory() as directory, patch("metals_policy_workflow._inventory_store_api", return_value=self.api), patch(
                "metals_policy_workflow._inventory_validation_api", return_value=self.validation):
            apply_metal_steel_policy(builder, policy=fixture_policy("high"), source_database=source,
                                     report_dir=Path(directory), verbose=False)
        staged = definition["_inventory_store"]
        self.assertEqual(tuple(staged._state.activity_exchanges[0])[:len(original_ids)], original_ids)
        self.assertEqual(len(staged._state.activity_exchanges[0]), len(original_ids) + 1)
        self.assertEqual(staged.activity(0).to_dict()["exchanges"][2]["amount"], .6)
        self.assertEqual(len(store._state.activity_exchanges[0]), len(original_ids))

    def test_real_native_primary_only_retains_uncertainty_free_zero_exchange_and_inherited_cycle(self):
        data = fixture_database()
        data[0]["exchanges"][2].update({"uncertainty type": 2, "loc": -1.6, "scale": .2,
                                         "minimum": 0, "maximum": None})
        data[2]["exchanges"].append({"name": data[0]["name"], "product": data[0]["reference product"],
                                      "unit": "kilogram", "location": "World", "type": "technosphere", "amount": .1,
                                      "input": ("fixture", "code0")})
        original = self.api.create_inventory_store(data, scenario_identity="CL")
        ids = tuple(original._state.activity_exchanges[0])
        definition = scenario(_inventory_store=original)
        builder = self.validating_builder([definition])
        with tempfile.TemporaryDirectory() as directory, patch("metals_policy_workflow._inventory_store_api", return_value=self.api), patch(
                "metals_policy_workflow._inventory_validation_api", return_value=self.validation):
            apply_metal_steel_policy(builder, policy=fixture_policy("primary_only"),
                                     report_dir=Path(directory), verbose=False)
        staged = definition["_inventory_store"]
        secondary = staged.activity(0).to_dict()["exchanges"][2]
        self.assertEqual(secondary["amount"], 0)
        self.assertEqual(secondary["uncertainty type"], 0)
        self.assertNotIn("scale", secondary)
        self.assertEqual(tuple(staged._state.activity_exchanges[0]), ids)
        self.assertEqual(original.activity(0).to_dict()["exchanges"][2]["amount"], .2)
        self.assertFalse(self.validation.ValidationReport.from_dict(definition["_validation_report"]).errors)

    def test_real_native_seed_new_feedback_cycle_is_rejected_before_publication(self):
        data = fixture_database(secondary=0)
        data[0]["exchanges"].pop()
        data[2]["exchanges"].append({"name": data[0]["name"], "product": data[0]["reference product"],
                                      "unit": "kilogram", "location": "World", "type": "technosphere", "amount": .1,
                                      "input": ("fixture", "code0")})
        original = self.api.create_inventory_store(data, scenario_identity="CL")
        definition = scenario(_inventory_store=original)
        builder = self.validating_builder([definition])
        with tempfile.TemporaryDirectory() as directory, patch("metals_policy_workflow._inventory_store_api", return_value=self.api), patch(
                "metals_policy_workflow._inventory_validation_api", return_value=self.validation):
            with self.assertRaisesRegex(MetalPolicyWorkflowError, "new technosphere cycle"):
                apply_metal_steel_policy(builder, policy=fixture_policy("high"), source_database=fixture_database(),
                                         report_dir=Path(directory), verbose=False)
        self.assertIs(definition["_inventory_store"], original)
        self.assertEqual(original.generation, 0)
        self.assertNotIn("metal steel policy", definition)


if __name__ == "__main__":
    unittest.main()
