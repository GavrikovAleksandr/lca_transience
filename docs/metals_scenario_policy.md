# Non-ferrous metals scenarios and endogenous steel

The export workflow now has a repo-owned non-ferrous metals policy, selected in
`1_export_packages.ipynb`. Both Brightway databases and the Pathways package
use the same policy after the normal `premise.update()` transformations.
There is no additional notebook to run and no need to copy scenario YAML into
`site-packages`.

The default is **native premise baseline**, as requested. It records supply
details but does not reweight any exchange. This means the baseline intentionally
preserves each installed premise version's behaviour, rather than making the two
versions mathematically identical.

**Steel remains endogenous to the selected IAM or EDM-I model.** This policy
never reweights steel supply, sets a steel recycling floor, or imposes an
exogenous steel primary/secondary scenario.

## Selecting a case

1. Start with a fresh kernel/builder. Select `NONFERROUS_METALS_SCENARIO` in
   the settings section of notebook 1. Keep it at `"baseline"` for native results.
2. Run the ordinary input/package preparation and the desired export block.
   `load_selected_metal_steel_policy()` prepares the live share files automatically,
   then loads the policy. Its post-update step is already attached to both export
   routes, before final supplier linking and export checks. No standalone
   metal-share generation or workbook-conversion step is required.
3. Review the printed summary and per-scenario reports in `export/metals_policy/`.
   Confirm the case, premise version, affected markets, effective recycled shares,
   and any skipped/unsupported recipes before accepting the export.
4. The baseline keeps the usual output names. Alternative cases receive an output
   `__metals-<case>` suffix identifying the selected non-ferrous policy, so they do not silently
   overwrite the baseline package/databases. Use the printed archive name when
   selecting a package in notebook 2; its calculation workflow is otherwise unchanged.
5. To compare policies, create a fresh builder for each case. Do not change the
   selectors on an already updated object, or apply a different policy to an
   already modified inventory. The guards deliberately reject that workflow.

Apply the same policy to CL and OCE for a clean ITOM scenario comparison. Change
the policy in a separate export run when studying metals/recycling sensitivity.
The policy does not silently make CL or OCE intrinsically more circular.

For example, the ordinary notebook settings are:

```python
NONFERROUS_METALS_SCENARIO = "baseline"
METALS_SHARE_SOURCE = "yaml"
FORCE_METALS_SHARE_REGENERATION = False
```

An increased-recycling sensitivity could instead select `"high_recycling"` for
non-ferrous metals; steel still follows the selected IAM or EDM-I model. The notebook loads
the shared configuration with
`prepare_policy_share_data(...)` followed by
`MetalSteelPolicy.from_file(METALS_POLICY_FILE, nonferrous_case=...)`.
There is no separate scenario-generating script to run.

The existing `MetalSteelPolicy` class and `metals_policy_workflow.py` module names
are retained for compatibility; their names do not enable a steel scenario.
Legacy steel arguments permit native baseline only, and non-native steel
selections are rejected rather than silently changing model outputs.

## Live share files and automatic preparation

Numeric assumptions are read from `primary_vs_secondary_metals/scenario_data/`,
not duplicated in the policy configuration. The policy YAML specifies case
selection, file/product mappings, supplier matching and safeguards. This separates
the editable supply assumptions from the implementation.

| Notebook selection | Live data source | Interpretation |
|---|---|---|
| Non-ferrous `baseline` | No numerical override | Preserve native premise; ignore the historical generated case named baseline. |
| Non-ferrous `reference` | `metal_primary_secondary_shares.yaml`, year 2020 | Freeze the documented reference fraction. |
| Non-ferrous `conservative` | `primary_secondary_split_conservative.yaml` | Use its curve as an increase-only floor. |
| Non-ferrous `moderate_recycling` | `primary_secondary_split_baseline.yaml` | Use the historical baseline data as an explicit moderate floor, **not** native premise baseline. |
| Non-ferrous `high_recycling` | `primary_secondary_split_optimistic.yaml` | Use the optimistic data curve as a higher floor. |
| Non-ferrous `primary_only` | Synthetic exact 0% target | No secondary supply in supported markets. |

The default `METALS_SHARE_SOURCE = "yaml"` uses the raw reference YAML together
with `metal_2050_literature_evidence.csv` and `metal_product_family_mapping.csv`.
Notebook 1 calls the repo-owned `ensure_share_outputs()` preparation through
`prepare_policy_share_data()`. Missing/empty named scenario files are generated;
generated files that are stale against changed raw inputs are refreshed. The
three named cases are written separately, fixing the former loop that repeatedly
overwrote one output and left the named files empty.

Preparation also maintains `primary_secondary_split.yaml` as an alias of the
generated **baseline data** case, the combined `metal_shares_2050_scenarios.yaml`,
and `share_generation_manifest.json`. The alias is not whichever case happened
to be generated last, and it does not change the notebook's native baseline
selection. These derived files are for consistency/provenance.
The folder's `.gitattributes` preserves YAML/CSV/JSON line endings so Git checkout
conversion does not invalidate the manifest's exact-byte source hashes.
The policy reads the selected named source. Generation covers the existing product table, whereas
the LCI override deliberately remains restricted to the five validated refined
non-ferrous metals. Generating 103 product records does
not make 103 recycling inventories or authorize 103 market overrides.

Valid edits to the named conservative/baseline/optimistic YAML files are retained
when upstream inputs are unchanged. They are recorded as manual outputs, and
their actual values feed the selected policy. Edit both primary and secondary
values consistently: fractions must be finite, in `[0, 1]`, and sum to one for
each year, with unchanged product/activity identities and complete product
coverage. The combined document and alias are refreshed from those authoritative
named files. Do not edit the derived alias/combined file expecting it to override
the named scenario.

If both upstream inputs and named scenario outputs were edited, preparation stops
with a conflict instead of silently choosing or overwriting assumptions. Review
and back up the edits; only set `FORCE_METALS_SHARE_REGENERATION = True` when you
deliberately want to rebuild scenario outputs from upstream data. Return it to
`False` afterwards. Malformed/incomplete manual files likewise require review,
not a silent fallback to default fractions.

For an Excel-owned reference source, explicitly set `METALS_SHARE_SOURCE = "xlsx"`.
This reads the `Shares` sheet in `metal_primary_secondary_shares.xlsx` through the
repo's read-only converter and prepares the reference YAML and named scenarios
inside notebook 1; it never writes the workbook. In this mode the workbook owns
the reference values. An independently edited extracted reference YAML triggers
a conflict: choose `"yaml"` to use that YAML or explicitly force workbook
re-extraction. The default YAML mode does not read/reimport the workbook and
therefore does not overwrite a deliberately edited YAML reference.

Changing a consumed named file, policy configuration or raw input
invalidates a loaded builder's source fingerprint. Reload the selection and
construct a fresh builder, rather than exporting an object built under old
assumptions. The resulting ZIP embeds resolved configuration and source snapshots
alongside hashes, so the selected values can be recovered even after live files
are edited.

## Available non-ferrous cases

| Case | Meaning |
|---|---|
| `baseline` | Audit-only. Native premise output is unchanged. |
| `reference` | Fixed documented 2020 recycled-input fractions for the five supported refined products. Future production/background inventories still apply. |
| `conservative` | Minimum recycled-input trajectory. Keep a higher native share rather than forcing it down. |
| `moderate_recycling` | Minimum trajectory read from the historical generated baseline-data file. This is an explicit override, not native premise baseline. |
| `high_recycling` | Higher minimum trajectory, subject to the same supplier/grade checks. This is a sensitivity, not a validated scrap forecast. |
| `primary_only` | Exact 0% secondary supply counterfactual where a validated primary recipe exists. Not a recommended central forecast. |

The initial live files contain these reference values and 2050 floor inputs.
Later validated file edits are used directly; this table is documentation, not a
second parameter store:

| Refined product | Fixed reference | Conservative file, 2050 | Moderate file, 2050 | High file, 2050 |
|---|---:|---:|---:|---:|
| Aluminium, cast alloy | 68.8245% | 45% | 60% | 70% |
| Copper, cathode | 20% | 25% | 35% | 45% |
| Nickel, class 1 | 0.2028465% | 20% | 25% | 30% |
| Cobalt | 0.5716258% | 20% | 30% | 40% |
| Lead | 54.9162034% | 65% | 77% | 85% |

The reference fractions come from
`primary_vs_secondary_metals/scenario_data/metal_primary_secondary_shares.yaml`.
They are model-specific/rounded reference inputs, not independently measured
recycled content in every region. For example, the actual exported copper market
has approximately 19.93% secondary supply; `reference` intentionally uses the
documented 20% reference setting. `baseline` retains the actual native recipe.

For minimum cases, linear interpolation between 2020 and 2050 supplies the floor
`f(year)`, with endpoint values outside that interval. The effective share is
`max(native_share, f(year))`. Thus selecting a conservative case cannot turn
cast-alloy aluminium from about 68.8% recycled input into the conservative file's
45% endpoint (or the moderate file's 60%), nor reduce an older premise native
copper target that is already above
the conservative floor. The case is a minimum, not a promise of cross-version
identical shares.

These endpoint values are **scenario hypotheses**, not direct reproductions of
the cited literature. Several literature indicators have different denominators:

- Collection/recovery rates describe how much waste is recovered.
- Recycled-input content describes secondary metal in present supply.
- Reduced new-mine requirements compare counterfactual mining trajectories.
- Battery-sector secondary supply is not the same as total refined-metal supply.

The [IEA recycling report](https://www.iea.org/reports/recycling-of-critical-minerals/executive-summary)
distinguishes these indicators and supports higher secondary supply. Its battery
results provide context for nickel/cobalt sensitivities, not an exact class-1
nickel or cobalt market prescription. The high cobalt case is particularly
exploratory. The [ICA Europe report](https://internationalcopper.org/wp-content/uploads/2023/03/Copper_The-Pathway-to-Net-Zero_Focus-on-Europe_final.pdf)
describes 0.9–1.0 Mt of end-of-life scrap in 3.4 Mt of EU refined copper in 2050;
the global cathode sensitivity here is not that EU forecast. The
[JRC aluminium report](https://publications.jrc.ec.europa.eu/repository/handle/JRC136525)
supports secondary production as a decarbonisation route but does not establish
our 70% global cast-alloy endpoint. The [van der Voet et al. study](https://doi.org/10.1111/jiec.12722)
contains a 77% secondary-lead scenario; 65% and 85% here are sensitivities around
that figure, not confidence limits.

## Steel stays with the IAM or EDM-I model

Steel supply cannot be classified merely by furnace type. Scrap-based EAF is
secondary; hydrogen/natural-gas DRI followed by EAF is primary, because its iron
originates from ore. An EAF label must therefore not overrule a DRI/direct-reduction,
pig-iron or ore-electrowinning/electrolysis label. Electric primary routes remain
in the primary group.

The IAM steel route mix, production inventories, primary/secondary quantities,
future energy inputs and regional market weights remain those produced by the
selected model and premise transformations. If the external EDM-I link is
explicitly enabled, its supply decisions remain authoritative instead. There is
no extra steel selector, share-data input or post-update steel override in this
non-ferrous policy. Changing the non-ferrous case cannot replace a steel market,
set its recycled-content share or alter a steel production exchange.

All steel grades, including low-alloyed, unalloyed and stainless/chromium steel,
are excluded from the exogenous supply sensitivity. An IAM World market that
aggregates regional steel markets retains those weights. Steel scenario choices
belong to the existing IAM/external-model setup, not a second independent
primary/secondary fraction layered on top of it. The usual prospective background
changes and validated PoR supplier-linking workflow still operate normally.

## What is changed, and what is retained?

Alternative cases reweight existing **positive supply exchanges** in the selected
market. The primary and secondary supplier distributions within their groups are
preserved. The total supplied product amount remains constant; it is not assumed
to be exactly one when an existing market includes losses. The supplier identities,
units and declared product grades must remain valid.

The policy does not edit mining, refining, recycling or steel-production LCIs.
Their electricity, fuels, process emissions, recycling treatment burdens and
allocation remain as modelled by premise/ecoinvent. Retained transport and other
non-supply exchanges are not zeroed or treated as free. Rescaling supplies does not
rebuild transport distances or future scrap logistics; that is a limitation of
this supply-mix sensitivity and should be assessed for large changes.

Negative/credit exchanges are not repurposed as supply. Uncertainty parameters on
changed supply exchanges are handled consistently with their deterministic
rescaling; unchanged exchanges are preserved. Production and biosphere exchanges
are not altered. No additional avoided-primary-production credit is added.

Same-product secondary providers are required. A missing recycling technology
does not justify relabelling a primary route, using unprocessed scrap as refined
metal, or silently selecting a different grade/country. In particular, nickel's
existing electronics-scrap recovery recipe and cobalt's battery-treatment recipe
are retained when their quantities are increased. These are prospective process
proxies, not new future technology inventories; scrap availability is not solved.

## Compatibility and native baseline

The integration has two storage paths:

- Legacy premise (including 2.3.8): stage an alternative in a separate updated
  scenario list/cache and publish only after validation succeeds.
- New premise: use the actual updated inventory store/checkpoint, patch only the
  affected markets in a fork, and refresh the inventory's validation certificate
  before final export.

Native validation reports contain immutable payloads. Rollback snapshots retain
the original report objects and restore their registry entries, without trying
to deep-copy live report/store proxies. This preserves validation certificates
and avoids `cannot pickle 'mappingproxy' object` on newer premise releases.

Native baseline is audit-only in either path; it does not fork a native inventory
or rewrite a legacy scenario cache just to record the selected case. The baseline
records the installed premise version and observed splits. Its legacy interpolated
targets versus newer retained ecoinvent fractions are intentional differences.
If an installed premise file was previously edited manually, native baseline
also reflects those existing settings; the repo does not silently restore vendor
files. Use a clean environment if a pristine vendor baseline is required.

Cases are applied after premise has created the relevant suppliers and markets,
and before final export, rather than patching undocumented package internals.
Both export types use the same helper. Classification safeguards and LCIA-factor
selection remain separate: a metal supply policy must not conceal missing
contributions or change the selected characterization method.

## Audits and extending the policy

Every run writes timestamped CSV/JSON reports beneath `export/metals_policy/`.
They record policy/source fingerprints, installed premise/ecoinvent versions,
scenario/year, observed and requested/effective shares, affected identities,
changed supplies and skipped/unsupported markets. Inspect the actual audit rather
than assuming the selector changed every market in a sector.

The configuration is `configuration_file/metals_scenario_policy.yaml`; numeric
case inputs live in the share-data folder. To extend it:

1. Add an explicit case under `nonferrous_cases`. Use `minimum`
   for an increase-only floor or `target` for an intentionally exact share.
2. Add the case's named data file mapping under
   `share_data.nonferrous` and provide validated product records with primary and
   secondary year/share curves. State the source, denominator, grade and uncertainty; shares must be
   between 0 and 1. Keep numeric assumptions out of the policy YAML.
3. For a new market, give its exact name, reference product, unit and locations,
   with secondary-route matching and an unambiguous primary recipe. A declared
   `supply_products` whitelist is required for any intentional grade-equivalent
   interface; do not introduce one just to make validation pass.
4. Test the actual recipes in both supported environments, including interpolation,
   mass balance, signs, uncertainty, supplier availability and repeat-application
   protection. Review audits for zero-change/unsupported cases.
5. Export from a fresh builder and compare complete signed impact totals.

Ores, concentrates, explicit-primary products, and products without validated
secondary providers such as zinc/rare-earth markets remain outside this curated
policy. Their native modelling is retained and the configuration documents why.
An unsupported nonzero target fails rather than manufacturing a recycling route.
For a scientifically stronger high-recycling case, the next step is to couple
product lifetimes, future waste quantities, collection and recovery yields,
grade constraints and scrap trade to technology-specific recovery inventories.
