"""Reconcile purified terminal p-xylene with ITOM's legacy raw-product labels.

Raw source rows remain immutable. Derived market quantities split the original
local extraction output by proportional mixing of the raw-labelled supply pool.
This follows the supplied ITOM purity clarification, not an inferred raw label.
"""
from __future__ import annotations

import math
import pandas as pd

TERMINAL_RAW = "Intermediate Product|raw_para_xylene|para_xylene_terminal_BAT"
EXTRACTION_GROSS = "Intermediate Product|para_xylene|para_xylene_extraction"
TERMINAL_PURE_LOCAL = "Intermediate Product|para_xylene|para_xylene_terminal_BAT|purified_local_supply"
EXTRACTION_NONTERMINAL = "Intermediate Product|para_xylene|para_xylene_extraction|nonterminal_supply"
DERIVED_VARIABLES = (TERMINAL_PURE_LOCAL, EXTRACTION_NONTERMINAL)
METADATA = ["model", "scenario", "region", "variables", "unit"]
CONTEXT = METADATA[:3]
TOLERANCE = 1e-8  # kt/year; matches the converter's existing mode reconciliation


def _number(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label}: boolean is not a physical p-xylene flow")
    try:
        number = float(value)
    except (ValueError, TypeError) as error:
        raise ValueError(f"{label}: missing/non-numeric p-xylene flow") from error
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label}: p-xylene physical flows must be finite and nonnegative")
    return number


def verify_px_extraction_yield(input_factors, output_factors):
    """Require the supplied 1:1 extraction definition; no inferred efficiency."""
    observed = []
    for table, product, value in ((input_factors, "raw_para_xylene", "InputActivityRatio"),
                                  (output_factors, "para_xylene", "OutputActivityRatio")):
        selected = table.loc[table.TECHNOLOGY.eq("para_xylene_extraction") & table.PRODUCT.eq(product)].copy()
        if selected.empty:
            raise ValueError(f"Missing para_xylene_extraction {product} coefficients")
        keys = ["REGION", "MODE_OF_OPERATION", "YEAR"]
        selected["MODE_OF_OPERATION"] = pd.to_numeric(selected.MODE_OF_OPERATION, errors="raise")
        if selected.duplicated(keys).any():
            raise ValueError("Duplicate p-xylene extraction coefficients")
        ratios = pd.to_numeric(selected[value], errors="raise")
        if not ratios.map(lambda q: math.isfinite(float(q)) and math.isclose(float(q), 1, rel_tol=0, abs_tol=1e-12)).all():
            raise ValueError("Terminal purity correction requires verified 1:1 p-xylene extraction")
        observed.append(set(map(tuple, selected[keys].itertuples(index=False, name=None))))
    if observed[0] != observed[1]:
        raise ValueError("P-xylene extraction input/output coefficient coverage differs")


def validate_px_source_flows(table, amount_column):
    """Check original material rows before the legacy converter coerces NaNs."""
    selected = table.loc[table.PRODUCT.isin(["raw_para_xylene", "para_xylene"])]
    for amount in selected[amount_column]:
        _number(amount, "original p-xylene " + amount_column)


def annual_raw_px_use(use_volume, *, model, scenario, region_map, time_step_years=5):
    """Read actual process use, including terminal mode-2 exports, for balance."""
    if not math.isfinite(time_step_years) or time_step_years <= 0:
        raise ValueError("P-xylene time-step duration must be positive and finite")
    required = {"LOCATION", "TECHNOLOGY", "PRODUCT", "YEAR", "LocalUseByMode"}
    if not required <= set(use_volume.columns):
        raise ValueError(f"Missing raw p-xylene use columns: {sorted(required - set(use_volume.columns))}")
    selected = use_volume.loc[use_volume.PRODUCT.eq("raw_para_xylene")].copy()
    rows = []
    for record in selected.to_dict("records"):
        total = _number(record["LocalUseByMode"], "raw p-xylene use") / time_step_years
        rows.append({"model": model, "scenario": scenario,
                     "region": region_map.get(record["LOCATION"], record["LOCATION"]),
                     "year": int(record["YEAR"]), "technology": record["TECHNOLOGY"],
                     "amount": total})
    return pd.DataFrame(rows, columns=CONTEXT + ["year", "technology", "amount"])


def reconcile_px_terminal_purity(iamc, raw_use):
    """Return source rows plus two audited, annual local-market supply rows.

    For supply S = terminal T + other raw production + raw EU imports, and
    original local extraction E, local terminal supply is E*T/S. Only E*(1-T/S)
    needs purification. E is preserved: no gross import is added on top of it.
    Terminal material leaving the local pool is T-E*T/S, never treated as local
    purified supply. Other uses/exports are checked against the full raw ledger.
    """
    missing = set(METADATA) - set(iamc.columns)
    if missing:
        raise ValueError(f"Missing IAMC metadata for p-xylene reconciliation: {sorted(missing)}")
    years = sorted((column for column in iamc.columns if str(column).isdigit()), key=int)
    if not years:
        raise ValueError("No years in p-xylene reconciliation")
    if iamc[METADATA].isna().any().any():
        raise ValueError("Missing p-xylene IAMC identities/units")
    if not set(CONTEXT + ["year", "technology", "amount"]) <= set(raw_use.columns):
        raise ValueError("Missing p-xylene physical-use ledger columns")
    source = iamc.loc[~iamc.variables.isin(DERIVED_VARIABLES)].copy()
    native_pure_terminal = source.loc[source.variables.eq("Intermediate Product|para_xylene|para_xylene_terminal_BAT")]
    if not native_pure_terminal.empty and any(_number(value, "native purified terminal") > 0
                                            for value in native_pure_terminal[years].to_numpy().flat):
        raise ValueError("ITOM already labels terminal p-xylene purified; revise the legacy-label correction before converting")
    additions, audit = [], []
    for context, group in source.groupby(CONTEXT, sort=False, dropna=False):
        parts = group.variables.str.split("|")
        raw_production = parts.str.len().eq(3) & parts.str[1].eq("raw_para_xylene") & ~group.variables.str.startswith("Trade|")
        # Transfer/hub/demand technologies are not additional physical supply.
        raw_production &= ~parts.str[2].fillna("").str.startswith(("pipeline_transfert_", "transport_hub_", "demand_of_"))
        chosen = group.loc[raw_production | group.variables.isin([
            TERMINAL_RAW, EXTRACTION_GROSS, "Trade|To PoR|raw_para_xylene", "Trade|From PoR|raw_para_xylene"])]
        if chosen.empty:
            continue
        if chosen.duplicated(["variables"]).any():
            raise ValueError(f"Duplicate p-xylene source rows for {context}")
        if not chosen.unit.eq("kt/yr").all():
            raise ValueError(f"P-xylene source quantities must use kt/yr: {context}")
        amounts = {variable: {} for variable in DERIVED_VARIABLES}
        uses = raw_use
        for field, value in zip(CONTEXT, context):
            uses = uses.loc[uses[field].eq(value)]
        for year in years:
            label = f"{context}/{year}"
            def flow(variable, *, export=False):
                row = chosen.loc[chosen.variables.eq(variable)]
                if row.empty:
                    return 0.0
                value = row.iloc[0][year]
                return _number(-float(value) if export else value, label + "/" + variable)
            terminal = flow(TERMINAL_RAW)
            extraction = flow(EXTRACTION_GROSS)
            imported_raw = flow("Trade|To PoR|raw_para_xylene")
            exported_raw = flow("Trade|From PoR|raw_para_xylene", export=True)
            domestic = sum(_number(row[year], label + "/" + row["variables"])
                           for row in chosen.loc[raw_production & ~group.variables.eq(TERMINAL_RAW)].to_dict("records"))
            pool = terminal + domestic + imported_raw
            local_uses = uses.loc[uses.year.eq(int(year))]
            used_extraction = sum(_number(q, label + "/extraction use") for q in
                                  local_uses.loc[local_uses.technology.eq("para_xylene_extraction"), "amount"])
            used_other = sum(_number(q, label + "/other raw use") for q in
                            local_uses.loc[~local_uses.technology.eq("para_xylene_extraction"), "amount"])
            if not math.isclose(used_extraction, extraction, rel_tol=0, abs_tol=TOLERANCE):
                raise ValueError(f"{label}: p-xylene extraction use/output does not reconcile 1:1")
            if not math.isclose(pool, extraction + used_other + exported_raw, rel_tol=0, abs_tol=TOLERANCE):
                raise ValueError(f"{label}: raw p-xylene supply/use/export balance does not close")
            if extraction > pool + TOLERANCE:
                raise ValueError(f"{label}: extraction exceeds raw-labelled supply")
            fraction = terminal / pool if pool else 0.0
            purified_local = extraction * fraction
            remaining_extraction = extraction - purified_local
            terminal_other = terminal - purified_local
            if min(purified_local, remaining_extraction, terminal_other) < -TOLERANCE:
                raise ValueError(f"{label}: negative derived p-xylene supply")
            amounts[TERMINAL_PURE_LOCAL][year] = max(0.0, purified_local)
            amounts[EXTRACTION_NONTERMINAL][year] = max(0.0, remaining_extraction)
            audit.append({**dict(zip(CONTEXT, context)), "year": int(year), "unit": "kt/yr",
                          "method": "proportional_pool_origin_allocation", "purity_basis": "ITOM team clarification: terminal imports purified",
                          "raw_labelled_pool": pool, "terminal_gross": terminal,
                          "other_raw_production": domestic, "raw_eu_imports": imported_raw,
                          "raw_trade_exports": exported_raw, "other_raw_use_including_terminal_exports": used_other,
                          "extraction_original": extraction, "terminal_local_purified": purified_local,
                          "extraction_nonterminal": remaining_extraction,
                          "terminal_other_use_or_export": max(0.0, terminal_other),
                          "local_supply_difference": purified_local + remaining_extraction - extraction})
        for variable, values in amounts.items():
            additions.append({**dict(zip(CONTEXT, context)), "variables": variable, "unit": "kt/yr", **values})
    derived = pd.DataFrame(additions, columns=iamc.columns)
    old_derived = iamc.loc[iamc.variables.isin(DERIVED_VARIABLES)]
    if not old_derived.empty:
        for _, row in old_derived.iterrows():
            matching = derived
            for field in METADATA:
                matching = matching.loc[matching[field].eq(row[field])]
            if len(matching) != 1 or any(not math.isclose(_number(row[year], "existing derived supply"),
                    float(matching.iloc[0][year]), rel_tol=0, abs_tol=TOLERANCE) for year in years):
                raise ValueError("Conflicting pre-existing derived p-xylene rows; regenerate from original ITOM inputs")
        if old_derived.duplicated(METADATA).any():
            raise ValueError("Duplicate derived p-xylene rows")
    return pd.concat([source, derived], ignore_index=True, sort=False), audit
