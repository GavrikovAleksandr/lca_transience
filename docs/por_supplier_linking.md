# Post-update PoR supplier linking

The repo now owns the additional supplier-linking step in `por_supplier_linking.py`. It runs after premise has created and replaced its external markets, and before export. `1_export_packages.ipynb` uses the same implementation for Brightway databases and the Pathways ZIP.

## Shared geography and reference-inventory preparation

`supplier_resolution.py` is production code shared by the exporter and the post-update linker. Tests exercise it; neither workflow imports test code. It owns supplier identity matching, duplicate detection, 1:1 identity substitution and active-IAM geographic definitions.

`0_export_act_to_excel.py` no longer imports `regionalization.py`, premise or Wurst. Its Brightway access is deferred until explicit execution. It prepares the existing 26 reference activities, including the three Morocco copies, before creating a workbook. The configured reference database is read, not modified. The output remains a candidate/reference workbook, not the final `inventories/lci-itom_por.xlsx`.

```powershell
python 0_export_act_to_excel.py --model remind
python 0_export_act_to_excel.py --model image --output export/reference_candidates_image.xlsx
```

The default output is unchanged. Supplying `--output` avoids overwriting the existing candidate workbook. The worksheet still uses Activity metadata followed by Exchanges, and now also retains available uncertainty fields. A required `Database` header names the candidate workbook after its output stem, allowing Brightway's Excel importer to parse it directly without conflating it with the reference database or final PoR inventory. The exporter does not write a Brightway database. `regionalize_to: None` preserves the original activity and suppliers. Target source matching remains exact-first with the existing contains/fuzzy fallbacks, but every stage is constrained to the requested reference product. Missing suppliers fail before writing a partial workbook. Missing targets are warned, skipped and listed in `missing_targets` in the JSON report; `--strict-targets` makes them fatal.

Live reference checks exposed an existing fuzzy-match hazard: the unavailable PBS target could select isobutyl acetate. Wrong-product fallbacks are now prohibited. The PET/PLA/PTA/polyol/PP target names and products were corrected to verified ecoinvent identities; PET amorphous and long-chain polyols match the existing PoR configuration. The unavailable PBS and bulk-polymerised PVC targets are kept as explicit missing items, not swapped to another technology. The suspension-PVC target is retained separately. In the inspected local reference database, this yields 24 available inventories from the 26 target slots.

`tests/check_reference_export_readonly.py` is an optional live-data test adapter for a locked Brightway project. It opens the existing SQLite file with `mode=ro`, reads the selected source activities, exercises the **same** preparation and workbook writer for both IAMs, and checks repeatability. It does not run Brightway project setup or alter the source database. For example:

```powershell
python tests/check_reference_export_readonly.py --sqlite "<Brightway project>/lci/databases.db" --output-dir export/reference_validation_new
```

Use a new output directory; this check refuses to overwrite previous validation workbooks.

### Topologies

The shared loader reads `data/iam_variables_mapping/topologies/<model>-topology.json` with an absolute repository-relative path. It supports the supplied REMIND and IMAGE files (and other models with an explicit matching definition file). Geographic definitions are added to constructive-geometries under `(model, region)` keys, rather than merging both IAMs into a global bare-label dictionary. The selected model determines which IAM region labels in datasets are interpreted. Countries and ecoinvent regions use the base geographic definitions. NL maps to REMIND EUR / IMAGE WEU; MA maps to REMIND MEA / IMAGE NAF. The World aggregate is not preferred over specific regional providers. A missing/invalid topology raises an error. Topology path and SHA-256 are recorded in both workflows' JSON reports.

Additional ecoinvent 3.12 regions are read from the repo-owned `ei312-topology.json`, mirrored from premise 2.3.8's supplied country definitions, and registered under the separate `ecoinvent` namespace. This includes `IAI Area, Western and Central Europe`; it is not guessed from a generic European label. `RER w/o RU` is explicitly resolved as RER excluding Russia for covering-region checks. The additional topology file/hash are also recorded in both workflows' provenance. The export guard rejects geographic definitions changed after validation.

Topologies are geographic definitions only: they do not import inventories, alter technology efficiencies or decarbonize a supplier. Select the same IAM as the subsequent premise scenarios. The post-update adapter automatically takes each scenario's `model`; the standalone exporter uses `IAM_MODEL = "remind"` unless overridden on the command line.

### Reference supplier rules

For copied reference inventories, the resolver prefers a valid same-product/unit local supplier, then the active IAM region, then the smallest wholly covering reference geography. It retains a valid original reference proxy when no covering equivalent exists; a global provider is a final fallback for an otherwise missing link. Explicit foreign country-level process inputs are kept where declared. Electricity market/group names are interchangeable only within the same electricity product/voltage and unit, and only when a real supplier is present. No other names are guessed or substituted through fuzzy matching.

Unlike the legacy blanket geographic relinker, this preparation does not split an exchange using arbitrary production-volume or equal weights. Quantities, units, uncertainty and biosphere flows remain unchanged. The production exchange receives the copied activity's destination location. Obsolete supplier identifiers are removed on a changed link; nonzero missing, ambiguous and direct self-supply links fail. Zero technosphere inputs are retained without attempting an unnecessary supplier repair. No supplier recipe in the reference database is edited.

The companion `<output-stem>_supplier_audit.csv` lists checked exchanges and their identities/amounts/reasons. `retained_reference_proxy` is a review item, not evidence that the supplier is physically local. Its JSON companion records the reference database, model, topology hash and totals. Review these files before copying candidates into the final foreground workbook, especially geographic proxies for imported methanol, hydrogen, heat and capital goods. Future renewable supply assumptions remain explicit foreground-inventory decisions, not geography-derived defaults.

### Legacy file

Keep `regionalization.py` for compatibility **only while the locally modified installed premise still has `from regionalization import *`**. This repo's exporter and post-update workflow do not use its relinking function. The legacy constructor hook is disabled by the runtime compatibility setup, but that does not remove an old import statement from site-packages. With a clean premise installation, neither of these new workflows needs that file. No installed package files are edited by this refactor.

## Notebook execution

The setup cell calls `configure_por_export_support()`. This retains the null uncertainty-bound workaround in `premise_compat.py`, disables the old user-added `fully_regionalize_created_inventories` method in memory if that specific legacy method exists, installs a geographic export guard, and installs the scoped Pathways variable-mapping adapter described below. It also installs the generic, rule-driven inventory supplier compatibility adapter before additional inventories are imported. A clean premise installation without the legacy method does not need that disabling operation. No site-packages file is edited. See [inventory supplier compatibility](inventory_supplier_compatibility.md) for the Moroccan PV rule, portable fallback, audit/caching safeguards and how to add another declared supplier alias.

For Brightway output, the notebook attaches the helper to the new builder before updating:

```python
attach_por_supplier_linking(ndb, stage="brightway")
ndb.update()
ndb.write_db_to_brightway(name=scenario_names)
```

The attached update method runs premise's normal update first, then the repo's validated post-step. Existing databases are replaced only after both steps succeed. For the Pathways ZIP, the notebook attaches it to the contained builder:

```python
attach_por_supplier_linking(ndb.datapackage, stage="pathways")
ndb.create_datapackage(...)
```

The current notebook uses `attach_por_pathways_export(ndb, stage="pathways")` instead of attaching only the internal builder. This includes the same supplier-linking hook and binds the mapping guard to the **concrete package instance**, including stale class aliases or instance overrides. After the normal update/linking step it validates and snapshots the small activity identities for every scenario before matrices are written. Matrix preparation can then release/mutate inventory dictionaries without erasing mapping metadata. A malformed real provider fails at this preflight with scenario/year/sector/variable context; diagnostics are retained separately. Scenario identities/order are checked before using the snapshot.

The package cell reloads only repo-owned helper modules, not premise, and prints the actual premise version/module path and Python executable. This addresses stale imports in long-lived notebook kernels; the kernelspec display name alone is not proof of the running package version. The installed environment `premise_pathways` contains 2.3.8, while `premise_pathways_new` contains 2.5.2 in the inspected setup. The class adapter has a versioned marker so an old wrapper can be upgraded instead of being mistaken for the current fix.

Both constructor calls receive `fresh_scenario_definitions(...)` from `por_scenario_inputs.py`. This reconstructs supported scenario inputs and discards previous applied-sector flags, stores, checkpoints, validation reports and other runtime state without copying those large objects. External datapackage resources retain their identity; mutable input lists/dictionaries are independent. This prevents the preceding 2050 Brightway build from contaminating the six-year package. Unknown public input fields raise an explicit compatibility error rather than being silently dropped. The notebook's existing OCE-only Brightway selection is retained, with its deletion/export names limited to the same selection; the Pathways package still includes CL and OCE for all six years.

### Pathways performance and progress

Attaching the hook registers it; it does not update any scenario. The expensive work runs after the package's normal premise update. The helper prints confirmation when attached, then per-scenario progress for loading, matching, baseline capture, validation, checkpoint writing and completion. JSON audits include `phase_seconds` and `linking_seconds`. The initial package constructor and normal twelve scenario-year transformations still take time independently of supplier linking.

For native stores, background entries are read as identity/code metadata; only policy-selected activities load full exchange inventories. Changed exchanges are patched in place, preserving native activity/exchange IDs and deleting stale supplier addresses explicitly. Sparse scenario checkpoint compatibility is enabled on the staged fork when supported, avoiding twelve complete graph rewrites. `sparse_checkpoint_eligible` records whether the native delta writer can be used. Older/native adapters without in-place patching retain the list-replacement fallback.

Validation remains enabled. A single baseline scan captures full activity fingerprints and reusable graph edges. Only changed consumer edges are updated to compute post-link and targeted cycles, rather than rereading/hashing the whole graph repeatedly. The native read-only rule pass is reused to issue the final certificate, with explicit inherited-duplicate exceptions applied to the already evaluated findings. The original graph, quantities, uncertainty, scope checks and cycle-review policy remain protected. Tests compare the optimized baseline with native premise and verify sparse checkpoint/mapping round trips.

Interrupt an old notebook run and restart its kernel before using changed helpers; already-attached closures do not automatically reload source edits. Recreate each builder and rerun the updated cells rather than adding a second explicit `update()` before `create_datapackage()`.

### Metals diagnostics in Pathways mappings (premise 2.5.2)

The new metals update puts `material decisions` and `material update metrics` beside `transformed activities` under `scenario["mapping"]["metals"]`. Decisions have an activity identity nested under `activity`; metrics are counters, not datasets. The native Pathways writer nevertheless assumes every value is an activity list and reads `name`, `reference product` and `unit`, causing `KeyError: 'name'` (or a string-indexing error for compacted metric keys).

`install_premise_pathways_mapping_fix()` creates a temporary activity-only view when writing the variable mapping. It excludes only those two exact diagnostic keys in the metals sector, retains all real providers and optional LHV values, and restores the original scenario mapping objects even if writing fails. Diagnostics stay available to other premise reporting. Malformed genuine activity mappings raise an error with scenario/year/sector/variable context even when metals diagnostics are absent; no default dataset names are invented and no unknown records are silently discarded. Older activity-only premise mapping content is unchanged. The adapter is installed automatically by the setup/attach workflow; the concrete package guard also prevents a cached class or instance override from bypassing it. Both are idempotent and change no installed package files or inventories.

`tests/test_por_pathways_mapping.py` reproduces the reported error using the installed exporter method and checks diagnostic filtering, compacted metric records, provider unions across scenarios, LHV, original-object restoration, failure behavior and legacy compatibility. Restart the kernel and rerun the package cell after updating the repo; the full production package is not generated by these isolated tests.

`tests/test_por_pathways_instance.py` additionally exercises the exact package-instance path: one native update/export, mapping capture after linking, early failure before matrix writes, unpatched or overridden methods, restoration on failure and an exporter which clears the original provider dictionaries. No actual Brightway database or production package is written by these tests.

The public package method calls its internal `NewDatabase.update()` and therefore executes the post-step before matrix export. This avoids copying the private package-export implementation or applying corrections after the ZIP has already been written. The hook is idempotent on a builder instance. It must be attached to each new builder.

Reload the notebook from disk and run its setup before creating new builders. Recreate any builder whose earlier update failed. The already generated ZIPs, Brightway databases and impact figures are not retroactively changed.

## Scope and selection

The policy reads `configuration_file/config_itom_por.yaml` and `inventories/lci-itom_por.xlsx` at attachment time, records their hashes and checks them again during processing. Editing either source while an update runs causes an explicit failure rather than mixing source versions.

Domestic targets are named activity/reference-product pairs under `production pathways`, `regionalize/datasets`, and declared market `add` suppliers. Their actual unit and location remain part of the runtime supplier identity. Domestic supplier substitutions are limited to NL by default. Selecting a pair does not rename or move the consuming activity itself.

The helper distinguishes:

- **Domestic processes:** apply explicit PoR replacement rules first. Other eligible generic inputs use NL, then the active IAM region, then an available wholly covering reference region, even when the original RoW/GLO/World supplier exists. Exact product/unit matching is required. The electricity market/group alternative must retain voltage and unit. Generic capital/service proxies are eligible too; a European construction proxy need not first be added to `regionalize/datasets`.
- **Constructed markets/upgrading activities:** preserve the modeled supply-route exchanges and quantities. Only their declared auxiliary `add` inputs are eligible for local linking.
- **Regionalized background markets:** retain the amount/share of each technology. A product-supply exchange from a generic region (e.g. RoW or RER) is linked to the unique same-name/product/unit NL producer only when that producer is explicitly declared in the PoR policy. Ancillary service/transport **identities** can use a covering regional proxy under version 3, but their quantities and distances remain unchanged. Intentional country-specific material/process supplies, import pathways and unconfigured product-supply technologies are retained. No equal-share or production-volume rebuilding is performed.
- **Imports:** identify Trade/terminal/import pathways and preserve their foreign supply. The original source-workbook recipe can repair a native replacement that inadvertently points the import wrapper at a PoR market. Regionalized copies use the unique original recipe for their activity/product/unit. Legitimate foreign future suppliers are retained.
- **Fixed renewable supply:** preserve the dedicated 50% solar/50% wind recipe and its supplier identities. A changed share fails validation rather than being silently renormalized.

The source profile reader inspects only import and fixed-power recipes. Under policy version 4, copied profiles first replay any applicable declared inventory-compatibility decision, using the same resolver as import. The original workbook and quantities remain unchanged. Missing reference-product fields can otherwise be resolved only when the live database supplies one unambiguous product for the original name/unit/location. Missing or ambiguous source identities fail explicitly; they are not guessed. Rule/fallback checksums and the effective per-consumer receipts are retained with the prepared scenario and verified during export, including in-memory policy recovery.

### Embedded market supply correction (policy version 2)

The earlier post-step validated `existing_market` exchanges without changing their identities. Consequently, MDI could consume NL aniline and NL nitrobenzene **markets**, while the latter still supplied RoW nitrobenzene production despite the configured NL copy. Both market and production entries were already correctly present in `regionalize/datasets`; adding more duplicate inventories or declarations would not fix that missing market-supply branch.

Version 2 links this declared local product supply through existing markets as well as processes. For example, the chain becomes MDI NL → aniline market NL → aniline production NL → nitrobenzene market NL → nitrobenzene production NL. The last process continues to consume the configured PoR benzene market. Each exchange keeps its amount/unit/uncertainty; market transport proxies remain unchanged. This implements the explicit NL regionalization assumption, rather than choosing suppliers by their LCIA score. An NL label alone is not evidence that all upstream inputs or impacts are domestic or low-carbon.

Reason `regionalized_market_product_supply` identifies these corrections in CSV reports. If the declared local counterpart is absent, a valid original supplier is retained and reported with `unavailable_local_supplier`; JSON includes `unavailable_local_suppliers`. Ambiguous local identities fail, rather than choosing arbitrarily. Constructed PoR market routes and protected imports/fixed renewable shares remain outside this rule.

Read-only checks on the existing 2050 CL and OCE Brightway databases reproduced the nitrobenzene RoW link and found 19 qualifying market-product corrections per scenario, with no missing local counterparts. These diagnostics do **not** modify the old databases. `tests/check_por_linking_readonly.py` can repeat that check and write CSV/JSON comparisons for any explicitly named scenario databases.

Restart the notebook kernel and rebuild through `1_export_packages.ipynb` to update Brightway outputs and the Pathways ZIP. The export guard requires the current `linking_policy_version` in its validation marker; an old prepared cache/export tag cannot silently count as validation under the new rules. Already generated matrices/results/figures need regeneration after the rebuilt package is selected.

### Regional input completion (policy version 3)

Version 2 completed the declared market-to-production links, but two gaps remained deeper in those production inventories: it tried European fallback only for a **missing** original supplier, and local lookup required the identical activity name. Thus valid RoW material/water proxies and World heat were retained, while a GLO electricity group missed the real NL electricity market. Unlisted generic construction proxies also remained outside the local-only branch.

Version 3 uses `ReferenceSupplierResolver.regional_candidate()` in both reference preparation and post-update linking. For eligible inputs the priority is explicit PoR replacement → exact local supplier → active IAM region → smallest available wholly covering reference region. A valid original supplier no longer blocks this search. Only the documented electricity market/group family can change names, with exact reference product, voltage and unit. Heat/steam products are not interchanged, transport distances are not invented and exchanges are not split or rescaled.

Explicit foreign country/subnational material and process suppliers remain pinned, as do import wrappers and fixed renewable supply. Operational electricity/heat in a copied domestic plant are instead sourced for that plant's location; a country label inherited from the original reference plant is not treated as a foreign energy import. This exception does not apply to protected imported inventories or fixed electricity recipes. Constructed PoR market supply routes remain protected, and generic background market recipes are not recursively rewritten simply to minimize impacts.

For the existing 2050 nitrobenzene-production inventories the verified corrections are:

| Input | Original proxy | Selected provider geography |
| --- | --- | --- |
| Chemical factory construction | RoW | RER |
| District/industrial heat | World | EUR |
| Nitric acid | RoW | RER w/o RU |
| Soda ash and sulfuric acid | RoW | RER |
| Wastewater and deionised water | RoW | Europe without Switzerland |
| Medium-voltage electricity group | GLO | NL medium-voltage electricity market |
| PoR benzene | NL | NL, unchanged |

These are geography-based modeling choices, **not a guarantee of lower LCIA scores** or evidence of specific real procurement contracts. Amounts (including the negative wastewater quantity), units, uncertainty and biosphere/production exchanges remain unchanged. The read-only scenario checks validate source/target identities and repeated application without editing the existing Brightway databases. Rebuild both export routes and recalculate results to deploy this version.

Reasons `geographic_local_supplier` and `geographic_covering_regional_supplier` identify new changes. Where no matching local/covering provider exists, a valid original broad proxy remains and is recorded with status `retained_geographic_proxy`; JSON includes `retained_geographic_proxies`. Review these residual assumptions rather than expecting every GLO/RoW supplier to disappear. A missing original market can use a documented global fallback; incompatible products/units and ambiguous supplier identities are never guessed.

## Replacement and validation rules

The helper uses `markets/replaces` and `replaces in` for explicit name/product substitutions. This is essential: geographic relinking alone cannot turn `market for methanol (SPS)` into `market for methanol (por)`. Inactive markets absent from a scenario are reported; a valid original supplier is retained in that case.

There is no supplier splitting, equal allocation or production-volume allocation in this step. All replacements are 1:1. Non-unit replacement ratios must be represented through documented inventory changes rather than this linking helper. Every processed nonzero technosphere exchange must resolve to one unique name/product/unit/location identity. Eligible generic markets, materials, services and capital proxies can use a covering regional supplier even when the original link exists. Explicit foreign country-level material/process supplies are not replaced through that fallback. Neither an input's biosphere flow nor its unit is changed to facilitate a supplier match.

Input quantities and units remain unchanged. Uncertainty fields, including numeric zero bounds, are retained. Production and biosphere exchanges are not edited. Changed supplier identities have obsolete `input`/supplier-database addresses removed so the exporter can build correct links. Unintended direct self-supply and direct market/import feedback loops are rejected. This check does not prohibit legitimate process/recycling loops elsewhere in the inventory.

## Scenario cache handling

The adapter detects the updated scenario's storage format instead of depending on a version-number comparison:

- **Legacy premise (tested with 2.3.8):** an in-memory `database` or `database filepath` pickle. Cached inventories are loaded with `delete=False` and `load_metadata=False`, linked and validated, then written to a new cache. No reference-database fallback is permitted.
- **Inventory-store premise (tested with 2.5.2):** `_inventory_store` for in-memory updates, or `_inventory_checkpoint` for persisted updates. The updated graph is materialized with metadata, forked, and changed exchange lists are applied in a native transaction. Activity IDs and input quantities/units/uncertainty remain unchanged. Native certification uses an explicit `por_supplier_linking` intent targeting the changed activities, with fingerprints/cycles captured from the **already updated scenario immediately before this post-step**. `exhaustive=True` evaluates that intent rather than reusing old sector certificates; it does not request an unrestricted background audit. A separate full-graph cycle comparison rejects new isolated cyclic components, including loops through unchanged suppliers. Changes to the membership of existing cyclic background components are review items, not evidence by themselves of a forbidden new loop. Native targeted-cycle signatures are compared on the same target subgraph; reviewed background-component reconfigurations are included explicitly in that cycle context. Direct self-supply and direct market/import feedback remain prohibited by the repo linker. Unchanged background activities retain premise's existing update validation; their pre-existing problems are not silently repaired or suppressed. Persisted changes are written to a fresh checkpoint under `export/por_supplier_linking/inventory_stores/`. An old single-use export handoff is removed so export cannot bypass the new links. Lazy scenario mappings are rebound to the new graph/checkpoint.

The notebook calls remain unchanged: attach the helper before `ndb.update()`, then export normally. After upgrading premise **or changing the helper**, restart the kernel and recreate the builder before rerunning; an already attached builder retains its old update wrapper. The same adapter handles the internal builder used by the Pathways ZIP export. In-memory native updates remain in-memory rather than forcing a pickle conversion.

Original scenario cache references are not changed until all selected scenarios and their reports succeed. A later scenario failure leaves the original references intact. Original cache files are retained. Newly staged files from a failed transaction can remain unreferenced: legacy caches follow premise's cache cleanup, while native checkpoints under the report directory are retained for inspection. Do not remove a checkpoint referenced by a prepared builder. Missing updated data never falls back silently to the unmodified reference inventory.

The storage API is deliberately isolated here. Missing/empty/corrupt stores, unsupported mapping APIs and stale certificates which cannot be refreshed fail closed. No installed package files or reference Brightway databases are modified. Future upstream storage/validation API changes still require compatibility testing; the native scenario-mapping bridge uses feature-checked premise internals.

`tests/test_por_inventory_store.py` covers live stores, persisted checkpoints, both scenarios, validation, uncertainty preservation, stale export handoffs and failure rollback. In an environment with native stores it additionally executes the installed checkpoint/mapping implementation and native export loader on a small isolated fixture, without importing premise's initialization or writing any actual LCA database. `inventory_storage` in the JSON audit distinguishes `legacy_cache`, `inventory_store` and `inventory_checkpoint`.

The native post-link validation scope/count, number of baseline cycles, background-component reconfigurations and reviewed targeted cycles are recorded in the JSON audit. Reconfigured background components require review if their feedback is methodologically important; acceptance here is not a numerical-productivity or spectral-radius test of all background loops. An unchanged store generation does not trigger a new background certification. A separate whole-background quality audit can still expose inherited duplicate exchanges, non-scalar numeric values or uncertainty issues; a successful scoped post-link check does not certify those unrelated inventories as error-free.

When different original country/database supply components become byte-identical after relinking to one regional provider, their original supplier/address is retained in a `por source component` exchange field. This matches premise's supported distinction between separate source components and accidental duplicate records. Neither row is dropped or summed: each amount, sign and uncertainty remains unchanged. `preserved_source_components` and CSV status `preserved_component` record these cases. Adding arbitrary row numbers to conceal duplication is not permitted.

For exact duplicate pairs **already present before linking**, the post-step compares the original exchange pair using its stable activity ID and exchange positions. Only when every duplicate finding for that exact activity is demonstrably inherited does it generate a narrow, explained `ValidationSuppression` for the installed version/system model. The certificate retains those findings as explicitly suppressed issues, and `inherited_duplicate_supplier_exchanges` / `inherited_duplicate_review` record the activity, original pair, amount and unit in JSON. A new duplicate in that activity prevents the exception from being issued. These exceptions acknowledge source-data quality issues; they do not remove, consolidate or rescale any demand or certify the original inventory as free of accidental double counting. Review the underlying LCI independently before deciding whether a repeated source row should actually be removed.

## Export preparation guard

The installed `premise.export.check_geographical_linking` can change a foreign supplier location to the consuming dataset's location immediately before export. That could undo validated import or renewable-supply links even after a successful post-update pass.

The runtime guard applies only to scenarios tagged by this repo's validated post-step. It retains the selected supplier identities across that native check, rejects unexpected quantity/unit changes, and appends any prevented location changes to the report. Untagged premise scenarios keep their native behavior. This guard also avoids editing the installed package. Native validation and the actual exporters still run afterward.

## Reports and checks

CSV and JSON reports are written to `export/por_supplier_linking/`. Each report identifies stage, model, pathway, external scenario labels and year. Exchange records contain consumer identity, old/new supplier, original/new amount, unit, reason and status. JSON records the source hashes and summary counts. Status `unavailable_market` flags a market not created for that scenario; `preserved_on_export` records an attempted native location change that was prevented.

JSON also records `linking_policy_version`. Version 2 included the embedded regionalized-market supply correction; version 3 adds appropriate regional inputs within those processes. `unavailable_local_supplier` records a missing declared domestic production counterpart, while `retained_geographic_proxy` records an eligible input without an appropriate regional substitute. Review missing-provider flags and residual geographic assumptions before interpreting impacts.

Review these reports after the first full rebuild, particularly imported methanol, propylene/aromatics, steam auxiliaries and the coupled methanol markets. This module validates coupling, not the physical accuracy of every inventory, carbon-origin classification or trade-distance assumption.

Run:

```powershell
python -m unittest discover -s tests -v
```

Regression cases cover explicit market-name substitution, unchanged uncertainty/amounts, import restoration, renewable shares, missing/ambiguous suppliers, regional fallback, idempotence, transactional cache handling, both export routes and the installed native geographic export check. Reference-export cases also cover REMIND/IMAGE definitions, cwd-independent paths, NL/MA suppliers, preserved foreign processes, electricity market aliases, all 26 existing targets, uncertainty-bearing Excel output and import-time safety. The source workbook and YAML are not modified by this step.
