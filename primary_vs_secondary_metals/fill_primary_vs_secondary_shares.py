#!/usr/bin/env python3
"""Repo-only share generation CLI; importing this module never writes files."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if __package__:
    from .share_files import DATA_DIR, ensure_share_outputs, prepare_policy_share_data
else:
    from share_files import DATA_DIR, ensure_share_outputs, prepare_policy_share_data


def build_outputs(baseline_yaml, evidence_csv, mapping_csv, output_dir, *, force=False, source="yaml"):
    """Backward-compatible repo-owned entry point; never writes site-packages."""
    return ensure_share_outputs(baseline_yaml=baseline_yaml, evidence_csv=evidence_csv,
                                mapping_csv=mapping_csv, output_dir=output_dir, source=source, force=force)


def _arguments(argv):
    if argv is not None:
        return argv
    values = list(sys.argv[1:])
    if "ipykernel_launcher" not in Path(sys.argv[0]).name:
        return values
    result, index = [], 0
    while index < len(values):
        if values[index].startswith("--f="):
            index += 1
        elif values[index] == "--f":
            index += 2
        else:
            result.append(values[index])
            index += 1
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--baseline-yaml", type=Path, default=DATA_DIR / "metal_primary_secondary_shares.yaml")
    parser.add_argument("--evidence-csv", type=Path, default=DATA_DIR / "metal_2050_literature_evidence.csv")
    parser.add_argument("--mapping-csv", type=Path, default=DATA_DIR / "metal_product_family_mapping.csv")
    parser.add_argument("--output-dir", type=Path, default=DATA_DIR)
    parser.add_argument("--source", choices=("yaml", "xlsx"), default="yaml")
    parser.add_argument("--xlsx-path", type=Path)
    parser.add_argument("--force", action="store_true", help="Explicitly replace conflicting generated assumptions after reviewing them.")
    args = parser.parse_args(_arguments(argv))
    summary = ensure_share_outputs(**vars(args))
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    main()
