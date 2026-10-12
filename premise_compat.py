"""Scoped runtime compatibility fixes for the PoR export workflow.

premise 2.3.8 creates optional ``minimum``/``maximum`` fields containing None
when external markets relink exchanges. A subsequent relink checks only whether
the keys exist, so it attempts None * float. This module changes those two
guards to require a non-null value. Numeric bounds, including zero, retain the
original scaling logic. Installed package files and inventory quantities are
not changed. The Pathways adapter also separates new metals diagnostic records
from activity mappings without discarding genuine dataset entries.
"""
from __future__ import annotations

import ast
from collections.abc import Mapping
from functools import update_wrapper
import inspect
import textwrap
from typing import Callable


class _NonNullBoundGuards(ast.NodeTransformer):
    def __init__(self) -> None:
        self.changed_fields: list[str] = []

    def visit_IfExp(self, node: ast.IfExp) -> ast.IfExp:
        self.generic_visit(node)
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Constant)
            and test.left.value in ("minimum", "maximum")
            and len(test.ops) == 1
            and isinstance(test.ops[0], ast.In)
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Name)
            and test.comparators[0].id == "exc"
        ):
            field = test.left.value
            # Checking for None explicitly preserves a real bound of zero.
            node.test = ast.copy_location(
                ast.Compare(
                    left=ast.Call(
                        func=ast.Attribute(value=ast.Name(id="exc", ctx=ast.Load()),
                                           attr="get", ctx=ast.Load()),
                        args=[ast.Constant(value=field)],
                        keywords=[],
                    ),
                    ops=[ast.IsNot()],
                    comparators=[ast.Constant(value=None)],
                ),
                test,
            )
            self.changed_fields.append(field)
        return node


def _compile_none_safe_relink(original: Callable) -> Callable | None:
    """Compile the installed function with only its two null-bound guards fixed."""
    source = textwrap.dedent(inspect.getsource(original))
    tree = ast.parse(source)
    transformer = _NonNullBoundGuards()
    transformer.visit(tree)
    if not transformer.changed_fields:
        # No matching vulnerable guards: leave other/native implementations alone.
        return None
    if sorted(transformer.changed_fields) != ["maximum", "minimum"]:
        raise RuntimeError(
            "The premise external relinking implementation has changed. "
            "Expected exactly one minimum and one maximum guard; "
            f"found {transformer.changed_fields}. Review premise_compat.py."
        )
    ast.fix_missing_locations(tree)
    namespace = original.__globals__.copy()
    filename = f"{__file__}:null_safe_external_relink"
    exec(compile(tree, filename, "exec"), namespace)
    patched = namespace[original.__name__]
    update_wrapper(patched, original)
    patched.__por_null_bounds_fix__ = True
    return patched


def install_premise_null_bounds_fix(external_class=None, *, verbose: bool = True) -> bool:
    """Install once in the current process; return True only when newly applied.

    ``external_class`` is optional so the exact installed relinking function can
    be tested on small datasets without importing premise or touching Brightway.
    Normal notebook callers omit it.
    """
    if external_class is None:
        from premise.external import ExternalScenario
        external_class = ExternalScenario
    original = external_class.relink_to_new_datasets
    if getattr(original, "__por_null_bounds_fix__", False):
        return False
    patched = _compile_none_safe_relink(original)
    if patched is None:
        return False
    external_class.relink_to_new_datasets = patched
    if verbose:
        print("Applied premise external-market compatibility fix for empty uncertainty bounds.")
    return True


def disable_legacy_por_regionalization(external_class=None, *, verbose: bool = True) -> bool:
    """Disable the old user-added constructor hook in memory, when present.

    The new repo-owned post-update step replaces this broad NL relinking pass.
    Clean/new premise installations without the legacy method need no change.
    """
    if external_class is None:
        from premise.external import ExternalScenario
        external_class = ExternalScenario
    legacy = getattr(external_class, "fully_regionalize_created_inventories", None)
    if legacy is None or getattr(legacy, "__por_legacy_disabled__", False):
        return False
    source = inspect.getsource(legacy)
    if "relink_technosphere_exchanges_full" not in source or "get_recursively" not in source:
        raise RuntimeError("An unfamiliar fully_regionalize_created_inventories hook exists; review it before disabling it.")

    def disabled(self, *args, **kwargs):
        return None

    update_wrapper(disabled, legacy)
    disabled.__por_legacy_disabled__ = True
    external_class.fully_regionalize_created_inventories = disabled
    if verbose:
        print("Disabled legacy pre-market NL relinking; supplier linking is managed by the repo after update.")
    return True


PATHWAYS_MAPPING_GUARD_VERSION = 2


def pathways_activity_mapping_views(scenarios):
    """Validate/snapshot tiny provider identities before matrix export mutates data."""
    diagnostic_keys = frozenset({"material decisions", "material update metrics"})
    views, omitted = [], 0
    for ordinal, scenario in enumerate(scenarios, 1):
        context = f"scenario {ordinal} ({scenario.get('model', '?')}/{scenario.get('pathway', '?')}/{scenario.get('year', '?')})"
        mapping = scenario.get("mapping")
        if not isinstance(mapping, Mapping):
            raise ValueError(f"Pathways {context} has no valid sector mapping after update.")
        projected = {}
        for sector, variables in mapping.items():
            if not isinstance(variables, Mapping):
                raise ValueError(f"Invalid Pathways sector mapping in {context}: {sector!r}.")
            projected[sector] = {}
            for variable, providers in variables.items():
                if sector == "metals" and variable in diagnostic_keys:
                    omitted += 1
                    continue
                location = f"{context}, {sector!r}/{variable!r}"
                if not isinstance(providers, (list, tuple)):
                    raise ValueError(f"Pathways mapping {location} must be a sequence of activities.")
                records = []
                for position, provider in enumerate(providers):
                    if not isinstance(provider, Mapping):
                        raise ValueError(f"Invalid dataset in Pathways mapping {location}, entry {position}.")
                    missing = [field for field in ("name", "reference product", "unit") if not provider.get(field)]
                    if missing:
                        raise ValueError(f"Pathways dataset mapping {location}, entry {position}, missing {missing}.")
                    record = {field: provider[field] for field in ("name", "reference product", "unit")}
                    if "lhv" in provider:
                        record["lhv"] = provider["lhv"]
                    records.append(record)
                projected[sector][variable] = records
        views.append(projected)
    return views, omitted


def write_pathways_activity_mapping(package, original, *args, snapshot=None, verbose=False, **kwargs):
    """Give only the mapper an independent export view, restoring all diagnostics."""
    scenarios = package.datapackage.scenarios
    if snapshot is None:
        views, omitted = pathways_activity_mapping_views(scenarios)
    else:
        views, omitted = snapshot
        if len(views) != len(scenarios):
            raise ValueError("Pathways scenario count changed after mapping preflight; recreate the package builder.")
    saved = [(scenario, scenario.get("mapping")) for scenario in scenarios]
    try:
        for scenario, view in zip(scenarios, views):
            scenario["mapping"] = view
        if omitted and verbose:
            print(f"Pathways mapping: excluded {omitted} metals diagnostic entries; activity mappings retained.", flush=True)
        return original(*args, **kwargs)
    finally:
        for scenario, mapping in saved:
            scenario["mapping"] = mapping


def install_premise_pathways_mapping_fix(pathways_class=None, *, verbose: bool = True) -> bool:
    """Separate new metals diagnostics from the activity-only Pathways mapping.

    premise 2.5.2 stores decisions/metrics alongside activity mappings, while its
    Pathways writer expects name/product/unit on every entry. Use a temporary
    export view; retain original diagnostics and never drop malformed real LCIs.
    Older activity-only mappings follow the original method unchanged.
    """
    if pathways_class is None:
        from premise.pathways import PathwaysDataPackage
        pathways_class = PathwaysDataPackage
    original = pathways_class._add_variables_mapping
    if getattr(original, "__por_pathways_mapping_guard_version__", None) == PATHWAYS_MAPPING_GUARD_VERSION:
        return False
    while getattr(original, "__por_pathways_mapping_fix__", False) and hasattr(original, "__wrapped__"):
        original = original.__wrapped__

    def export_activity_mapping(self, *args, **kwargs):
        return write_pathways_activity_mapping(self, lambda *a, **k: original(self, *a, **k),
                                               *args, verbose=verbose, **kwargs)

    update_wrapper(export_activity_mapping, original)
    export_activity_mapping.__por_pathways_mapping_fix__ = True
    export_activity_mapping.__por_pathways_mapping_guard_version__ = PATHWAYS_MAPPING_GUARD_VERSION
    pathways_class._add_variables_mapping = export_activity_mapping
    if verbose:
        print("Installed repo Pathways mapping compatibility guard for metals diagnostics.", flush=True)
    return True
