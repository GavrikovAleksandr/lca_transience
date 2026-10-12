"""Exact, inventory-scoped supplier compatibility for additional inventories.

Rules belong to the repository; installed premise files are never modified.
Supplier substitutions change only the supplier identity, and record a frozen
decision on the consumer so protected profiles can replay the same choice.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from functools import wraps
import hashlib
import json
import math
from numbers import Real
from pathlib import Path
import re
from urllib.parse import urlsplit

from supplier_resolution import (
    SupplierLinkingError, dataset_key, exchange_key, substitute_supplier,
)

ROOT = Path(__file__).resolve().parent
DEFAULT_RULES_PATH = ROOT / "configuration_file/inventory_supplier_compatibility.yaml"
METADATA_KEY = "inventory supplier compatibility"
SCHEMA_VERSION = 1
IDENTITY_FIELDS = ("name", "reference product", "unit", "location")


class InventoryCompatibilityError(SupplierLinkingError):
    """An exact compatibility decision cannot be supported by its inputs."""


def _fail(message):
    raise InventoryCompatibilityError(message)


def _fields(value, allowed, required, label):
    if not isinstance(value, dict):
        _fail(f"{label} must be a mapping")
    if set(value) - set(allowed) or set(required) - set(value):
        _fail(f"Invalid fields in {label}: {sorted(value)}")


def _text(value, label, *, padded=False):
    if not isinstance(value, str) or not value.strip() or (not padded and value != value.strip()):
        _fail(f"{label} must be a nonempty, unpadded string")
    return value


def _identity(value, label, *, reason=False):
    _fields(value, (*IDENTITY_FIELDS, "reason") if reason else IDENTITY_FIELDS,
            IDENTITY_FIELDS, label)
    for field in IDENTITY_FIELDS:
        _text(value[field], f"{label}.{field}")
    if "reason" in value:
        _text(value["reason"], f"{label}.reason")
    return dataset_key(value)


def _list(value, label):
    if not isinstance(value, list) or not value:
        _fail(f"{label} must be a nonempty list")
    return value


def _consumer_matches(selector, consumer):
    if selector.get("location") != consumer.get("location"):
        return False
    if "name" in selector:
        return dataset_key(selector) == dataset_key(consumer)
    name = consumer.get("name", "")
    return (isinstance(name, str)
            and all(consumer.get(field) == selector[field]
                    for field in ("unit", "reference product") if field in selector)
            and ("name_prefix" not in selector or name.startswith(selector["name_prefix"]))
            and ("name_contains" not in selector or selector["name_contains"] in name))


def _consumer_overlap(left, right):
    if left["location"] != right["location"]:
        return False
    if any(field in left and field in right and left[field] != right[field]
           for field in ("unit", "reference product")):
        return False
    if "name" in left:
        return _consumer_matches(right, left)
    if "name" in right:
        return _consumer_matches(left, right)
    # Prefixes may be proven disjoint; substring predicates alone cannot be.
    a, b = left.get("name_prefix", ""), right.get("name_prefix", "")
    return a.startswith(b) or b.startswith(a)


def _exchange_matches(rule, exchange):
    source = rule["exchange"]
    key = exchange_key(exchange)
    return (exchange.get("type") == "technosphere" and key[0] == source["name"]
            and key[1] in source["reference_products"]
            and key[2:] == (source["unit"], source["location"]))


def _source_matches(rule, key):
    return _exchange_matches(rule, dict(type="technosphere", name=key[0],
                                      product=key[1], unit=key[2], location=key[3]))


@dataclass(frozen=True)
class CompatibilityRules:
    rules: tuple
    path: Path
    sha256: str
    source_paths: tuple
    checksums: dict
    _fallback_documents: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def rules_sha256(self):
        return self.sha256

    def matching(self, consumer, exchange, inventory):
        matches = [rule for rule in self.rules if inventory in rule["inventories"]
                   and any(_consumer_matches(selector, consumer) for selector in rule["consumers"])
                   and _exchange_matches(rule, exchange)]
        if len(matches) > 1:
            _fail(f"Overlapping compatibility rules for {dataset_key(consumer)} / {exchange_key(exchange)}")
        return matches[0] if matches else None

    def by_id(self, identifier):
        matches = [rule for rule in self.rules if rule["id"] == identifier]
        if len(matches) != 1:
            _fail(f"Unknown compatibility rule: {identifier!r}")
        return matches[0]


def load_compatibility_rules(path=DEFAULT_RULES_PATH):
    """Read and strictly validate rules afresh, including reproducibility hashes.

    Missing fallback files are only an error when a fallback is actually needed.
    Existing fallback snapshots are always included in ``source_paths`` and
    ``checksums``, even when a native candidate makes their use unnecessary.
    """
    import yaml
    class UniqueKeyLoader(yaml.SafeLoader):
        pass

    def unique_mapping(loader, node, deep=False):
        loader.flatten_mapping(node)
        mapping = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in mapping:
                raise yaml.constructor.ConstructorError(
                    "compatibility mapping", node.start_mark,
                    f"non-string or duplicate mapping key: {key!r}", key_node.start_mark)
            mapping[key] = loader.construct_object(value_node, deep=deep)
        return mapping

    UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)
    path = Path(path).resolve()
    if not path.is_file():
        _fail(f"Missing inventory supplier compatibility rules: {path}")
    raw = path.read_bytes()
    try:
        data = yaml.load(raw.decode("utf-8-sig"), Loader=UniqueKeyLoader)
    except (UnicodeError, yaml.YAMLError) as error:
        _fail(f"Invalid compatibility YAML {path}: {error}")
    _fields(data, ("schema_version", "rules"), ("schema_version", "rules"), "compatibility rules")
    if type(data["schema_version"]) is not int or data["schema_version"] != SCHEMA_VERSION:
        _fail(f"Unsupported compatibility schema: {data['schema_version']!r}")
    if not isinstance(data["rules"], list):
        _fail("compatibility rules.rules must be a list")
    identifiers = set()
    paths = [path]
    for rule in data["rules"]:
        _fields(rule, ("id", "inventories", "consumers", "exchange", "candidates", "fallback", "reason"),
                ("id", "inventories", "consumers", "exchange", "candidates", "reason"), "rule")
        identifier = _text(rule["id"], "rule.id")
        if identifier in identifiers:
            _fail(f"Duplicate compatibility rule id: {identifier}")
        identifiers.add(identifier)
        _text(rule["reason"], f"{identifier}.reason")
        for inventory in _list(rule["inventories"], f"{identifier}.inventories"):
            _text(inventory, f"{identifier}.inventory")
            if Path(inventory).name != inventory or "/" in inventory or "\\" in inventory:
                _fail(f"Inventory scope must be a basename: {inventory}")
        if len(set(rule["inventories"])) != len(rule["inventories"]):
            _fail(f"Duplicate inventory scope in {identifier}")
        for consumer in _list(rule["consumers"], f"{identifier}.consumers"):
            if not isinstance(consumer, dict):
                _fail(f"{identifier}.consumer must be a mapping")
            if "name" in consumer:
                _identity(consumer, f"{identifier}.consumer")
            else:
                _fields(consumer, ("name_prefix", "name_contains", "location", "unit", "reference product"), ("location",),
                        f"{identifier}.consumer")
                if not ({"name_prefix", "name_contains"} & set(consumer)):
                    _fail(f"{identifier}.consumer requires a name predicate")
                for field, value in consumer.items():
                    _text(value, f"{identifier}.consumer.{field}", padded=field in ("name_prefix", "name_contains"))
        source = rule["exchange"]
        _fields(source, ("name", "reference_products", "unit", "location"),
                ("name", "reference_products", "unit", "location"), f"{identifier}.exchange")
        for field in ("name", "unit", "location"):
            _text(source[field], f"{identifier}.exchange.{field}")
        products = _list(source["reference_products"], f"{identifier}.exchange.reference_products")
        for product in products:
            _text(product, f"{identifier}.exchange.reference_product")
        if len(set(products)) != len(products):
            _fail(f"Duplicate source product in {identifier}")
        keys = []
        for candidate in _list(rule["candidates"], f"{identifier}.candidates"):
            key = _identity(candidate, f"{identifier}.candidate", reason=True)
            if key[2:] != (source["unit"], source["location"]):
                _fail(f"{identifier}: candidate changes unit or geography without an explicit supported policy")
            keys.append(key)
        if len(set(keys)) != len(keys):
            _fail(f"Duplicate candidate identity in {identifier}")
        fallback = rule.get("fallback")
        if fallback is not None:
            _fields(fallback, ("path", "provider"), ("path", "provider"), f"{identifier}.fallback")
            fallback_path = Path(_text(fallback["path"], f"{identifier}.fallback.path"))
            if fallback_path.is_absolute() or fallback_path.drive:
                _fail(f"{identifier}: fallback path must be relative to the rules file")
            key = _identity(fallback["provider"], f"{identifier}.fallback.provider")
            if key[2:] != (source["unit"], source["location"]):
                _fail(f"{identifier}: fallback changes unit or geography")
            resolved = (path.parent / fallback_path).resolve()
            if resolved.is_file() and resolved not in paths:
                paths.append(resolved)
    for position, left in enumerate(data["rules"]):
        for right in data["rules"][position + 1:]:
            a, b = left["exchange"], right["exchange"]
            if (set(left["inventories"]) & set(right["inventories"])
                    and (a["name"], a["unit"], a["location"]) == (b["name"], b["unit"], b["location"])
                    and set(a["reference_products"]) & set(b["reference_products"])
                    and any(_consumer_overlap(x, y) for x in left["consumers"] for y in right["consumers"])):
                _fail(f"Overlapping compatibility rules: {left['id']} and {right['id']}")
    checksums = {str(source): hashlib.sha256(source.read_bytes()).hexdigest() for source in paths[1:]}
    checksums[str(path)] = hashlib.sha256(raw).hexdigest()
    return CompatibilityRules(tuple(deepcopy(data["rules"])), path,
                              hashlib.sha256(raw).hexdigest(), tuple(paths), checksums)


def _read_fallback(rule, rules):
    fallback = rule.get("fallback")
    if fallback is None:
        _fail(f"No exact supplier candidate or declared fallback for rule {rule['id']}")
    path = (rules.path.parent / fallback["path"]).resolve()
    if not path.is_file():
        _fail(f"Missing required fallback for {rule['id']}: {path}")
    raw = path.read_bytes()
    checksum = hashlib.sha256(raw).hexdigest()
    if rules.checksums.get(str(path)) != checksum:
        _fail(f"Fallback changed after compatibility rules were loaded: {path}")
    cache_key = (str(path), dataset_key(fallback["provider"]))
    cached = rules._fallback_documents.get(cache_key)
    if cached is not None:
        return cached
    try:
        document = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as error:
        _fail(f"Invalid fallback JSON {path}: {error}")
    _fields(document, ("schema_version", "ecoinvent_version", "system_model", "datasets", "provenance", "provider"),
            ("schema_version", "ecoinvent_version", "system_model", "datasets", "provenance"), f"fallback {path}")
    if "provider" in document:
        recorded_provider = _identity(document["provider"], f"fallback {path}.provider")
        if recorded_provider != dataset_key(fallback["provider"]):
            _fail(f"Fallback document provider differs from its rule: {path}")
    if type(document["schema_version"]) is not int or document["schema_version"] != SCHEMA_VERSION:
        _fail(f"Unsupported fallback schema: {path}")
    if not isinstance(document["ecoinvent_version"], str) or not re.fullmatch(r"3\.\d+(?:\.\d+)?", document["ecoinvent_version"]):
        _fail(f"Fallback must declare a valid ecoinvent_version: {path}")
    _normalize_system_model(document["system_model"], f"fallback {path}.system_model")
    provenance = document["provenance"]
    if not isinstance(provenance, dict):
        _fail(f"Fallback provenance must be a mapping: {path}")
    for field in ("source_description", "source_path", "source_sha256"):
        _text(provenance.get(field), f"fallback provenance.{field}")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", provenance["source_sha256"]):
        _fail(f"Invalid fallback source SHA256: {path}")
    if "sources" in provenance and not isinstance(provenance["sources"], list):
        _fail(f"Fallback provenance.sources must be a list: {path}")
    if "assumptions" in provenance and not isinstance(provenance["assumptions"], dict):
        _fail(f"Fallback provenance.assumptions must be a mapping: {path}")
    keys = set()
    for supplier in _list(document["datasets"], f"fallback datasets {path}"):
        if not isinstance(supplier, dict):
            _fail(f"Fallback dataset must be a mapping: {path}")
        key = dataset_key(supplier)
        for value, field in zip(key, IDENTITY_FIELDS):
            _text(value, f"fallback dataset.{field}")
        if key in keys:
            _fail(f"Duplicate fallback supplier identity: {key}")
        keys.add(key)
        exchanges = _list(supplier.get("exchanges"), f"fallback exchanges {key}")
        production = []
        for exchange in exchanges:
            if not isinstance(exchange, dict) or exchange.get("type") not in ("production", "technosphere", "biosphere"):
                _fail(f"Invalid fallback exchange in {key}")
            amount = exchange.get("amount")
            if isinstance(amount, bool) or not isinstance(amount, Real) or not math.isfinite(float(amount)):
                _fail(f"Invalid fallback exchange amount in {key}")
            _text(exchange.get("unit"), f"fallback exchange unit in {key}")
            if exchange["type"] in ("production", "technosphere"):
                for value, field in zip(exchange_key(exchange), IDENTITY_FIELDS):
                    _text(value, f"fallback exchange.{field} in {key}")
            if exchange["type"] == "production":
                production.append(exchange)
                if exchange_key(exchange) != key or amount <= 0:
                    _fail(f"Fallback production does not match its supplier: {key}")
            elif exchange["type"] == "biosphere":
                _text(exchange.get("name"), f"fallback biosphere name in {key}")
                if not isinstance(exchange.get("categories"), (list, tuple)) or not exchange["categories"]:
                    _fail(f"Fallback biosphere requires categories in {key}")
        if len(production) != 1:
            _fail(f"Fallback requires one production exchange: {key}")
        if not any(exchange["type"] in ("technosphere", "biosphere") and exchange["amount"] != 0
                   for exchange in exchanges):
            _fail(f"Fallback dataset has no physical inventory burdens: {key}")
    provider = dataset_key(fallback["provider"])
    if provider not in keys:
        _fail(f"Declared fallback provider is missing from {path}: {provider}")
    result = (document, path, checksum)
    rules._fallback_documents[cache_key] = result
    return result


def _normalize_system_model(value, label):
    _text(value, label)
    aliases = {"cutoff": "cutoff", "cut-off": "cutoff", "cut_off": "cutoff", "cut off": "cutoff",
               "consequential": "consequential"}
    normalized = aliases.get(value.lower())
    if normalized is None:
        _fail(f"Unsupported system model in {label}: {value!r}")
    return normalized


def _validate_fallback_context(importer, document, path):
    """A prepared snapshot is valid only for its declared version and model."""
    version_in = str(getattr(importer, "version_in", None))
    version_out = str(getattr(importer, "version_out", None))
    version = document["ecoinvent_version"]
    if version_in != version or version_out != version:
        _fail(f"Unsupported fallback migration for {path}: snapshot ecoinvent {version}, "
              f"importer source {version_in}, target {version_out}. "
              "A fallback snapshot must match both importer versions.")
    source_model = _normalize_system_model(document["system_model"], f"fallback {path}.system_model")
    target_model = _normalize_system_model(getattr(importer, "system_model", None), "importer.system_model")
    if source_model != target_model:
        _fail(f"Fallback system model mismatch for {path}: snapshot {source_model}, importer {target_model}. "
              "A fallback snapshot cannot be converted between system models.")


def _unique(index, key):
    """Read only one exact key; supports lazy Mapping-backed indexes."""
    matches = index.get(tuple(key), ())
    if matches is None:
        return None
    if isinstance(matches, Mapping):
        matches = (matches,)
    else:
        matches = tuple(matches)
    if len(matches) > 1:
        _fail(f"Ambiguous exact compatibility supplier ({len(matches)} activities): {tuple(key)}")
    if matches and dataset_key(matches[0]) != tuple(key):
        _fail(f"Supplier index returned a different identity for {tuple(key)}")
    return matches[0] if matches else None


class InventoryCompatibilityResolver:
    def __init__(self, index, rules, *, inventory="lci-itom_por.xlsx"):
        self.index = index
        self.rules = rules
        self.inventory = inventory

    def resolve(self, consumer, exchange, *, exchange_index=None, allow_fallback=True):
        rule = self.rules.matching(consumer, exchange, self.inventory)
        if rule is None:
            return None
        source = exchange_key(exchange)
        for candidate in rule["candidates"]:
            target = dataset_key(candidate)
            if _unique(self.index, target) is not None:
                return self._decision(rule, source, target, exchange_index,
                                      candidate.get("reason", rule["reason"]))
        if not allow_fallback:
            _fail(f"No native exact supplier for rule {rule['id']}: {source}")
        document, path, checksum = _read_fallback(rule, self.rules)
        target = dataset_key(rule["fallback"]["provider"])
        _unique(self.index, target)  # Still reject ambiguity when already inserted.
        decision = self._decision(rule, source, target, exchange_index, rule["reason"], checksum)
        decision["fallback_path"] = str(path)
        return decision

    @staticmethod
    def _decision(rule, source, target, position, reason, checksum=None):
        return {"rule": rule["id"], "exchange_index": position,
                "source_key": list(source), "target_key": list(target), "reason": reason,
                "fallback_sha256": checksum, "status": "changed" if source != target else "unchanged"}

    def frozen(self, consumer, receipt):
        if (not isinstance(receipt, dict) or type(receipt.get("schema_version")) is not int
                or receipt.get("schema_version") != SCHEMA_VERSION):
            _fail(f"Invalid compatibility receipt on {dataset_key(consumer)}")
        if receipt.get("rules_sha256") != self.rules.sha256:
            _fail(f"Compatibility rules changed for frozen consumer {dataset_key(consumer)}")
        inventory = receipt.get("inventory", self.inventory)
        if inventory != self.inventory:
            _fail(f"Frozen compatibility inventory scope differs: {inventory!r}")
        identity = receipt.get("consumer_key", dataset_key(consumer))
        if not isinstance(identity, (list, tuple)) or len(identity) != 4:
            _fail(f"Invalid frozen compatibility consumer identity: {identity}")
        if tuple(identity) != dataset_key(consumer):
            _fail(f"Frozen compatibility receipt was transplanted onto a different consumer: {dataset_key(consumer)}")
        original_consumer = dict(zip(IDENTITY_FIELDS, identity))
        decisions = receipt.get("decisions")
        if not isinstance(decisions, list) or not decisions:
            _fail("Frozen compatibility receipt requires nonempty decisions")
        positions = set()
        profile_positions = set()
        for decision in decisions:
            if not isinstance(decision, dict):
                _fail("Frozen compatibility decision must be a mapping")
            rule = self.rules.by_id(decision.get("rule"))
            source, target = decision.get("source_key"), decision.get("target_key")
            position = decision.get("exchange_index")
            if (not isinstance(source, (list, tuple)) or len(source) != 4
                    or not isinstance(target, (list, tuple)) or len(target) != 4
                    or type(position) is not int or position < 0 or position in positions):
                _fail(f"Invalid or duplicate frozen row decision in rule {rule['id']}")
            positions.add(position)
            for key in (source, target):
                for value, field in zip(key, IDENTITY_FIELDS):
                    _text(value, f"frozen decision.{field}")
            _text(decision.get("reason"), "frozen decision.reason", padded=True)
            status = "changed" if tuple(source) != tuple(target) else "unchanged"
            if decision.get("status") != status:
                _fail(f"Invalid frozen compatibility status in rule {rule['id']}")
            if "unit" in decision and decision["unit"] != source[2]:
                _fail(f"Frozen compatibility unit changed in rule {rule['id']}")
            if "amount_before" in decision or "amount_after" in decision:
                amounts = (decision.get("amount_before"), decision.get("amount_after"))
                if (any(isinstance(amount, bool) or not isinstance(amount, Real)
                        or not math.isfinite(float(amount)) for amount in amounts)
                        or amounts[0] != amounts[1]):
                    _fail(f"Frozen compatibility quantities changed in rule {rule['id']}")
            if "profile_index" in decision and (type(decision["profile_index"]) is not int
                                                  or decision["profile_index"] < 0
                                                  or decision["profile_index"] in profile_positions):
                _fail(f"Invalid frozen profile row in rule {rule['id']}")
            if "profile_index" in decision:
                profile_positions.add(decision["profile_index"])
            if (inventory not in rule["inventories"]
                    or not any(_consumer_matches(selector, original_consumer) for selector in rule["consumers"])
                    or not _source_matches(rule, source)):
                _fail(f"Frozen decision falls outside rule {rule['id']}")
            candidates = {dataset_key(candidate) for candidate in rule["candidates"]}
            fallback = rule.get("fallback")
            fallback_key = dataset_key(fallback["provider"]) if fallback else None
            checksum = decision.get("fallback_sha256")
            if checksum is not None:
                _, path, expected = _read_fallback(rule, self.rules)
                if tuple(target) != fallback_key or checksum != expected:
                    _fail(f"Frozen fallback snapshot changed for rule {rule['id']}")
                if "fallback_path" in decision and decision["fallback_path"] != str(path):
                    _fail(f"Frozen fallback path differs from its rule {rule['id']}")
            elif tuple(target) not in candidates:
                _fail(f"Frozen target is not a declared candidate in rule {rule['id']}")
            elif "fallback_path" in decision:
                _fail(f"Native frozen decision has an undeclared fallback path in rule {rule['id']}")
            if _unique(self.index, target) is None:
                _fail(f"Frozen compatibility supplier is missing: {tuple(target)}")
        return decisions


def _substitute(exchange, target):
    if exchange_key(exchange) != tuple(target):
        return substitute_supplier(exchange, tuple(target))
    result = deepcopy(exchange)
    result["product"] = target[1]
    if "reference product" in result:
        result["reference product"] = target[1]
    return result


def effective_profile(dataset, source_rows, index, rules, *, inventory="lci-itom_por.xlsx"):
    """Return copied source rows with the same resolver used during importing.

    Fallback resolution here is read-only. A receipt freezes the exact choice
    per consumer and per source row; it is never shared between scenario years.
    """
    if rules is None:
        return deepcopy(source_rows)
    if not isinstance(rules, CompatibilityRules):
        rules = load_compatibility_rules(rules)
    resolver = InventoryCompatibilityResolver(index, rules, inventory=inventory)
    rows = deepcopy(source_rows)
    receipt = dataset.get(METADATA_KEY)
    if receipt is not None:
        decisions = resolver.frozen(dataset, receipt)
        for decision in decisions:
            position = decision.get("profile_index", decision["exchange_index"])
            source = tuple(decision["source_key"])
            target = tuple(decision["target_key"])
            # Source profiles often omit production/biosphere rows, so use the
            # source identity as a checked alternate to the original row index.
            if position < len(rows) and exchange_key(rows[position]) in (source, target):
                matches = [position]
            else:
                matches = [i for i, row in enumerate(rows) if exchange_key(row) in (source, target)]
            if not matches:
                _fail(f"Frozen compatibility source row is missing: {source}")
            if len(matches) > 1:
                _fail(f"Frozen compatibility source row is ambiguous: {source}")
            rows[matches[0]] = _substitute(rows[matches[0]], target)
        return rows
    for position, exchange in enumerate(rows):
        decision = resolver.resolve(dataset, exchange, exchange_index=position)
        if decision is not None:
            if _unique(index, decision["target_key"]) is None:
                _fail(f"Compatibility profile supplier is missing: {tuple(decision['target_key'])}. "
                      "Post-link profile resolution cannot insert fallback datasets.")
            rows[position] = _substitute(exchange, decision["target_key"])
    return rows


def _needed_names(rules):
    names = set()
    for rule in rules.rules:
        names.add(rule["exchange"]["name"])
        names.update(candidate["name"] for candidate in rule["candidates"])
        fallback = rule.get("fallback")
        if fallback:
            names.add(fallback["provider"]["name"])
            path = (rules.path.parent / fallback["path"]).resolve()
            if path.is_file():
                # Read dependency names before the single background scan.
                # This does not accept the fallback: full validation is deferred
                # until a native candidate is unavailable and it is needed.
                try:
                    document = json.loads(path.read_bytes().decode("utf-8-sig"))
                except (UnicodeError, ValueError):
                    continue
                suppliers = document.get("datasets", []) if isinstance(document, dict) else []
                if not isinstance(suppliers, list):
                    continue
                for supplier in suppliers:
                    if not isinstance(supplier, dict):
                        continue
                    if isinstance(supplier.get("name"), str):
                        names.add(supplier["name"])
                    exchanges = supplier.get("exchanges", [])
                    if not isinstance(exchanges, list):
                        continue
                    names.update(exchange["name"] for exchange in exchanges
                                 if isinstance(exchange, dict) and exchange.get("type") == "technosphere"
                                 and isinstance(exchange.get("name"), str))
    return names


def build_supplier_index(database, rules, *, imports=(), needed_names=None):
    """Index only configured names without copying or materializing a database."""
    names = _needed_names(rules) if needed_names is None else needed_names
    index = defaultdict(list)
    for records in (database, imports):
        for supplier in records:
            if supplier.get("name") in names:
                index[dataset_key(supplier)].append(supplier)
    return index


def _inventory_name(importer):
    native_name = getattr(importer, "inventory_filename", None)
    if isinstance(native_name, str):
        return native_name
    path = str(getattr(importer, "path", "")).replace("\\", "/")
    if "://" in path:
        path = urlsplit(path).path
    return path.rsplit("/", 1)[-1]


def _clear_product_cache(importer):
    method = getattr(importer, "correct_product_field", None)
    clear = getattr(method, "cache_clear", None)
    if clear is not None:
        clear()
    # New importers rebuild this index in their native method. Use their own
    # builder when it exists, rather than assuming the index's internal shape.
    builder = getattr(importer, "_build_import_product_index", None)
    if callable(builder):
        importer.import_product_index = builder()


def _prepare_compatibility(importer, rules):
    inventory = _inventory_name(importer)
    if not any(inventory in rule["inventories"] for rule in rules.rules):
        return None
    names = _needed_names(rules)
    background = build_supplier_index(importer.database, rules, needed_names=names)
    index = defaultdict(list, {key: list(values) for key, values in background.items()})
    for supplier in importer.import_db.data:
        if supplier.get("name") in names:
            index[dataset_key(supplier)].append(supplier)
    resolver = InventoryCompatibilityResolver(index, rules, inventory=inventory)
    inserted = []
    # A stale or transplanted receipt must fail before native migration or any
    # fallback insertion can change the parsed inventory records.
    for consumer in importer.import_db.data:
        if METADATA_KEY in consumer:
            decisions = resolver.frozen(consumer, consumer[METADATA_KEY])
            for decision in decisions:
                if decision.get("fallback_sha256") is not None:
                    document, path, _ = _read_fallback(rules.by_id(decision["rule"]), rules)
                    _validate_fallback_context(importer, document, path)
    for consumer in tuple(importer.import_db.data):
        for position, exchange in enumerate(consumer.get("exchanges", [])):
            decision = resolver.resolve(consumer, exchange, exchange_index=position)
            if decision is None or decision["fallback_sha256"] is None:
                continue
            rule = rules.by_id(decision["rule"])
            document, path, checksum = _read_fallback(rule, rules)
            _validate_fallback_context(importer, document, path)
            staged = []
            closure = defaultdict(list, {key: list(values) for key, values in index.items()})
            for supplier in document["datasets"]:
                key = dataset_key(supplier)
                if _unique(closure, key) is None:
                    copied = deepcopy(supplier)
                    copied["inventory supplier compatibility fallback"] = {
                        "schema_version": SCHEMA_VERSION, "path": str(path), "sha256": checksum,
                        "provenance": deepcopy(document["provenance"]),
                    }
                    closure[key].append(copied)
                    staged.append(copied)
            for supplier in document["datasets"]:
                for row in supplier["exchanges"]:
                    if row["type"] == "technosphere" and _unique(closure, exchange_key(row)) is None:
                        _fail(f"Fallback dependency is missing: {exchange_key(row)} in {dataset_key(supplier)}")
            for copied in staged:
                key = dataset_key(copied)
                importer.import_db.data.append(copied)
                index[key].append(copied)
                inserted.append(key)
    if inserted:
        _clear_product_cache(importer)
    return {"rules": rules, "background": background, "names": names,
            "inventory": inventory, "inserted": inserted}


def _apply_compatibility(importer, context):
    rules = context["rules"]
    # Detect files modified between preparation and the native exact lookup.
    for path, checksum in rules.checksums.items():
        if not Path(path).is_file() or hashlib.sha256(Path(path).read_bytes()).hexdigest() != checksum:
            _fail(f"Compatibility source changed during import: {path}")
    index = defaultdict(list, {key: list(values) for key, values in context["background"].items()})
    for supplier in importer.import_db.data:
        if supplier.get("name") in context["names"]:
            index[dataset_key(supplier)].append(supplier)
    resolver = InventoryCompatibilityResolver(index, rules, inventory=context["inventory"])
    audit = []
    for consumer in importer.import_db.data:
        receipt = consumer.get(METADATA_KEY)
        if receipt is not None:
            frozen = resolver.frozen(consumer, receipt)
            for decision in frozen:
                position = decision["exchange_index"]
                rows = consumer.get("exchanges", [])
                if position >= len(rows) or exchange_key(rows[position]) not in (
                        tuple(decision["source_key"]), tuple(decision["target_key"])):
                    _fail(f"Frozen compatibility row changed during repeated import: {decision['source_key']}")
                exchange = rows[position]
                old = exchange_key(exchange)
                target = tuple(decision["target_key"])
                rows[position] = _substitute(exchange, target)
                audit.append({**deepcopy(decision), "source_key": list(old),
                              "status": "changed" if old != target else "unchanged",
                              "consumer_key": list(dataset_key(consumer)),
                              "amount_before": exchange.get("amount"), "amount_after": exchange.get("amount"),
                              "unit": exchange.get("unit"), "inventory": context["inventory"],
                              "rules_sha256": rules.sha256})
            continue
        decisions = []
        profile_position = -1
        for position, exchange in enumerate(consumer.get("exchanges", [])):
            if exchange.get("type") == "technosphere":
                profile_position += 1
            decision = resolver.resolve(consumer, exchange, exchange_index=position)
            if decision is None:
                continue
            if _unique(index, decision["target_key"]) is None:
                _fail(f"Compatibility target missing after native migration: {tuple(decision['target_key'])}")
            for value, field_name in zip(dataset_key(consumer), IDENTITY_FIELDS):
                _text(value, f"compatibility consumer.{field_name}")
            decision["profile_index"] = profile_position
            decision.update(amount_before=exchange.get("amount"), amount_after=exchange.get("amount"),
                            unit=exchange.get("unit"))
            consumer["exchanges"][position] = _substitute(exchange, decision["target_key"])
            decisions.append(decision)
            audit.append({**deepcopy(decision), "consumer_key": list(dataset_key(consumer)),
                          "amount_before": exchange.get("amount"), "amount_after": exchange.get("amount"),
                          "unit": exchange.get("unit"), "inventory": context["inventory"],
                          "rules_sha256": rules.sha256})
        if decisions:
            consumer[METADATA_KEY] = {"schema_version": SCHEMA_VERSION,
                                      "rules_sha256": rules.sha256,
                                      "inventory": context["inventory"],
                                      "consumer_key": list(dataset_key(consumer)), "decisions": decisions}
    if audit or context["inserted"]:
        _clear_product_cache(importer)
    importer.inventory_supplier_compatibility_audit = audit
    importer.inventory_supplier_compatibility_summary = {
        "targeted_exchanges": len(audit),
        "changed_exchanges": sum(row["status"] == "changed" for row in audit),
        "unchanged_exchanges": sum(row["status"] == "unchanged" for row in audit),
        "fallback_datasets_inserted": len(context["inserted"]),
    }


def install_inventory_supplier_compatibility(additional_class=None, *, rules_path=DEFAULT_RULES_PATH, verbose=True):
    """Install scoped in-memory hooks before constructing AdditionalInventory.

    Wrappers retain native arguments, results and exceptions. Installation is
    idempotent across notebook/module reloads, and does not patch base classes.
    """
    if additional_class is None:
        from premise.inventory_imports import AdditionalInventory
        additional_class = AdditionalInventory
    marker = "__inventory_supplier_compatibility_hooks__"
    existing = additional_class.__dict__.get(marker)
    if existing is not None:
        existing.update(rules_path=Path(rules_path).resolve(), verbose=bool(verbose))
        return False
    state = {"rules_path": Path(rules_path).resolve(), "verbose": bool(verbose)}
    original_prepare = additional_class.prepare_inventory
    original_products = additional_class.add_product_field_to_exchanges

    @wraps(original_prepare)
    def prepare(self, *args, **kwargs):
        rules = load_compatibility_rules(state["rules_path"])
        context = _prepare_compatibility(self, rules)
        self._inventory_supplier_compatibility_context = context
        try:
            return original_prepare(self, *args, **kwargs)
        finally:
            self.__dict__.pop("_inventory_supplier_compatibility_context", None)

    @wraps(original_products)
    def products(self, *args, **kwargs):
        context = getattr(self, "_inventory_supplier_compatibility_context", None)
        if context is not None:
            _apply_compatibility(self, context)
        return original_products(self, *args, **kwargs)

    additional_class.prepare_inventory = prepare
    additional_class.add_product_field_to_exchanges = products
    setattr(additional_class, marker, state)
    if verbose:
        print("Installed exact inventory supplier compatibility hooks for configured additional inventories.")
    return True
