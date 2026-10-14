"""Validated repo-owned share outputs with explicit source/edit provenance."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import csv
import hashlib
import json
import math
import os
import tempfile
import yaml

DATA_DIR = Path(__file__).resolve().parent / "scenario_data"
SCENARIOS = ("conservative", "baseline", "optimistic")
SCENARIO_FILES = {case: f"primary_secondary_split_{case}.yaml" for case in SCENARIOS}
ALIAS_FILE = "primary_secondary_split.yaml"
COMBINED_FILE = "metal_shares_2050_scenarios.yaml"
MANIFEST_FILE = "share_generation_manifest.json"
GENERATOR_VERSION = 1
TOLERANCE = 1e-6


class ShareDataError(ValueError):
    """A share source/output is incomplete or invalid."""


class ShareDataConflictError(ShareDataError):
    """Regeneration would overwrite independently edited assumptions."""


class _UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ShareDataError(f"Duplicate YAML key {key!r} at line {key_node.start_mark.line + 1}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def load_yaml(path):
    try:
        return yaml.load(Path(path).read_text(encoding="utf-8-sig"), Loader=_UniqueLoader)
    except yaml.YAMLError as error:
        raise ShareDataError(f"Invalid YAML in {path}: {error}") from error


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _bytes_hash(data):
    return hashlib.sha256(data).hexdigest()


def _file_state(path):
    path = Path(path)
    if not path.exists():
        return None
    if not path.is_file():
        raise ShareDataError(f"Expected a regular share data file: {path}")
    return _hash(path)


def _yaml_bytes(data):
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True).encode("utf-8")


def _resolved(path):
    return Path(path).expanduser().resolve()


def _manifest_key(path, directory):
    try:
        return path.relative_to(directory).as_posix()
    except ValueError:
        return str(path)


def _safe_output(path):
    resolved = _resolved(path)
    if any(part.lower() in {"site-packages", "dist-packages"} for part in resolved.parts):
        raise ShareDataError(f"Installed-package writes are prohibited: {resolved}. Use a repo-owned output directory.")
    return resolved


def _number(value, label):
    if isinstance(value, bool) or value in (None, ""):
        raise ShareDataError(f"{label}: a numeric share is required")
    try:
        number = float(value)
    except (ValueError, TypeError) as error:
        raise ShareDataError(f"{label}: invalid numeric share {value!r}") from error
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise ShareDataError(f"{label}: shares must be finite within [0, 1], got {value!r}")
    return number


def validate_share_mapping(data, *, label="shares", expected_products=None):
    """Validate identities and paired, normalized 2020/2050+ year curves."""
    if not isinstance(data, dict) or not data:
        raise ShareDataError(f"{label}: a nonempty product mapping is required")
    if expected_products is not None and set(data) != set(expected_products):
        raise ShareDataError(f"{label}: product coverage differs; missing={sorted(set(expected_products)-set(data))[:8]}, extra={sorted(set(data)-set(expected_products))[:8]}")
    result, identities = {}, set()
    for product, entry in data.items():
        if not isinstance(product, str) or not product.strip() or product != product.strip():
            raise ShareDataError(f"{label}: invalid product key {product!r}")
        if not isinstance(entry, dict):
            raise ShareDataError(f"{label}/{product}: each product needs an activity mapping")
        for field in ("name", "reference product"):
            if not isinstance(entry.get(field), str) or not entry[field].strip() or entry[field] != entry[field].strip():
                raise ShareDataError(f"{label}/{product}: missing {field!r}")
        identity = (entry["name"], entry["reference product"])
        if identity in identities:
            raise ShareDataError(f"{label}: duplicate activity identity {identity!r}")
        identities.add(identity)
        shares = entry.get("shares")
        if not isinstance(shares, dict) or set(shares) != {"primary", "secondary"}:
            raise ShareDataError(f"{label}/{product}: shares require primary and secondary mappings")
        normalized = {}
        for role in ("primary", "secondary"):
            curve = shares[role]
            if not isinstance(curve, dict) or not curve:
                raise ShareDataError(f"{label}/{product}/{role}: a nonempty year mapping is required")
            values = {}
            for year, share in curve.items():
                if isinstance(year, bool) or not str(year).isdigit() or not 1900 <= int(year) <= 2300:
                    raise ShareDataError(f"{label}/{product}/{role}: invalid share year {year!r}")
                year = int(year)
                if year in values:
                    raise ShareDataError(f"{label}/{product}/{role}: duplicate normalized share year {year}")
                values[year] = _number(share, f"{label}/{product}/{role}/{year}")
            if not {2020, 2050} <= set(values):
                raise ShareDataError(f"{label}/{product}/{role}: required share years 2020 and 2050 are missing")
            normalized[role] = dict(sorted(values.items()))
        if set(normalized["primary"]) != set(normalized["secondary"]):
            raise ShareDataError(f"{label}/{product}: primary and secondary year coverage differs")
        for year in normalized["primary"]:
            total = normalized["primary"][year] + normalized["secondary"][year]
            if not math.isclose(total, 1.0, rel_tol=0, abs_tol=TOLERANCE):
                raise ShareDataError(f"{label}/{product}/{year}: primary + secondary = {total}, expected 1")
        result[product] = {**deepcopy(entry), "shares": normalized}
    return result


def _read_csv(path, required):
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        headers = reader.fieldnames or []
        if len(headers) != len(set(headers)) or not set(required) <= set(headers):
            raise ShareDataError(f"{path}: missing/duplicate CSV columns; required={list(required)}")
        rows = list(reader)
    if not rows:
        raise ShareDataError(f"{path}: CSV table is empty")
    if any(None in row for row in rows):
        raise ShareDataError(f"{path}: a row has more values than its header")
    return rows


def _inputs(baseline, evidence_csv, mapping_csv):
    columns = ["family", *(f"secondary_share_2050_{case}" for case in SCENARIOS),
               "basis_type", "confidence", "source_short", "source_url", "notes"]
    evidence = {}
    for row in _read_csv(evidence_csv, columns):
        family = (row.get("family") or "").strip()
        if not family or family in evidence:
            raise ShareDataError(f"{evidence_csv}: empty/duplicate family {family!r}")
        evidence[family] = {case: _number(row[f"secondary_share_2050_{case}"], f"{family}/{case}") for case in SCENARIOS}
        evidence[family].update({field: row.get(field) or "" for field in columns[4:]})
    mapping = {}
    for row in _read_csv(mapping_csv, ("product", "family")):
        product, family = (row.get("product") or "").strip(), (row.get("family") or "").strip()
        if not product or product in mapping:
            raise ShareDataError(f"{mapping_csv}: empty/duplicate product mapping {product!r}")
        if family not in evidence:
            raise ShareDataError(f"{mapping_csv}: product {product!r} uses missing evidence family {family!r}")
        mapping[product] = family
    if set(mapping) != set(baseline):
        raise ShareDataError(f"{mapping_csv}: coverage differs; missing={sorted(set(baseline)-set(mapping))[:8]}, extra={sorted(set(mapping)-set(baseline))[:8]}")
    return evidence, mapping


def _generated_cases(baseline, evidence, mapping):
    result = {}
    for case in SCENARIOS:
        scenario = deepcopy(baseline)
        for product, entry in scenario.items():
            secondary = evidence[mapping[product]][case]
            entry["shares"]["secondary"][2050] = secondary
            entry["shares"]["primary"][2050] = 1.0 - secondary
        result[case] = validate_share_mapping(scenario, label=f"generated/{case}")
    return result


def _current_output(path, baseline, *, force=False, enforce_identity=True):
    if not path.is_file():
        return None
    try:
        raw = load_yaml(path)
        if raw in (None, {}):
            return None
        result = validate_share_mapping(raw, label=str(path), expected_products=baseline if enforce_identity else None)
        if enforce_identity:
            for product in baseline:
                if (result[product]["name"], result[product]["reference product"]) != (baseline[product]["name"], baseline[product]["reference product"]):
                    raise ShareDataError(f"{path}/{product}: activity identity differs from the baseline source")
        return result
    except ShareDataError:
        if force:
            return None
        raise


def _publish(payloads, expected_inputs, expected_targets=None):
    temporary = []
    try:
        for path, payload in payloads.items():
            path = _safe_output(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temp_path = tempfile.mkstemp(prefix=".metal-shares-", suffix=".tmp", dir=path.parent)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
            temporary.append((Path(temp_path), path))
        if any(_file_state(path) != expected for path, expected in expected_inputs.items()):
            raise ShareDataConflictError("A share input changed during generation; no outputs were published. Retry with stable inputs.")
        if any(_file_state(path) != expected for path, expected in (expected_targets or {}).items()):
            raise ShareDataConflictError("A scenario output/manifest changed during generation; no outputs were published. Review the concurrent edit and retry.")
        for temp_path, path in temporary:
            os.replace(temp_path, path)
    finally:
        for temp_path, _ in temporary:
            if temp_path.exists():
                temp_path.unlink()


def ensure_share_outputs(*, baseline_yaml=DATA_DIR / "metal_primary_secondary_shares.yaml",
                         evidence_csv=DATA_DIR / "metal_2050_literature_evidence.csv",
                         mapping_csv=DATA_DIR / "metal_product_family_mapping.csv",
                         output_dir=DATA_DIR, source="yaml", xlsx_path=None, force=False):
    """Ensure named outputs and hashes, preserving validated independent edits.

    Named YAMLs are authoritative after generation. Changed upstream inputs
    plus edited named outputs require explicit force. XLSX mode reads only;
    its extracted baseline YAML cannot independently override the workbook.
    """
    source = "xlsx" if source == "excel" else source
    if source not in {"yaml", "xlsx"}:
        raise ShareDataError("source must be 'yaml' or 'xlsx'")
    baseline_yaml, evidence_csv, mapping_csv = map(_resolved, (baseline_yaml, evidence_csv, mapping_csv))
    output_dir = _safe_output(output_dir)
    manifest_path = output_dir / MANIFEST_FILE
    input_paths = ((_resolved(xlsx_path or baseline_yaml.with_suffix(".xlsx")), evidence_csv, mapping_csv)
                   if source == "xlsx" else (baseline_yaml, evidence_csv, mapping_csv))
    input_hashes = {str(path): _hash(path) for path in input_paths}
    observed_targets = {output_dir / filename: _file_state(output_dir / filename)
                        for filename in (*SCENARIO_FILES.values(), ALIAS_FILE, COMBINED_FILE, MANIFEST_FILE)}
    if source == "xlsx":
        observed_targets[baseline_yaml] = _file_state(baseline_yaml)
    manifest = None
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
                raise ShareDataError("Unsupported share-generation manifest schema")
            if not all(isinstance(manifest.get(field), dict) for field in
                       ("input_sha256", "output_sha256", "generated_output_sha256")):
                raise ShareDataError("Manifest input/output hashes are incomplete")
        except (ValueError, ShareDataError) as error:
            if not force:
                raise ShareDataError(f"Invalid manifest {manifest_path}; explicitly force regeneration: {error}") from error
            manifest = None
    extraction, extraction_hash, baseline_before_hash = None, None, None
    if source == "xlsx":
        xlsx_path = _resolved(xlsx_path or baseline_yaml.with_suffix(".xlsx"))
        if __package__:
            from .metals_primary_vs_secondary import read_excel_shares
        else:
            from metals_primary_vs_secondary import read_excel_shares
        baseline = read_excel_shares(xlsx_path)
        current = None
        if baseline_yaml.is_file():
            baseline_before_hash = observed_targets[baseline_yaml]
            try:
                raw_current = load_yaml(baseline_yaml)
                current = validate_share_mapping(raw_current, label=str(baseline_yaml)) if raw_current not in (None, {}) else None
            except ShareDataError:
                if not force:
                    raise
        prior_extraction = (manifest or {}).get("baseline_yaml_generated_sha256")
        if current is not None:
            modified = (_hash(baseline_yaml) != prior_extraction) if prior_extraction else current != baseline
            if modified and not force:
                raise ShareDataConflictError("The workbook's extracted baseline YAML was edited. Use source='yaml' to accept it, or force=True to re-extract the workbook.")
        workbook_same = ((manifest or {}).get("source") == "xlsx" and
                         (manifest or {}).get("input_sha256", {}).get(_manifest_key(xlsx_path, output_dir)) == _hash(xlsx_path))
        if current is None or not workbook_same or force:
            extraction = _yaml_bytes(baseline)
            extraction_hash = _bytes_hash(extraction)
        else:
            extraction_hash = _hash(baseline_yaml)
        input_paths = (xlsx_path, evidence_csv, mapping_csv)
    else:
        baseline = validate_share_mapping(load_yaml(baseline_yaml), label=str(baseline_yaml))
        input_paths = (baseline_yaml, evidence_csv, mapping_csv)
    portable_hashes = {_manifest_key(path, output_dir): input_hashes[str(path)] for path in input_paths}
    evidence, mapping = _inputs(baseline, evidence_csv, mapping_csv)
    generated = _generated_cases(baseline, evidence, mapping)
    source_changed = manifest is not None and (manifest.get("source") != source or manifest.get("generator_version") != GENERATOR_VERSION or manifest.get("input_sha256") != portable_hashes)
    expected_generated = dict((manifest or {}).get("generated_output_sha256", {}))
    actual, existing, manual = {}, {}, []
    for case, filename in SCENARIO_FILES.items():
        path = output_dir / filename
        if path in input_paths:
            raise ShareDataError(f"Scenario output collides with an upstream input: {path}")
        existing[case] = _current_output(path, baseline, force=force, enforce_identity=not source_changed)
        if existing[case] is not None:
            actual[filename] = _hash(path)
            expected = expected_generated.get(filename, _bytes_hash(_yaml_bytes(generated[case])))
            if actual[filename] != expected:
                manual.append(filename)
    if source_changed and manual and not force:
        raise ShareDataConflictError(f"Upstream inputs changed and named scenario outputs were edited: {manual}. Review/back up these assumptions, then force=True to regenerate explicitly.")
    payloads, selected, rebuilt = {}, {}, []
    for case, filename in SCENARIO_FILES.items():
        if force or source_changed or existing[case] is None:
            selected[case] = generated[case]
            payloads[output_dir / filename] = _yaml_bytes(selected[case])
            expected_generated[filename] = _bytes_hash(payloads[output_dir / filename])
            rebuilt.append(filename)
        else:
            selected[case] = existing[case]
            expected_generated.setdefault(filename, _bytes_hash(_yaml_bytes(generated[case])))
    if force or source_changed:
        manual = []
    combined = {"metadata": {"description": "Conservative, baseline and optimistic primary/secondary scenarios; named files are authoritative.",
                             "generator_version": GENERATOR_VERSION, "source": source, "source_sha256": portable_hashes,
                             "scenario_files": SCENARIO_FILES, "manual_outputs": manual},
                "evidence": evidence, "products": selected}
    derived = {output_dir / ALIAS_FILE: _yaml_bytes(selected["baseline"]),
               output_dir / COMBINED_FILE: _yaml_bytes(combined)}
    for path, payload in derived.items():
        if path in input_paths:
            raise ShareDataError(f"Derived output collides with an upstream input: {path}")
        if not path.is_file() or path.read_bytes() != payload:
            payloads[path] = payload
    if extraction is not None:
        payloads[_safe_output(baseline_yaml)] = extraction
    output_hashes = {filename: _bytes_hash(payloads[output_dir / filename]) if output_dir / filename in payloads else _hash(output_dir / filename)
                     for filename in (*SCENARIO_FILES.values(), ALIAS_FILE, COMBINED_FILE)}
    new_manifest = {"schema_version": 1, "generator_version": GENERATOR_VERSION, "source": source,
                    "input_sha256": portable_hashes, "output_sha256": output_hashes,
                    "generated_output_sha256": expected_generated, "manual_outputs": manual,
                    "scenario_files": SCENARIO_FILES, "baseline_alias": ALIAS_FILE,
                    "combined_file": COMBINED_FILE, "product_count": len(baseline), "family_count": len(evidence)}
    if extraction_hash is not None:
        new_manifest["baseline_yaml_generated_sha256"] = extraction_hash
    comparable_manifest = dict(manifest or {})
    comparable_manifest.pop("generated_at_utc", None)
    if comparable_manifest != new_manifest:
        new_manifest["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
        payloads[manifest_path] = json.dumps(new_manifest, indent=2).encode("utf-8")
    publication_inputs = dict(input_hashes)
    if source == "xlsx" and baseline_before_hash is not None:
        publication_inputs[str(baseline_yaml)] = baseline_before_hash
    _publish(payloads, publication_inputs, observed_targets)
    return {"status": "rebuilt" if rebuilt else "preserved_manual_outputs" if manual else "reused",
            "source": source, "product_count": len(baseline), "family_count": len(evidence),
            "manifest_path": str(manifest_path), "source_sha256": input_hashes,
            "output_sha256": output_hashes, "manual_outputs": manual, "rebuilt_outputs": rebuilt,
            "output_files": {case: str(output_dir / filename) for case, filename in SCENARIO_FILES.items()},
            "baseline_alias": str(output_dir / ALIAS_FILE), "combined_file": str(output_dir / COMBINED_FILE)}


def prepare_policy_share_data(policy_path, *, source="yaml", force=False):
    """Resolve the policy's share_data.directory and prepare its standard files."""
    policy_path = _resolved(policy_path)
    configuration = load_yaml(policy_path)
    share_data = configuration.get("share_data", {}) if isinstance(configuration, dict) else {}
    directory = share_data.get("directory")
    if directory is None:
        raise ShareDataError(f"{policy_path}: share_data.directory is required for automatic preparation")
    directory = _resolved(policy_path.parent / directory)
    return ensure_share_outputs(baseline_yaml=directory / "metal_primary_secondary_shares.yaml",
                                evidence_csv=directory / "metal_2050_literature_evidence.csv",
                                mapping_csv=directory / "metal_product_family_mapping.csv",
                                output_dir=directory, source=source, force=force)
