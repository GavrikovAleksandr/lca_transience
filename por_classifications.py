"""Repo-owned activity classification capture and Pathways export compatibility.

Only activity metadata is read. This module never rewrites an inventory, an
exchange, a Brightway database, or installed package code. Unknown activities
are explicit ``undefined`` entries, which must remain included in LCIA totals.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import csv
import hashlib
from importlib import metadata as package_metadata
import io
import json
import math
import os
from pathlib import Path
import pickle
import re
import tempfile
import weakref
import zipfile

import yaml

ROOT = Path(__file__).resolve().parent
DEFAULT_RULES_PATH = ROOT / "configuration_file/por_classifications.yaml"
DEFAULT_REFERENCE_PATH = ROOT / "configuration_file/por_classification_references.json"
DEFAULT_CATALOG_PATH = ROOT / "configuration_file/ecoinvent_312_classification_catalog.json"
SYSTEMS = ("CPC", "ISIC rev.4 ecoinvent")
FIELDS = ("name", "reference product", "classification_system", "classification_code")
Pair = tuple[str, str]


class ClassificationError(RuntimeError):
    """Incomplete, conflicting, or stale classification metadata."""


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _zip_index_checksum(info):
    # Reading an index validates ZIP's CRC32; central-directory verification can
    # then detect a changed index without hashing unrelated numerical matrices.
    return hashlib.sha256(json.dumps([info.filename, info.CRC, info.file_size], separators=(",", ":")).encode()).hexdigest()


def _resource_checksum(content):
    return hashlib.sha256(content if isinstance(content, bytes) else str(content).encode("utf-8")).hexdigest()


def _clean(value):
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("nan", "none", "null") else text


def _classes(value):
    """Preserve exact official codes and reject incompatible duplicate systems."""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        items = value.items()
    else:
        items = value
    result = {}
    for item in items:
        try:
            system, code = item
        except (TypeError, ValueError) as error:
            raise ClassificationError(f"Invalid classification metadata: {item!r}") from error
        system, code = _clean(system), _clean(code)
        if system == "ISIC":
            system = "ISIC rev.4 ecoinvent"
        if not system or not code:
            continue
        if system in SYSTEMS and code in ("0", "0.0"):
            continue
        previous = result.get(system)
        if previous is not None and previous != code:
            raise ClassificationError(f"Conflicting {system} classifications: {previous!r}, {code!r}")
        result[system] = code
    return result


def _same_code(system, first, second):
    if first == second:
        return True
    # Ecoinvent versions sometimes change or truncate a description while
    # preserving its official numeric code. Keep source text, compare identity.
    if system in SYSTEMS:
        a, b = first.split(":", 1)[0].strip(), second.split(":", 1)[0].strip()
        return bool(a.isdigit() and b.isdigit() and a == b)
    return False


def _pair(record):
    return (_clean(record.get("name")),
            _clean(record.get("reference product", record.get("ref_product", record.get("product")))))


def _metadata_record(record):
    """Read lazy classification sidecars without asking for exchange payloads."""
    common = getattr(record, "_premise_common_value", None)
    read = common if callable(common) else record.get
    result = {key: read(key) for key in ("name", "reference product", "unit", "location")}
    if not result["reference product"]:
        result["reference product"] = read("product")
    lazy = getattr(record, "_metadata", None)
    if callable(lazy) and isinstance(record, dict):
        if dict.__contains__(record, "classifications"):
            result["classifications"] = dict.__getitem__(record, "classifications")
        elif "classifications" not in getattr(record, "_deleted", ()):
            result["classifications"] = lazy().get("classifications")
    else:
        result["classifications"] = record.get("classifications")
    return result


def _store_records(store):
    underlying = getattr(store, "_store", store)
    iterator = getattr(underlying, "_iter_storage_activities", None)
    state = getattr(underlying, "_state", None)
    activities = getattr(state, "activities", None)
    if callable(iterator):
        for activity_id, record, _exchange_ids in iterator():
            # A MappingProxyType hides the lazy metadata accessor. Borrow the
            # underlying read-only payload instead, without copying exchanges.
            if activities is not None:
                record = activities[activity_id]
            yield _metadata_record(record)
        return
    # Public fallback supports older stores and test doubles. Real native
    # stores take the metadata-only branch above.
    for activity_id in store.iter_activity_ids():
        record = store.activity(activity_id)
        if not isinstance(record, Mapping):
            record = record.to_dict()
        yield _metadata_record(record)


def _inventory_store_api():
    from premise import inventory_store
    return inventory_store


def _legacy_loader(**kwargs):
    from premise.utils import load_database
    return load_database(**kwargs)


def _legacy_sidecar_classifications(scenario, source_hashes):
    """Old premise trims classifications into metadata pickles, not inventories."""
    result = {}
    keys = ("database metadata filepath", "database metadata cache filepath", "inventories metadata cache filepath")
    seen = set()
    for key in keys:
        value = scenario.get(key)
        if not isinstance(value, (str, Path)):
            continue  # New SQL/cache references are restored by premise's loader.
        path = Path(value).resolve()
        if path in seen or path.suffix.lower() not in (".pickle", ".pkl") or not path.is_file():
            continue
        seen.add(path)
        source_hashes[str(path)] = _sha(path)
        with path.open("rb") as handle:
            metadata = pickle.load(handle)
        chunks = metadata if isinstance(metadata, list) else [metadata]
        for chunk in chunks:
            if not isinstance(chunk, Mapping):
                continue
            for identity, values in chunk.items():
                if not isinstance(identity, (tuple, list)) or len(identity) < 3 or not isinstance(values, Mapping):
                    continue
                classes = _classes(values.get("classifications"))
                if not classes:
                    continue
                entry = result.setdefault(tuple(identity[:3]), {"classifications": {}, "conflicts": {}, "sources": set()})
                entry["sources"].add(str(path))
                for system, code in classes.items():
                    old = entry["classifications"].get(system)
                    if system in entry["conflicts"]:
                        entry["conflicts"][system].add(code)
                    elif old and not _same_code(system, old, code):
                        entry["conflicts"][system] = {old, code}
                        entry["classifications"][system] = "undefined"
                    else:
                        entry["classifications"][system] = code
    return result


def _scenario_records(scenario, load_database_func=None, source_hashes=None):
    # A checkpoint is authoritative over a consumed/stale export handoff.
    if scenario.get("_inventory_store") is not None:
        yield from _store_records(scenario["_inventory_store"])
    elif scenario.get("_inventory_checkpoint") is not None:
        store = _inventory_store_api().InventoryStore.open(scenario["_inventory_checkpoint"])
        yield from _store_records(store)
    elif scenario.get("_inventory_export_handoff") is not None:
        yield from _store_records(scenario["_inventory_export_handoff"])
    elif scenario.get("_inventory_working_copy") is not None:
        for record in scenario["_inventory_working_copy"]:
            yield _metadata_record(record)
    elif scenario.get("database") is not None or scenario.get("database filepath") is not None:
        database = scenario.get("database")
        if database is None:
            loader = load_database_func or _legacy_loader
            working = loader(scenario=dict(scenario), original_database=[], delete=False,
                             load_metadata=False, warning=False)
            database = working["database"]
        sidecars = _legacy_sidecar_classifications(scenario, source_hashes if source_hashes is not None else {})
        for record in database:
            row = _metadata_record(record)
            cached = sidecars.get((*_pair(row), _clean(row.get("location"))))
            if cached:
                own = _classes(row.get("classifications"))
                row["classifications"] = {**cached["classifications"], **own}
                row["_classification_source"] = "; ".join(sorted(cached["sources"]))
                row["_classification_conflicts"] = {system: values for system, values in cached["conflicts"].items()
                                                    if system not in own}
            yield row
    else:
        raise ClassificationError("Scenario has no readable inventory; capture classifications after updates and before matrix export.")


def _csv_records(handle):
    sample = handle.read(4096)
    handle.seek(0)
    delimiter = ";" if sample.splitlines() and sample.splitlines()[0].count(";") > sample.splitlines()[0].count(",") else ","
    reader = csv.DictReader(handle, delimiter=delimiter)
    for row in reader:
        row = {str(key).strip().lstrip("\ufeff"): value for key, value in row.items() if key is not None}
        pair = _pair(row)
        if not all(pair):
            continue
        if "classification_system" in row:
            classes = _classes([(row["classification_system"], row.get("classification_code"))])
        else:
            classes = _classes({key: value for key, value in row.items()
                                if key not in ("name", "reference product", "product", "unit", "location", "index")})
        yield {"name": pair[0], "reference product": pair[1], "unit": row.get("unit"),
               "location": row.get("location"), "classifications": classes}


def _bundled_reference_records():
    """Read installed metadata CSVs without importing premise or Brightway.

    The premise import catalogue is preferred to the Pathways compatibility
    catalogue. These catalogues omit units, so they are used for exact identity
    matches only. Suffix inheritance requires a unit-bearing inventory record.
    """
    candidates = (("premise", "premise/data/utils/import/classifications.csv"),
                  ("pathways", "pathways/data/classifications.csv"))
    records, hashes, covered_pairs = [], {}, set()
    for distribution, relative in candidates:
        try:
            path = Path(package_metadata.distribution(distribution).locate_file(relative))
        except package_metadata.PackageNotFoundError:
            continue
        if path.is_file():
            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(_csv_records(handle))
            # Complete the first catalogue with additional exact identities
            # from the compatibility catalogue, preserving source preference.
            for row in rows:
                if _pair(row) not in covered_pairs:
                    row["_source"] = str(path.resolve())
                    records.append(row)
            covered_pairs.update(_pair(row) for row in rows)
            hashes[str(path.resolve())] = _sha(path)
    return records, hashes


def _project_reference_records(path=DEFAULT_REFERENCE_PATH):
    """Small reviewed metadata snapshot; no live Brightway project dependency."""
    path = Path(path).resolve()
    if not path.is_file():
        return [], {}
    content = json.loads(path.read_text(encoding="utf-8"))
    if content.get("schema_version") != 1 or not isinstance(content.get("datasets"), list):
        raise ClassificationError(f"Invalid reviewed classification reference snapshot: {path}")
    records = []
    for dataset in content["datasets"]:
        row = _metadata_record(dataset)
        row["_source"] = f"{path} ({content.get('source_inventory_version', 'reviewed reference metadata')})"
        row["ambiguous_systems"] = dataset.get("ambiguous_systems", {})
        records.append(row)
    return records, {str(path): _sha(path)}


def _coerce_reference(reference):
    if isinstance(reference, Mapping):
        for key, value in reference.items():
            if isinstance(key, (tuple, list)) and len(key) >= 2:
                if isinstance(value, Mapping) and "classifications" in value:
                    row = dict(value)
                else:
                    row = {"classifications": value}
                row.update(name=key[0], **{"reference product": key[1]})
                if len(key) >= 3:
                    row["unit"] = key[2]
                yield row
            else:
                raise ClassificationError("Reference catalogue keys must be (name, reference product[, unit]).")
    else:
        for record in reference or ():
            row = _metadata_record(record)
            if record.get("ambiguous_systems"):
                row["ambiguous_systems"] = record["ambiguous_systems"]
            if record.get("_source"):
                row["_source"] = record["_source"]
            yield row


def _load_rules(path):
    if path is None:
        return [], {}
    path = Path(path).resolve()
    if not path.exists():
        raise ClassificationError(f"Classification rules file does not exist: {path}")
    content = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if content.get("schema_version", 1) != 1:
        raise ClassificationError(f"Unsupported classification rules schema: {content.get('schema_version')}")
    rules = content.get("rules", [])
    if not isinstance(rules, list):
        raise ClassificationError("Classification rules must be a list.")
    identifiers = set()
    for rule in rules:
        identifier = rule.get("id")
        if not identifier or identifier in identifiers or not rule.get("match"):
            raise ClassificationError("Each classification rule requires a unique id and a match selector.")
        identifiers.add(identifier)
        if not rule.get("source") or not rule.get("reason"):
            raise ClassificationError(f"Classification rule {identifier!r} requires source and reason.")
        if not rule.get("classifications") and not rule.get("reference"):
            raise ClassificationError(f"Classification rule {identifier!r} requires classifications or an explicit reference.")
    return rules, {str(path): _sha(path)}


def _matches(record, selector):
    aliases = {"ref_product": "reference product", "product": "reference product"}
    for key, wanted in selector.items():
        if key in ("name_prefix", "name_contains", "name_regex", "ref_product_contains", "reference_product_contains"):
            actual = record["name"] if key.startswith("name") else record["reference product"]
            matched = (actual.startswith(str(wanted)) if key == "name_prefix" else
                       re.search(str(wanted), actual) is not None if key == "name_regex" else
                       str(wanted) in actual)
        elif key in ("name", "reference product", "ref_product", "product", "unit", "location"):
            matched = record.get(aliases.get(key, key)) == wanted
        else:
            raise ClassificationError(f"Unsupported classification selector: {key}")
        if not matched:
            return False
    return True


def _base_names(name):
    # Only known PoR suffixes. No fuzzy matching or broad name token inference.
    patterns = (r"\s*\(por\)$", r"\s*\(retrofit\)$", r",\s*(?:imported|import|exported|export)$")
    queue, seen = [name], {name}
    while queue:
        current = queue.pop(0)
        for pattern in patterns:
            candidate = re.sub(pattern, "", current, flags=re.IGNORECASE).strip()
            if candidate and candidate not in seen:
                seen.add(candidate)
                queue.append(candidate)
                yield candidate


@dataclass
class ClassificationSnapshot:
    classifications: dict[Pair, dict[str, str]]
    audit: list[dict]
    stats: dict
    source_hashes: dict[str, str] = field(default_factory=dict)
    captured_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    _stores: list[tuple[object, object]] = field(default_factory=list, repr=False)
    _captured_checksum: str = field(default="", repr=False)
    _resource_readers: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self._captured_checksum = self.snapshot_sha256

    @property
    def snapshot_sha256(self):
        data = [[*pair, sorted(values.items())] for pair, values in sorted(self.classifications.items())]
        return hashlib.sha256(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

    def verify_sources(self, *, check_inventory=True):
        if self.snapshot_sha256 != self._captured_checksum:
            raise ClassificationError("Classification snapshot changed after capture. Capture a new snapshot.")
        for filename, digest in self.source_hashes.items():
            if filename.startswith("resource:"):
                reader = self._resource_readers.get(filename)
                actual = _resource_checksum(reader()) if reader is not None else None
            elif filename.startswith("zip-index:"):
                archive_path, member = filename[len("zip-index:"):].rsplit("::", 1)
                try:
                    with zipfile.ZipFile(archive_path) as archive:
                        actual = _zip_index_checksum(archive.getinfo(member))
                except (OSError, KeyError, zipfile.BadZipFile) as error:
                    raise ClassificationError(f"Classification source changed after capture: {filename}. Capture a new snapshot.") from error
            else:
                path = Path(filename)
                actual = _sha(path) if path.is_file() else None
            if actual != digest:
                raise ClassificationError(f"Classification source changed after capture: {filename}. Capture a new snapshot.")
        for store_ref, generation in self._stores if check_inventory else ():
            store = store_ref()
            if store is None:
                continue  # Export is allowed to release its captured store.
            if getattr(store, "generation", None) != generation:
                raise ClassificationError("Scenario inventory changed after classification capture. Capture a new snapshot.")


def _build_snapshot(records, reference_records, rules, source_hashes, *, stores=None):
    references = defaultdict(dict)
    reference_sources = {}
    ambiguous_references = defaultdict(set)

    def add_reference(record, source):
        pair = _pair(record)
        if not all(pair):
            raise ClassificationError(f"Activity has incomplete classification identity: {pair}")
        unit = _clean(record.get("unit"))
        identity = (*pair, unit)
        for system, values in record.get("ambiguous_systems", {}).items():
            system = "ISIC rev.4 ecoinvent" if system == "ISIC" else system
            codes = [_clean(value) for value in values]
            ambiguous_references[(identity, system)].update(code for code in codes if code)
            references[identity].pop(system, None)
        for system, code in _classes(record.get("classifications")).items():
            if code == "undefined":
                continue
            before = references[identity].get(system)
            ambiguity = ambiguous_references.get((identity, system))
            if ambiguity:
                ambiguity.add(code)
                continue
            if before is not None and not _same_code(system, before, code):
                ambiguous_references[(identity, system)].update((before, code))
                references[identity].pop(system, None)
                continue
            references[identity][system] = code
            reference_sources[(identity, system)] = source

    # Actual scenario/source metadata takes precedence over a bundled catalogue.
    for record in reference_records:
        add_reference(record, record.get("_source", "reference_catalogue"))
    groups = {}
    activity_count = 0
    for record in records:
        pair = _pair(record)
        if not all(pair):
            raise ClassificationError(f"Activity has incomplete classification identity: {pair}")
        activity_count += 1
        group = groups.setdefault(pair, {"records": {}, "count": 0, "locations": set(), "scenarios": set()})
        group["count"] += 1
        group["locations"].add(_clean(record.get("location")))
        group["scenarios"].add(_clean(record.get("_scenario")))
        unit = _clean(record.get("unit"))
        variant = group["records"].setdefault(unit, {"name": pair[0], "reference product": pair[1], "unit": unit,
                                                     "classifications": {}, "conflicts": {}, "sources": {}, "statuses": {}, "preserve_undefined": set()})
        variant["preserve_undefined"].update(record.get("_preserve_undefined_systems", ()))
        for system, values in record.get("_classification_conflicts", {}).items():
            variant["conflicts"].setdefault(system, set()).update(values)
            variant["classifications"][system] = "undefined"
        for system, code in _classes(record.get("classifications")).items():
            variant["sources"][system] = record.get("_classification_sources", {}).get(system, record.get("_classification_source", "scenario_metadata"))
            variant["statuses"][system] = record.get("_classification_statuses", {}).get(system, "original_metadata")
            if system in variant["conflicts"]:
                if code != "undefined":
                    variant["conflicts"][system].add(code)
                continue
            old = variant["classifications"].get(system)
            if old and not _same_code(system, old, code) and old != "undefined" and code != "undefined":
                variant["conflicts"][system] = {old, code}
                variant["classifications"][system] = "undefined"
                continue
            if old in (None, "undefined") or code != "undefined":
                variant["classifications"][system] = code
    # Classified copies within the final scenarios can resolve identical missing
    # metadata on another geography; retain the exact reference product/unit.
    for group in groups.values():
        for record in group["records"].values():
            own = _classes(record["classifications"])
            identity = (*_pair(record), record["unit"])
            for system, code in own.items():
                if code != "undefined":
                    references[identity][system] = code
                    reference_sources[(identity, system)] = "scenario_metadata"
                    ambiguous_references.pop((identity, system), None)
            for system, values in record["conflicts"].items():
                references[identity].pop(system, None)
                ambiguous_references[(identity, system)].update(values)

    ambiguity_index = defaultdict(set)
    for identity, system in ambiguous_references:
        ambiguity_index[identity].add(system)

    def lookup(pair, unit, *, allow_unitless):
        result, source = {}, {}
        for identity in ((*pair, ""), (*pair, unit)) if allow_unitless else ((*pair, unit),):
            if not unit and not allow_unitless:
                continue
            for system, code in references.get(identity, {}).items():
                result[system] = code
                source[system] = reference_sources[(identity, system)]
            for system in ambiguity_index.get(identity, ()):
                # A reviewed ambiguity is not repaired by a less specific,
                # unitless compatibility catalogue entry.
                result.pop(system, None)
                source.pop(system, None)
        return result, source

    classifications, audit = {}, []
    for pair, group in sorted(groups.items()):
        merged, provenance = {}, {}
        pair_conflicts = defaultdict(set)
        for unit, record in group["records"].items():
            resolved = {key: value for key, value in record["classifications"].items() if value != "undefined"}
            details = {system: {"status": record["statuses"].get(system, "original_metadata"), "source": record["sources"].get(system, "scenario_metadata"), "rule_id": "",
                                "reference_name": pair[0], "reference_product": pair[1], "reason": "Preserved dataset classification metadata."}
                       for system in resolved}
            for system in record["preserve_undefined"]:
                resolved[system] = "undefined"
                details[system] = {"status": "package_undefined", "source": record["sources"].get(system, "package_classifications"),
                                   "rule_id": "", "reference_name": pair[0], "reference_product": pair[1],
                                   "reason": "Explicit unresolved package classification retained in LCIA totals."}
            for system, values in record["conflicts"].items():
                resolved[system] = "undefined"
                details[system] = {"status": "conflicting_metadata", "source": record["sources"].get(system, "scenario_metadata"), "rule_id": "",
                                   "reference_name": pair[0], "reference_product": pair[1],
                                   "reason": "Pathways ignores geography; incompatible source codes retained as undefined: " + "; ".join(sorted(values))}

            def fill(values, detail, *, override=False):
                for system, code in values.items():
                    if system not in resolved or override:
                        resolved[system] = code
                        details[system] = dict(detail) if not callable(detail) else detail(system)

            values, sources = lookup(pair, unit, allow_unitless=True)
            fill(values, lambda system: {"status": "exact_reference", "source": sources[system], "rule_id": "",
                                         "reference_name": pair[0], "reference_product": pair[1],
                                         "reason": "Exact activity and reference product match."})
            for base in _base_names(pair[0]):
                values, sources = lookup((base, pair[1]), unit, allow_unitless=False)
                fill(values, lambda system, base=base: {"status": "normalized_reference", "source": sources[system], "rule_id": "",
                                                       "reference_name": base, "reference_product": pair[1],
                                                       "reason": "Known PoR suffix removed; reference product and unit match."})
            applicable = [rule for rule in rules if _matches(record, rule["match"])]
            supplied = {}
            unavailable_references = []
            for rule in applicable:
                values = _classes(rule.get("classifications"))
                target = rule.get("reference")
                if target:
                    ref_pair = _pair(target)
                    ref_unit = _clean(target.get("unit", unit))
                    if target.get("unit") and ref_unit != unit:
                        raise ClassificationError(f"Classification rule {rule['id']} unit differs from {pair}: {unit!r} / {ref_unit!r}")
                    borrowed, _ = lookup(ref_pair, ref_unit, allow_unitless=True)
                    values = {**borrowed, **values}
                    if not borrowed and not rule.get("classifications"):
                        unavailable_references.append(rule)
                for system, code in values.items():
                    if system in supplied and supplied[system] != code:
                        raise ClassificationError(f"Conflicting matching classification rules for {pair}, {system}.")
                    supplied[system] = code
                detail = {"status": rule.get("status", "explicit_rule"), "source": str(rule["source"]), "rule_id": rule["id"],
                          "reference_name": _pair(target)[0] if target else "", "reference_product": _pair(target)[1] if target else "",
                          "reason": rule["reason"]}
                fill(values, detail, override=bool(rule.get("override_existing", False)))
            for system in SYSTEMS:
                ambiguities = set().union(*(ambiguous_references.get((identity, system), set())
                                            for identity in ((*pair, ""), (*pair, unit))))
                detail = {"status": "ambiguous_reference" if ambiguities else "undefined",
                          "source": "reference_catalogue" if ambiguities else "explicit_unresolved_fallback", "rule_id": "",
                          "reference_name": pair[0] if ambiguities else "", "reference_product": pair[1] if ambiguities else "",
                          "reason": ("Reference catalogue contains incompatible codes; retained as undefined: " + "; ".join(sorted(ambiguities)))
                                    if ambiguities else "No verified classification metadata; retained in LCIA totals."}
                if unavailable_references and not ambiguities:
                    rule = unavailable_references[0]
                    ref_pair = _pair(rule["reference"])
                    detail = {"status": "unavailable_rule_reference", "source": str(rule["source"]),
                              "rule_id": "; ".join(entry["id"] for entry in unavailable_references),
                              "reference_name": ref_pair[0], "reference_product": ref_pair[1],
                              "reason": "Explicit proxy reference lacks verified metadata; retained as undefined. " + rule["reason"]}
                fill({system: "undefined"}, detail)
            for system, code in resolved.items():
                old = merged.get(system)
                if pair_conflicts.get(system):
                    pair_conflicts[system].add(code)
                    continue
                if old is not None and not _same_code(system, old, code):
                    pair_conflicts[system].update((old, code))
                    merged[system] = "undefined"
                    provenance[system] = {"status": "conflicting_pair_metadata", "source": "scenario_metadata", "rule_id": "",
                                          "reference_name": pair[0], "reference_product": pair[1], "reason": ""}
                    continue
                merged[system] = code
                provenance.setdefault(system, details[system])
        for system, values in pair_conflicts.items():
            provenance[system]["reason"] = "Pathways ignores units and geography; incompatible classifications retained as undefined: " + "; ".join(sorted(values))
        classifications[pair] = merged
        for system, code in sorted(merged.items()):
            audit.append({"name": pair[0], "reference product": pair[1], "classification_system": system,
                          "classification_code": code, **provenance[system], "units": "; ".join(sorted(group["records"])),
                          "locations": "; ".join(sorted(group["locations"])), "scenarios": "; ".join(sorted(group["scenarios"])),
                          "activity_count": group["count"]})
    status_counts = dict(Counter(row["status"] for row in audit))
    stats = {"activity_count": activity_count, "unique_pairs": len(classifications), "classification_rows": len(audit),
             "undefined_pairs": {system: sum(values.get(system) == "undefined" for values in classifications.values()) for system in SYSTEMS},
             "status_counts": status_counts,
             "conflicts": sum(row["status"] in ("conflicting_metadata", "conflicting_pair_metadata", "ambiguous_reference") for row in audit),
             "ambiguous_reference_entries": len(ambiguous_references)}
    return ClassificationSnapshot(classifications, audit, stats, dict(source_hashes), _stores=stores or [])


def capture_scenario_classifications(builder, rules_path=DEFAULT_RULES_PATH, reference_func=None, *,
                                     load_database_func=None, progress=None, verbose=False):
    """Capture complete pair metadata before matrix export consumes scenario stores.

    ``reference_func`` is an optional zero-argument callable returning metadata
    datasets or a mapping ``(name, product[, unit]) -> classifications``. Supplying
    it replaces the installed catalogue lookup, which makes unit tests and
    caller-selected reference inventories independent of installed LCA packages.
    No captured snapshot is reused across builder runs.
    """
    rules, hashes = _load_rules(rules_path)
    references = []
    if reference_func is None:
        bundled, bundled_hashes = _bundled_reference_records()
        references.extend(bundled)
        hashes.update(bundled_hashes)
        stock, stock_hashes = _project_reference_records(DEFAULT_CATALOG_PATH)
        references.extend(stock)
        hashes.update(stock_hashes)
        reviewed, reviewed_hashes = _project_reference_records()
        references.extend(reviewed)
        hashes.update(reviewed_hashes)
    else:
        references.extend(_coerce_reference(reference_func()))
    source_store = getattr(builder, "_source_inventory_store", None)
    if source_store is not None:
        # Do not combine source classifications with an incompatible stale
        # catalogue value: real reference inventory metadata takes precedence.
        source = list(_store_records(source_store))
        source_pairs = {_pair(row) for row in source if row.get("classifications")}
        references = [row for row in references if _pair(row) not in source_pairs] + source
    else:
        database = getattr(builder, "__dict__", {}).get("_database")
        if database is None and not getattr(builder, "_inventory_api_active", False):
            # Historical builders keep this as an ordinary instance value.
            # Never invoke the removed/native ``database`` property, which can
            # trigger full reference materialization on compatibility objects.
            database = getattr(builder, "__dict__", {}).get("database")
        if database:
            source = [_metadata_record(row) for row in database]
            source_pairs = {_pair(row) for row in source if row.get("classifications")}
            references = [row for row in references if _pair(row) not in source_pairs] + source
    stores = []
    scenarios = getattr(builder, "scenarios", None)
    if not scenarios:
        raise ClassificationError("Builder has no scenarios to capture.")

    def records():
        for position, scenario in enumerate(scenarios):
            store = scenario.get("_inventory_store")
            if store is not None:
                underlying = getattr(store, "_store", store)
                try:
                    stores.append((weakref.ref(underlying), getattr(underlying, "generation", None)))
                except TypeError:
                    pass  # Never retain a full inventory graph for metadata QA.
            label = f"{scenario.get('model', '')}/{scenario.get('pathway', '')}/{scenario.get('year', '')}/{position}"
            message = f"Capturing classifications for scenario {position + 1}/{len(scenarios)}: {label}"
            if progress is not None:
                progress(message)
            elif verbose:
                print(message, flush=True)
            for row in _scenario_records(scenario, load_database_func, hashes):
                row["_scenario"] = label
                yield row

    snapshot = _build_snapshot(records(), references, rules, hashes, stores=stores)
    # _build_snapshot consumes the generator before stores has been populated.
    snapshot._stores = stores
    snapshot.stats["scenario_count"] = len(scenarios)
    snapshot.verify_sources()
    return snapshot


def _atomic_csv(path, rows, fields):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def _atomic_json(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(content, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def _write_audit(snapshot, directory):
    directory = Path(directory)
    fields = (*FIELDS, "status", "source", "rule_id", "reference_name", "reference_product", "reason", "units", "locations", "scenarios", "activity_count")
    _atomic_csv(directory / "classification_audit.csv", snapshot.audit, fields)
    _atomic_json(directory / "classification_audit.json", {"captured_at": snapshot.captured_at,
                 "snapshot_sha256": snapshot.snapshot_sha256, "source_hashes": snapshot.source_hashes,
                 "stats": snapshot.stats, "source_hash_policy": "SHA256 of local files; SHA256 of matrix-index ZIP member name, validated CRC32 and size for zip-index sources.",
                 "classification_policy": "Preserve source metadata; verified references; explicit foreground rules; unresolved entries remain undefined."})


def write_classification_snapshot(package, snapshot, output_path=None, *, audit_dir=None):
    """Atomically write the populated resource used by premise's data package."""
    # Matrix export is allowed to consume a handoff or advance/release a native
    # store. Classification metadata has already been captured and checksummed.
    snapshot.verify_sources(check_inventory=False)
    if not snapshot.classifications:
        raise ClassificationError("Refusing to export a header-only classification resource.")
    output = Path(output_path) if output_path is not None else Path.cwd() / "pathways_temp/classifications/classifications.csv"
    rows = ({"name": pair[0], "reference product": pair[1], "classification_system": system, "classification_code": code}
            for pair, values in sorted(snapshot.classifications.items()) for system, code in sorted(values.items()))
    _atomic_csv(output, rows, FIELDS)
    directory = Path(audit_dir) if audit_dir is not None else Path.cwd() / "export/por_classifications" / snapshot.snapshot_sha256[:16]
    _write_audit(snapshot, directory)
    package.classifications = {pair: dict(values) for pair, values in snapshot.classifications.items()}
    package.classification_details = {pair: dict(values) for pair, values in snapshot.classifications.items()}
    package.classification_audit = snapshot.audit
    package.classification_stats = snapshot.stats
    package.classification_snapshot_sha256 = snapshot.snapshot_sha256
    package.classification_audit_path = directory
    return output


def _matrix_records(package, package_path, hashes):
    seen_paths = set()
    # Pathways already extracted the resources it will actually calculate.
    # Prefer their small index files to reopening/hashing a large matrix ZIP.
    def paths(value):
        if isinstance(value, (str, Path)):
            yield value
        elif isinstance(value, Mapping):
            for nested in value.values():
                yield from paths(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                yield from paths(nested)
    for candidate in paths(getattr(package, "filepaths", ())):
        if isinstance(candidate, (str, Path)):
            path = Path(candidate)
            if path.name.lower() == "a_matrix_index.csv" and path.is_file():
                seen_paths.add(path.resolve())
    if package_path is not None and not seen_paths:
        path = Path(package_path).resolve()
        if path.is_file() and zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                for name in sorted(archive.namelist()):
                    if name.lower().endswith("a_matrix_index.csv"):
                        hashes[f"zip-index:{path}::{name}"] = _zip_index_checksum(archive.getinfo(name))
                        with archive.open(name) as raw:
                            with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as handle:
                                for row in _csv_records(handle):
                                    row["_scenario"] = name
                                    yield row
            return
        directory = path.parent if path.is_file() else path
        for index_path in sorted(directory.rglob("A_matrix_index.csv")):
            seen_paths.add(index_path.resolve())
    if not seen_paths:
        raise ClassificationError("No A_matrix_index.csv resources found; classification coverage cannot be checked.")
    for path in sorted(seen_paths):
        hashes[str(path)] = _sha(path)
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in _csv_records(handle):
                row["_scenario"] = str(path)
                yield row


def _provided_classifications(package, package_path, hashes, resource_readers):
    """Identify declared package metadata separately from Pathways fallbacks."""
    data = getattr(package, "data", None)
    resource = None
    if data is not None and callable(getattr(data, "get_resource", None)):
        try:
            resource = data.get_resource("classifications")
        except (KeyError, ValueError):
            return {}, "missing_package_classifications"
        if resource is None:
            return {}, "missing_package_classifications"

    def read(handle):
        result = {}
        for row in _csv_records(handle):
            pair = _pair(row)
            for system, code in _classes(row.get("classifications")).items():
                values = result.setdefault(pair, {})
                old = values.get(system)
                if old and not _same_code(system, old, code):
                    raise ClassificationError(f"Conflicting declared package classifications for {pair}, {system}: {old!r} / {code!r}")
                values[system] = code
        return result

    if resource is not None:
        source = getattr(resource, "source", None)
        if isinstance(source, (str, Path)) and Path(source).is_file():
            path = Path(source).resolve()
            hashes[str(path)] = _sha(path)
            with path.open(encoding="utf-8-sig", newline="") as handle:
                return read(handle), str(path)
        raw_read = getattr(resource, "raw_read", None)
        if callable(raw_read):
            content = raw_read()
            key = f"resource:classifications:{source or 'in_memory'}"
            hashes[key] = _resource_checksum(content)
            resource_readers[key] = raw_read
            content = content.decode("utf-8-sig") if isinstance(content, bytes) else str(content)
            return read(io.StringIO(content)), key
        raise ClassificationError("Package classifications resource cannot be read.")
    if package_path is not None:
        path = Path(package_path).resolve()
        if path.is_file() and zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                names = [name for name in archive.namelist() if Path(name).name.lower() == "classifications.csv"]
                if not names:
                    return {}, "missing_package_classifications"
                if len(names) != 1:
                    raise ClassificationError("Package has multiple classification resources; cannot determine their precedence.")
                name = names[0]
                hashes[f"zip-index:{path}::{name}"] = _zip_index_checksum(archive.getinfo(name))
                with archive.open(name) as raw:
                    with io.TextIOWrapper(raw, encoding="utf-8-sig", newline="") as handle:
                        return read(handle), f"{path}::{name}"
    # A lightweight caller without a native data resource supplies its own
    # selected categories. They are authoritative, rather than hidden fallbacks.
    return None, "selected_existing_categories"


def prepare_pathways_classifications(package, package_path=None, rules_path=DEFAULT_RULES_PATH,
                                     reference_func=None, *, audit_dir=None):
    """Complete in-memory classifications for every matrix activity pair.

    This compatibility path also repairs a legacy header-only package without
    modifying its ZIP or extracted source classification resource. Existing
    selected categories are preserved. ``audit_dir`` optionally writes a
    separate audit; the returned snapshot always exposes provenance and counts.
    """
    rules, hashes = _load_rules(rules_path)
    if reference_func is None:
        references, reference_hashes = _bundled_reference_records()
        hashes.update(reference_hashes)
        stock, stock_hashes = _project_reference_records(DEFAULT_CATALOG_PATH)
        references.extend(stock)
        hashes.update(stock_hashes)
        reviewed, reviewed_hashes = _project_reference_records()
        references.extend(reviewed)
        hashes.update(reviewed_hashes)
    else:
        references = list(_coerce_reference(reference_func()))
    selected_system = getattr(package, "classification_system", "CPC")
    selected_system = "ISIC rev.4 ecoinvent" if selected_system == "ISIC" else selected_system
    existing = getattr(package, "classifications", {})
    resource_readers = {}
    provided, provided_source = _provided_classifications(package, package_path, hashes, resource_readers)

    def records():
        for row in _matrix_records(package, package_path, hashes):
            pair = _pair(row)
            declared = dict(provided.get(pair, {})) if provided is not None else {}
            row["classifications"] = declared
            row["_classification_sources"] = {system: provided_source for system in declared}
            preserve_undefined = {system for system, value in declared.items() if value == "undefined"}
            code = _clean(existing.get(pair))
            # Activity mappings may deliberately replace official numeric codes
            # with selected human category names. Preserve those selections.
            custom_selected = bool(code and code != "undefined" and not code.split(":", 1)[0].strip().isdigit())
            if code and (provided is None or custom_selected):
                row["classifications"][selected_system] = code
                row["_classification_sources"][selected_system] = "selected_existing_categories"
                row["_classification_statuses"] = {selected_system: "selected_category"}
                if code == "undefined":
                    preserve_undefined.add(selected_system)
                else:
                    preserve_undefined.discard(selected_system)
            row["_preserve_undefined_systems"] = preserve_undefined
            yield row

    snapshot = _build_snapshot(records(), references, rules, hashes)
    snapshot._resource_readers = resource_readers
    snapshot.verify_sources()
    package.classifications = {pair: values.get(selected_system, "undefined") for pair, values in snapshot.classifications.items()}
    reverse = defaultdict(list)
    for pair, code in package.classifications.items():
        reverse[code].append(pair)
    reverse.setdefault("undefined", [])
    reverse.setdefault("unclassified", [])
    package.reverse_classifications = reverse
    package.classification_audit = snapshot.audit
    package.classification_details = {pair: dict(values) for pair, values in snapshot.classifications.items()}
    package.classification_stats = snapshot.stats
    package.classification_snapshot_sha256 = snapshot.snapshot_sha256
    if audit_dir is not None:
        _write_audit(snapshot, audit_dir)
    return snapshot
