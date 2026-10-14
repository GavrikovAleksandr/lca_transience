"""Post-update non-ferrous metal policy for legacy and native premise stores.

This module never edits installed premise files.  A native baseline is an
audit-only pass: it neither forks a store nor rewrites a scenario cache.  An
alternative policy is planned against the updated scenario, staged in a fork
or a new legacy cache, validated, and published only when every scenario and
its reports have succeeded. Steel remains endogenous to premise's IAM/EDM-I
routes; historical public function names are retained for compatibility only.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
from functools import wraps
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import csv
import hashlib
import json
import os
import re
import tempfile
import time
import uuid

from metals_scenario_policy import (
    MetalSteelPolicy, apply_database, is_policy_market, plan_database,
)

ROOT = Path(__file__).resolve().parent
WORKFLOW_VERSION = 1


class MetalPolicyWorkflowError(RuntimeError):
    """The selected policy cannot safely be applied to the updated inventory."""


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True,
                                     default=lambda item: dict(item) if isinstance(item, Mapping) else
                                     item.item() if hasattr(item, "item") else str(item),
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _identity(dataset):
    return tuple(dataset.get(key) for key in
                 ("name", "reference product", "unit", "location"))


def _version():
    try:
        return version("premise")
    except PackageNotFoundError:
        return "unavailable"


def _preflight_system_model(builder, policy):
    system_model = str(getattr(builder, "system_model", "cutoff") or "cutoff").strip().lower()
    if not policy.baseline and system_model not in {"cutoff", "apos"}:
        raise MetalPolicyWorkflowError(
            f"Alternative recycled-content policies support cutoff/apos inventories, not {system_model!r}. "
            "Keep native baseline for consequential/substitution models or develop a separate consequential policy.")


def _audit_summary(plan):
    summary = dict(plan.stats)
    summary.pop("steel_case", None)
    summary["steel_owner"] = "endogenous"
    summary["warning_rows"] = sum(bool(str(row.get("warnings") or "").strip()) for row in plan.audit)
    return summary


def _policy_description(policy):
    description = dict(policy.describe())
    description.pop("steel_case", None)
    description.update(steel_owner="endogenous", steel_model="premise native IAM/EDM-I")
    return description


def _inventory_store_api():
    from premise import inventory_store
    return inventory_store


def _inventory_validation_api():
    from premise import validation_framework
    return validation_framework


def _has_native_inventory(scenario):
    return any(scenario.get(key) is not None for key in (
        "_inventory_store", "_inventory_checkpoint", "_inventory_export_handoff",
    ))


def _open_store(scenario, api):
    from native_inventory_access import open_inventory_store

    try:
        return open_inventory_store(scenario, api)
    except (ValueError, TypeError, RuntimeError) as error:
        raise MetalPolicyWorkflowError(f"Updated native inventory is missing or unsupported; no reference fallback is allowed: {error}") from error


class _LazyActivity(Mapping):
    """Keep background metadata light; materialize exchanges only on demand."""

    def __init__(self, store, activity_id, metadata):
        self.store, self.activity_id = store, activity_id
        # The shared metadata iterator returns the original read-only mapping.
        # Keeping it avoids copying every background activity's metadata.
        self.metadata = metadata
        self.payload = None

    def __getitem__(self, key):
        if key == "exchanges":
            if self.payload is None:
                self.payload = self.store.activity(self.activity_id).to_dict()
                # Reopened native checkpoints can return columnar Mapping
                # proxies even from to_dict(); flatten only this selected
                # market's rows for engine hashing and uncertainty checks.
                self.payload["exchanges"] = [dict(exchange) for exchange in self.payload.get("exchanges", ())]
            return self.payload.get("exchanges", ())
        return self.metadata[key]

    def __iter__(self):
        return iter((*self.metadata, "exchanges"))

    def __len__(self):
        return len(self.metadata) + ("exchanges" not in self.metadata)


def _selected_store_inventory(store, *, policy=None, markets_only=False, diagnostics=None):
    from native_inventory_access import iter_activity_metadata

    database, activity_ids = [], []
    scanned = 0
    for activity_id, metadata in iter_activity_metadata(store):
        scanned += 1
        if markets_only and (policy is None or not is_policy_market(metadata, policy)):
            continue
        database.append(_LazyActivity(store, activity_id, metadata))
        activity_ids.append(activity_id)
    if scanned == 0 or (not database and not markets_only) or len(set(activity_ids)) != len(activity_ids):
        raise MetalPolicyWorkflowError("Updated native inventory is empty or has duplicate activity IDs.")
    if diagnostics is not None:
        diagnostics.update(metadata_activity_count=scanned, selected_activity_count=len(database))
    return database, activity_ids


def _source_inventory(builder, policy):
    """Capture the source's market recipes without triggering native .database."""
    source_store = getattr(builder, "_source_inventory_store", None)
    if source_store is not None:
        database = _selected_store_inventory(source_store, policy=policy, markets_only=True)[0]
        # Keep only small market recipes: holding lazy source activity proxies
        # here would prevent premise from releasing its full reference graph.
        return [dict(dataset) for dataset in database]
    source_checkpoint = getattr(builder, "_compact_source_checkpoint", None)
    if source_checkpoint is not None:
        database = _selected_store_inventory(_open_store({"_inventory_checkpoint": source_checkpoint}, _inventory_store_api()),
                                            policy=policy, markets_only=True)[0]
        return [dict(dataset) for dataset in database]
    state = vars(builder)
    for field in ("_database", "database"):
        if isinstance(state.get(field), (list, tuple)):
            return state[field]
    return None


def _native_patch(store, database, activity_ids, changes):
    """Patch existing exchange IDs; append genuine missing recipe components."""
    with store.transaction("repo:nonferrous_metal_policy") as transaction:
        patch = getattr(transaction, "patch_exchange", None)
        add = getattr(transaction, "add_exchange", None)
        for change in changes:
            activity_id = activity_ids[change.dataset_index]
            before = database[change.dataset_index]
            if _identity(before) != _identity(store.activity(activity_id).to_dict()):
                raise MetalPolicyWorkflowError("Native market activity IDs no longer match the policy plan.")
            old, new = list(before.get("exchanges", ())), list(change.new_exchanges)
            if _digest(old) != change.old_exchanges_sha256:
                raise MetalPolicyWorkflowError("Native metals exchanges changed after policy preflight.")
            ids = getattr(getattr(store, "_state", None), "activity_exchanges", {}).get(activity_id)
            if not callable(patch) or ids is None:
                raise MetalPolicyWorkflowError("premise exchange-ID patch API is unavailable; refusing an unsafe store rewrite.")
            if len(ids) != len(old) or len(new) < len(old):
                raise MetalPolicyWorkflowError("Policy removed exchanges or native IDs differ; cannot preserve the checkpoint graph.")
            address_fields = ("type", "name", "product", "reference product", "unit", "location", "input", "database")
            for exchange_id, previous, proposed in zip(ids, old, new):
                if any(previous.get(field) != proposed.get(field) for field in address_fields):
                    raise MetalPolicyWorkflowError("Policy reordered/replaced an existing supplier; cannot safely preserve native exchange IDs.")
                if previous != proposed:
                    patch(exchange_id, dict(proposed), delete_fields=set(previous) - set(proposed), activity_id=activity_id)
            if len(new) > len(old):
                if not callable(add):
                    raise MetalPolicyWorkflowError("premise cannot append the policy's verified supplier recipe.")
                for exchange in new[len(old):]:
                    add(activity_id, dict(exchange))


def _certify(builder, scenario, store, database, activity_ids, changes, summary):
    certify = getattr(builder, "_certify_scenario_store", None)
    if not callable(certify):
        if scenario.get("_validation_report") is not None:
            raise MetalPolicyWorkflowError("Native certificate cannot be refreshed after the metals policy; refusing stale validation.")
        summary["native_validation_scope"] = "no_native_validation_api"
        return
    validation = _inventory_validation_api()
    source_store = _open_store(scenario, _inventory_store_api())
    # Reuse the supplier adapter's native baseline/certification machinery; it
    # handles inherited duplicate exchanges explicitly, not blanket suppression.
    from por_supplier_linking import _graph_baseline, _certify_linked_store, _consumer_edges
    graph_context = _graph_baseline(source_store, validation)
    if graph_context is None:
        fingerprints, cycles = validation.inventory_baseline_snapshot(source_store)
    else:
        fingerprints, cycles = graph_context[:2]
    if graph_context is None:
        after_cycles = validation.inventory_cycle_signatures(store)
    else:
        _, _, before_graph, graph_keys, identities, identifiers = graph_context
        after_graph = dict(before_graph)
        for change in changes:
            after_graph[activity_ids[change.dataset_index]] = _consumer_edges(
                change.new_exchanges, identities, identifiers)
        after_cycles = validation._cycle_signatures(after_graph, graph_keys)
    changed_components = after_cycles - cycles
    # SCC signatures describe whole components, not individual cycles. Removing
    # or reweighting a route can split an inherited background component; do
    # not misreport such a split as a new isolated foreground feedback loop.
    background_reconfigurations = frozenset(component for component in changed_components
                                            if any(component & previous for previous in cycles))
    added_cycles = changed_components - background_reconfigurations
    if added_cycles:
        raise MetalPolicyWorkflowError("Metals policy introduced a new technosphere cycle; the staged graph was not published.")
    target_ids = frozenset(activity_ids[change.dataset_index] for change in changes)
    intent = validation.ValidationIntent(
        transformation="repo_nonferrous_metal_policy", targeted=True,
        affected_activity_ids=target_ids, expected_match_count=len(changes),
        baseline_fingerprints=fingerprints, baseline_cycles=cycles,
    )
    if cycles:
        # A targeted native validation sees an induced subgraph, whose cycle
        # signature may be smaller than the complete background SCC above.
        def target_cycles(inventory):
            if graph_context is not None:
                graph = before_graph if inventory is source_store else after_graph
                return set(validation._cycle_signatures(
                    {activity_id: graph.get(activity_id, set()) for activity_id in target_ids}, graph_keys))
            cycle_intent = validation.ValidationIntent(
                transformation="repo_nonferrous_metal_policy", targeted=True,
                affected_activity_ids=target_ids, expected_match_count=len(changes))
            report = validation.InventoryGraphValidator(
                inventory, intent=cycle_intent, version=str(getattr(builder, "version", "unknown")),
                system_model=str(getattr(builder, "system_model", "cutoff")), suppressions=()).validate()
            return {frozenset(tuple(key) for key in issue.actual)
                    for issue in report.issues if issue.rule_id == "GRAPH.NEW_FORBIDDEN_CYCLE"}
        known_target_cycles = target_cycles(source_store)
        reviewed_target_cycles = {cycle for cycle in target_cycles(store) - known_target_cycles
                                  if any(cycle <= component for component in background_reconfigurations)}
        if known_target_cycles or reviewed_target_cycles:
            from dataclasses import replace
            intent = replace(intent, baseline_cycles=frozenset(
                set(cycles) | known_target_cycles | reviewed_target_cycles))
    _certify_linked_store(builder, scenario, store, validation, intent,
                          activity_ids, database, summary)
    summary.update(native_validation_scope="post_update_changed_metals_markets",
                   native_validation_targets=len(changes), native_validation_new_cycles=0,
                   native_validation_background_cycle_reconfigurations=len(background_reconfigurations))


def _stage_native(builder, original, policy, source_database, directory, *, verbose=False):
    timings, diagnostics = {}, {}
    started = time.perf_counter()
    api = _inventory_store_api()
    store = _open_store(original, api)
    timings["checkpoint_acquisition"] = round(time.perf_counter() - started, 6)
    if verbose:
        print("  Native metals audit: scanning activity metadata (no background exchanges)", flush=True)
    started = time.perf_counter()
    database, activity_ids = _selected_store_inventory(store, policy=policy, markets_only=policy.baseline, diagnostics=diagnostics)
    timings["identity_scan"] = round(time.perf_counter() - started, 6)
    if verbose:
        print(f"  Native metals audit: planning {len(database)} selected identity record(s)", flush=True)
    started = time.perf_counter()
    plan = plan_database(database, policy, scenario=original, source_database=source_database)
    timings["policy_planning"] = round(time.perf_counter() - started, 6)
    summary = _audit_summary(plan)
    summary.update(phase_seconds=timings, **diagnostics)
    if not plan.changes:
        summary.update(inventory_storage="inventory_checkpoint" if original.get("_inventory_checkpoint") is not None else "inventory_store",
                       native_validation_scope="unchanged_store_generation",
                       store_generation=getattr(store, "generation", None))
        return dict(original), plan, summary, []
    plan.verify()
    fork = getattr(store, "fork", None)
    if not callable(fork):
        raise MetalPolicyWorkflowError("premise native inventory cannot be forked safely.")
    staged = fork(scenario_identity=store.scenario_identity)
    _native_patch(staged, database, activity_ids, plan.changes)
    working = dict(original)
    _certify(builder, working, staged, database, activity_ids, plan.changes, summary)
    for key in ("database", "database filepath", "database metadata filepath",
                "_inventory_export_handoff", "_inventory_working_copy"):
        working.pop(key, None)
    working["_inventory_store"] = staged
    persisted = original.get("_inventory_checkpoint") is not None
    checkpoint = None
    if persisted:
        if callable(getattr(api, "_scenario_delta_base_storage", None)):
            staged._scenario_cache_compatibility = True
        destination = directory / "inventory_stores" / f"metals_{uuid.uuid4().hex}.inventory-store"
        checkpoint = staged.checkpoint(destination)
        if checkpoint is None:
            raise MetalPolicyWorkflowError("premise did not persist the staged metals inventory.")
        working["_inventory_checkpoint"] = checkpoint
    if original.get("mapping"):
        hydrate, compact = getattr(api, "_hydrate_scenario_mapping", None), getattr(api, "_compact_scenario_mapping", None)
        activities = getattr(getattr(staged, "_state", None), "activities", None)
        if not callable(hydrate) or activities is None or (persisted and not callable(compact)):
            raise MetalPolicyWorkflowError("Native scenario-mapping API cannot rebind the metals policy's graph.")
        mapping = hydrate(original["mapping"], activities)
        working["mapping"] = compact(mapping, staged, checkpoint) if persisted else mapping
    if persisted:
        working.pop("_inventory_store", None)
    else:
        working.pop("_inventory_checkpoint", None)
    receipts = [{"activity_id": activity_ids[change.dataset_index],
                 "identity": list(_identity(database[change.dataset_index])),
                 "exchanges_sha256": _digest(staged.activity(activity_ids[change.dataset_index]).to_dict().get("exchanges", ()))
                } for change in plan.changes]
    summary.update(inventory_storage="inventory_checkpoint" if persisted else "inventory_store",
                   store_generation=getattr(staged, "generation", None))
    return working, plan, summary, receipts


def _legacy_helpers(load_database_func, dump_database_func):
    if load_database_func is None or dump_database_func is None:
        from premise.utils import load_database, dump_database
        load_database_func = load_database_func or load_database
        dump_database_func = dump_database_func or dump_database
    return load_database_func, dump_database_func


def _load_legacy(scenario, loader):
    working = dict(scenario)
    if working.get("database") is None:
        if not working.get("database filepath"):
            raise MetalPolicyWorkflowError("Updated inventory/cache is missing; refusing the unmodified reference database.")
        working = loader(scenario=working, original_database=[], delete=False,
                         load_metadata=False, warning=False)
    if not isinstance(working.get("database"), (list, tuple)) or not working["database"]:
        raise MetalPolicyWorkflowError("Updated legacy inventory is empty or unsupported.")
    return working


def _stage_legacy(original, policy, source_database, loader, dumper):
    started = time.perf_counter()
    working = _load_legacy(original, loader)
    acquisition = round(time.perf_counter() - started, 6)
    started = time.perf_counter()
    plan = plan_database(working["database"], policy, scenario=original, source_database=source_database)
    summary = _audit_summary(plan)
    summary["phase_seconds"] = {"checkpoint_acquisition": acquisition, "identity_scan": 0.0,
                                "policy_planning": round(time.perf_counter() - started, 6)}
    if not plan.changes:
        # Loading was into a temporary scenario dictionary only. Keep the exact
        # original cache/list and all pointers for the native baseline.
        summary["inventory_storage"] = "legacy_cache" if original.get("database") is None else "legacy_list"
        return dict(original), plan, summary, []
    working["database"] = apply_database(working["database"], plan, policy=policy)
    receipts = [{"identity": list(change.identity),
                 "exchanges_sha256": _digest(change.new_exchanges)} for change in plan.changes]
    if original.get("database") is None:
        original_path = original.get("database filepath")
        dumper(working)
        if working.get("database") is not None or not working.get("database filepath") or working.get("database filepath") == original_path:
            raise MetalPolicyWorkflowError("premise did not persist a new metals-policy cache; original cache was not published over.")
        summary["inventory_storage"] = "legacy_cache"
    else:
        summary["inventory_storage"] = "legacy_list"
    return working, plan, summary, receipts


def _write_reports(directory, rows):
    """Publish reports only after all inventory plans/validation have succeeded."""
    reports = []
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".reports-", dir=directory) as staging:
        staging = Path(staging)
        for ordinal, (scenario, working, plan, summary, receipts, common) in enumerate(rows):
            name = re.sub(r"[^A-Za-z0-9_.-]+", "-",
                          f"{common['stage']}_{scenario.get('model')}_{scenario.get('year')}_{ordinal}")
            csv_path, json_path = directory / f"{name}.csv", directory / f"{name}.json"
            audit = list(plan.audit)
            fields = ["model", "pathway", "year", "external_scenarios"]
            fields += sorted({key for row in audit for key in row} - set(fields))
            with (staging / csv_path.name).open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                for row in audit:
                    writer.writerow({"model": scenario.get("model"), "pathway": scenario.get("pathway"),
                                     "year": scenario.get("year"),
                                     "external_scenarios": " + ".join(str(entry.get("scenario")) for entry in scenario.get("external scenarios", [])),
                                     **row})
            report = {**common, "model": scenario.get("model"), "pathway": scenario.get("pathway"),
                      "year": scenario.get("year"),
                      "external_scenarios": [entry.get("scenario") for entry in scenario.get("external scenarios", [])],
                      **summary, "protected_market_receipts": receipts,
                      "change_report_path": str(csv_path.resolve()), "report_path": str(json_path.resolve())}
            (staging / json_path.name).write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
            reports.append(report)
        for path in staging.iterdir():
            os.replace(path, directory / path.name)
    return reports


def apply_metal_steel_policy(builder, *, policy, report_dir=ROOT / "export/metals_policy",
                             stage="export", verbose=True, source_database=None,
                             load_database_func=None, dump_database_func=None, prepared_baselines=None):
    """Stage every scenario, then atomically switch scenario references together."""
    policy.verify_sources()
    _preflight_system_model(builder, policy)
    scenarios = getattr(builder, "scenarios", None)
    if not isinstance(scenarios, list) or not scenarios:
        raise MetalPolicyWorkflowError("Expected an updated premise builder with a nonempty scenarios list.")
    for scenario in scenarios:
        if scenario.get("metal steel policy"):
            marker = scenario["metal steel policy"]
            if marker.get("policy_fingerprint") != policy.fingerprint:
                raise MetalPolicyWorkflowError("A different metals policy is already applied; recreate the builder.")
            raise MetalPolicyWorkflowError("Metals policy was already applied; recreate the builder instead of applying it twice.")
        if not scenario.get("applied functions"):
            raise MetalPolicyWorkflowError("Metals policy must run after premise.update(), not on the unmodified reference inventory.")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_" + uuid.uuid4().hex[:8]
    directory = Path(report_dir) / run_id
    common = {"workflow_version": WORKFLOW_VERSION, "stage": stage,
              "premise_version": _version(), "ecoinvent_version": str(getattr(builder, "version", "unknown")),
              "system_model": str(getattr(builder, "system_model", "unknown")),
              "nonferrous_case": policy.nonferrous_case, "steel_owner": "endogenous",
              "policy_fingerprint": policy.fingerprint, "source_sha256": dict(policy.source_hashes),
              "policy_description": _policy_description(policy),
              "excluded_scope": deepcopy(policy.configuration.get("excluded_scope", ())),
              "supported_market_rules": [{key: deepcopy(rule.get(key)) for key in
                                           ("id", "sector", "name", "reference product", "unit", "locations")}
                                          for rule in policy.rules]}
    pending = []
    had_validation_reports = hasattr(builder, "_validation_reports")
    saved_validation_reports = getattr(builder, "_validation_reports", None)
    # Native ValidationReport/Issue objects are immutable, and their nested
    # MappingProxyType payloads cannot be deep-copied. Certification replaces
    # registry entries with new reports; snapshot the associations, not the
    # live reports or inventory proxies. Preserve registry identity on rollback.
    original_validation_reports = dict(saved_validation_reports) if isinstance(saved_validation_reports, dict) else None
    try:
        for ordinal, scenario in enumerate(scenarios, 1):
            started = time.perf_counter()
            if verbose:
                print(f"Non-ferrous metals policy {ordinal}/{len(scenarios)} — {scenario.get('year')} {scenario.get('pathway')}: planning {policy.nonferrous_case}; steel remains endogenous", flush=True)
            if _has_native_inventory(scenario):
                prepared = (prepared_baselines or {}).get(id(scenario)) if policy.baseline else None
                if prepared is not None:
                    from native_inventory_access import checkpoint_token
                    try:
                        current_token = checkpoint_token(scenario)
                    except (OSError, ValueError, TypeError):
                        current_token = None
                    if current_token != prepared["checkpoint_token"] or prepared["plan"].policy_fingerprint != policy.fingerprint:
                        prepared = None
                if prepared is None:
                    working, plan, summary, receipts = _stage_native(builder, scenario, policy, source_database, directory, verbose=verbose)
                else:
                    prepared["plan"].verify()
                    working, plan = dict(scenario), prepared["plan"]
                    summary = deepcopy(prepared["summary"])
                    summary["baseline_capture_reused"] = True
                    receipts = []
            else:
                load_database_func, dump_database_func = _legacy_helpers(load_database_func, dump_database_func)
                working, plan, summary, receipts = _stage_legacy(scenario, policy, source_database, load_database_func, dump_database_func)
            summary["elapsed_seconds"] = round(time.perf_counter() - started, 3)
            pending.append((scenario, working, plan, summary, receipts, common))
        policy.verify_sources()
        reports = _write_reports(directory, pending)
        policy.verify_sources()
    except BaseException:
        if original_validation_reports is not None:
            saved_validation_reports.clear()
            saved_validation_reports.update(original_validation_reports)
        if had_validation_reports:
            builder._validation_reports = saved_validation_reports
        elif hasattr(builder, "_validation_reports"):
            del builder._validation_reports
        raise
    for (original, working, _, _, _, _), report in zip(pending, reports):
        working["metal steel policy"] = report
        original.clear()
        original.update(working)
    if verbose:
        print(f"Non-ferrous metals policy: {len(reports)} scenario(s). Native baseline is audit-only; steel is unchanged. Reports: {directory}", flush=True)
    return reports


def verify_metal_steel_policy(builder, policy, *, load_database_func=None):
    """Verify policy sources and final changed-market recipes before export."""
    policy.verify_sources()
    _preflight_system_model(builder, policy)
    for scenario in getattr(builder, "scenarios", []):
        marker = scenario.get("metal steel policy")
        if not marker or marker.get("policy_fingerprint") != policy.fingerprint:
            raise MetalPolicyWorkflowError("Scenario has no current metals-policy receipt; update a freshly configured builder before export.")
        receipts = marker.get("protected_market_receipts", ())
        if not receipts and not (_has_native_inventory(scenario) and scenario.get("_validation_report") is not None):
            continue
        if _has_native_inventory(scenario):
            store = _open_store(scenario, _inventory_store_api())
            for receipt in receipts:
                dataset = store.activity(receipt["activity_id"]).to_dict()
                if list(_identity(dataset)) != receipt["identity"] or _digest(dataset.get("exchanges", ())) != receipt["exchanges_sha256"]:
                    raise MetalPolicyWorkflowError("A metals market changed after policy validation; recreate the builder.")
            generation = getattr(store, "generation", None)
            certificate = scenario.get("_validation_report")
            if certificate is not None and certificate.get("store_generation") != generation:
                raise MetalPolicyWorkflowError("Native inventory certificate is stale after a later post-update step.")
        else:
            if scenario.get("database") is None and load_database_func is None:
                from premise.utils import load_database
                load_database_func = load_database
            working = _load_legacy(scenario, load_database_func)
            identities = {_identity(dataset): dataset for dataset in working["database"]}
            for receipt in receipts:
                dataset = identities.get(tuple(receipt["identity"]))
                if dataset is None or _digest(dataset.get("exchanges", ())) != receipt["exchanges_sha256"]:
                    raise MetalPolicyWorkflowError("A metals market changed after policy validation; recreate the builder.")
    return True


def attach_metal_steel_policy(builder, *, policy, report_dir=ROOT / "export/metals_policy",
                              stage="export", verbose=True, source_database=None,
                              load_database_func=None, dump_database_func=None):
    """Wrap one builder, so Brightway and Pathways share exactly the same step."""
    previous = getattr(builder, "_metal_steel_policy_fingerprint", None)
    if previous is not None:
        if previous != policy.fingerprint:
            raise MetalPolicyWorkflowError("Cannot switch policy on a live builder; recreate it for the new non-ferrous metals case.")
        return False
    if getattr(builder, "_por_supplier_linking_attached", False):
        raise MetalPolicyWorkflowError("Attach the non-ferrous metals policy before PoR supplier linking, so classifications capture the final graph.")
    policy.verify_sources()
    _preflight_system_model(builder, policy)
    if source_database is None and policy.nonferrous_case != "baseline":
        source_database = _source_inventory(builder, policy)
    original_update = builder.update

    @wraps(original_update)
    def update_with_metal_steel_policy(*args, **kwargs):
        policy.verify_sources()
        _preflight_system_model(builder, policy)
        prepared = {}
        try:
            if policy.baseline:
                from native_inventory_access import capture_updated_stores, checkpoint_token

                def prepare_baseline(definition, _published_store):
                    # The shared capture scope binds only the vendor's freshly
                    # certified store. Retain small results, never store/views.
                    _, plan, summary, _ = _stage_native(builder, definition, policy, None,
                                                       Path(report_dir), verbose=verbose)
                    if plan.changes:
                        raise MetalPolicyWorkflowError("Native baseline capture unexpectedly changed inventory exchanges")
                    prepared[id(definition)] = {"plan": plan, "summary": summary,
                                                "checkpoint_token": checkpoint_token(definition)}

                with capture_updated_stores(builder, prepare_baseline, priority=10):
                    result = original_update(*args, **kwargs)
            else:
                result = original_update(*args, **kwargs)
            builder.metal_steel_policy_reports = apply_metal_steel_policy(
                builder, policy=policy, report_dir=report_dir, stage=stage, verbose=verbose,
                source_database=source_database, prepared_baselines=prepared,
                load_database_func=load_database_func, dump_database_func=dump_database_func)
            return result
        finally:
            prepared.clear()

    builder.update = update_with_metal_steel_policy
    for name in ("write_db_to_brightway", "write_db_to_matrices", "write_db_to_simapro"):
        original_export = getattr(builder, name, None)
        if not callable(original_export):
            continue
        def guard(original):
            @wraps(original)
            def export_with_policy_guard(*args, **kwargs):
                verify_metal_steel_policy(builder, policy, load_database_func=load_database_func)
                return original(*args, **kwargs)
            return export_with_policy_guard
        setattr(builder, name, guard(original_export))
    builder._metal_steel_policy_fingerprint = policy.fingerprint
    builder._metal_steel_policy_nonferrous_case = policy.nonferrous_case
    if verbose:
        print(f"Attached non-ferrous metals policy ({stage}): {policy.nonferrous_case}; native baseline and endogenous steel remain unchanged.", flush=True)
    return True


def attach_metal_steel_package_provenance(package, policy, *, verbose=True):
    """Include a small policy receipt in the native archive, before it is built.

    No matrix, scenario table, classifications file, or numerical resource is
    changed.  This is instance-scoped and works with both premise exporters.
    """
    previous = getattr(package, "_metal_steel_provenance_fingerprint", None)
    if previous is not None:
        if previous != policy.fingerprint:
            raise MetalPolicyWorkflowError("Cannot change the non-ferrous metals provenance policy on a live package.")
        return False
    original_build = getattr(package, "_build_datapackage", None)
    if not callable(original_build):
        raise MetalPolicyWorkflowError("premise package archive builder API is unavailable.")

    @wraps(original_build)
    def build_with_provenance(*args, **kwargs):
        policy.verify_sources()
        builder = getattr(package, "datapackage", None)
        reports = getattr(builder, "metal_steel_policy_reports", None)
        if not reports or any(report.get("policy_fingerprint") != policy.fingerprint for report in reports):
            raise MetalPolicyWorkflowError("No validated metals-policy reports are available for package provenance.")
        path = Path.cwd() / "pathways_temp" / "metal_steel_policy.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"workflow_version": WORKFLOW_VERSION,
                   "nonferrous_case": policy.nonferrous_case, "steel_owner": "endogenous",
                   "policy_fingerprint": policy.fingerprint,
                   "source_sha256": dict(policy.source_hashes), "premise_version": _version(),
                   "policy_description": _policy_description(policy),
                   "configuration_snapshot": deepcopy(policy.configuration),
                   "baseline_semantics": "Each installed premise version's native non-ferrous behaviour; audit-only for baseline. Steel always retains native IAM/EDM-I routes.",
                   "scenario_reports": reports}
        descriptor, temporary = tempfile.mkstemp(prefix=".metal-policy-", suffix=".json", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, default=str)
            os.replace(temporary, path)
        finally:
            if Path(temporary).exists():
                Path(temporary).unlink()
        package.metal_steel_policy_provenance = payload
        return original_build(*args, **kwargs)

    package._build_datapackage = build_with_provenance
    package._metal_steel_provenance_fingerprint = policy.fingerprint
    if verbose:
        print("Attached non-ferrous metals package provenance; endogenous steel ownership is recorded in JSON metadata.", flush=True)
    return True
