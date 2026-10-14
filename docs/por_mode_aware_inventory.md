# Mode-aware PoR propylene and aromatic inventories

The PoR datapackage uses `inventories/lci-itom_por.xlsx`. The initial integration added 21 unit-process datasets: eight propylene and 13 raw-aromatic route/mode combinations. The MTO CO2 revision on 7 October 2026 updates the active propylene mode-2 inventory and adds a matching ethylene mode-2 inventory in this same workbook. `inventories/lci-itom_por-mode-aware-additions.xlsx` is the earlier review export and does not contain this later MTO revision. Use the main workbook when building the datapackage.

## Scenario mapping

`0_convert_ITOM_por_to_IAMC.py` continues to export the original technology-level production rows. For each pathway in `config_itom_por.yaml` whose variable ends in `|modeN`, it also exports an annualized production row from ITOM `production_volume.MODE_OF_OPERATION`. The converter writes both CL and OCE to the same combined CSV and checks, for every mapped product/technology/scenario, that the modes sum to the original aggregate within `1e-8 kt/yr`. A newly active operating mode without a configured pathway raises an error.

The additional mode rows cover propylene from retrofit MTA and retrofit naphtha cracking, and raw benzene, raw toluene and raw p-xylene from the active naphtha-cracker and MTA modes. The non-mode propylene pathways now include dedicated FCC, default naphtha cracker, MTO mode 2, and gasoil-cracker retrofit activities. Both ethylene and propylene from `MTO_with_loop` now select custom ITOM mode-2 inventories, with the same allocation and CO2 boundary. The FT-distillation ethylene and propylene pathways were revised on 8 October 2026 to use custom mode-1 inventories with a user-approved, provisional natural-gas FT light-gas proxy instead of high-temperature coal FT. ITOM still does not provide their input/utility/emission coefficients. See [LT FT inventories](por_ft_lt_inventory.md) for the source-group energy allocation, within-light-gas mass split and remaining limitations.

The raw-aromatic inventories stop before extraction. Their `benzene`, `toluene, liquid`, or `p-xylene` reference products are compatibility equivalents for the current market-construction method, not claims that the material has already been purified. The extraction markets add electricity and steam/high-temperature heat once, and the purified markets take extraction output plus explicitly purified imports. The older atmospheric-distillation raw-aromatic route still uses an existing BTX proxy; its purification boundary should be reviewed separately for possible double counting.

## Inventory construction and limitations

Terminal p-xylene is now supplied directly as purified material, per the ITOM
team clarification. Its original raw-labelled source rows remain for audit.
Two derived local-supply rows reconcile terminal origin and non-terminal
extraction, including re-exports, so moving the terminal does not double-count
the original extraction output. Domestic/raw European supply retains the
additional 1:1 extraction step. See [terminal p-xylene purity](por_px_terminal_purity.md).

The new process amounts use the ITOM input/output coefficients and the active CL/OCE technology modes. Material-feed burdens are mass-allocated over non-CO₂ hydrocarbon outputs; auxiliaries and direct flue CO₂ are allocated over intended chemical outputs, excluding methane and heavy fuel oil. For MTO mode 2, all represented joint-process exchanges use the same four-product pool: ethylene, propylene, ethane and propane. The inventory comments and sources record individual conversions and proxies. Output steam and high-temperature heat from MTA are not treated as purchased inputs or automatically credited. `CO2_allowance` is bookkeeping, not a direct emission.

## MTO captured CO2 and storage

The ITOM clarification distinguishes actual atmospheric `flue_gas_CO2` from `stored_CO2`, which in these source files means captured and compressed syngas CO2 ready for transport. The MTO ethylene and propylene inventories keep the actual flue emission and add a positive technosphere demand for `carbon dioxide compression, transport and storage`, reference product `carbon dioxide, stored`, location `RER`. They assume all captured CO2 is transported and durably stored. This destination is a provisional LCA assumption, not a confirmed ITOM storage result.

Each kilogram of allocated MTO product has 0.363765800 kg of direct flue CO2 and 0.111299861 kg of storage-service demand. No negative CO2 exchange is added. The legacy standalone MTO activity's former -1 kg fossil CO2 exchange is set to zero and `FE_CC_MTO_with_loop` remains disabled. Do not add separate demand for that activity when storage is already embedded in the product inventories.

The requested premise service includes initial compression as well as transport and injection. ITOM already includes capture/compression energy. The full service is retained as a conservative proxy because its 0.118 kWh/kg operational electricity is not split into initial compression and injection. This overlap remains unresolved; no unsupported energy deduction is made. See [MTO CO2 accounting](por_mto_co2_accounting.md) for source coefficients, allocation, checks and rerun instructions.

These are provisional process LCIs, not independently measured PoR plant inventories. In particular:

- Dedicated FCC propylene uses a literature-based coke/emission estimate because ITOM omits the feed and direct-emission factors.
- The gasoil-cracker retrofit uses a waste-polyolefin hydrothermal-treatment distillate as an upstream proxy; it is not demonstrated to be equivalent to the modeled waste-oil route.
- FT-naphtha mode currently uses a fossil naphtha supplier as a placeholder. Its climate results should not be interpreted as a validated FT-naphtha supply chain.
- Direct `Carbon dioxide, fossil` in routes with methanol or FT-derived carbon is provisional until carbon origin and accounting are resolved.
- Steam, heat, electricity, methanol, hydrogen and CO₂ suppliers are linked to available ecoinvent/custom activities. Their future mixes and geography still depend on premise transformations and the scenario setup; the new unit LCIs alone do not establish an ITOM-specific future energy supply mix.

## Validation and rerun

1. Run `python 0_convert_ITOM_por_to_IAMC.py` from the repo root. This rewrites the three ignored PoR scenario CSVs.
2. Run `python -m unittest discover -s tests -p test_por_mode_aware.py -v`.
3. Rebuild `remind-transience_por.zip` with `1_export_packages.ipynb`; an existing ZIP or Brightway database contains the *previous* inventories until rebuilt.
4. Recalculate the CL and OCE results with `2_calc_impacts.ipynb` and compare market shares, contribution analysis and carbon accounting against the previous package.

The local checks on 7 October 2026 confirmed 34 added mode rows (17 pathway variables in each scenario), exact agreement with the previously proposed mode-row values, both scenario labels, 21 unique added activities, successful Brightway Excel import, and exact technosphere supplier matches in the local ecoinvent 3.12/custom inventory set. These checks do **not** substitute for rebuilding all prospective databases and recalculating the LCA.
