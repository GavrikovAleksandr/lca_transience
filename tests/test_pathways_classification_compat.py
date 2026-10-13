"""Exercise installed pure Pathways functions without imports or cache cleanup."""
from __future__ import annotations

import ast
from collections import OrderedDict
from copy import deepcopy
import importlib
from importlib.util import find_spec
import multiprocessing
import os
from pathlib import Path
import pickle
from types import SimpleNamespace
from typing import List
import unittest
from unittest.mock import patch

import numpy as np
from scipy import sparse as sps
import xarray as xr

import pathways_classification_compat as compat


def native_modules():
    spec = find_spec("pathways")
    if spec is None:
        raise unittest.SkipTest("Installed Pathways source is required")
    root = Path(next(iter(spec.submodule_search_locations)))
    namespace = {"List": List, "np": np, "xr": xr, "sps": sps, "OrderedDict": OrderedDict}
    functions = {}
    for filename, names in (("utils.py", {"create_lca_results_array", "_group_technosphere_indices"}),
                            ("lca.py", {"_build_column_aggregation_matrix"})):
        source = root / filename
        tree = ast.parse(source.read_text(encoding="utf-8"))
        selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        # Only exact, pure native helpers are compiled; package initialization,
        # filesystem constants, Pathways construction and clean_cache never run.
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), namespace)
        functions.update({node.name: namespace[node.name] for node in selected})
    utils = SimpleNamespace(**{name: functions[name] for name in
                              ("create_lca_results_array", "_group_technosphere_indices")})
    lca = SimpleNamespace(_group_technosphere_indices=functions["_group_technosphere_indices"])
    if "_build_column_aggregation_matrix" in functions:
        lca._build_column_aggregation_matrix = functions["_build_column_aggregation_matrix"]
    return utils, lca


def toy_pathways(utils):
    namespace = {"create_lca_results_array": utils.create_lca_results_array}
    exec("def calculate(self, **kwargs):\n"
         "    self.received_options = kwargs\n"
         "    if self.lca_results is None:\n"
         "        self.lca_results = create_lca_results_array(**self.array_options)\n"
         "    return 'native calculation'\n", namespace)

    class Pathways:
        calculate = namespace["calculate"]

        def __init__(self):
            self.classifications = {("known", "product"): "industry"}
            self.lca_results = None
            self.array_options = array_options(self.classifications)

    return Pathways


def array_options(classifications):
    return dict(methods=["climate", "toxicity"], years=[2025, 2050], regions=["EUR"],
                locations=["NL", "RER"], models=["remind"], scenarios=["transition"],
                classifications=classifications, mapping={"polymer": {}, "fuel": {}})


_SPAWN_MODULES = {}


def _spawn_fixture_module(name):
    return _SPAWN_MODULES[name]


def _initialize_spawn_fixture():
    """Use exact installed helpers in a fresh worker, without package imports."""
    utils, lca = native_modules()
    lca._calculate_year = _spawn_native_year
    _SPAWN_MODULES.update({"pathways.pathways": SimpleNamespace(Pathways=toy_pathways(utils)),
                          "pathways.utils": utils, "pathways.lca": lca})
    compat.import_module = _spawn_fixture_module


def _spawn_native_year(args):
    utils, lca = _SPAWN_MODULES["pathways.utils"], _SPAWN_MODULES["pathways.lca"]
    activities = {("known", "product", "NL"): 0,
                  ("missing", "product", "NL"): 1,
                  ("credit", "product", "RER"): 2}
    classifications = {("known", "product"): "industry", ("credit", "product"): "credit"}
    results = utils.create_lca_results_array(**array_options(classifications))
    categories = lca._group_technosphere_indices(
        activities, lambda activity: classifications.get(activity[:2], "undefined"),
        results.act_category.values.tolist())
    locations = lca._group_technosphere_indices(activities, lambda activity: activity[-1], ["NL", "RER"])
    # Characterize three columns: the unclassified activity contributes 50,
    # and the credit contributes -30. The conserved total must therefore be 100.
    characterization = sps.csr_matrix([[1.0]])
    inventory = sps.csr_matrix([[80.0, 50.0, -30.0]])
    characterized = characterization @ inventory
    if hasattr(lca, "_build_column_aggregation_matrix"):
        grouped = characterized @ lca._build_column_aggregation_matrix(categories, locations, 3)
        total = float(grouped.sum())
    else:
        total = sum(float(characterized[:, sorted(set(cat) & set(loc))].sum())
                    for cat in categories.values() for loc in locations.values())
    return {"pid": os.getpid(), "total": total, "undefined": categories["undefined"],
            "guarded": compat._is_guarded(lca._group_technosphere_indices), "args": args}


class PathwaysClassificationGuardTest(unittest.TestCase):
    def setUp(self):
        self.utils, self.lca = native_modules()
        self.Pathways = toy_pathways(self.utils)

    def install(self):
        return compat.install_pathways_classification_guard(
            self.Pathways, utils_module=self.utils, lca_module=self.lca, verbose=False)

    def test_fallback_coords_keep_all_variables_locations_and_input_classifications(self):
        p = self.Pathways()
        original = deepcopy(p.classifications)
        self.assertTrue(self.install())
        self.assertEqual(p.calculate(multiprocessing=True), "native calculation")
        result = p.lca_results
        self.assertEqual(set(result.act_category.values), {"industry", "unclassified", "undefined"})
        self.assertEqual(result.coords["variable"].values.tolist(), ["polymer", "fuel"])
        self.assertEqual(result.location.values.tolist(), ["NL", "RER"])
        self.assertEqual(result.impact_category.values.tolist(), ["climate", "toxicity"])
        self.assertEqual(p.classifications, original)
        self.assertEqual(p.received_options, {"multiprocessing": True})
        self.assertIs(self.Pathways.calculate.__wrapped__.__globals__["create_lca_results_array"],
                      self.utils.create_lca_results_array)

    def test_positional_signatures_and_distributions_remain_supported(self):
        self.install()
        original = self.utils.create_lca_results_array.__wrapped__
        options = array_options({})
        if "aggregate_by" in original.__annotations__:
            result = self.utils.create_lca_results_array(*options.values(), None, True)
        else:
            result = self.utils.create_lca_results_array(*options.values(), True)
        self.assertEqual(result.coords["quantile"].values.tolist(), [0.05, 0.5, 0.95])
        self.assertEqual(set(result.act_category.values), {"unclassified", "undefined"})

    def test_category_and_location_collapse_do_not_gain_fallback_dimensions(self):
        self.install()
        if "aggregate_by" not in self.utils.create_lca_results_array.__wrapped__.__annotations__:
            self.skipTest("Installed Pathways does not expose aggregate_by")
        result = self.utils.create_lca_results_array(**array_options({}),
                                                   aggregate_by=["act_category", "location"])
        self.assertEqual(result.act_category.values.tolist(), ["aggregated"])
        self.assertEqual(result.location.values.tolist(), ["aggregated"])
        self.assertEqual(result.coords["variable"].values.tolist(), ["polymer", "fuel"])
        p = self.Pathways()
        p.lca_results = result
        p.calculate(aggregate_by=["act_category", "location"])
        self.assertIs(p.lca_results, result)

    def test_cached_results_extend_fallbacks_without_changing_signed_values_or_attrs(self):
        self.install()
        p = self.Pathways()
        result = self.utils.create_lca_results_array(**p.array_options).sel(act_category=["industry"])
        values = np.arange(result.size, dtype=float).reshape(result.shape) - 8.0
        result.values[:] = values
        result.attrs["unit"] = "kg CO2-eq"
        p.lca_results = result
        p.calculate()
        np.testing.assert_array_equal(p.lca_results.sel(act_category=["industry"]).values, values)
        self.assertEqual(p.lca_results.attrs, {"unit": "kg CO2-eq"})
        self.assertEqual(float(p.lca_results.sum()), float(result.sum()))
        self.assertEqual(float(p.lca_results.sel(act_category="undefined").sum()), 0.0)

    def test_category_and_geography_groups_retain_missing_and_negative_contributions(self):
        self.install()
        activities = {("known", "product", "NL"): 0,
                      ("missing", "product", "NL"): 1,
                      ("credit", "product", "RER"): 2}
        classifications = {("known", "product"): "industry", ("credit", "product"): "credit"}
        categories = self.utils._group_technosphere_indices(
            activities, lambda activity: classifications.get(activity[:2], "undefined"),
            ["industry", "credit", "unclassified", "undefined"])
        locations = self.lca._group_technosphere_indices(
            activities, lambda activity: activity[-1], ["NL", "RER"],
            {"NL": "Europe", "RER": "Europe"})
        self.assertEqual(categories["undefined"], [1])
        self.assertEqual(locations["Europe"], [0, 1, 2])
        contributions = np.array([[80.0, 50.0, -30.0], [-10.0, 30.0, -5.0]])
        # Equivalent to native legacy grouping, keeping negative credits intact.
        grouped = np.array([[contributions[:, sorted(set(cat) & set(loc))].sum(axis=1)
                             for loc in locations.values()] for cat in categories.values()])
        np.testing.assert_array_equal(grouped.sum(axis=(0, 1)), contributions.sum(axis=1))
        self.assertEqual(grouped[1, 0, 0], -30.0)
        if hasattr(self.lca, "_build_column_aggregation_matrix"):
            aggregation = self.lca._build_column_aggregation_matrix(categories, locations, 3)
            native_aggregation = self.lca._build_column_aggregation_matrix.__wrapped__(categories, locations, 3)
            np.testing.assert_array_equal(aggregation.toarray(), native_aggregation.toarray())
            np.testing.assert_array_equal((contributions @ aggregation).sum(axis=1), contributions.sum(axis=1))
            np.testing.assert_array_equal(np.asarray(aggregation.sum(axis=1)).ravel(), [1, 1, 1])

    def test_group_missing_category_or_partial_geography_map_fails(self):
        self.install()
        activities = {("a", "p", "NL"): 0, ("b", "p", "RER"): 1}
        with self.assertRaisesRegex(ValueError, "missing=.*1"):
            self.utils._group_technosphere_indices(activities, lambda a: a[0], ["a"])
        with self.assertRaisesRegex((ValueError, KeyError), "missing|RER"):
            self.lca._group_technosphere_indices(activities, lambda a: a[-1], ["NL", "RER"],
                                                {"NL": "Europe"})

    def test_group_empty_and_malformed_inputs_fail(self):
        self.install()
        helper = self.utils._group_technosphere_indices
        for activities, labels, mapping in [({}, ["a"], None), ({("a",): 0}, [], None),
                                            ({("a",): 0}, ["a", "a"], None),
                                            ({("a",): -1}, ["a"], None),
                                            ({("a",): 0.5}, ["a"], None),
                                            ({("a",): 0}, ["a"], []),
                                            ({("a",): 0}, ["a"], {"a": ""})]:
            with self.subTest(activities=activities, labels=labels, mapping=mapping):
                with self.assertRaises(ValueError):
                    helper(activities, lambda a: a[0], labels, mapping)
        for classifications in (None, [], {("a", "p"): ""}, {("a", "p"): None}):
            with self.subTest(classifications=classifications):
                with self.assertRaises(ValueError):
                    self.utils.create_lca_results_array(**array_options(classifications))

    def test_group_overlap_is_rejected_even_for_a_native_helper_regression(self):
        def buggy(technosphere_indices, group_by, group_values, mapping=None):
            return {"a": [0], "b": [0, 1]}
        self.utils._group_technosphere_indices = buggy
        self.lca._group_technosphere_indices = buggy
        self.install()
        with self.assertRaisesRegex(ValueError, "overlap=.*0"):
            self.utils._group_technosphere_indices({("a",): 0, ("b",): 1}, lambda a: a[0], ["a", "b"])

    def test_sparse_aggregation_rejects_missing_overlap_negative_and_extra_indices(self):
        self.install()
        if not hasattr(self.lca, "_build_column_aggregation_matrix"):
            self.skipTest("Legacy Pathways uses direct category/location grouping")
        build = self.lca._build_column_aggregation_matrix
        locations = {"NL": [0, 1, 2]}
        for categories in ({"a": [0, 1]}, {"a": [0, 1], "b": [1, 2]},
                           {"a": [-1, 0, 1, 2]}, {"a": [0, 1, 2, 3]}, {}):
            with self.subTest(categories=categories):
                with self.assertRaises(ValueError):
                    build(categories, locations, 3)
        with self.assertRaisesRegex(ValueError, "acts_location_idx_dict"):
            build({"a": [0, 1, 2]}, {"NL": [0, 1]}, 3)

    def test_sparse_native_result_with_nonconservative_weights_is_rejected(self):
        def buggy(acts_category_idx_dict, acts_location_idx_dict, n_cols):
            return sps.csc_matrix([[1.0], [0.0], [2.0]])
        self.lca._build_column_aggregation_matrix = buggy
        self.install()
        with self.assertRaisesRegex(ValueError, "retain each activity column exactly once"):
            self.lca._build_column_aggregation_matrix({"a": [0, 1, 2]}, {"NL": [0, 1, 2]}, 3)

    def test_install_is_idempotent_across_compat_reload_and_repairs_native_alias_reload(self):
        self.assertTrue(self.install())
        wrappers = (self.Pathways.calculate, self.utils.create_lca_results_array,
                    self.utils._group_technosphere_indices)
        importlib.reload(compat)
        self.assertFalse(self.install())
        self.assertEqual(wrappers, (self.Pathways.calculate, self.utils.create_lca_results_array,
                                    self.utils._group_technosphere_indices))
        self.lca._group_technosphere_indices = self.lca._group_technosphere_indices.__wrapped__
        with self.assertRaisesRegex(RuntimeError, "reinstall"):
            self.Pathways().calculate()
        self.assertTrue(self.install())
        self.assertFalse(self.install())
        self.assertEqual(self.Pathways().calculate(), "native calculation")

    def test_worker_entry_is_picklable_and_bootstraps_guards_before_native_year(self):
        self.assertIs(pickle.loads(pickle.dumps(compat._guarded_calculate_year)), compat._guarded_calculate_year)
        modules = {"pathways.pathways": SimpleNamespace(Pathways=self.Pathways),
                   "pathways.utils": self.utils, "pathways.lca": self.lca}

        def native_year(args):
            self.assertTrue(compat._is_guarded(self.lca._group_technosphere_indices))
            self.assertTrue(compat._is_guarded(self.utils.create_lca_results_array))
            return ("native", args)

        self.lca._calculate_year = native_year
        namespace = self.Pathways.calculate.__globals__
        namespace["_calculate_year"] = native_year
        with patch.object(compat, "import_module", side_effect=modules.__getitem__):
            self.assertEqual(compat._guarded_calculate_year((2025,)), ("native", (2025,)))
        self.assertIs(namespace["_calculate_year"], compat._guarded_calculate_year)

    def test_windows_spawn_worker_conserves_signed_native_aggregation(self):
        context = multiprocessing.get_context("spawn")
        with context.Pool(1, initializer=_initialize_spawn_fixture) as workers:
            result = workers.apply(compat._guarded_calculate_year, ((2025,),))
        self.assertNotEqual(result["pid"], os.getpid())
        self.assertTrue(result["guarded"])
        self.assertEqual(result["undefined"], [1])
        self.assertEqual(result["total"], 100.0)
        self.assertEqual(result["args"], (2025,))


if __name__ == "__main__":
    unittest.main()
