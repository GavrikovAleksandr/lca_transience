# Purified terminal p-xylene and local supply reconciliation

The ITOM team clarified that aromatic terminal imports are purified products,
ready for downstream use. The supplied 2026 workbooks nevertheless label the
`para_xylene_terminal_BAT` output `raw_para_xylene`. This implementation follows
the supplied purity clarification while retaining the original source rows.
Neither original ITOM workbook nor polymer production is changed.

## Local use and exports

Gross terminal throughput is not necessarily local supply. OCE 2045 reports
1,036.284734 kt/year terminal output, 18.465478 kt/year other raw production,
775 kt/year extraction output and 279.750211 kt/year raw-labelled exports.
Subtracting all terminal output would create negative extraction; adding it
directly to the purified market would double-count already represented supply.

For each model/scenario/region/year: `T` is gross terminal production; `D` is
other physical raw production; `U` is raw European imports; `S = T + D + U`
is the raw-labelled supply pool; and `E` is original local extraction output.

**Proportional origin mixing is an explicit LCA assumption**, consistent with
the previous market shares, not traced source-specific transport. Local terminal
supply is `E * T / S`; non-terminal extraction is `E - E * T / S`. A zero pool
gives zero local quantities. Already-purified European imports remain separate.
Terminal output not used locally is not added to the local market.

The converter checks `S = E + other raw process use + raw trade exports`.
Other use includes terminal mode-2 exports, which must not be subtracted twice.
It verifies 1:1 extraction coefficients and observed raw input/output quantities.
Missing, nonfinite, negative, duplicate, wrong-unit or unbalanced inputs fail
instead of silently adopting another allocation or clipping invalid extraction.

| Scenario/year | Gross terminal | Terminal local purified | Non-terminal extraction | Terminal other use/export |
|---|---:|---:|---:|---:|
| CL 2040 | 334.497808 | 334.497808 | 3.851960 | 0 |
| CL 2045 | 771.148040 | 771.148040 | 3.851960 | 0 |
| OCE 2040 | 186.714227 | 186.714227 | 140.755773 | 0 |
| OCE 2045 | 1,036.284734 | 761.432100 | 13.567900 | 274.852633 |

All quantities are kt/year. The two local outputs sum to original extraction
in every year; original gross terminal and export quantities remain unchanged.

## Coupling

The converter adds two annual derived rows, without overwriting source rows:

```
Intermediate Product|para_xylene|para_xylene_terminal_BAT|purified_local_supply
Intermediate Product|para_xylene|para_xylene_extraction|nonterminal_supply
```

The purified YAML market includes both, plus purified European imports. Raw
upgrading excludes the terminal but retains domestic/raw European providers.
Its existing 0.055555556 kWh electricity and 0.8 MJ heat per kg are unchanged;
they apply only to non-terminal extraction. No material-loss factor is added.

The new active wrapper `p-xylene, purified, imported from outside EU (por)` has
reference product `p-xylene` and one 1 kg input from `market for p-xylene`, RoW.
This provider already represents purified product. No extra purification,
transport distance or terminal energy is invented; existing market transport
remains. The import-protection policy retains its RoW supplier and prevents
replacement with the PoR market. The old raw-labelled wrapper is retained for
compatibility but is no longer selected by the active YAML.

## Audit and rebuilding

Run `0_convert_ITOM_por_to_IAMC.py` to regenerate the three existing scenario
CSVs and `scenario_data/p_xylene_terminal_purity_audit.json`. The audit records
all 14 scenario/year balances, source hashes, method, local supply split and
other use/export quantities. Repeated reconciliation is idempotent; contradictory
existing derived rows are rejected.

Run `python -m unittest tests.test_por_px_terminal_purity -v`, rebuild notebook 1,
then recalculate notebook 2. Existing ZIPs/databases/results are not regenerated
by this edit. The correction changes purification burdens, not local p-xylene
supply or polymer demand.
