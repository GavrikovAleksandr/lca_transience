"""Repo-owned, inventory-only non-ferrous primary/secondary market policy.

The native baseline deliberately does nothing: it preserves the installed
premise release's quantities and suppliers. Explicit sensitivity cases only
rescale verified material-supply recipes; recycling is not an avoided burden.
No premise, Brightway, store or filesystem export API is imported here.
Steel supply remains endogenous to the native IAM/EDM-I scenario.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable, Mapping


DEFAULT_POLICY_PATH = Path(__file__).resolve().parent / "configuration_file" / "metals_scenario_policy.yaml"
IDENTITY_FIELDS = ("name", "reference product", "unit", "location")
SECONDARY_TERMS = ("treatment of", "used", "recover", "discard", "recycl", "scrap", "waste", "sludge", "secondary")


class MetalPolicyError(ValueError):
    """An explicit metals scenario cannot safely be applied."""


class MissingMetalSupplierError(MetalPolicyError):
    """A required genuine primary/secondary supply recipe is unavailable."""


class StaleMetalPolicyError(MetalPolicyError):
    """The source policy or planned inventory changed after preflight."""


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, tuple):
        return list(value)
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(f"Cannot fingerprint {type(value).__name__} in metals policy")


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_default).encode("utf-8")).hexdigest()


def activity_identity(dataset: Mapping) -> tuple:
    return tuple(dataset.get(field) for field in IDENTITY_FIELDS)


def _exc_identity(exchange: Mapping) -> tuple:
    return (exchange.get("name"), exchange.get("product", exchange.get("reference product")), exchange.get("unit"), exchange.get("location"))


def _float(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise MetalPolicyError(f"{label} must be a finite number, got {value!r}") from error
    if not math.isfinite(result):
        raise MetalPolicyError(f"{label} must be finite, got {value!r}")
    return result


def _strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


def _validate_share_specification(value, label, *, regional=True):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not 0 <= _float(value, label) <= 1:
            raise MetalPolicyError(f"{label}: secondary shares must be in [0, 1]")
        return
    if not isinstance(value, dict) or not value:
        raise MetalPolicyError(f"{label}: expected a share, nonempty year curve or region map")
    numeric_keys = []
    for key in value:
        try:
            numeric_keys.append(_float(key, f"{label} year"))
        except MetalPolicyError:
            numeric_keys.append(None)
    if all(year is not None for year in numeric_keys):
        if len(set(numeric_keys)) != len(numeric_keys):
            raise MetalPolicyError(f"{label}: duplicate target years")
        for year, share in value.items():
            if not isinstance(share, (int, float)) or isinstance(share, bool):
                raise MetalPolicyError(f"{label}/{year}: year-curve values must be numeric secondary shares")
            _validate_share_specification(share, f"{label}/{year}", regional=False)
    elif any(year is not None for year in numeric_keys):
        raise MetalPolicyError(f"{label}: cannot mix year keys and region keys")
    elif regional:
        for region, share in value.items():
            _validate_share_specification(share, f"{label}/{region}", regional=False)
    else:
        raise MetalPolicyError(f"{label}: nested region maps are not supported")


def _unique_yaml_load(data):
    import yaml

    class UniqueLoader(yaml.SafeLoader):
        pass

    def unique_mapping(loader, node, deep=False):
        loader.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            try:
                duplicate = key in result
            except TypeError as error:
                raise yaml.constructor.ConstructorError("while constructing a mapping", node.start_mark,
                                                        "unhashable mapping key", key_node.start_mark) from error
            if duplicate:
                raise yaml.constructor.ConstructorError("while constructing a mapping", node.start_mark,
                                                        f"duplicate key {key!r}", key_node.start_mark)
            result[key] = loader.construct_object(value_node, deep=deep)
        return result

    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)
    return yaml.load(data, Loader=UniqueLoader)


def _read_share_document(path, source_hashes):
    import yaml

    try:
        data = Path(path).read_bytes()
    except OSError as error:
        raise MetalPolicyError(f"Metals share data are unavailable: {path}. Run ensure_share_outputs() in notebook 1 before loading the policy") from error
    source_hashes[str(path)] = sha256(data).hexdigest()
    try:
        document = _unique_yaml_load(data)
    except yaml.YAMLError as error:
        raise MetalPolicyError(f"Invalid metals share data in {path}: {error}") from error
    if not isinstance(document, dict) or not document:
        raise MetalPolicyError(f"Metals share data are empty or not a mapping: {path}. Generate the named scenario files first")
    return document


def _source_curve(record, rule, filename):
    if not isinstance(record, dict) or record.get("name") != rule["name"] or record.get("reference product") != rule["reference product"]:
        raise MetalPolicyError(f"{filename}: source record for {rule['id']} must match exact market name and reference product; no metal-grade substitution is permitted")
    if record.get("unit", "kilogram") != rule.get("unit", "kilogram"):
        raise MetalPolicyError(f"{filename}/{rule['id']}: source unit must be kilogram; no implicit unit conversion is permitted")
    shares = record.get("shares", {})
    if not isinstance(shares, dict):
        raise MetalPolicyError(f"{filename}/{rule['id']}: missing primary/secondary shares")
    primary, secondary = shares.get("primary"), shares.get("secondary")
    for role, curve in (("primary", primary), ("secondary", secondary)):
        if not isinstance(curve, dict) or not curve:
            raise MetalPolicyError(f"{filename}/{rule['id']}: {role} must be a nonempty year curve")
        _validate_share_specification(curve, f"{filename}/{rule['id']}/{role}", regional=False)
        if any(not isinstance(key, (int, float, str)) for key in curve):
            raise MetalPolicyError(f"{filename}/{rule['id']}: invalid target year")
    primary_by_year = {_float(year, "primary target year"): _float(value, "primary share") for year, value in primary.items()}
    secondary_by_year = {_float(year, "secondary target year"): _float(value, "secondary share") for year, value in secondary.items()}
    if set(primary_by_year) != set(secondary_by_year):
        raise MetalPolicyError(f"{filename}/{rule['id']}: primary and secondary curves must use the same years")
    for year in secondary_by_year:
        if not math.isclose(primary_by_year[year] + secondary_by_year[year], 1, rel_tol=0, abs_tol=1e-6):
            raise MetalPolicyError(f"{filename}/{rule['id']}/{year:g}: primary and secondary shares must sum to 1")
    return deepcopy(secondary)


def _is_steel_rule(rule):
    labels = (rule.get("name"), rule.get("reference product"), *_strings(rule.get("supply_products")))
    return rule.get("sector") == "steel" or any(re.search(r"\bsteel\b", str(label), flags=re.IGNORECASE) for label in labels)


def _reject_exogenous_steel(config, steel_case="baseline"):
    message = "Steel supply remains endogenous to IAM/EDM-I. Exogenous steel cases, rules and share files are not supported; remove the steel override configuration and use steel_case='baseline' only for backward compatibility"
    if steel_case != "baseline":
        raise MetalPolicyError(message)
    cases = config.get("steel_cases")
    if cases is not None and (not isinstance(cases, dict) or any(case != "baseline" for case in cases)
                              or ("baseline" in cases and (not isinstance(cases["baseline"], dict) or cases["baseline"].get("mode") != "native"))):
        raise MetalPolicyError(message)
    if cases and any(key in cases.get("baseline", {}) for key in ("targets", "shares", "reference_shares")):
        raise MetalPolicyError(message)
    rules = config.get("markets", [])
    if isinstance(rules, list) and any(isinstance(rule, dict) and _is_steel_rule(rule) for rule in rules):
        raise MetalPolicyError(message)
    share_data = config.get("share_data", {})
    if isinstance(share_data, dict) and "steel" in share_data:
        raise MetalPolicyError(message)
    if any(key in config for key in ("steel_source", "steel_share_data", "steel_targets", "steel_shares")):
        raise MetalPolicyError(message)


def _load_share_data(config, policy_path, nonferrous_case, source_hashes):
    """Read selected case inputs; never generate or alter scenario files."""
    specification = config.get("share_data")
    if not specification:
        return
    if not isinstance(specification, dict):
        raise MetalPolicyError("share_data must be a mapping")
    directory = Path(specification.get("directory", "../primary_vs_secondary_metals/scenario_data"))
    directory = directory.resolve() if directory.is_absolute() else (policy_path.parent / directory).resolve()
    provenance, selected_paths = [], set()
    documents = {}

    def read(filename):
        path = Path(filename)
        path = path.resolve() if path.is_absolute() else (directory / path).resolve()
        if path not in documents:
            documents[path] = _read_share_document(path, source_hashes)
        selected_paths.add(path)
        return path, documents[path]

    selected = (("nonferrous", nonferrous_case),)
    for sector, case in selected:
        cases = config.get(f"{sector}_cases", {})
        settings = cases.get(case, {})
        mode = settings.get("mode")
        # 'baseline' is the installed premise baseline, not the generated
        # file whose historical scenario happens to be named 'baseline'.
        if mode == "native" or case == "primary_only":
            continue
        sector_data = specification.get(sector)
        if not sector_data:
            continue
        if sector == "nonferrous":
            case_data = sector_data.get(case) if isinstance(sector_data, dict) else None
            if not isinstance(case_data, dict) or not case_data.get("file"):
                raise MetalPolicyError(f"share_data.nonferrous has no file for selected case {case!r}")
            path, document = read(case_data["file"])
            products = document.get("products", document)
            # Combined legacy documents may nest products by scenario. The
            # named product-to-record files remain the preferred interface.
            source_case = case_data.get("scenario")
            if source_case is not None:
                products = products.get(source_case, {})
            for rule in config.get("markets", []):
                if rule.get("sector") != sector:
                    continue
                source_product = rule.get("source_product", rule.get("reference product"))
                if not isinstance(products, dict) or source_product not in products:
                    raise MetalPolicyError(f"{path}: no source product {source_product!r} for {rule.get('id')!r}")
                curve = _source_curve(products[source_product], rule, str(path))
                receipt = {"sector": sector, "case": case, "path": str(path), "sha256": source_hashes[str(path)],
                           "source_product": source_product, "source_curve": deepcopy(curve)}
                if mode == "reference":
                    year = _float(case_data.get("year", 2020), "reference share year")
                    reference = _curve(curve, year, "*", rule["id"], case)
                    rule["reference_shares"] = reference
                    rule.setdefault("targets", {}).pop(case, None)
                    receipt.update(reference_year=year, effective_targets=reference)
                else:
                    rule.setdefault("targets", {})[case] = deepcopy(curve)
                    receipt["effective_targets"] = deepcopy(curve)
                rule["share_data_source"] = receipt
                provenance.append(receipt)

    if not provenance:
        return
    manifest_name = specification.get("manifest", "share_generation_manifest.json")
    manifest_path = (directory / manifest_name).resolve() if manifest_name else None
    if manifest_path and manifest_path.is_file():
        manifest = _read_share_document(manifest_path, source_hashes)
        if manifest.get("schema_version") != 1:
            raise MetalPolicyError(f"Unsupported share-generation manifest schema: {manifest_path}")
        inputs = manifest.get("input_sha256", {})
        if not isinstance(inputs, dict):
            raise MetalPolicyError(f"Share-generation manifest inputs must be a mapping: {manifest_path}")
        for filename, expected in inputs.items():
            path = Path(filename)
            path = path.resolve() if path.is_absolute() else (manifest_path.parent / path).resolve()
            try:
                observed = sha256(path.read_bytes()).hexdigest()
            except OSError as error:
                raise MetalPolicyError(f"Share-generation input is unavailable: {path}") from error
            if observed != expected:
                raise StaleMetalPolicyError(f"Share-generation input changed: {path}. Run ensure_share_outputs() before loading the policy")
            source_hashes[str(path)] = observed
        for receipt in provenance:
            receipt["manifest_path"] = str(manifest_path)
            receipt["manifest_sha256"] = source_hashes[str(manifest_path)]
            declared = manifest.get("output_sha256", {}).get(Path(receipt["path"]).name)
            receipt["matches_generated_manifest"] = declared == receipt["sha256"] if declared else None
    elif specification.get("manifest_required"):
        raise MetalPolicyError(f"Share-generation manifest is missing: {manifest_path}. Run ensure_share_outputs() first")
    config["_share_data_provenance"] = provenance


@dataclass(frozen=True)
class MetalSteelPolicy:
    configuration: Mapping
    nonferrous_case: str = "baseline"
    steel_case: str = "baseline"
    path: Path | None = None
    source_hashes: Mapping | None = None
    fingerprint: str = ""

    @classmethod
    def from_file(cls, path=DEFAULT_POLICY_PATH, *, nonferrous_case="baseline", steel_case="baseline"):
        import yaml

        policy_path = Path(path).resolve()
        data = policy_path.read_bytes()
        try:
            configuration = _unique_yaml_load(data)
        except yaml.YAMLError as error:
            raise MetalPolicyError(f"Invalid metals policy in {policy_path}: {error}") from error
        source_hashes = {str(policy_path): sha256(data).hexdigest()}
        if isinstance(configuration, dict):
            _reject_exogenous_steel(configuration, steel_case)
            _load_share_data(configuration, policy_path, nonferrous_case, source_hashes)
        return cls.from_mapping(configuration, nonferrous_case=nonferrous_case, steel_case=steel_case,
                                path=policy_path, source_hashes=source_hashes)

    @classmethod
    def from_mapping(cls, configuration, *, nonferrous_case="baseline", steel_case="baseline", path=None, source_hashes=None):
        config = deepcopy(configuration)
        if not isinstance(config, dict) or config.get("schema_version", config.get("policy_version")) != 1:
            raise MetalPolicyError("Metals policy must be a mapping with schema_version: 1")
        _reject_exogenous_steel(config, steel_case)
        config["steel_cases"] = {"baseline": {"mode": "native"}}
        for sector, selected in (("nonferrous", nonferrous_case),):
            cases = config.get(f"{sector}_cases", {})
            if selected not in cases:
                raise MetalPolicyError(f"Unknown {sector} case {selected!r}; available cases: {sorted(cases)}")
            if "baseline" in cases and (not isinstance(cases["baseline"], dict) or cases["baseline"].get("mode") != "native"):
                raise MetalPolicyError(f"{sector} case 'baseline' is reserved for mode: native. Choose a distinct custom case name for any supply override so native-baseline outputs are not overwritten")
            for case, settings in cases.items():
                if not isinstance(settings, dict) or settings.get("mode") not in ("native", "reference", "target", "minimum"):
                    raise MetalPolicyError(f"{sector} case {case!r} must declare mode native/reference/target/minimum")
        rules = config.get("markets", [])
        if not isinstance(rules, list):
            raise MetalPolicyError("markets must be a list of exact market rules")
        seen = set()
        for rule in rules:
            if not isinstance(rule, dict):
                raise MetalPolicyError("Each metals market rule must be a mapping")
            if rule.get("id") in seen or not rule.get("id"):
                raise MetalPolicyError("Market rule IDs must be nonempty and unique")
            seen.add(rule["id"])
            if rule.get("sector") != "nonferrous" or not rule.get("name") or not rule.get("reference product"):
                raise MetalPolicyError(f"Rule {rule['id']!r} needs sector, exact name and reference product")
            if rule.get("unit", "kilogram") != "kilogram":
                raise MetalPolicyError(f"Rule {rule['id']!r}: only kilogram material markets can be adjusted")
            if rule["sector"] == "nonferrous" and any(term in rule["reference product"].lower() for term in ("ore", "concentrate", "primary")):
                raise MetalPolicyError(f"Rule {rule['id']!r}: ores, concentrates and explicit-primary products cannot be recycling markets")
            targets = rule.get("targets", {})
            if not isinstance(targets, dict):
                raise MetalPolicyError(f"Rule {rule['id']}: targets must map case names to share specifications")
            cases = config[f"{rule['sector']}_cases"]
            for case, specification in targets.items():
                if case not in cases:
                    raise MetalPolicyError(f"Rule {rule['id']}: target refers to unknown case {case!r}")
                _validate_share_specification(specification, f"{rule['id']}/{case}")
            for key in ("reference_shares", "reference_share"):
                if key in rule:
                    _validate_share_specification(rule[key], f"{rule['id']}/{key}")
            selected = nonferrous_case if rule["sector"] == "nonferrous" else steel_case
            if cases[selected]["mode"] in ("minimum", "target") and selected not in targets and selected != "primary_only":
                raise MetalPolicyError(f"Rule {rule['id']}: missing target for selected case {selected!r}")
        fingerprint = _digest({"configuration": config, "nonferrous_case": nonferrous_case, "steel_case": steel_case})
        return cls(config, nonferrous_case, steel_case, Path(path) if path else None, dict(source_hashes or {}), fingerprint)

    @property
    def rules(self):
        return tuple(self.configuration.get("markets", []))

    @property
    def baseline(self):
        return self.case_settings("nonferrous").get("mode") == "native"

    @property
    def case_suffix(self):
        if self.nonferrous_case == "baseline":
            return ""
        safe = lambda value: re.sub(r"[^a-zA-Z0-9_-]+", "-", value)
        return f"__metals-{safe(self.nonferrous_case)}"

    def describe(self):
        return {"nonferrous_case": self.nonferrous_case, "steel_case": self.steel_case,
                "steel_owner": "endogenous IAM/EDM-I",
                "native_baseline": self.baseline, "policy_sha256": self.fingerprint,
                "policy_file": str(self.path) if self.path else None, "output_suffix": self.case_suffix,
                "market_rules": len(self.rules),
                "share_data_sources": deepcopy(self.configuration.get("_share_data_provenance", []))}

    def case_name(self, sector):
        return self.nonferrous_case if sector == "nonferrous" else self.steel_case

    def case_settings(self, sector):
        return self.configuration[f"{sector}_cases"][self.case_name(sector)]

    def verify_sources(self):
        current = _digest({"configuration": self.configuration, "nonferrous_case": self.nonferrous_case, "steel_case": self.steel_case})
        if current != self.fingerprint:
            raise StaleMetalPolicyError("Metals policy object was modified after preflight; reload the policy")
        for filename, expected in (self.source_hashes or {}).items():
            try:
                observed = sha256(Path(filename).read_bytes()).hexdigest()
            except OSError as error:
                raise StaleMetalPolicyError(f"Metals policy source is unavailable: {filename}") from error
            if observed != expected:
                raise StaleMetalPolicyError(f"Metals policy source changed: {filename}; reload before updating/exporting")


def matching_market_rule(dataset: Mapping, policy: MetalSteelPolicy):
    matches = []
    for rule in policy.rules:
        locations = _strings(rule.get("locations", ("*",)))
        if (dataset.get("name") == rule["name"] and dataset.get("reference product") == rule["reference product"]
                and dataset.get("unit") == rule.get("unit", "kilogram")
                and ("*" in locations or dataset.get("location") in locations)):
            matches.append(rule)
    if len(matches) > 1:
        raise MetalPolicyError(f"Overlapping market rules for {activity_identity(dataset)!r}")
    return matches[0] if matches else None


def is_policy_market(dataset: Mapping, policy: MetalSteelPolicy) -> bool:
    return matching_market_rule(dataset, policy) is not None


def _predicate(name: str, settings: Mapping | None) -> bool:
    if not settings:
        return False
    text = name.lower()
    excludes = _strings(settings.get("name_excludes", settings.get("name_excludes_contains")))
    if any(term.lower() in text for term in excludes):
        return False
    exact = _strings(settings.get("names", settings.get("name_equals")))
    contains = _strings(settings.get("name_contains"))
    starts = _strings(settings.get("name_prefix", settings.get("name_startswith")))
    return (name in exact or any(term.lower() in text for term in contains)
            or any(text.startswith(term.lower()) for term in starts))


def classify_supply(exchange: Mapping, rule: Mapping) -> str | None:
    """Classify actual material inputs, never transport or negative coproducts.

    Steel routes are intentionally outside this exogenous policy's scope.
    """
    if _is_steel_rule(rule):
        raise MetalPolicyError("Steel routes remain endogenous to IAM/EDM-I; no exogenous steel supply classification is applied")
    if exchange.get("type") != "technosphere" or exchange.get("unit") != rule.get("unit", "kilogram"):
        return None
    products = _strings(rule.get("supply_products", (rule["reference product"],)))
    if exchange.get("product", exchange.get("reference product")) not in products:
        return None
    try:
        if _float(exchange.get("amount", 0), "supply amount") < 0:
            return None
    except MetalPolicyError:
        return "invalid"
    name = str(exchange.get("name", ""))
    text = name.lower()
    if text.startswith(("market for ", "market group for ")):
        return "aggregate"
    if _predicate(name, rule.get("secondary")) or (not rule.get("secondary") and any(term in text for term in SECONDARY_TERMS)):
        return "secondary"
    if _predicate(name, rule.get("primary")) or not rule.get("primary"):
        return "primary"
    return "unknown"


def _curve(value, year, location, rule_id, case):
    if isinstance(value, (int, float)):
        share = _float(value, f"{rule_id}/{case} share")
    elif isinstance(value, dict) and value:
        # A region map is distinguished from a year curve by nonnumeric keys.
        try:
            points = sorted((_float(k, "target year"), _float(v, "secondary target")) for k, v in value.items())
        except MetalPolicyError:
            selected = value.get(location, value.get("*"))
            if selected is None:
                raise MetalPolicyError(f"Rule {rule_id}/{case}: no target for region {location!r}")
            return _curve(selected, year, location, rule_id, case)
        if year <= points[0][0]:
            share = points[0][1]
        elif year >= points[-1][0]:
            share = points[-1][1]
        else:
            for (left, low), (right, high) in zip(points, points[1:]):
                if left <= year <= right:
                    share = low + (high - low) * (year - left) / (right - left)
                    break
        if any(not 0 <= point[1] <= 1 for point in points):
            raise MetalPolicyError(f"Rule {rule_id}/{case}: secondary shares must be in [0, 1]")
    else:
        raise MetalPolicyError(f"Rule {rule_id}/{case}: missing secondary-share target curve")
    if not 0 <= share <= 1:
        raise MetalPolicyError(f"Rule {rule_id}/{case}: secondary share {share} is outside [0, 1]")
    return share


def _target(rule, settings, case, year, location, observed, source_market):
    mode = settings["mode"]
    value = rule.get("targets", {}).get(case)
    if mode == "reference" and value is None:
        value = rule.get("reference_shares", rule.get("reference_share"))
        if value is None and source_market is not None:
            groups, _ = _groups(source_market.get("exchanges", []), rule)
            total = sum(float(exc["amount"]) for _, exc in groups["primary"] + groups["secondary"])
            if total > 0:
                value = sum(float(exc["amount"]) for _, exc in groups["secondary"]) / total
    if mode in ("target", "minimum") and value is None and case == "primary_only":
        value = 0.0
    target = _curve(value, year, location, rule["id"], case)
    return max(observed, target) if mode == "minimum" else target


def _region_targeted(rule, settings, case, location):
    value = rule.get("targets", {}).get(case)
    if value is None and settings["mode"] == "reference":
        value = rule.get("reference_shares", rule.get("reference_share"))
    if not isinstance(value, dict) or not value:
        return True
    try:
        for key in value:
            _float(key, "target year")
    except MetalPolicyError:
        return location in value or "*" in value
    return True


def _share_data_audit(rule):
    receipt = rule.get("share_data_source", {})
    return {"share_data_case": receipt.get("case"), "share_data_path": receipt.get("path"),
            "share_data_sha256": receipt.get("sha256"), "share_data_source_product": receipt.get("source_product"),
            "share_data_targets": deepcopy(receipt.get("effective_targets")),
            "share_data_manifest_sha256": receipt.get("manifest_sha256")}


def _groups(exchanges, rule):
    groups = {key: [] for key in ("primary", "secondary", "aggregate", "unknown", "invalid")}
    warnings = []
    for index, exchange in enumerate(exchanges):
        role = classify_supply(exchange, rule)
        if role is not None:
            groups[role].append((index, exchange))
    if groups["unknown"]:
        warnings.append("Unrecognised same-product material suppliers")
    if groups["invalid"]:
        warnings.append("Invalid/non-finite material-supply amounts")
    return groups, warnings


def _supplier_index(database):
    result = {}
    for dataset in database:
        result.setdefault(activity_identity(dataset), []).append(dataset)
    return result


def _available_supplier(exchange, index, label):
    candidates = index.get(_exc_identity(exchange), [])
    if not candidates:
        raise MissingMetalSupplierError(f"{label}: provider {_exc_identity(exchange)!r} is absent from the updated inventory; provide a compatible, burden-carrying LCI or keep baseline")
    if len(candidates) > 1:
        link = exchange.get("input")
        linked = [ds for ds in candidates if link and tuple(link) == (ds.get("database"), ds.get("code"))]
        if len(linked) != 1:
            raise MissingMetalSupplierError(f"{label}: provider {_exc_identity(exchange)!r} is ambiguous; resolve supplier identity explicitly")
        return linked[0]
    return candidates[0]


def _scale_exchange(exchange, factor):
    result = deepcopy(dict(exchange))
    if factor == 1:
        return result
    result["amount"] = float(exchange["amount"]) * factor
    raw_uncertainty = result.get("uncertainty type", 0)
    if factor == 0:
        uncertainty = 0
    else:
        try:
            uncertainty = int(raw_uncertainty or 0)
            if float(raw_uncertainty or 0) != uncertainty:
                raise ValueError("non-integral distribution ID")
        except (TypeError, ValueError) as error:
            raise MetalPolicyError(f"Cannot rescale invalid uncertainty type {raw_uncertainty!r}") from error
        # Dimensionful location/scale families are closed under positive
        # multiplication. Bernoulli/discrete and beta shape parameters are
        # not: multiplying a beta parameter changes its distribution shape.
        if uncertainty not in (0, 1, 2, 3, 4, 5, 8, 9, 11, 12):
            raise MetalPolicyError(f"Cannot safely rescale uncertainty type {uncertainty} on {exchange.get('name')!r}. Supply a supported amount distribution or keep the native baseline")
    for field in ("minimum", "maximum", "loc"):
        value = result.get(field)
        if value is None:
            continue
        if field == "loc" and uncertainty == 2:
            result[field] = _float(value, field) + math.log(factor) if factor > 0 else 0.0
        else:
            result[field] = _float(value, field) * factor
    if result.get("scale") is not None and uncertainty not in (0, 2):
        result["scale"] = _float(result["scale"], "scale") * factor
    if factor == 0:
        result["uncertainty type"] = 0
        for field in ("loc", "scale", "shape", "minimum", "maximum", "negative"):
            result.pop(field, None)
    # A static scenario override must not leave a formula that restores an old amount.
    if "formula" in result:
        result["por original formula"] = result.pop("formula")
    return result


def _source_market(dataset, source_database, rule):
    if source_database is None:
        return None
    matches = [ds for ds in source_database if activity_identity(ds) == activity_identity(dataset)]
    if not matches:
        # A source World market may not exist in unmodified ecoinvent. Select
        # only an explicitly configured reference location, not an arbitrary country.
        for location in _strings(rule.get("reference_locations")):
            matches = [ds for ds in source_database if activity_identity(ds)[:3] == activity_identity(dataset)[:3]
                       and ds.get("location") == location]
            if matches:
                break
        if not matches:
            return None
    if len(matches) != 1:
        raise MissingMetalSupplierError(f"Reference market {activity_identity(dataset)!r} is ambiguous")
    return matches[0]


def _seed_recipe(role, rule, source_market, provider_index, label):
    if source_market is None:
        raise MissingMetalSupplierError(f"{label}: a nonzero {role} share is required but no positive recipe exists. Supply a verified matching reference market with compatible providers, or keep baseline")
    groups, warnings = _groups(source_market.get("exchanges", []), rule)
    if warnings or groups["aggregate"]:
        raise MissingMetalSupplierError(f"{label}: reference market is not a verified direct material-supply recipe")
    result = []
    for _, original in groups[role]:
        if float(original.get("amount", 0)) <= 0:
            continue
        supplier = _available_supplier(original, provider_index, label)
        exchange = deepcopy(dict(original))
        # Never carry a source-database UUID into the transformed inventory.
        if supplier.get("code") and supplier.get("database"):
            exchange["input"] = (supplier["database"], supplier["code"])
        else:
            exchange.pop("input", None)
        exchange.pop("output", None)
        exchange["por metals recipe source"] = "verified matching reference market"
        result.append(exchange)
    if not result:
        raise MissingMetalSupplierError(f"{label}: reference market has no positive {role} recipe with suppliers available in the updated inventory")
    return result


@dataclass(frozen=True)
class MarketChange:
    dataset_index: int
    identity: tuple
    old_exchanges_sha256: str
    new_exchanges: tuple


@dataclass(frozen=True)
class MetalScenarioPlan:
    changes: tuple[MarketChange, ...]
    audit: tuple[dict, ...]
    stats: Mapping
    policy_fingerprint: str
    plan_sha256: str

    @property
    def changed_indices(self):
        return tuple(change.dataset_index for change in self.changes)

    def verify(self):
        if _plan_digest(self.changes, self.policy_fingerprint) != self.plan_sha256:
            raise StaleMetalPolicyError("Planned metals exchanges were modified after validation")


def _plan_digest(changes, fingerprint):
    return _digest({"policy": fingerprint, "changes": [
        {"index": item.dataset_index, "identity": item.identity, "before": item.old_exchanges_sha256, "after": item.new_exchanges}
        for item in changes]})


def plan_database(database: Iterable[Mapping], policy: MetalSteelPolicy, *, scenario=None, source_database=None) -> MetalScenarioPlan:
    """Audit native markets or plan all explicit overrides without mutation.

    Activity mappings may be lazy: only identity fields are read on unrelated
    activities. Only exact policy markets need exchange dictionaries. Baseline
    is audit-only and does not reject native anomalies or require source data.
    """
    policy.verify_sources()
    activities = list(database)
    references = list(source_database) if source_database is not None and not policy.baseline else None
    provider_index = None

    def suppliers():
        nonlocal provider_index
        if provider_index is None:
            provider_index = _supplier_index(activities)
        return provider_index
    context = dict(scenario or {})
    if context.get("system_model") == "consequential" and not policy.baseline:
        raise MetalPolicyError("Primary/secondary supply sensitivities require attributional/cut-off modelling. Consequential scenarios must retain the native baseline; recycled-input fractions are not marginal suppliers")
    year = _float(context.get("year", 2025), "scenario year")
    changes, audit = [], []
    observed_rules = set()
    for dataset_index, dataset in enumerate(activities):
        rule = matching_market_rule(dataset, policy)
        if rule is None:
            continue
        # Storage adapters may supply identity-only records for providers.
        # A regional child-market identity is not a loaded market recipe.
        if "exchanges" not in dataset:
            continue
        observed_rules.add(rule["id"])
        settings = policy.case_settings(rule["sector"])
        case = policy.case_name(rule["sector"])
        exchanges = list(dataset.get("exchanges", []))
        groups, warnings = _groups(exchanges, rule)
        primary = sum(float(exc.get("amount", 0)) for _, exc in groups["primary"])
        secondary = sum(float(exc.get("amount", 0)) for _, exc in groups["secondary"])
        total = primary + secondary
        aggregate_total = sum(float(exc.get("amount", 0)) for _, exc in groups["aggregate"])
        native_share = secondary / total if total > 0 else None
        row = {"rule_id": rule["id"], "sector": rule["sector"], "case": case, "mode": settings["mode"],
               "name": dataset.get("name"), "reference product": dataset.get("reference product"),
               "unit": dataset.get("unit"), "location": dataset.get("location"), "year": year,
               "model": context.get("model"), "pathway": context.get("pathway"),
               "native_primary_amount": primary, "native_secondary_amount": secondary,
               "native_supply_total": total + aggregate_total, "native_direct_supply_total": total,
               "aggregate_supply_amount": aggregate_total, "native_secondary_share": native_share,
               "target_secondary_share": native_share, "applied_secondary_share": native_share,
               "status": "native_preserved", "warnings": "; ".join(warnings), "seeded_roles": "",
               "recipe_source_market": None, "recipe_source_exchanges_sha256": None,
               "policy_sha256": policy.fingerprint, **_share_data_audit(rule)}
        if settings["mode"] == "native":
            if groups["aggregate"]:
                row["status"] = "native_aggregate_preserved"
                row["warnings"] += ("; " if row["warnings"] else "") + "Regional market hierarchy retained; aggregate inputs do not establish a recycled fraction"
            if not math.isclose(total, 1, rel_tol=0, abs_tol=1e-6) and not groups["aggregate"]:
                row["warnings"] += ("; " if row["warnings"] else "") + "Native material inputs do not sum to 1 kg (not changed)"
            audit.append(row)
            continue
        if not _region_targeted(rule, settings, case, dataset.get("location")):
            row["status"] = "region_not_targeted"
            row["warnings"] += ("; " if row["warnings"] else "") + "No explicit target for this region; native suppliers and quantities retained"
            audit.append(row)
            continue
        if groups["aggregate"]:
            if groups["primary"] or groups["secondary"] or groups["unknown"]:
                raise MetalPolicyError(f"{rule['id']} at {dataset.get('location')}: mixed aggregate and direct suppliers cannot be safely rescaled")
            row["status"] = "aggregate_preserved"
            row["warnings"] = "Regional market hierarchy retained; adjust direct route markets, not region weights"
            audit.append(row)
            continue
        if warnings or groups["unknown"] or groups["invalid"]:
            raise MetalPolicyError(f"{rule['id']} at {dataset.get('location')}: {row['warnings']}; provide exact supplier predicates")
        if total < 1 - 1e-6:
            raise MetalPolicyError(f"{rule['id']} at {dataset.get('location')}: material supply is {total} kg; expected at least 1 kg for a 1 kg market output before an override. Resolve missing supply routes first")
        label = f"{rule['id']} at {dataset.get('location')} in {year:g} ({case})"
        reference = _source_market(dataset, references, rule)
        target = _target(rule, settings, case, year, dataset.get("location"), native_share, reference)
        row["target_secondary_share"] = target
        production = [exc for exc in exchanges if exc.get("type") == "production" and exc.get("product", exc.get("reference product")) == rule["reference product"]]
        if len(production) != 1 or production[0].get("unit") != "kilogram" or not math.isclose(_float(production[0].get("amount"), "market output"), 1, abs_tol=1e-6):
            raise MetalPolicyError(f"{label}: expected one 1 kg reference-production exchange")
        # Exact numerical identity avoids touching uncertainty/supplier metadata.
        if math.isclose(target, native_share, rel_tol=0, abs_tol=1e-12):
            row["status"] = "already_satisfied"
            audit.append(row)
            continue
        revised = [deepcopy(dict(exc)) for exc in exchanges]
        seeded_roles = []
        # A market may include distribution losses: keep its gross material
        # demand, rather than normalising 1.01 kg supplied to 1 kg output.
        for role, desired in (("primary", (1 - target) * total), ("secondary", target * total)):
            recipe = [(index, exc) for index, exc in groups[role] if float(exc.get("amount", 0)) > 0]
            if desired > 0 and not recipe:
                seeds = _seed_recipe(role, rule, reference, suppliers(), label)
                recipe = [(len(revised) + offset, exc) for offset, exc in enumerate(seeds)]
                revised.extend(deepcopy(seeds))
                seeded_roles.append(role)
            recipe_total = sum(float(exc["amount"]) for _, exc in recipe)
            for index, exc in recipe:
                _available_supplier(exc, suppliers(), label)
                if role == "secondary" and any(term in str(exc.get("name", "")).lower() for term in ("burden-free", "burden free")):
                    raise MissingMetalSupplierError(f"{label}: explicitly burden-free secondary provider is not a valid recycling LCI for this sensitivity")
                revised[index] = _scale_exchange(exc, desired / recipe_total if recipe_total else 0)
        revised_groups, _ = _groups(revised, rule)
        after_primary = sum(float(exc["amount"]) for _, exc in revised_groups["primary"])
        after_secondary = sum(float(exc["amount"]) for _, exc in revised_groups["secondary"])
        after_total = after_primary + after_secondary
        if not math.isclose(after_total, total, rel_tol=0, abs_tol=1e-9) or not math.isclose(after_secondary / after_total, target, rel_tol=0, abs_tol=1e-9):
            raise MetalPolicyError(f"{label}: planned material balance did not conserve native gross supply")
        row.update(status="planned", applied_secondary_share=after_secondary / after_total, applied_supply_total=after_total,
                   seeded_roles=", ".join(seeded_roles), changed_exchange_count=sum(a != b for a, b in zip(exchanges, revised)) + abs(len(revised) - len(exchanges)))
        if seeded_roles:
            row["recipe_source_market"] = activity_identity(reference)
            row["recipe_source_exchanges_sha256"] = _digest(list(reference.get("exchanges", [])))
        changes.append(MarketChange(dataset_index, activity_identity(dataset), _digest(exchanges), tuple(revised)))
        audit.append(row)
    for rule in policy.rules:
        if rule["id"] not in observed_rules:
            audit.append({"rule_id": rule["id"], "sector": rule["sector"], "case": policy.case_name(rule["sector"]),
                          "status": "market_not_present", "name": rule["name"], "reference product": rule["reference product"],
                          "year": year, "policy_sha256": policy.fingerprint, **_share_data_audit(rule)})
    stats = {"matched_markets": len([row for row in audit if row["status"] != "market_not_present"]),
             "changed_markets": len(changes), "changed_exchanges": sum(row.get("changed_exchange_count", 0) for row in audit),
             "native_baseline": policy.baseline, "nonferrous_case": policy.nonferrous_case, "steel_case": policy.steel_case,
             "steel_owner": "endogenous IAM/EDM-I",
             "statuses": {status: sum(row["status"] == status for row in audit) for status in sorted({row["status"] for row in audit})}}
    return MetalScenarioPlan(tuple(changes), tuple(audit), stats, policy.fingerprint, _plan_digest(changes, policy.fingerprint))


def apply_database(database: Iterable[Mapping], plan: MetalScenarioPlan, *, policy: MetalSteelPolicy | None = None):
    """Return a list with only planned markets copied; never mutate originals.

    Every change is validated before any copied market is constructed, so a
    stale later market cannot cause a partial application. Native baseline
    returns references to the original activity dictionaries unchanged.
    """
    if policy is not None:
        policy.verify_sources()
        if policy.fingerprint != plan.policy_fingerprint:
            raise StaleMetalPolicyError("Plan belongs to a different metals policy")
    plan.verify()
    result = list(database)
    for change in plan.changes:
        if change.dataset_index >= len(result):
            raise StaleMetalPolicyError("Inventory size changed after metals preflight")
        dataset = result[change.dataset_index]
        if activity_identity(dataset) != change.identity or _digest(list(dataset.get("exchanges", []))) != change.old_exchanges_sha256:
            raise StaleMetalPolicyError(f"Market changed after metals preflight: {change.identity!r}")
    for change in plan.changes:
        dataset = deepcopy(dict(result[change.dataset_index]))
        dataset["exchanges"] = deepcopy(list(change.new_exchanges))
        result[change.dataset_index] = dataset
    return result
