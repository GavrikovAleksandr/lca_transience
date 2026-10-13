"""Targeted, auditable supplier linking after premise scenario updates.

No installed package files are edited. All changes are 1:1 supplier substitutions
with unchanged quantities/units and uncertainty data. Imported routes and fixed
renewable mixes are protected using the original inventory workbook. Scenario
cache references are committed only after every selected scenario is validated.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
import math
from numbers import Number
from pathlib import Path
import re
import time
import uuid

import yaml

from supplier_resolution import (
    Key, SupplierLinkingError, dataset_key, exchange_key,
    unique_supplier as _unique, substitute_supplier as _substitute,
    ReferenceSupplierResolver,
)

ROOT = Path(__file__).resolve().parent
LINKING_POLICY_VERSION = 4  # v4 protects the effective, explicitly declared import-compatibility identities.
Pair = tuple[str, str]
_POLICIES: dict[str, "PorLinkingPolicy"] = {}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _matches(record: dict, selector: dict) -> bool:
    operator = selector.get("operator", "equals")
    if operator not in ("equals", "contains"):
        raise SupplierLinkingError(f"Unsupported replacement operator: {operator}")
    compare = (lambda actual, wanted: actual == wanted) if operator == "equals" else (
        lambda actual, wanted: isinstance(actual, str) and isinstance(wanted, str) and wanted in actual)
    for key in ("name", "product", "reference product", "location", "unit"):
        if key in selector and not compare(record.get(key), selector[key]):
            return False
    for key, wanted in selector.get("excludes", {}).items():
        if compare(record.get(key), wanted):
            return False
    return True


def _import_pathway(alias: str, entry: dict) -> bool:
    variable = str(entry.get("production volume", {}).get("variable", ""))
    name = str(entry.get("ecoinvent alias", {}).get("name", "")).lower()
    return variable.startswith("Trade|") or "terminal" in variable.lower() or "_import" in alias or ", imported" in name


def _fixed_supply(name: str) -> bool:
    return name.startswith("electricity supply, ") and "for DAC methanol (por)" in name


def read_inventory_profiles(path: Path, protected_pairs: set[Pair] | None = None) -> dict[Key, list[dict]]:
    """Read supplier identities/amounts from the existing Brightway-style workbook."""
    import openpyxl

    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    profiles = {}
    try:
        for sheet in workbook:
            activity = header = None

            def finish():
                wanted = activity and (protected_pairs is None or dataset_key(activity)[:2] in protected_pairs
                                       or _fixed_supply(str(activity.get("name", ""))))
                if wanted and all(dataset_key(activity)):
                    key = dataset_key(activity)
                    if key in profiles:
                        raise SupplierLinkingError(f"Duplicate source activity identity in inventory: {key}")
                    profiles[key] = activity["exchanges"]

            for row in sheet.iter_rows(values_only=True):
                if not row or row[0] is None:
                    continue
                if row[0] == "Activity":
                    finish()
                    activity = {"name": row[1], "exchanges": []}
                    header = None
                elif activity is not None and row[0] == "name":
                    header = row
                elif activity is not None and header is not None:
                    record = {key: value for key, value in zip(header, row) if key}
                    wanted = protected_pairs is None or dataset_key(activity)[:2] in protected_pairs or _fixed_supply(str(activity.get("name", "")))
                    if wanted and record.get("type") == "technosphere":
                        record["product"] = record.pop("reference product", None)
                        if not all(record.get(key) for key in ("name", "unit", "location")):
                            raise SupplierLinkingError(
                                f"Incomplete source supplier at {sheet.title}: {activity['name']}. "
                                "Calculate/save workbook formulas before export.")
                        activity["exchanges"].append(record)
                elif activity is not None:
                    activity[row[0]] = row[1]
            finish()
    finally:
        workbook.close()
    return profiles


@dataclass
class PorLinkingPolicy:
    config: dict
    profiles: dict[Key, list[dict]] = field(default_factory=dict)
    locations: tuple[str, ...] = ("NL",)
    config_path: Path | None = None
    inventory_path: Path | None = None
    source_hashes: dict[str, str] = field(default_factory=dict)
    compatibility_rules: object | None = None

    def __post_init__(self):
        self.domestic_pairs: set[Pair] = set()
        self.import_pairs: set[Pair] = set()
        for alias, entry in self.config.get("production pathways", {}).items():
            provider = entry.get("ecoinvent alias", {})
            pair = (provider.get("name"), provider.get("reference product"))
            if not all(pair):
                continue
            (self.import_pairs if _import_pathway(alias, entry) else self.domestic_pairs).add(pair)
        for provider in self.config.get("regionalize", {}).get("datasets", []):
            pair = (provider.get("name"), provider.get("reference product"))
            if all(pair):
                self.domestic_pairs.add(pair)
        self.markets = self.config.get("markets", [])
        self.market_pairs = {(market["name"], market["reference product"]) for market in self.markets}
        self.market_names = {market["name"] for market in self.markets}
        for market in self.markets:
            for provider in market.get("add", []):
                pair = (provider.get("name"), provider.get("reference product"))
                if all(pair):
                    self.domestic_pairs.add(pair)
        self.market_additions = {
            (market["name"], market["reference product"]):
            {(entry["name"], entry["reference product"]) for entry in market.get("add", [])}
            for market in self.markets
        }
        for market in self.markets:
            if float(market.get("replacement ratio", 1)) != 1:
                raise SupplierLinkingError("Post-update linking supports 1:1 replacements only; encode other quantity changes in the LCI.")

    @classmethod
    def from_files(cls, config_path=ROOT / "configuration_file/config_itom_por.yaml",
                   inventory_path=ROOT / "inventories/lci-itom_por.xlsx", *,
                   compatibility_rules_path=ROOT / "configuration_file/inventory_supplier_compatibility.yaml"):
        config_path, inventory_path = Path(config_path).resolve(), Path(inventory_path).resolve()
        hashes = {str(path): _digest(path) for path in (config_path, inventory_path)}
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        rules = None
        if compatibility_rules_path is not None:
            from inventory_supplier_compat import load_compatibility_rules
            rules = load_compatibility_rules(compatibility_rules_path)
            hashes.update(rules.checksums)
        policy = cls(config, config_path=config_path, inventory_path=inventory_path,
                     source_hashes=hashes, compatibility_rules=rules)
        policy.profiles = read_inventory_profiles(inventory_path, policy.import_pairs)
        policy.verify_sources()
        return policy

    def verify_sources(self):
        for name, digest in self.source_hashes.items():
            if not Path(name).exists() or _digest(Path(name)) != digest:
                raise SupplierLinkingError(f"Model input changed during the update: {name}. Recreate the builder.")

    def role(self, dataset: dict) -> str | None:
        pair = dataset_key(dataset)[:2]
        if pair in self.import_pairs:
            return "import"
        if _fixed_supply(str(dataset.get("name", ""))):
            return "fixed_power"
        if dataset.get("location") not in self.locations:
            return None
        if pair in self.market_pairs:
            return "constructed_market"
        if pair in self.domestic_pairs:
            if str(dataset.get("name", "")).startswith(("market for ", "market group for ")):
                return "existing_market"
            return "domestic_process"
        return None


def _note(dataset, row, old, new, reason, status="changed"):
    return {"activity": dataset["name"], "reference_product": dataset["reference product"],
            "activity_location": dataset["location"], "exchange_index": row,
            "old_name": old.get("name"), "old_product": old.get("product"), "old_location": old.get("location"),
            "new_name": new.get("name"), "new_product": new.get("product"), "new_location": new.get("location"),
            "amount_before": old.get("amount"), "amount_after": new.get("amount"),
            "unit": old.get("unit"), "reason": reason, "status": status}


def _raw_source_profile(dataset, policy):
    profile = policy.profiles.get(dataset_key(dataset))
    if profile is None:
        candidates = [value for key, value in policy.profiles.items() if key[:3] == dataset_key(dataset)[:3]]
        if len(candidates) != 1:
            raise SupplierLinkingError(f"No unique original inventory profile for protected activity {dataset_key(dataset)}")
        profile = candidates[0]  # A regionalized copy retains its original import recipe.
    return profile


def _source_profile(dataset, policy, index):
    profile = _raw_source_profile(dataset, policy)
    if policy.compatibility_rules is not None:
        from inventory_supplier_compat import effective_profile
        profile = effective_profile(
            dataset, profile, index, policy.compatibility_rules,
            inventory=policy.inventory_path.name if policy.inventory_path else "lci-itom_por.xlsx",
        )
    normalized = []
    for source in profile:
        source = dict(source)
        if not source.get("product"):
            products = {key[1] for key in index if key[0] == source.get("name")
                        and key[2] == source.get("unit") and key[3] == source.get("location")}
            if len(products) != 1:
                raise SupplierLinkingError(f"Cannot identify original supplier product: {source}")
            source["product"] = products.pop()
        normalized.append(source)
    return normalized


def _protected_target(dataset, exchange, role, policy, index):
    profile = _source_profile(dataset, policy, index)
    current_key = exchange_key(exchange)
    source_exact = [source for source in profile if exchange_key(source) == current_key]
    if source_exact:
        return current_key
    needs_restore = role == "fixed_power" or exchange.get("name") in policy.market_names
    if role == "import" and exchange.get("location") in policy.locations:
        needs_restore = True
    if not needs_restore:
        return current_key  # Retain a legitimate future foreign supplier.
    if policy.compatibility_rules is not None:
        # A declared compatibility rule may change reference product (e.g.
        # voltage). Restore its canonical provider rather than requiring the
        # displaced product to equal the normalized source-profile product.
        inventory = policy.inventory_path.name if policy.inventory_path else "lci-itom_por.xlsx"
        targets = set()
        for source, normalized in zip(_raw_source_profile(dataset, policy), profile):
            rule = policy.compatibility_rules.matching(dataset, source, inventory)
            if rule is None:
                continue
            allowed = [rule["exchange"]] + rule["candidates"]
            if rule.get("fallback"):
                allowed.append(rule["fallback"]["provider"])
            names = {item["name"] for item in allowed}
            products = set(rule["exchange"]["reference_products"])
            products.update(item["reference product"] for item in rule["candidates"])
            if rule.get("fallback"):
                products.add(rule["fallback"]["provider"]["reference product"])
            if current_key[0] in names and current_key[1] in products and current_key[2] == source.get("unit"):
                targets.add(exchange_key(normalized))
        if len(targets) > 1:
            raise SupplierLinkingError(f"Ambiguous protected compatibility target for {current_key}")
        if targets:
            target = targets.pop()
            if _unique(index, target) is None:
                raise SupplierLinkingError(f"Protected compatibility supplier is missing: {target}")
            return target
    sources = {exchange_key(source): source for source in profile
               if source.get("product") == exchange.get("product") and source.get("unit") == exchange.get("unit")}
    if len(sources) != 1:
        raise SupplierLinkingError(f"Cannot uniquely restore protected supplier in {dataset_key(dataset)}: {current_key}")
    source_key = next(iter(sources))
    if _unique(index, source_key) is None:
        raise SupplierLinkingError(f"Protected source supplier is missing: {source_key}. Do not substitute an NL producer.")
    return source_key


def _regionalized_market_supply(dataset, exchange, policy, generic_regions):
    """Whether a copied NL market's product supply has a declared local route.

    This does not rebuild a market: each existing technology exchange keeps its
    quantity. Ancillary transport, unconfigured producers, explicit country
    imports and the scenario's constructed PoR market mix are not eligible.
    """
    key = exchange_key(exchange)
    pair = key[:2]
    return (
        key[1] == dataset.get("reference product")
        and key[2] == dataset.get("unit")
        and pair in policy.domestic_pairs
        and pair not in policy.import_pairs
        and not key[0].startswith(("market for ", "market group for "))
        and not _fixed_supply(key[0])
        and key[3] != dataset.get("location")
        and key[3] in generic_regions
    )


def _regional_target(dataset, exchange, policy, resolver, generic_regions):
    """Resolve generic inputs without relocating intentional foreign suppliers."""
    key = exchange_key(exchange)
    if key[:2] in policy.import_pairs or _fixed_supply(key[0]):
        return key, "protected_supply_route"
    if key[3] == dataset["location"]:
        if _unique(resolver.index, key) is not None:
            return key, "kept_local_supplier"
        # A missing local electricity group can have an exact-voltage market
        # equivalent; don't reject the alias before trying that provider.
        target, reason = resolver.regional_candidate(exchange, dataset["location"], exclude=dataset_key(dataset))
        return (target, "geographic_" + reason) if target is not None else (key, "kept_existing_supplier")
    is_market = key[0].startswith(("market for ", "market group for "))
    # Operational energy is consumed at the copied plant, not imported from
    # the source inventory's country. Imported/fixed-energy routes have already
    # been excluded above, and their consuming datasets bypass this helper.
    operational_energy = is_market and (
        (key[1].startswith("electricity, ") and key[2] == "kilowatt hour")
        or (key[1].startswith("heat, ") and key[2] == "megajoule")
    )
    # Explicit country/subnational supplies stay pinned. Aggregate reference
    # regions are geographic proxies, not evidence of a specific trade origin.
    explicit_country = bool(re.fullmatch(r"[A-Z]{2}(?:-.+)?", key[3]))
    if explicit_country and not operational_energy:
        return key, "preserved_specific_foreign_supplier"
    generic = operational_energy or key[3] in generic_regions or (
        not explicit_country and resolver.geography.faces(key[3]) is not None
    )
    missing_market = is_market and not explicit_country and _unique(resolver.index, key) is None
    if not generic and not missing_market:
        return key, "preserved_specific_foreign_supplier"
    target, reason = resolver.regional_candidate(exchange, dataset["location"], exclude=dataset_key(dataset))
    if target is not None:
        return target, "geographic_" + reason
    if _unique(resolver.index, key) is None and is_market:
        for location in ("World", "GLO", "RoW"):
            for name in resolver._names(key[0]):
                candidate = (name, key[1], key[2], location)
                if candidate != dataset_key(dataset) and _unique(resolver.index, candidate) is not None:
                    return candidate, "missing_market_global_fallback"
    return key, "retained_geographic_proxy" if key[3] in ("RoW", "GLO", "World") or operational_energy else "kept_existing_supplier"


def link_por_database(database: list[dict], policy: PorLinkingPolicy, *, model="remind"):
    """Return a validated replacement list and audit; never mutate the input list."""
    resolver = ReferenceSupplierResolver(database, model=model)
    topology, topology_path, topology_hash = resolver.geography.topology, resolver.geography.path, resolver.geography.sha256
    generic_regions = set(topology) | {
        "RER", "EUR", "RoW", "GLO", "World", "Europe without Switzerland", "RER w/o RU",
    }
    index = resolver.index
    result = list(database)
    audit = []
    selected = 0
    for position, dataset in enumerate(database):
        role = policy.role(dataset)
        if role is None:
            continue
        selected += 1
        if not all(dataset_key(dataset)):
            raise SupplierLinkingError(f"Incomplete selected activity identity: {dataset_key(dataset)}")
        # A shallow dataset copy plus copied changed exchanges preserves unrelated metadata.
        updated = dict(dataset)
        exchanges = []
        for row, original in enumerate(dataset.get("exchanges", [])):
            if original.get("type") != "technosphere":
                exchanges.append(original)
                continue
            amount = original.get("amount")
            if not isinstance(amount, Number) or isinstance(amount, bool) or not math.isfinite(float(amount)):
                raise SupplierLinkingError(f"Invalid exchange amount in {dataset_key(dataset)}: {original}")
            if amount == 0:
                exchanges.append(original)
                continue
            key = exchange_key(original)
            if not all(key):
                raise SupplierLinkingError(f"Incomplete supplier in {dataset_key(dataset)}: {key}")
            target, reason = key, "kept_existing_supplier"
            if role in ("import", "fixed_power"):
                target = _protected_target(dataset, original, role, policy, index)
                if target != key:
                    reason = "restored_protected_source_supplier"
                if role == "fixed_power":
                    matching = [source for source in _source_profile(dataset, policy, index) if exchange_key(source) == target]
                    if not matching or not math.isclose(float(amount), float(matching[0]["amount"]), rel_tol=1e-12, abs_tol=1e-12):
                        raise SupplierLinkingError(f"Fixed renewable supply share changed in {dataset_key(dataset)}")
            elif role == "existing_market" and _regionalized_market_supply(
                dataset, original, policy, generic_regions
            ):
                candidate = (*key[:3], dataset["location"])
                if _unique(index, candidate) is not None:
                    target, reason = candidate, "regionalized_market_product_supply"
                else:
                    audit.append(_note(dataset, row, original, original,
                                       f"Declared local market supplier not created: {candidate}",
                                       "unavailable_local_supplier"))
            elif role == "existing_market" and (
                key[1] != dataset["reference product"] or key[2] != dataset["unit"]
            ):
                # Regionalize ancillary services (e.g. transport), not the
                # unconfigured technologies making up the market's product mix.
                target, reason = _regional_target(dataset, original, policy, resolver, generic_regions)
            elif role == "domestic_process" or (
                role == "constructed_market" and (key[0], key[1]) in policy.market_additions[dataset_key(dataset)[:2]]
            ):
                if not _fixed_supply(key[0]):
                    targets = set()
                    for market in policy.markets:
                        # Match premise's AND semantics for 'replaces in'.
                        if not all(_matches(dataset, selector) for selector in market.get("replaces in", [])):
                            continue
                        if any(_matches(original, selector) for selector in market.get("replaces", [])):
                            candidate = (market["name"], market["reference product"], market["unit"], dataset["location"])
                            if _unique(index, candidate) is not None:
                                targets.add(candidate)
                            else:
                                audit.append(_note(dataset, row, original, original,
                                                   f"Market not created in this scenario: {candidate}", "unavailable_market"))
                    if len(targets) > 1:
                        raise SupplierLinkingError(f"Conflicting PoR replacement rules for {dataset_key(dataset)}: {key}")
                    if targets:
                        target, reason = targets.pop(), "configured_por_market"
                    else:
                        target, reason = _regional_target(dataset, original, policy, resolver, generic_regions)
            if reason == "retained_geographic_proxy":
                audit.append(_note(dataset, row, original, original, reason, "retained_geographic_proxy"))
            supplier = _unique(index, target)
            if supplier is None:
                raise SupplierLinkingError(f"Unlinked selected exchange in {dataset_key(dataset)}: {target}")
            if target == dataset_key(dataset):
                raise SupplierLinkingError(f"Direct self-supply would be introduced in {target}")
            if target != key:
                exchange = _substitute(original, target)
                audit.append(_note(dataset, row, original, exchange, reason))
            else:
                exchange = original
                address = original.get("input")
                if address is not None and supplier.get("code") and (
                    not isinstance(address, (tuple, list)) or len(address) != 2 or address[1] != supplier["code"]
                ):
                    exchange = _substitute(original, target)
                    audit.append(_note(dataset, row, original, exchange, "removed_stale_supplier_address"))
            if exchange["amount"] != amount or exchange["unit"] != original["unit"]:
                raise SupplierLinkingError("Supplier linking changed an input quantity or unit.")
            for field_name in ("uncertainty type", "loc", "scale", "minimum", "maximum", "negative"):
                left, right = exchange.get(field_name), original.get(field_name)
                same = left is right or left == right
                if not same and isinstance(left, Number) and isinstance(right, Number):
                    same = math.isnan(float(left)) and math.isnan(float(right))
                if not same:
                    raise SupplierLinkingError(f"Supplier linking changed uncertainty field {field_name}")
            exchanges.append(exchange)
        updated["exchanges"] = exchanges
        result[position] = updated

    # Do not allow an import provider to consume one of the PoR markets supplying it.
    linked_index = {dataset_key(dataset): dataset for dataset in result}
    for market in policy.markets:
        for location in policy.locations:
            key = (market["name"], market["reference product"], market["unit"], location)
            dataset = linked_index.get(key)
            if dataset is None:
                continue
            for exchange in dataset.get("exchanges", []):
                imported = linked_index.get(exchange_key(exchange)) if exchange.get("type") == "technosphere" else None
                if imported is not None and policy.role(imported) == "import":
                    if any(exchange_key(feed) == key and feed.get("amount", 0) != 0
                           for feed in imported.get("exchanges", []) if feed.get("type") == "technosphere"):
                        raise SupplierLinkingError(f"Circular market/import supply: {key} -> {dataset_key(imported)} -> {key}")
    compatibility_receipts = []
    if policy.compatibility_rules is not None:
        for dataset in result:
            receipt = dataset.get("inventory supplier compatibility")
            if receipt:
                compatibility_receipts.append({"activity": list(dataset_key(dataset)), **receipt})
    summary = {"selected_activities": selected,
               "linking_policy_version": LINKING_POLICY_VERSION,
               "inventory_supplier_compatibility": compatibility_receipts,
               "compatibility_targeted_exchanges": sum(len(receipt.get("decisions", [])) for receipt in compatibility_receipts),
               "changed_exchanges": sum(row["status"] == "changed" for row in audit),
               "unavailable_markets": sum(row["status"] == "unavailable_market" for row in audit),
               "unavailable_local_suppliers": sum(row["status"] == "unavailable_local_supplier" for row in audit),
               "retained_geographic_proxies": sum(row["status"] == "retained_geographic_proxy" for row in audit),
               "topology": str(topology_path), "topology_sha256": topology_hash,
               "ecoinvent_topology": str(resolver.geography.ecoinvent_path),
               "ecoinvent_topology_sha256": resolver.geography.ecoinvent_sha256}
    return result, audit, summary


def configure_por_export_support(*, verbose=True):
    from inventory_supplier_compat import install_inventory_supplier_compatibility
    from premise_compat import (disable_legacy_por_regionalization, install_premise_null_bounds_fix,
                               install_premise_pathways_mapping_fix)
    install_premise_null_bounds_fix(verbose=verbose)
    disable_legacy_por_regionalization(verbose=verbose)
    install_por_export_geography_guard(verbose=verbose)
    install_premise_pathways_mapping_fix(verbose=verbose)
    install_inventory_supplier_compatibility(verbose=verbose)


def _register_policy(policy):
    token = getattr(policy, "_runtime_token", None)
    if token is None:
        token = uuid.uuid4().hex
        policy._runtime_token = token
    _POLICIES[token] = policy
    return token


def _policy_for_marker(marker):
    if marker.get("linking_policy_version") != LINKING_POLICY_VERSION:
        raise SupplierLinkingError(
            "Prepared PoR scenario uses outdated supplier-linking rules. "
            "Restart the kernel, recreate the builder and run update with the repo hook before exporting.")
    for path_field, hash_field in (("topology", "topology_sha256"),
                                   ("ecoinvent_topology", "ecoinvent_topology_sha256")):
        if marker.get(path_field) and marker.get(hash_field):
            if _digest(Path(marker[path_field])) != marker[hash_field]:
                raise SupplierLinkingError("Geographic definitions changed after supplier linking; rebuild the prepared scenario.")
    for source_path, expected in marker.get("source_sha256", {}).items():
        path = Path(source_path)
        if not path.is_file() or _digest(path) != expected:
            raise SupplierLinkingError(
                f"Prepared scenario source changed: {source_path}. Recreate the builder."
            )
    policy = _POLICIES.get(marker.get("policy_token"))
    if policy is not None:
        policy.verify_sources()
        if policy.source_hashes != marker.get("source_sha256", {}):
            raise SupplierLinkingError("Prepared scenario sources differ from its in-memory policy; recreate the builder.")
        return policy
    if not marker.get("config_path") or not marker.get("inventory_path"):
        raise SupplierLinkingError("No supplier-linking policy is available for this prepared scenario.")
    policy = PorLinkingPolicy.from_files(
        marker["config_path"], marker["inventory_path"],
        compatibility_rules_path=marker.get("compatibility_rules_path"),
    )
    if policy.source_hashes != marker.get("source_sha256"):
        raise SupplierLinkingError("Prepared scenario sources differ from the current files; recreate the builder.")
    _register_policy(policy)
    return policy


def install_por_export_geography_guard(export_module=None, *, verbose=True):
    """Keep validated repo links through premise's later geographic export check."""
    if export_module is None:
        import premise.export as export_module
    original = export_module.check_geographical_linking
    if getattr(original, "__por_supplier_link_guard__", False):
        return False

    @wraps(original)
    def preserve_validated_links(scenario, original_database):
        marker = scenario.get("por supplier linking")
        if not marker:
            return original(scenario, original_database)
        policy = _policy_for_marker(marker)
        before_database = scenario["database"]
        pins = []
        for dataset in scenario["database"]:
            if policy.role(dataset) is None:
                continue
            for row, exchange in enumerate(dataset.get("exchanges", [])):
                if exchange.get("type") == "technosphere":
                    pins.append((dataset, row, exchange, dict(exchange)))
        prepared = original(scenario, original_database)
        if prepared.get("database") is not before_database:
            raise SupplierLinkingError("premise's geographical export check replaced the database object; review the adapter.")
        restored = []
        for dataset, row, exchange, saved in pins:
            if exchange.get("amount") != saved.get("amount") or exchange.get("unit") != saved.get("unit"):
                raise SupplierLinkingError("premise export preparation changed a validated input amount/unit.")
            if exchange_key(exchange) != exchange_key(saved):
                attempted = dict(exchange)
                for key in ("name", "product", "unit", "location"):
                    exchange[key] = saved[key]
                exchange.pop("input", None)
                exchange.pop("database", None)
                restored.append(_note(dataset, row, attempted, exchange,
                                      "preserved_repo_link_during_export", "preserved_on_export"))
        if restored:
            marker["prevented_export_geography_changes"] = marker.get("prevented_export_geography_changes", 0) + len(restored)
            report_path = marker.get("change_report_path")
            if report_path:
                path = Path(report_path)
                with path.open("r", encoding="utf-8", newline="") as stream:
                    fields = next(csv.reader(stream))
                with path.open("a", encoding="utf-8", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=fields)
                    for row in restored:
                        writer.writerow({"model": marker.get("model"), "pathway": marker.get("pathway"),
                                         "year": marker.get("year"),
                                         "external_scenarios": " + ".join(map(str, marker.get("external_scenarios", []))), **row})
                path.with_suffix(".json").write_text(json.dumps(marker, indent=2), encoding="utf-8")
        return prepared

    preserve_validated_links.__por_supplier_link_guard__ = True
    export_module.check_geographical_linking = preserve_validated_links
    if verbose:
        print("Applied export guard for scenarios with validated repo-managed PoR supplier links.")
    return True


def _has_por_scenario(scenario: dict) -> bool:
    if "external scenarios" not in scenario:
        return "external" in scenario.get("applied functions", [])
    for entry in scenario["external scenarios"]:
        data = entry.get("data")
        if isinstance(data, (str, Path)) and "itom_por" in str(data):
            return True
        if getattr(data, "descriptor", {}).get("name") == "itom_por":
            return True
    return False


def _inventory_store_api():
    """Import the store API only for premise versions/scenarios which use it."""
    from premise import inventory_store
    return inventory_store


def _inventory_validation_api():
    from premise import validation_framework
    return validation_framework


def _has_inventory_store(scenario):
    return any(scenario.get(key) is not None for key in (
        "_inventory_store", "_inventory_checkpoint", "_inventory_export_handoff",
    ))


def _selected_store_inventory(store, policy):
    """Load background identities only; full exchanges only for linked targets."""
    underlying = getattr(store, "_store", store)
    iterator = getattr(underlying, "_iter_storage_activities", None)
    if not callable(iterator):
        database = store.materialize(restore_metadata=True)
        return database, list(store.iter_activity_ids())
    fields = ("name", "reference product", "unit", "location", "code", "database")
    database, activity_ids = [], []
    for activity_id, metadata, _ in iterator():
        entry = {key: metadata.get(key) for key in fields}
        if policy.role(entry) is not None:
            entry = store.activity(activity_id).to_dict()
        database.append(entry)
        activity_ids.append(activity_id)
    return database, activity_ids


def _patch_linked_exchanges(store, changed, originals_by_id):
    """Patch 1:1 exchanges, retaining the IDs needed for sparse checkpoints."""
    with store.transaction("repo:por_supplier_linking") as transaction:
        patch_exchange = getattr(transaction, "patch_exchange", None)
        for activity_id, dataset in changed:
            if dataset_key(store.activity(activity_id).to_dict()) != dataset_key(dataset):
                raise SupplierLinkingError("premise store/materialization activity order differs; refusing to modify the wrong activity.")
            old = originals_by_id[activity_id].get("exchanges", [])
            new = dataset["exchanges"]
            ids = getattr(getattr(store, "_state", None), "activity_exchanges", {}).get(activity_id)
            if not callable(patch_exchange) or ids is None:
                transaction.replace_exchanges(activity_id, new)
                continue
            if len(old) != len(new) or len(ids) != len(old):
                raise SupplierLinkingError("Supplier linking changed the exchange count; refusing to rewrite native IDs.")
            for exchange_id, before, after in zip(ids, old, new):
                if before is after:
                    continue
                # Full replacement fields preserve all metadata; delete removed
                # addresses explicitly rather than leaving a stale input pointer.
                patch_exchange(exchange_id, after, delete_fields=set(before) - set(after), activity_id=activity_id)


def _consumer_edges(exchanges, identities, identifiers):
    edges = set()
    for exchange in exchanges:
        if exchange.get("type") != "technosphere":
            continue
        address = exchange.get("input")
        provider = identifiers.get(tuple(address)) if isinstance(address, (tuple, list)) and len(address) == 2 else None
        if provider is None:
            key = (exchange.get("name"), exchange.get("product", exchange.get("reference product")), exchange.get("location"))
            matches = identities.get(key, ())
            if len(matches) == 1:
                provider = matches[0]
        if provider is not None:
            edges.add(provider)
    return edges


def _graph_baseline(store, validation):
    """One native storage scan supplies fingerprints AND reusable graph edges."""
    required = ("_iter_activity_metadata", "_iter_storage", "_stored_activity_fingerprint", "_stable_hash", "_cycle_signatures")
    if not all(callable(getattr(validation, name, None)) for name in required):
        return None
    keys, identities, identifiers = {}, defaultdict(list), {}
    for activity_id, metadata in validation._iter_activity_metadata(store):
        key = (metadata.get("name"), metadata.get("reference product", metadata.get("product")), metadata.get("location"))
        keys[activity_id] = key
        identities[key].append(activity_id)
        if metadata.get("database") is not None and metadata.get("code") is not None:
            identifiers[(metadata["database"], metadata["code"])] = activity_id
    rows, graph = defaultdict(list), {}
    for activity_id, metadata, exchanges in validation._iter_storage(store):
        exchanges = tuple(exchanges)
        rows[keys[activity_id]].append(validation._stored_activity_fingerprint(store, activity_id, metadata, exchanges))
        graph[activity_id] = _consumer_edges((exchange for _, exchange in exchanges), identities, identifiers)
    fingerprints = {key: hashes[0] if len(hashes) == 1 else validation._stable_hash(sorted(hashes)) for key, hashes in rows.items()}
    cycles = validation._cycle_signatures(graph, keys)
    return fingerprints, cycles, graph, keys, identities, identifiers


def _preserve_relinked_source_components(database, linked, audit):
    """Keep distinct original supply components distinct after regional relinking.

    Two different country/database providers can become byte-identical exchanges
    after a 1:1 substitution. Do not drop one or merge uncertain amounts: retain
    their actual source identities in exchange provenance instead. Originally
    identical rows stay identical and remain explicit inherited-data findings.
    """
    preserved = 0
    for old, new in zip(database, linked):
        if old is new:
            continue
        groups = {}
        for row, exchange in enumerate(new.get("exchanges", [])):
            if exchange.get("type") == "technosphere":
                signature = json.dumps(exchange, sort_keys=True, default=repr)
                groups.setdefault(signature, []).append(row)
        for rows in groups.values():
            if len(rows) < 2:
                continue
            origins = [{"supplier": list(exchange_key(old["exchanges"][row])),
                        "input": old["exchanges"][row].get("input"),
                        "database": old["exchanges"][row].get("database")} for row in rows]
            if len({json.dumps(origin, sort_keys=True, default=repr) for origin in origins}) == 1:
                continue
            for row, origin in zip(rows, origins):
                exchange = dict(new["exchanges"][row])
                exchange["por source component"] = origin
                new["exchanges"][row] = exchange
                audit.append(_note(new, row, old["exchanges"][row], exchange,
                                   "preserved_distinct_source_component", "preserved_component"))
                preserved += 1
    return preserved


def _certify_linked_store(builder, working, staged_store, validation, intent,
                          activity_ids, database, summary):
    """Certify changes; record only demonstrably inherited duplicate findings.

    Exceptions are generated afresh after comparing the exact original exchange
    pair. They are never a blanket duplicate-rule suppression and never remove
    a demand or alter its amount/distribution.
    """
    version = str(getattr(builder, "version", "unknown"))
    system_model = str(getattr(builder, "system_model", "cutoff"))
    if not hasattr(validation, "InventoryGraphValidator"):
        # Lightweight test adapters can still exercise the native call contract.
        return builder._certify_scenario_store(working, staged_store, intent=intent, exhaustive=True)
    probe = validation.InventoryGraphValidator(
        staged_store, intent=intent, version=version, system_model=system_model, suppressions=(),
    ).validate()
    duplicate_issues = [issue for issue in probe.issues if issue.rule_id == "GRAPH.DUPLICATE_SUPPLIER"]
    original_by_id = dict(zip(activity_ids, database))
    pairs = {}
    inherited = []
    unsafe_activities = set()
    for issue in duplicate_issues:
        exchange_ids = list(staged_store._state.activity_exchanges[issue.activity_id])
        try:
            first = exchange_ids.index(issue.actual["first_exchange_id"])
            second = exchange_ids.index(issue.exchange_id)
            originals = original_by_id[issue.activity_id]["exchanges"]
            existing = second < len(originals) and first < len(originals) and originals[first] == originals[second]
        except (KeyError, ValueError, IndexError):
            existing = False
        if not existing:
            unsafe_activities.add(issue.activity_id)
        else:
            inherited.append({"activity": list(issue.activity_key), "exchange_indices": [first, second],
                              "amount": float(originals[second]["amount"]), "unit": originals[second]["unit"]})
            pairs.setdefault(issue.activity_id, []).append(issue)
    if unsafe_activities:
        # The already evaluated report is sufficient to reject a new duplicate.
        probe.raise_for_errors()

    explanation = ("Exact duplicate exchange pair already present in the updated scenario before repo supplier linking. "
                   "Both original demands/uncertainties are retained unchanged; review the source LCI separately. "
                   "This exception is regenerated only after checking the original pair.")
    exceptions = [validation.ValidationSuppression(
        rule_id="GRAPH.DUPLICATE_SUPPLIER",
        selector=validation.ActivitySelector(name=issues[0].activity_key[0], product=issues[0].activity_key[1],
                                            location=issues[0].activity_key[2], code=issues[0].activity_code),
        versions=(version,), system_models=(system_model,), explanation=explanation,
    ) for issues in pairs.values()]
    from dataclasses import replace
    if pairs:
        intent = replace(intent, computed_target_values={"inherited_duplicate_review": inherited})
    identity = builder._scenario_identity(working)
    validator = validation.InventoryGraphValidator(
        staged_store, scenario_identity=identity,
        source_fingerprint=builder._validation_source_fingerprint(),
        iam_fingerprint=builder._validation_iam_fingerprint(working),
        version=version, system_model=system_model, intent=intent,
        suppressions=(*validation.load_validation_suppressions(), *exceptions),
    )
    # Reuse the read-only pass above. Changing certificate identity/exception
    # metadata does not change any rule inputs or require a second graph scan.
    report = replace(probe, certificate_key=validator.cache_key,
                     rule_results=tuple(validator._apply_suppressions(rule) for rule in probe.rule_results),
                     phase_results=tuple(replace(phase, rule_results=tuple(
                         validator._apply_suppressions(rule) for rule in phase.rule_results)) for phase in probe.phase_results))
    certificate = validation.ValidationCertificate(
        cache_key=validator.cache_key, store_generation=validator.generation,
        ruleset_version=validation.VALIDATION_RULESET_VERSION, scenario_identity=identity,
        source_fingerprint=validator.source_fingerprint, iam_fingerprint=validator.iam_fingerprint,
        system_model=system_model, version=version, report=report,
    )
    for payload in working.get("_validation_phase_results", ()):
        phase = validation.ValidationPhaseResult.from_dict(payload)
        if phase.kind == "sector":
            report = report.with_phase(phase)
    report.raise_for_errors()
    certificate = replace(certificate, report=report)
    staged_store._validation_certificate_payload = certificate.to_dict()
    working["_validation_report"] = report.to_dict()
    if not isinstance(getattr(builder, "_validation_reports", None), dict):
        builder._validation_reports = {}
    builder._validation_reports[identity] = report
    if pairs:
        summary.update(inherited_duplicate_supplier_exchanges=len(inherited), inherited_duplicate_review=inherited)
    return certificate


def _stage_store_linking(builder, scenario, policy, report_dir, *, progress=None):
    """Fork, validate and persist a native store without changing its source.

    premise 2.5 uses private stores/checkpoints instead of ``database filepath``.
    Converting only the payload to a legacy pickle is unsafe: native exporters
    prefer the old checkpoint and would silently discard the new supplier links.
    """
    started = previous = time.perf_counter()
    current_phase = None
    timings = {}
    def phase(name):
        nonlocal previous, current_phase
        now = time.perf_counter()
        if current_phase is not None:
            timings[current_phase] = round(now - previous, 3)
        current_phase, previous = name, now
        if progress is not None:
            progress(name)
    phase("loading updated inventory")
    api = _inventory_store_api()
    store = scenario.get("_inventory_store")
    if store is None:
        if scenario.get("_inventory_checkpoint") is not None:
            # Prefer the durable checkpoint to a possibly consumed export handoff.
            store = api.InventoryStore.open(scenario["_inventory_checkpoint"])
        else:
            store = scenario.get("_inventory_export_handoff")
    if store is None or not all(callable(getattr(store, name, None)) for name in (
        "materialize", "iter_activity_ids", "activity", "fork", "transaction", "checkpoint",
    )):
        raise SupplierLinkingError("Unsupported premise inventory store; refusing reference-database fallback.")
    phase("loading PoR activities and supplier identities")
    database, activity_ids = _selected_store_inventory(store, policy)
    if not database or len(activity_ids) != len(database):
        raise SupplierLinkingError("Updated inventory store is empty or inconsistent; recreate the builder and update again.")
    phase("matching and linking suppliers")
    linked, audit, summary = link_por_database(database, policy, model=str(scenario.get("model", "remind")))
    summary["preserved_source_components"] = _preserve_relinked_source_components(database, linked, audit)
    # The linker returns new exchanges only for an actual supplier/address change.
    # Replacing those activities keeps native activity IDs, quantities and metadata.
    changed = [(activity_id, new) for activity_id, old, new in zip(activity_ids, database, linked)
               if any(left is not right for left, right in zip(old.get("exchanges", []), new.get("exchanges", [])))]
    certify = getattr(builder, "_certify_scenario_store", None)
    graph_context = None
    if changed and callable(certify):
        phase("capturing validation baseline")
        validation = _inventory_validation_api()
        graph_context = _graph_baseline(store, validation)
        if graph_context is None:
            baseline_fingerprints, baseline_cycles = validation.inventory_baseline_snapshot(store)
        else:
            baseline_fingerprints, baseline_cycles = graph_context[:2]
    # Fork after baseline capture so cached unmodified fingerprints are shared.
    staged_store = store.fork(scenario_identity=store.scenario_identity)
    if changed:
        _patch_linked_exchanges(staged_store, changed, dict(zip(activity_ids, database)))
    working = dict(scenario)
    for key in ("database", "database filepath", "database metadata filepath",
                "_inventory_export_handoff", "_inventory_working_copy"):
        working.pop(key, None)
    working["_inventory_store"] = staged_store

    # Validate this post-step against the already updated scenario, not against
    # the pristine source database. A whole-background certification here would
    # misattribute pre-existing premise issues/cycles to our supplier changes.
    if changed and callable(certify):
        phase("validating links and cycle changes")
        # Native targeted validation walks only target activities, so also check
        # the complete graph for cycles crossing unchanged upstream activities.
        if graph_context is None:
            after_cycles = validation.inventory_cycle_signatures(staged_store)
        else:
            _, _, before_graph, graph_keys, graph_identities, graph_identifiers = graph_context
            after_graph = dict(before_graph)
            for activity_id, dataset in changed:
                after_graph[activity_id] = _consumer_edges(dataset["exchanges"], graph_identities, graph_identifiers)
            after_cycles = validation._cycle_signatures(after_graph, graph_keys)
        changed_components = after_cycles - baseline_cycles
        # SCC membership is not a list of individual cycles. Regional copies can
        # join/split an existing cyclic background component without introducing
        # an isolated foreground feedback loop. Record that for review instead
        # of treating an entire 19,000-activity background SCC as a new error.
        background_reconfigurations = frozenset(
            component for component in changed_components
            if any(component & previous for previous in baseline_cycles)
        )
        added_cycles = changed_components - background_reconfigurations
        if added_cycles:
            preview = [{"activity_count": len(cycle), "first_activities": sorted(cycle, key=repr)[:5]}
                       for cycle in sorted(added_cycles, key=repr)[:3]]
            raise SupplierLinkingError(f"PoR supplier linking introduced {len(added_cycles)} new technosphere cycle(s): {preview}")
        intent = validation.ValidationIntent(
            transformation="por_supplier_linking",
            targeted=True,
            affected_activity_ids=frozenset(activity_id for activity_id, _ in changed),
            expected_match_count=len(changed),
            baseline_fingerprints=baseline_fingerprints,
            baseline_cycles=baseline_cycles,
        )
        known_target_cycles = set()
        reviewed_target_cycles = set()
        if baseline_cycles:
            # The native targeted pass sees an induced subgraph, so its smaller
            # SCC signatures cannot be compared literally to whole-graph SCCs.
            cycle_intent = validation.ValidationIntent(
                transformation="por_supplier_linking",
                targeted=True,
                affected_activity_ids=intent.affected_activity_ids,
                expected_match_count=len(changed),
            )
            def target_cycles(inventory):
                if graph_context is not None:
                    graph = before_graph if inventory is store else after_graph
                    return set(validation._cycle_signatures(
                        {activity_id: graph.get(activity_id, set()) for activity_id in intent.affected_activity_ids}, graph_keys))
                report = validation.InventoryGraphValidator(
                    inventory, intent=cycle_intent,
                    version=str(getattr(builder, "version", "unknown")),
                    system_model=str(getattr(builder, "system_model", "cutoff")),
                    suppressions=(),
                ).validate()
                return {frozenset(tuple(key) for key in issue.actual)
                        for issue in report.issues if issue.rule_id == "GRAPH.NEW_FORBIDDEN_CYCLE"}
            known_target_cycles = target_cycles(store)
            if background_reconfigurations:
                reviewed_target_cycles = {
                    cycle for cycle in target_cycles(staged_store) - known_target_cycles
                    if any(cycle <= component for component in background_reconfigurations)
                }
            if known_target_cycles or reviewed_target_cycles:
                from dataclasses import replace
                intent = replace(intent, baseline_cycles=frozenset(
                    set(baseline_cycles) | known_target_cycles | reviewed_target_cycles))
        # exhaustive=True means evaluate the declared intent, not reuse sector
        # certificates. intent.targeted prevents re-auditing unrelated exchanges.
        _certify_linked_store(builder, working, staged_store, validation, intent, activity_ids, database, summary)
        summary.update(native_validation_scope="post_update_changed_activities",
                       native_validation_targets=len(changed),
                       native_validation_baseline_cycles=len(baseline_cycles),
                       native_validation_new_cycles=0,
                       native_validation_background_cycle_reconfigurations=len(background_reconfigurations),
                       native_validation_reviewed_target_cycles=len(reviewed_target_cycles))
        if background_reconfigurations:
            summary["native_validation_cycle_review"] = [
                {"activity_count": len(component), "first_activities": sorted(component, key=repr)[:5]}
                for component in sorted(background_reconfigurations, key=repr)
            ]
    elif changed and scenario.get("_validation_report") is not None:
        raise SupplierLinkingError("Cannot refresh premise's inventory-store certificate; refusing export with stale validation.")
    elif not changed:
        summary["native_validation_scope"] = "unchanged_store_generation"

    persisted = scenario.get("_inventory_checkpoint") is not None
    if persisted:
        phase("saving linked checkpoint")
        # Native reopened stores default to a full checkpoint writer. Matching
        # premise's update persistence flag keeps unchanged source columns shared;
        # patch_exchange above preserves the contiguous IDs required for deltas.
        if callable(getattr(api, "_scenario_delta_base_storage", None)):
            staged_store._scenario_cache_compatibility = True
            summary["sparse_checkpoint_eligible"] = api._scenario_delta_base_storage(staged_store) is not None
        destination = Path(report_dir) / "inventory_stores" / f"por_{uuid.uuid4().hex}.inventory-store"
        checkpoint = staged_store.checkpoint(destination)
        if checkpoint is None:
            raise SupplierLinkingError("premise did not persist the relinked inventory store.")
        working["_inventory_checkpoint"] = checkpoint

    # Rebind premise's lazy scenario-mapping entries to the relinked graph. Their
    # activity IDs remain stable across the fork, including checkpoint round trips.
    if scenario.get("mapping"):
        hydrate = getattr(api, "_hydrate_scenario_mapping", None)
        compact = getattr(api, "_compact_scenario_mapping", None)
        activities = getattr(getattr(staged_store, "_state", None), "activities", None)
        if not callable(hydrate) or activities is None or (persisted and not callable(compact)):
            raise SupplierLinkingError("premise scenario-mapping API changed; cannot safely rebind the linked inventory store.")
        mapping = hydrate(scenario["mapping"], activities)
        working["mapping"] = compact(mapping, staged_store, checkpoint) if persisted else mapping
    if persisted:
        # Export must load this new checkpoint, never an old single-use handoff.
        working.pop("_inventory_store", None)
    else:
        working.pop("_inventory_checkpoint", None)
    summary["inventory_storage"] = "inventory_checkpoint" if persisted else "inventory_store"
    phase("ready")
    summary["phase_seconds"] = timings
    summary["linking_seconds"] = round(time.perf_counter() - started, 3)
    return working, audit, summary


def apply_por_supplier_linking(builder, *, policy=None, report_dir=ROOT / "export/por_supplier_linking",
                               stage="export", load_database_func=None, dump_database_func=None,
                               verbose=True):
    """Link updated legacy inventories or native stores, then commit references."""
    policy = policy or PorLinkingPolicy.from_files()
    policy.verify_sources()
    scenarios = getattr(builder, "scenarios", None)
    if not isinstance(scenarios, list):
        raise SupplierLinkingError("Expected a premise NewDatabase object with a scenarios list.")
    selected = [scenario for scenario in scenarios if _has_por_scenario(scenario)]
    if not selected:
        return []
    pending = []
    for ordinal, scenario in enumerate(selected, 1):
        external_names = " + ".join(str(item.get("scenario")) for item in scenario.get("external scenarios", []))
        label = f"PoR supplier linking {ordinal}/{len(selected)} — {scenario.get('year')} {external_names}"
        def progress(phase):
            if verbose:
                print(f"{label}: {phase}", flush=True)
        if "external" not in scenario.get("applied functions", []):
            raise SupplierLinkingError("PoR post-linking must run after the external scenario update.")
        if _has_inventory_store(scenario):
            working, audit, summary = _stage_store_linking(builder, scenario, policy, report_dir, progress=progress)
            pending.append((scenario, working, audit, summary))
            progress(f"complete ({summary['linking_seconds']:.1f} s; {summary['changed_exchanges']} changed exchanges)")
            continue
        progress("loading/linking legacy scenario cache")
        if scenario.get("database") is None and not scenario.get("database filepath"):
            raise SupplierLinkingError("Updated scenario data/cache is missing; refusing to use the unmodified reference database.")
        if load_database_func is None or dump_database_func is None:
            from premise.utils import load_database, dump_database
            load_database_func = load_database_func or load_database
            dump_database_func = dump_database_func or dump_database
        working = dict(scenario)
        if working.get("database") is None:
            working = load_database_func(scenario=working, original_database=[],
                                         delete=False, load_metadata=False, warning=False)
        database, audit, summary = link_por_database(working["database"], policy, model=str(scenario.get("model", "remind")))
        working["database"] = database
        # Keep original caches/pointers intact until every scenario and report succeeds.
        dump_database_func(working)
        if working.get("database") is not None or not working.get("database filepath"):
            raise SupplierLinkingError("premise dump_database did not persist the modified scenario as expected.")
        summary["inventory_storage"] = "legacy_cache"
        pending.append((scenario, working, audit, summary))
    policy.verify_sources()
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    reports = []
    for ordinal, (scenario, working, audit, summary) in enumerate(pending):
        label = f"{stage}_{scenario.get('model')}_{scenario.get('year')}_{ordinal}_{timestamp}"
        label = re.sub(r"[^A-Za-z0-9_.-]+", "-", label)
        csv_path = report_dir / f"{label}.csv"
        fields = ["model", "pathway", "year", "external_scenarios", "activity", "reference_product",
                  "activity_location", "exchange_index", "old_name", "old_product", "old_location",
                  "new_name", "new_product", "new_location", "amount_before", "amount_after", "unit", "reason", "status"]
        external_labels = [entry.get("scenario") for entry in scenario.get("external scenarios", [])]
        with csv_path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for row in audit:
                writer.writerow({"model": scenario.get("model"), "pathway": scenario.get("pathway"),
                                 "year": scenario.get("year"), "external_scenarios": " + ".join(map(str, external_labels)), **row})
        metadata = {"model": scenario.get("model"), "pathway": scenario.get("pathway"), "year": scenario.get("year"),
                    "external_scenarios": external_labels, "stage": stage, **summary,
                    "source_sha256": policy.source_hashes, "change_report": csv_path.name,
                    "change_report_path": str(csv_path.resolve()), "policy_token": _register_policy(policy),
                    "config_path": str(policy.config_path) if policy.config_path else None,
                    "inventory_path": str(policy.inventory_path) if policy.inventory_path else None,
                    "compatibility_rules_path": str(policy.compatibility_rules.path) if policy.compatibility_rules is not None else None,
                    "policy": "Explicit PoR replacement and inventory compatibility rules; protected imports/fixed power; quantities and uncertainty retained"}
        csv_path.with_suffix(".json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        working["por supplier linking"] = metadata
        reports.append(metadata)
    for original, working, _, _ in pending:
        original.clear()
        original.update(working)
    if verbose:
        print(f"PoR supplier linking: {len(reports)} scenario(s), {sum(report['changed_exchanges'] for report in reports)} changed exchange(s). Reports: {report_dir}")
    return reports


def attach_por_supplier_linking(builder, *, policy=None, report_dir=ROOT / "export/por_supplier_linking",
                                stage="export", install_support=True, verbose=True,
                                load_database_func=None, dump_database_func=None):
    """Wrap one builder's update so both public export paths run the same post-step."""
    if getattr(builder, "_por_supplier_linking_attached", False):
        return False
    if install_support:
        configure_por_export_support(verbose=verbose)
    policy = policy or PorLinkingPolicy.from_files()
    original_update = builder.update

    @wraps(original_update)
    def update_with_por_links(*args, **kwargs):
        policy.verify_sources()
        result = original_update(*args, **kwargs)
        builder.por_supplier_linking_reports = apply_por_supplier_linking(
            builder, policy=policy, report_dir=report_dir, stage=stage,
            load_database_func=load_database_func, dump_database_func=dump_database_func,
            verbose=verbose,
        )
        return result

    builder.update = update_with_por_links
    builder._por_supplier_linking_attached = True
    if verbose:
        print(f"Attached PoR supplier linking ({stage}); processing starts after premise.update().", flush=True)
    return True


def attach_por_pathways_export(package, **kwargs):
    """Guard this exact package instance and preflight mappings before matrices.

    Public/stale class aliases can differ from premise.pathways' imported class.
    Bind the mapper on the concrete instance, not only a global library class.
    """
    from premise_compat import (PATHWAYS_MAPPING_GUARD_VERSION,
                               pathways_activity_mapping_views, write_pathways_activity_mapping)
    if getattr(package, "_por_pathways_export_guard_version", None) == PATHWAYS_MAPPING_GUARD_VERSION:
        return False
    if not hasattr(package, "datapackage") or not callable(getattr(package, "_add_variables_mapping", None)):
        raise SupplierLinkingError("Expected a premise PathwaysDataPackage instance with its internal builder.")
    verbose = kwargs.get("verbose", True)
    kwargs.setdefault("stage", "pathways")
    attach_por_supplier_linking(package.datapackage, **kwargs)
    original_mapper = package._add_variables_mapping
    # Avoid nesting an old class adapter; binding the original to this instance
    # also handles an instance-level override which class patching cannot reach.
    while getattr(original_mapper, "__por_pathways_mapping_fix__", False) and hasattr(original_mapper, "__wrapped__"):
        function = original_mapper.__wrapped__
        original_mapper = function.__get__(package, type(package))
    original_update = package.datapackage.update
    original_create = package.create_datapackage
    package._por_pathways_mapping_snapshot = None
    package._por_pathways_mapping_scenarios = None

    @wraps(original_update)
    def update_and_preflight(*args, **options):
        package._por_pathways_mapping_snapshot = None
        package._por_pathways_mapping_scenarios = None
        result = original_update(*args, **options)
        package._por_pathways_mapping_snapshot = pathways_activity_mapping_views(package.datapackage.scenarios)
        package._por_pathways_mapping_scenarios = tuple(id(scenario) for scenario in package.datapackage.scenarios)
        if verbose:
            views, omitted = package._por_pathways_mapping_snapshot
            print(f"Pathways mapping preflight passed for {len(views)} scenario(s); {omitted} diagnostic entries excluded.", flush=True)
        return result

    @wraps(original_mapper)
    def guarded_mapper(*args, **options):
        if package._por_pathways_mapping_snapshot is not None and package._por_pathways_mapping_scenarios != tuple(
                id(scenario) for scenario in package.datapackage.scenarios):
            raise SupplierLinkingError("Pathways scenario order changed after mapping preflight; recreate the package builder.")
        return write_pathways_activity_mapping(package, original_mapper, *args,
                                               snapshot=package._por_pathways_mapping_snapshot,
                                               verbose=verbose, **options)
    guarded_mapper.__por_pathways_mapping_guard_version__ = PATHWAYS_MAPPING_GUARD_VERSION

    @wraps(original_create)
    def guarded_create(*args, **options):
        if getattr(package._add_variables_mapping, "__por_pathways_mapping_guard_version__", None) != PATHWAYS_MAPPING_GUARD_VERSION:
            raise SupplierLinkingError("The concrete package mapping guard was replaced; recreate the package builder before exporting.")
        return original_create(*args, **options)

    package.datapackage.update = update_and_preflight
    package._add_variables_mapping = guarded_mapper
    package.create_datapackage = guarded_create
    package._por_pathways_export_guard_version = PATHWAYS_MAPPING_GUARD_VERSION
    if verbose:
        print("Attached concrete Pathways package guard; mapping preflight runs before matrix export.", flush=True)
    return True
