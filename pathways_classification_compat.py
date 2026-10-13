"""Repository-owned guards against dropping Pathways activity contributions.

Install before constructing ``Pathways``. Both supported Pathways layouts use
``undefined`` for missing classifications, while some releases only allocate an
``unclassified`` result coordinate. A temporary classification view supplies both
coordinate labels without assigning a category to any invented activity.

Every category/location grouping must partition the activity columns exactly
once. The newer sparse column aggregation is checked as well. Parallel year
calculations use an importable trampoline which installs these same guards in
Windows spawn workers; no installed package files or LCI amounts are modified.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from functools import update_wrapper
from importlib import import_module
import inspect
import operator


PATHWAYS_CLASSIFICATION_GUARD_VERSION = 1
_GUARD_MARKER = "__por_pathways_classification_guard_version__"
_FALLBACK_LABELS = ("unclassified", "undefined")


def _is_guarded(function) -> bool:
    return getattr(function, _GUARD_MARKER, None) == PATHWAYS_CLASSIFICATION_GUARD_VERSION


def _mark_guard(wrapper, original):
    update_wrapper(wrapper, original)
    setattr(wrapper, _GUARD_MARKER, PATHWAYS_CLASSIFICATION_GUARD_VERSION)
    return wrapper


def _native(function):
    """Remove prior revisions of this repo's wrappers, retaining other wrappers."""
    while hasattr(function, _GUARD_MARKER) and hasattr(function, "__wrapped__"):
        function = function.__wrapped__
    return function


def _column_index(value, context: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{context}: boolean activity column index {value!r}.")
    try:
        index = operator.index(value)
    except TypeError as error:
        raise ValueError(f"{context}: non-integer activity column index {value!r}.") from error
    if index < 0:
        raise ValueError(f"{context}: negative activity column index {index}.")
    return index


def _validate_partition(groups, expected, context: str) -> None:
    if not isinstance(groups, Mapping) or not groups:
        raise ValueError(f"{context}: empty or malformed activity grouping.")
    counts = Counter()
    for label, indices in groups.items():
        if label is None or label == "":
            raise ValueError(f"{context}: empty group label {label!r}.")
        if isinstance(indices, (str, bytes)):
            raise ValueError(f"{context}: malformed index list for {label!r}.")
        try:
            counts.update(_column_index(index, context) for index in indices)
        except TypeError as error:
            raise ValueError(f"{context}: malformed index list for {label!r}.") from error
    expected = set(expected)
    missing = expected - counts.keys()
    extra = counts.keys() - expected
    repeated = [index for index, count in counts.items() if count != 1]
    if missing or extra or repeated:
        raise ValueError(
            f"{context}: each activity column must occur exactly once; "
            f"missing={sorted(missing)[:10]}, extra={sorted(extra)[:10]}, "
            f"overlap={sorted(repeated)[:10]}."
        )


def _classification_view(classifications):
    if not isinstance(classifications, Mapping):
        raise ValueError("Pathways classifications must be an activity-to-category mapping.")
    for label in classifications.values():
        if not isinstance(label, str) or not label.strip():
            raise ValueError(f"Pathways classifications contain an empty or invalid category {label!r}.")
    view = dict(classifications)
    # These keys are not activity descriptor pairs and never reach grouping.
    for label in _FALLBACK_LABELS:
        view[object()] = label
    return view


def _ensure_fallback_coordinates(results, *, collapsed=False):
    if "act_category" not in results.dims:
        raise ValueError("Pathways results lack the act_category dimension.")
    labels = results.coords["act_category"].values.tolist()
    if collapsed or labels == ["aggregated"]:
        return results
    missing = [label for label in _FALLBACK_LABELS if label not in labels]
    if missing:
        # Reindex preserves all existing values, other coordinates, and attrs.
        return results.reindex(act_category=labels + missing, fill_value=0)
    return results


def _wrap_create_array(original):
    signature = inspect.signature(original)
    if "classifications" not in signature.parameters:
        raise RuntimeError("Unrecognized Pathways create_lca_results_array signature.")

    def create_array(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        bound.arguments["classifications"] = _classification_view(bound.arguments["classifications"])
        results = original(*bound.args, **bound.kwargs)
        collapsed = "act_category" in (bound.arguments.get("aggregate_by") or ())
        return _ensure_fallback_coordinates(results, collapsed=collapsed)

    return _mark_guard(create_array, original)


def _wrap_group_indices(original):
    signature = inspect.signature(original)
    required = {"technosphere_indices", "group_by", "group_values", "mapping"}
    if not required.issubset(signature.parameters):
        raise RuntimeError("Unrecognized Pathways _group_technosphere_indices signature.")

    def group_indices(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        technosphere = bound.arguments["technosphere_indices"]
        labels = bound.arguments["group_values"]
        mapping = bound.arguments["mapping"]
        if not isinstance(technosphere, Mapping) or not technosphere:
            raise ValueError("Pathways grouping: empty or malformed technosphere index mapping.")
        if isinstance(labels, (str, bytes)) or not labels:
            raise ValueError("Pathways grouping: empty or malformed group_values.")
        try:
            if len(set(labels)) != len(labels):
                raise ValueError("Pathways grouping: duplicate group_values.")
        except TypeError as error:
            raise ValueError("Pathways grouping: invalid group_values.") from error
        if mapping is not None and not isinstance(mapping, Mapping):
            raise ValueError("Pathways grouping: malformed aggregation mapping.")
        if mapping is not None and any(label is None or label == "" for label in mapping.values()):
            raise ValueError("Pathways grouping: empty aggregation mapping label.")
        expected = [_column_index(index, "Pathways grouping") for index in technosphere.values()]
        if len(set(expected)) != len(expected):
            raise ValueError("Pathways grouping: multiple activities share an activity column index.")
        grouped = original(*bound.args, **bound.kwargs)
        _validate_partition(grouped, expected, "Pathways category/location grouping")
        return grouped

    return _mark_guard(group_indices, original)


def _wrap_column_aggregation(original):
    signature = inspect.signature(original)
    required = {"acts_category_idx_dict", "acts_location_idx_dict", "n_cols"}
    if not required.issubset(signature.parameters):
        raise RuntimeError("Unrecognized Pathways _build_column_aggregation_matrix signature.")

    def column_aggregation(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        n_cols = _column_index(bound.arguments["n_cols"], "Pathways aggregation")
        if not n_cols:
            raise ValueError("Pathways aggregation: no activity columns.")
        expected = range(n_cols)
        for name in ("acts_category_idx_dict", "acts_location_idx_dict"):
            _validate_partition(bound.arguments[name], expected, f"Pathways {name}")
        matrix = original(*bound.args, **bound.kwargs)
        import numpy as np

        expected_groups = (len(bound.arguments["acts_category_idx_dict"])
                           * len(bound.arguments["acts_location_idx_dict"]))
        if matrix.shape != (n_cols, expected_groups):
            raise ValueError("Pathways aggregation matrix has an unexpected shape.")
        if (not np.all(np.asarray(matrix.sum(axis=1)).ravel() == 1)
                or not np.all(matrix.data == 1)
                or not np.all(np.asarray(matrix.getnnz(axis=1)).ravel() == 1)):
            raise ValueError("Pathways aggregation matrix must retain each activity column exactly once.")
        return matrix

    return _mark_guard(column_aggregation, original)


def _guarded_calculate_year(args):
    """Picklable worker entry point: protect each spawned interpreter before LCA."""
    install_pathways_classification_guard(verbose=False)
    return import_module("pathways.lca")._calculate_year(args)


def install_pathways_classification_guard(
    pathways_class=None, *, utils_module=None, lca_module=None, verbose=True
) -> bool:
    """Install once per process; return whether any hook was newly guarded.

    Optional class/modules support tests using installed pure native functions
    without importing Pathways, constructing a package, or cleaning its cache.
    Imported helper aliases are checked separately so module reloads can be
    repaired by calling the installer again. Native calculations and all public
    arguments, including multiprocessing and aggregate_by, are preserved.
    """
    if pathways_class is None:
        pathways_class = import_module("pathways.pathways").Pathways
    if utils_module is None:
        utils_module = import_module("pathways.utils")
    if lca_module is None:
        lca_module = import_module("pathways.lca")
    calculate = _native(pathways_class.calculate)
    namespace = calculate.__globals__
    changed = False
    replacements = {}

    def protect(owner, name, factory):
        nonlocal changed
        original = owner[name] if isinstance(owner, dict) else getattr(owner, name)
        if _is_guarded(original):
            return
        original = _native(original)
        if original not in replacements:
            replacements[original] = factory(original)
        guarded = replacements[original]
        if isinstance(owner, dict):
            owner[name] = guarded
        else:
            setattr(owner, name, guarded)
        changed = True

    protect(utils_module, "create_lca_results_array", _wrap_create_array)
    protect(utils_module, "_group_technosphere_indices", _wrap_group_indices)
    protect(lca_module, "_group_technosphere_indices", _wrap_group_indices)
    if "create_lca_results_array" in namespace:
        protect(namespace, "create_lca_results_array", _wrap_create_array)
    if hasattr(lca_module, "_build_column_aggregation_matrix"):
        protect(lca_module, "_build_column_aggregation_matrix", _wrap_column_aggregation)
    if "_calculate_year" in namespace and namespace["_calculate_year"] is not _guarded_calculate_year:
        namespace["_calculate_year"] = _guarded_calculate_year
        changed = True

    if not _is_guarded(pathways_class.calculate):
        def guarded_calculate(self, *args, **kwargs):
            required = [utils_module.create_lca_results_array,
                        utils_module._group_technosphere_indices,
                        lca_module._group_technosphere_indices]
            if "create_lca_results_array" in namespace:
                required.append(namespace["create_lca_results_array"])
            if hasattr(lca_module, "_build_column_aggregation_matrix"):
                required.append(lca_module._build_column_aggregation_matrix)
            if not all(_is_guarded(function) for function in required):
                raise RuntimeError("Pathways classification guards were replaced; reinstall before calculate().")
            if "_calculate_year" in namespace and namespace["_calculate_year"] is not _guarded_calculate_year:
                raise RuntimeError("Pathways worker guard was replaced; reinstall before calculate().")
            _classification_view(self.classifications)
            existing = getattr(self, "lca_results", None)
            if existing is not None:
                self.lca_results = _ensure_fallback_coordinates(existing)
            return calculate(self, *args, **kwargs)

        pathways_class.calculate = _mark_guard(guarded_calculate, calculate)
        changed = True
    if changed and verbose:
        print("Installed repo Pathways classification guards, including spawned LCA workers.", flush=True)
    return changed
