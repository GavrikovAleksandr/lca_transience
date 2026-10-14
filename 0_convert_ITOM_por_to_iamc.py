from __future__ import annotations

import re
import json
import hashlib
from pathlib import Path

import pandas as pd
from por_px_terminal_purity import (
    annual_raw_px_use, reconcile_px_terminal_purity, verify_px_extraction_yield,
    validate_px_source_flows,
)


# ----------------------------
# Configuration
# ----------------------------

ENERGY_PRODUCTS_TJ = {"electricity", "steam", "HT_heat"}

# Aggregate only storage outputs, not capture, allowances, or avoided emissions.
# Family matching includes the different technology variants in CL and OCE.
CCS_AGGREGATES = {
    "CO2 Related|stored_CO2|waste_pyrolysis": {
        "prefixes": ("plastic_waste_pyrolysis_",),
        "technologies": (),
    },
    "CO2 Related|stored_CO2|gasification": {
        "prefixes": ("gasification_of_plastic_waste_",),
        "technologies": ("gasification_of_cracker_HFO",),
    },
}

DEFAULT_REGION_MAP = {
    "Rotterdam": "NL",
}

OUTPUT_PRODUCTS = {
    "HDPE", "LDPE", "LLDPE", "PVC", "polyvinyl_chloride",
    "PS", "PS-E", "PET", "PC", "PA 6", "PA 6.6",
    "ABS", "PMMA", "PBR", "SBR", "PUR", "polyols", "TDI", "MDI",
    "nylon_6_6", "PLA",
    "acrylonitrile", "adipic_acid", "ethylene_oxide",
    "isopropanol", "polypropylene",
}

SKIP_PRODUCTS = {
    "CO2_allowance",
    "avoided_incineration_CO2",
    "stored_CO2",
    "unavoidable_CO2",
    "flue_gas_CO2",
    "uncompressed_CO2",
    "syngas_CO",
    "syngas_CO2",
    "syngas_H2",
}


# ----------------------------
# Helpers
# ----------------------------

def infer_unit_from_product(product: str) -> str:
    return "TJ/yr" if str(product) in ENERGY_PRODUCTS_TJ else "kt/yr"


def build_variable(
    product: str,
    technology: str,
    mode: str | int | None,
    location: str | None,
    include_mode: bool,
    include_location_in_variable: bool,
    product_type_map: dict[str, str],
    output_products: set[str] | None = None,
) -> str:

    ptype = (product_type_map.get(product) or "").strip()

    if ptype == "outputs":
        category = "Final Product"
    elif ptype == "intermediates, by-products":
        category = "Intermediate Product_by_product"
    elif ptype == "HVC":
        category = "Intermediate Product"
    elif ptype == "material input":
        category = "Material Input"
    elif ptype == "energy input":
        category = "Energy Input"
    elif ptype == "CO2-related":
        category = "CO2 Related"
    else:
        category = (
            "Final Product"
            if output_products and product in output_products
            else "Intermediate Product"
        )

    base = f"{category}|{product}|{technology}"

    if include_mode and mode is not None:
        # Keep a stable, machine-readable suffix matching config_itom_por.yaml.
        mode_number = int(mode)
        if float(mode) != mode_number:
            raise ValueError(f"Non-integer operating mode: {mode}")
        base += f"|mode{mode_number}"

    if include_location_in_variable and location is not None:
        base += f"|{location}"

    return base


# ----------------------------
# Production IAMC export
# ----------------------------

def to_iamc_production_volume(
    df: pd.DataFrame,
    product_type_map: dict[str, str],
    value_col: str = "LocalProductionByMode",
    location_col: str = "LOCATION",
    tech_col: str = "TECHNOLOGY",
    product_col: str = "PRODUCT",
    mode_col: str = "MODE_OF_OPERATION",
    year_col: str = "YEAR",
    model: str = "Petchem",
    scenario: str = "Carbon Looping (CL)",
    run_id: str | None = "Petchem_CL_240826_11",
    region_map: dict[str, str] | None = None,
    annualize_time_step_years: int = 5,
    include_mode: bool = True,
    include_location_in_variable: bool = False,
) -> pd.DataFrame:

    region_map = region_map or DEFAULT_REGION_MAP

    required = {location_col, tech_col, product_col, year_col, value_col}
    if include_mode:
        required.add(mode_col)

    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required production columns: {sorted(missing)}")

    d = df.copy()
    d[year_col] = pd.to_numeric(d[year_col], errors="raise").astype(int)
    d[value_col] = pd.to_numeric(d[value_col], errors="coerce")

    if annualize_time_step_years and annualize_time_step_years != 1:
        d[value_col] = d[value_col] / float(annualize_time_step_years)

    d["region"] = d[location_col].map(region_map).fillna(d[location_col])
    d["unit"] = d[product_col].apply(infer_unit_from_product)

    d["variables"] = d.apply(
        lambda r: build_variable(
            product=r[product_col],
            technology=r[tech_col],
            mode=r[mode_col] if include_mode else None,
            location=r[location_col],
            include_mode=include_mode,
            include_location_in_variable=include_location_in_variable,
            product_type_map=product_type_map,
            output_products=OUTPUT_PRODUCTS,
        ),
        axis=1,
    )

    d["model"] = model
    d["scenario"] = f"{scenario}|{run_id}" if run_id else scenario

    iamc = (
        d.pivot_table(
            index=["model", "scenario", "region", "variables", "unit"],
            columns=year_col,
            values=value_col,
            aggfunc="sum",
        )
        .reset_index()
    )

    year_cols = sorted([c for c in iamc.columns if isinstance(c, int)])
    return iamc[["model", "scenario", "region", "variables", "unit"] + year_cols]


# ----------------------------
# Trade / transport IAMC export
# ----------------------------

def to_iamc_trade_volume(
    df: pd.DataFrame,
    value_col: str = "Transport",
    product_col: str = "PRODUCT",
    year_col: str = "YEAR",
    model: str = "Petchem",
    scenario: str = "Carbon Looping (CL)",
    run_id: str | None = "Petchem_CL_240826_11",
    region: str = "NL",
    annualize_time_step_years: int = 5,
    trade_direction: str = "To PoR",
) -> pd.DataFrame:

    required = {product_col, year_col, value_col}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required trade columns: {sorted(missing)}")

    d = df.copy()
    d[year_col] = pd.to_numeric(d[year_col], errors="raise").astype(int)
    d[value_col] = pd.to_numeric(d[value_col], errors="coerce")

    # Exports from PoR are negative
    if trade_direction.lower() == "from por":
        d[value_col] *= -1

    if annualize_time_step_years and annualize_time_step_years != 1:
        d[value_col] = d[value_col] / float(annualize_time_step_years)

    d["model"] = model
    d["scenario"] = f"{scenario}|{run_id}" if run_id else scenario
    d["region"] = region
    d["unit"] = d[product_col].apply(infer_unit_from_product)

    # Important:
    # We only keep product-level trade variables.
    # LOCATION_1, LOCATION_2 and TRANSPORTMODE are intentionally excluded,
    # so all routes and carriers are summed together.
    d["variables"] = (
        "Trade|"
        + trade_direction
        + "|"
        + d[product_col].astype(str)
    )

    iamc_trade = (
        d.pivot_table(
            index=["model", "scenario", "region", "variables", "unit"],
            columns=year_col,
            values=value_col,
            aggfunc="sum",
        )
        .reset_index()
    )

    year_cols = sorted([c for c in iamc_trade.columns if isinstance(c, int)])
    return iamc_trade[["model", "scenario", "region", "variables", "unit"] + year_cols]

def add_ccs_aggregates(iamc: pd.DataFrame) -> pd.DataFrame:
    """Append annual storage totals per model/scenario/region; retain source rows.

    Inputs have already been annualized by the production converter. Never
    divide these totals by five again. Replacing previous aggregate rows makes
    repeated calls idempotent and prevents aggregates being counted as sources.
    """
    metadata = ["model", "scenario", "region"]
    years = [column for column in iamc.columns if str(column).isdigit()]
    if iamc.empty or not years:
        raise ValueError("Cannot aggregate CCS without scenario data and years.")
    if iamc[metadata + ["variables", "unit"]].isna().any().any():
        raise ValueError("Missing IAMC metadata in CCS aggregation.")

    detailed = iamc.loc[~iamc["variables"].isin(CCS_AGGREGATES)].copy()
    parts = detailed["variables"].astype(str).str.split("|")
    products = parts.str[1]
    technologies = parts.str[2].fillna("")
    additions = []

    for variable, selection in CCS_AGGREGATES.items():
        selected = products.eq("stored_CO2") & (
            technologies.str.startswith(selection["prefixes"])
            | technologies.isin(selection["technologies"])
        )
        source = detailed.loc[selected].copy()
        if not source.empty:
            if (parts.loc[selected].str.len().ne(3).any()
                    or parts.loc[selected].str[0].ne("CO2 Related").any()):
                raise ValueError(
                    f"{variable}: fix classification/mode aggregation before summing."
                )
            if source["unit"].ne("kt/yr").any():
                raise ValueError(f"{variable}: storage sources must use kt/yr.")
            if source.duplicated(metadata + ["variables"]).any():
                raise ValueError(f"{variable}: duplicate storage source rows.")
            source[years] = source[years].apply(pd.to_numeric, errors="raise")
            if source[years].isna().any().any():
                raise ValueError(f"{variable}: missing storage quantities.")

        totals = source.groupby(metadata, sort=False)[years].sum()
        contexts = detailed[metadata].drop_duplicates().itertuples(index=False, name=None)
        for context in contexts:
            values = totals.loc[context].to_dict() if context in totals.index else {
                year: 0.0 for year in years
            }
            additions.append({
                **dict(zip(metadata, context)),
                "variables": variable,
                "unit": "kt/yr",
                **values,
            })

    return pd.concat([detailed, pd.DataFrame(additions)], ignore_index=True)


def ensure_required_fe_variables(iamc, config_path):
    import yaml

    with open(config_path, encoding="utf-8") as stream:
        config = yaml.safe_load(stream)

    required = {}
    for alias, settings in config["production pathways"].items():
        if not alias.startswith("FE_"):
            continue

        variable = settings["production volume"]["variable"]
        # FE_CC_ pathways are storage-service demands, not polymer outputs.
        expected_prefix = (
            "CO2 Related|stored_CO2|" if alias.startswith("FE_CC_")
            else "Final Product|"
        )
        if not isinstance(variable, str) or not variable.startswith(expected_prefix):
            raise ValueError(
                f"Unexpected FE mapping for {alias}: {variable}; "
                f"expected prefix {expected_prefix}"
            )

        required[alias] = variable

    if not required:
        raise ValueError(f"No FE_ production pathways found in {config_path}")

    metadata = ["model", "scenario", "region"]
    years = [column for column in iamc.columns if str(column).isdigit()]

    if iamc.empty or not years:
        raise ValueError(
            "Cannot complete FE variables without scenario data and year columns."
        )

    if iamc[metadata + ["variables", "unit"]].isna().any().any():
        raise ValueError(
            "Missing IAMC metadata; check conversion before adding zero rows."
        )

    additions = []

    for group_key, group in iamc.groupby(metadata, sort=False):
        context = dict(zip(metadata, group_key))

        for alias, variable in required.items():
            matches = group.loc[group["variables"] == variable]
            product = variable.split("|")[1]
            expected_unit = infer_unit_from_product(product)

            if not matches.empty:
                if len(matches) != 1 or matches["unit"].iloc[0] != expected_unit:
                    raise ValueError(
                        f"{context}: duplicate rows or incorrect unit for {alias}"
                    )
                continue

            # Do not mistake a classification/mode mismatch for zero production.
            suffix = variable.split("|", 1)[1]
            comparable = (
                group["variables"].astype(str).str.split("|", n=1).str[-1]
            )

            if (
                comparable.eq(suffix)
                | comparable.str.startswith(suffix + "|")
            ).any():
                raise ValueError(
                    f"{context}: {alias} exists under another variable format. "
                    "Fix its classification/mode aggregation instead of adding zero."
                )

            additions.append({
                **context,
                "variables": variable,
                "unit": expected_unit,
                **{year: 0.0 for year in years},
            })

            print(
                f"[{context['scenario']}/{context['region']}] "
                f"Added {alias}: {variable} = 0 for all exported years"
            )

    if additions:
        iamc = pd.concat(
            [iamc, pd.DataFrame(additions)],
            ignore_index=True,
        )

    return iamc


def add_mode_aware_rows(iamc: pd.DataFrame, mode_detail: pd.DataFrame,
                        config_path: Path) -> pd.DataFrame:
    """Add only the operating-mode variables used by the PoR configuration.

    The existing aggregated rows remain in the IAMC file for backward
    compatibility. Each mapped product/technology must reconcile to the sum
    of its modes; a newly active, unmapped mode is an error, not a silent loss.
    """
    import yaml

    with config_path.open(encoding="utf-8") as stream:
        pathways = yaml.safe_load(stream)["production pathways"]
    mapped = {
        settings["production volume"]["variable"]
        for settings in pathways.values()
        if re.search(r"\|mode\d+$", settings["production volume"]["variable"])
    }
    if not mapped:
        raise ValueError("No mode-aware pathways found in PoR configuration.")

    metadata = ["model", "scenario", "region"]
    years = [column for column in iamc.columns if str(column).isdigit()]
    mapped_bases = {variable.rsplit("|", 1)[0] for variable in mapped}
    mode_detail = mode_detail.copy()
    mode_detail[years] = mode_detail[years].fillna(0)
    all_related = mode_detail["variables"].str.rsplit("|", n=1).str[0].isin(mapped_bases)
    unexpected = set(mode_detail.loc[all_related, "variables"]) - mapped
    if unexpected:
        raise ValueError(f"Active operating modes lack PoR pathways: {sorted(unexpected)}")
    selected = mode_detail.loc[mode_detail["variables"].isin(mapped)].copy()
    if selected.duplicated(metadata + ["variables", "unit"]).any():
        raise ValueError("Duplicate mode-aware IAMC variables.")

    additions = []
    for context in iamc[metadata].drop_duplicates().itertuples(index=False, name=None):
        context_mask = selected[metadata].eq(context).all(axis=1)
        existing = set(selected.loc[context_mask, "variables"])
        for variable in mapped - existing:
            additions.append({
                **dict(zip(metadata, context)),
                "variables": variable,
                "unit": infer_unit_from_product(variable.split("|")[1]),
                **{year: 0.0 for year in years},
            })
    if additions:
        selected = pd.concat([selected, pd.DataFrame(additions)], ignore_index=True)

    for base in mapped_bases:
        mode_totals = selected.loc[
            selected["variables"].str.startswith(base + "|mode")
        ].groupby(metadata, sort=False)[years].sum()
        aggregate = iamc.loc[iamc["variables"].eq(base)].set_index(metadata)
        for context, totals in mode_totals.iterrows():
            expected = (aggregate.loc[context, years]
                        if context in aggregate.index else pd.Series(0.0, index=years))
            if isinstance(expected, pd.DataFrame):
                raise ValueError(f"Duplicate aggregate IAMC rows: {base}, {context}")
            if not (totals.astype(float) - expected.astype(float)).abs().le(1e-8).all():
                raise ValueError(f"Mode rows do not reconcile with {base}, {context}")

    return pd.concat([iamc, selected], ignore_index=True, sort=False)
# ----------------------------
# Main
# ----------------------------
# ----------------------------
# Main: convert both scenarios
# ----------------------------

SCENARIO_INPUTS = {
    "CL": "TRANSIENCE-WP8_ITOM-petchem_data_CL-scenario_2026-04-15.xlsx",
    "OCE": "TRANSIENCE-WP8_ITOM-petchem_data_OCE-scenario_2026-08-19.xlsx",
}


def main():
    # Resolve paths relative to this script.
    base_dir = Path(__file__).resolve().parent
    data_dir = base_dir / "scenario_data"

    # Check both inputs before starting.
    for scenario, filename in SCENARIO_INPUTS.items():
        path = data_dir / filename
        if not path.is_file():
            raise FileNotFoundError(f"{scenario} input not found: {path}")

    scenario_outputs = {}
    mode_outputs = []
    px_use_outputs = []

    for scenario, filename in SCENARIO_INPUTS.items():
        sheets = pd.read_excel(
            data_dir / filename,
            sheet_name=[
                "products",
                "production_volume",
                "transport_to_PoR",
                "transport_from_PoR",
                "use_volume",
                "input_factors",
                "output_factors",
            ],
        )

        # Use classifications from this scenario's own workbook.
        products = sheets["products"].dropna(subset=["PRODUCT"])
        product_type_map = dict(
            zip(products["PRODUCT"], products["type"].fillna(""))
        )

        common_options = {
            "model": "Petchem",
            "scenario": scenario,
            "run_id": None,  # Keep scenario labels exactly CL and OCE.
            "annualize_time_step_years": 5,
        }

        verify_px_extraction_yield(sheets["input_factors"], sheets["output_factors"])
        validate_px_source_flows(sheets["production_volume"], "LocalProductionByMode")
        validate_px_source_flows(sheets["transport_to_PoR"], "Transport")
        validate_px_source_flows(sheets["transport_from_PoR"], "Transport")
        px_use_outputs.append(annual_raw_px_use(
            sheets["use_volume"], model=common_options["model"], scenario=scenario,
            region_map=DEFAULT_REGION_MAP, time_step_years=5,
        ))

        production = to_iamc_production_volume(
            sheets["production_volume"],
            product_type_map=product_type_map,
            region_map=DEFAULT_REGION_MAP,
            include_mode=False,
            include_location_in_variable=False,
            **common_options,
        )

        mode_outputs.append(to_iamc_production_volume(
            sheets["production_volume"],
            product_type_map=product_type_map,
            region_map=DEFAULT_REGION_MAP,
            include_mode=True,
            include_location_in_variable=False,
            **common_options,
        ))

        imports = to_iamc_trade_volume(
            sheets["transport_to_PoR"],
            region="NL",
            trade_direction="To PoR",
            **common_options,
        )

        exports = to_iamc_trade_volume(
            sheets["transport_from_PoR"],
            region="NL",
            trade_direction="From PoR",
            **common_options,
        )

        iamc = pd.concat(
            [production, imports, exports],
            ignore_index=True,
            sort=False,
        )

        iamc["scenario"] = scenario
        scenario_outputs[scenario] = iamc

    # Assemble both scenarios before writing any outputs.
    combined = pd.concat(
        list(scenario_outputs.values()),
        ignore_index=True,
        sort=False,
    )

    metadata_columns = ["model", "scenario", "region", "variables", "unit"]
    year_columns = sorted(
        (column for column in combined.columns if str(column).isdigit()),
        key=int,
    )

    combined = combined[metadata_columns + year_columns]
    combined[year_columns] = combined[year_columns].fillna(0)

    # Preserve original ITOM rows. Terminal p-xylene is purified according to
    # the team clarification; only its local-used share bypasses extraction.
    combined, px_purity_audit = reconcile_px_terminal_purity(
        combined, pd.concat(px_use_outputs, ignore_index=True),
    )

    mode_detail = pd.concat(mode_outputs, ignore_index=True, sort=False)
    mode_detail = mode_detail[metadata_columns + year_columns]
    combined = add_mode_aware_rows(
        combined, mode_detail,
        base_dir / "configuration_file" / "config_itom_por.yaml",
    )

    # Sum annualized storage flows while retaining the detailed technology rows.
    combined = add_ccs_aggregates(combined)

    # Add missing final-product/storage pathways independently per scenario/region.
    combined = ensure_required_fe_variables(
        combined,
        base_dir / "configuration_file" / "config_itom_por.yaml",
    )

    combined = combined[metadata_columns + year_columns].sort_values(
        ["scenario", "region", "variables", "unit"]
    ).reset_index(drop=True)

    if set(combined["scenario"]) != set(SCENARIO_INPUTS):
        raise ValueError("Combined output must contain both CL and OCE.")

    # Write one CSV per scenario.
    for scenario in SCENARIO_INPUTS:
        output = data_dir / f"scenario_data_itom_por_{scenario}.csv"
        scenario_data = combined.loc[combined["scenario"] == scenario]
        scenario_data.to_csv(output, index=False)
        print(f"Wrote {output.name}: {len(scenario_data)} rows")

    # Write the combined CSV.
    output = data_dir / "scenario_data_itom_por.csv"
    combined.to_csv(output, index=False)
    print(f"Wrote {output.name}: {len(combined)} rows (CL + OCE)")

    audit_path = data_dir / "p_xylene_terminal_purity_audit.json"
    audit_path.write_text(json.dumps({
        "schema_version": 1,
        "source_sha256": {filename: hashlib.sha256((data_dir / filename).read_bytes()).hexdigest()
                          for filename in SCENARIO_INPUTS.values()},
        "records": px_purity_audit,
    }, indent=2), encoding="utf-8")
    print(f"Wrote {audit_path.name}: {len(px_purity_audit)} scenario-year balances")


if __name__ == "__main__":
    main()
