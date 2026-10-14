"""Bounded access to inventories already published by premise.

Repo post-processors can borrow a certified store instead of reopening the
freshly written checkpoint. A scoped InventoryStore.open adapter also makes
premise scenario-delta reads columnar for their legacy-backend base checkpoints.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import inspect
import json
from pathlib import Path


_BOUND_STORES = ContextVar("por_bound_native_stores", default=None)
_CAPTURE_ATTRIBUTE = "_repo_native_store_capture"


def iter_activity_metadata(store):
    """Yield identities without allocating every activity's exchange-ID tuple."""
    underlying = getattr(store, "_store", store)
    state = getattr(underlying, "_state", None)
    activities = getattr(state, "activities", None)
    if isinstance(activities, Mapping):
        order = getattr(state, "activity_order", None)
        ids = order if order is not None else store.iter_activity_ids()
        for activity_id in ids:
            yield activity_id, activities[activity_id]
        return
    iterator = getattr(underlying, "_iter_storage_activities", None)
    if callable(iterator):
        for activity_id, metadata, _ in iterator():
            yield activity_id, metadata
        return
    for activity_id in store.iter_activity_ids():
        record = store.activity(activity_id)
        if not isinstance(record, Mapping):
            record = record.to_dict()
        yield activity_id, record


def _file_token(path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def checkpoint_token(scenario):
    """Small freshness receipt, never a substitute for initial native validation.

    The capture hook binds only the store returned by the native publisher after
    its certification/checkpoint write. Hash its manifest/checksum documents and
    stat every referenced payload (including a delta's base), so changed sources
    invalidate that binding/prepared results before publication.
    """
    checkpoint = scenario.get("_inventory_checkpoint")
    if checkpoint is None:
        store = scenario.get("_inventory_store")
        if store is None:
            store = scenario.get("_inventory_export_handoff")
        if store is None:
            return ("missing",)
        return ("live", id(store), getattr(store, "generation", None))
    path = Path(checkpoint).resolve()
    visited = set()

    def token(directory):
        if directory in visited:
            raise ValueError("Cyclic native checkpoint base references")
        visited.add(directory)
        if directory.is_file():
            return (str(directory), _file_token(directory))  # lightweight test/legacy adapters
        manifest_path, checksum_path = directory / "manifest.json", directory / "checksums.json"
        manifest_bytes, checksum_bytes = manifest_path.read_bytes(), checksum_path.read_bytes()
        manifest, checksums = json.loads(manifest_bytes), json.loads(checksum_bytes)
        files = tuple((name, _file_token(directory / name)) for name in sorted(checksums))
        base = manifest.get("base_checkpoint")
        base_token = token((directory.parent / base).resolve()) if isinstance(base, str) else None
        return (str(directory), hashlib.sha256(manifest_bytes).hexdigest(),
                hashlib.sha256(checksum_bytes).hexdigest(), files, base_token)

    live = scenario.get("_inventory_store")
    live_token = (id(live), getattr(live, "generation", None)) if live is not None else None
    return ("checkpoint", token(path), live_token)


@contextmanager
def legacy_delta_base_columnar_scope(api, *, reader_bypasses_inventory_open=False):
    """Open nested scenario-delta bases columnarly for the duration of a call."""
    inventory_store_type = api.InventoryStore
    if not isinstance(inventory_store_type, type):
        yield
        return
    descriptor = inventory_store_type.__dict__["open"]
    if not isinstance(descriptor, classmethod):
        raise TypeError("InventoryStore.open must be a classmethod")
    original_open = descriptor.__func__
    depth = ContextVar("por_inventory_checkpoint_open_depth", default=0)

    @wraps(original_open)
    def open_with_columnar_delta_base(cls, path):
        nested_open = depth.get() > 0
        token = depth.set(depth.get() + 1)
        try:
            if nested_open:
                store = api._open_checkpoint(Path(path), prefer_columnar=True)
                api.mark_checkpoint(Path(path))
                return store
            return original_open(cls, path)
        finally:
            depth.reset(token)

    inventory_store_type.open = classmethod(open_with_columnar_delta_base)
    try:
        if reader_bypasses_inventory_open:
            token = depth.set(1)
            try:
                yield
            finally:
                depth.reset(token)
        else:
            yield
    finally:
        inventory_store_type.open = descriptor


def open_checkpoint_with_legacy_base(api, path, reader=None):
    """Open a checkpoint while making any scenario-delta base read columnar."""
    if reader is None:
        with legacy_delta_base_columnar_scope(api):
            return api.InventoryStore.open(path)
    with legacy_delta_base_columnar_scope(api, reader_bypasses_inventory_open=True):
        return reader(path)


def open_inventory_store(scenario, api):
    """Current live store > freshly bound checkpoint > native open > handoff."""
    store = scenario.get("_inventory_store")
    if store is not None:
        return store
    checkpoint = scenario.get("_inventory_checkpoint")
    if checkpoint is not None:
        bindings = _BOUND_STORES.get()
        path = str(Path(checkpoint).resolve())
        bound = bindings.get(path) if bindings else None
        if bound is not None:
            receipt, generation, store = bound
            if getattr(store, "generation", None) == generation and checkpoint_token(scenario) == receipt:
                return store
        # Outside a publication callback, retain the vendor's full corruption
        # and checksum checks. Do not trust arbitrary export handoffs.
        reporting_open = getattr(api.InventoryStore, "open_for_reporting", None)
        if callable(reporting_open):
            # Native legacy Arrow checkpoints are otherwise eagerly expanded
            # into every exchange dictionary. The public reporting reader keeps
            # them columnar, with the SAME vendor checksum/corruption checks.
            # Consumers mutate only a separate writable fork of this view.
            return open_checkpoint_with_legacy_base(api, checkpoint, reporting_open)
        return open_checkpoint_with_legacy_base(api, checkpoint)
    return scenario.get("_inventory_export_handoff")


@contextmanager
def capture_updated_stores(builder, callback, *, priority=10):
    """Call a small/staging repo callback once per successfully published store.

    Hooks exist only on this builder and only inside update(), and are removed
    even on failure. Nested metals/PoR scopes share one hook; deterministic
    priorities retain their required order. Native reports are restored after
    staging: each adapter publishes staged reports with its prepared inventory
    only after all scenarios and audits succeeded. Never retain stores here.
    """
    publisher = getattr(builder, "_store_updated_scenario", None)
    state = getattr(builder, _CAPTURE_ATTRIBUTE, None)
    if state is None:
        if not callable(publisher):
            yield False
            return
        try:
            signature = inspect.signature(publisher)
        except (ValueError, TypeError):
            yield False
            return
        if "scenario_definition" not in signature.parameters:
            yield False
            return
        state = {"original": publisher, "signature": signature, "callbacks": []}

        @wraps(publisher)
        def publish_and_capture(*args, **kwargs):
            store = publisher(*args, **kwargs)
            if store is None or not callable(getattr(store, "activity", None)):
                return store
            definition = signature.bind(*args, **kwargs).arguments["scenario_definition"]
            bindings = {}
            if definition.get("_inventory_checkpoint") is not None:
                path = str(Path(definition["_inventory_checkpoint"]).resolve())
                bindings[path] = (checkpoint_token(definition), getattr(store, "generation", None), store)
            scope = _BOUND_STORES.set(bindings)
            present = hasattr(builder, "_validation_reports")
            registry = getattr(builder, "_validation_reports", None)
            associations = dict(registry) if isinstance(registry, dict) else None
            try:
                for _, active_callback in sorted(state["callbacks"], key=lambda entry: entry[0]):
                    active_callback(definition, store)
            finally:
                _BOUND_STORES.reset(scope)
                if associations is not None:
                    registry.clear()
                    registry.update(associations)
                if present:
                    builder._validation_reports = registry
                elif hasattr(builder, "_validation_reports"):
                    del builder._validation_reports
            return store

        state["wrapper"] = publish_and_capture
        setattr(builder, _CAPTURE_ATTRIBUTE, state)
        builder._store_updated_scenario = publish_and_capture
    entry = (priority, callback)
    state["callbacks"].append(entry)
    try:
        yield True
    finally:
        state["callbacks"].remove(entry)
        if not state["callbacks"]:
            builder._store_updated_scenario = state["original"]
            delattr(builder, _CAPTURE_ATTRIBUTE)
