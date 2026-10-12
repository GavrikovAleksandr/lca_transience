# Inventory supplier compatibility across premise versions

The repository owns an explicit, availability-based adapter for supplier names
and reference products that change between premise releases. It does not edit
site-packages, the source ecoinvent database, or the existing PoR workbook.

`inventory_supplier_compat.py` contains the reusable implementation.
`configuration_file/inventory_supplier_compatibility.yaml` declares the rules.
These rules are **not** part of premise's production-pathway YAML schema. The
existing `config_itom_por.yaml` and ITOM variables remain unchanged.

## Running the workflow

Restart the notebook kernel after updating Python helpers, and recreate the
builders. The existing setup call installs the adapter before inventories are
imported:

```python
from por_supplier_linking import configure_por_export_support
configure_por_export_support()

# Then construct the normal NewDatabase or PathwaysDataPackage builder.
```

Keep the existing `attach_por_supplier_linking(...)` or
`attach_por_pathways_export(...)` call as well. The compatibility adapter acts
during inventory import; the PoR hook acts after premise updates and protects
the effective supplier choice through export. Neither replaces the other.

Installation is idempotent. Rules are loaded afresh for each matching inventory
import. A previously prepared builder/cache is not updated by editing YAML;
recreate it. Do not reload helper modules during an active update.

## First rule: Moroccan photovoltaic electricity

The rule `por_dac_methanol_pv` applies only to `lci-itom_por.xlsx`, only to the
dedicated MA electricity-supply activities whose names start with
`electricity supply, ` and contain `for DAC methanol (por)`, and only to their
declared Moroccan commercial-PV technosphere inputs.

Candidates are tried in this order:

1. `electricity production, photovoltaic, commercial`, product
   `electricity, medium voltage`, MA, kilowatt hour.
2. The same activity and location with product `electricity, low voltage`.
3. The declared repo-owned Moroccan PV fallback, if neither native candidate
   exists.

Both installed releases retain the commercial-PV name. The inspected newer
release supplies its commercial country adapters at medium voltage; the older
release's **extension sheet** also contains a Moroccan commercial adapter,
labelled low voltage. Inspecting only the old workbook's main sheet misses that
Moroccan record. The adapter checks the actual scenario suppliers, rather than
assuming that a package version guarantees a particular inventory is present.

Only the exchange's supplier identity changes. The 0.5 kWh solar and 0.5 kWh wind
inputs, process demands, units, biosphere exchanges and uncertainty fields are
preserved. The dedicated mix still produces medium-voltage electricity. Its
lossless voltage-interface assumption remains explicit: no grid backup,
transformer losses, transmission, storage or hourly balancing is inferred.

Duplicate exact identities fail instead of selecting the first provider. A grid
market, a different country, a unit conversion or a fuzzy name match is never
an automatic fallback.

## Portable fallback inventory

`inventories/compatibility/por_pv_ma.json` is a versioned ecoinvent-3.12 snapshot.
Its main activity is `electricity production, photovoltaic, Morocco (por)` /
`electricity, medium voltage` / MA / kilowatt hour. It preserves the actual
Moroccan commercial recipe already migrated into the local reference inventory,
and bundles 57 necessary legacy PV auxiliaries so it does not rely on a newer
premise release providing those construction/manufacturing activities.

The snapshot has 58 activities and 1,261 exchanges. Its technosphere closure was
checked against the existing unmodified ecoinvent 3.12 cut-off supplier catalog:
213 distinct external supplier identities and 103 distinct biosphere identities.
No Brightway project setup or database write was used for these checks.

Important assumptions and limitations:

- Original technology shares are preserved. They are a generation-weighted
  proxy based on the source's 33 report countries, not measured Moroccan plant
  shares. The source prose calls it an OECD proxy, but its formulas also include
  non-OECD countries.
- Original lifetime is 30 years. Ground yield is 1,845.81788273762 kWh/kWp/year;
  roof and facade yield factors are 0.94 and 0.66. These are inherited Atlas-based
  assumptions, not a newly selected project site. The original retrieval date
  is unavailable.
- Original cleaning water, wastewater and manufacturing supplier geographies
  are preserved. Moving the generation activity to MA does not move all its
  capital-goods manufacturing to MA.
- Inferred electricity shares sum to 1.0003653010668472. This small original
  discrepancy (0.03653%) is recorded, not silently corrected.
- The fallback is a static foreground technology recipe. Its suppliers can
  still reflect the updated background database, but the fallback itself does
  not acquire the newer release's PV technology forecast. Functional linking
  compatibility does not promise identical LCIA results across premise versions.
- Source inventories and derivatives retain their original licensing
  restrictions. The PoR datapackage's general CC0 declaration does not grant
  redistribution rights to ecoinvent-derived data. Do not publish the snapshot
  without checking the applicable source licenses.

The adapter inserts only missing exact identities when the fallback is needed.
It does not overwrite a native activity's recipe. It validates the complete
required technosphere closure before insertion. Missing dependencies, malformed
or zero-burden placeholder recipes, incompatible ecoinvent versions and
ambiguous suppliers fail explicitly. The snapshot must match both the
inventory's declared source version and the target database version, as well
as its system model (`cutoff` for this PV snapshot). A consequential importer
cannot silently use a cut-off snapshot, including when reusing a frozen receipt.
Automatic snapshot migration or system-model conversion is deliberately unsupported.

`scripts/build_por_pv_fallback.py` can reproduce the snapshot from the source
workbook and an existing read-only Brightway SQLite database. It opens SQLite
with `mode=ro`, `query_only` and an explicit read transaction; it does not import
Brightway or premise, select projects, or write files. Use `--help` for required
source and database arguments. Its JSON goes to standard output for review.
The snapshot is already bundled, so these source files and script are not needed
during normal exports.

Methodological context: [IEA-PVPS PV LCA guidelines](https://iea-pvps.org/key-topics/methodology-guidelines-on-life-cycle-assessment-of-photovoltaic-2020/)
and [Global Solar Atlas methodology](https://www.globalsolaratlas.info/support/methodology).
These describe the general approach; the exact numbers and source locations
used here are recorded in the snapshot's `provenance`.

## Adding another compatibility rule

Add a new item under `rules` in
`configuration_file/inventory_supplier_compatibility.yaml`. No new Python
branch is required for another ordinary supplier rename/reference-product
alias. This illustrative template is not an active rule:

```yaml
- id: my_reviewed_supplier_rename
  inventories:
    - my_inventory.xlsx
  consumers:
    - name: my consuming process
      reference product: my process output
      unit: kilogram
      location: NL
  exchange:
    name: old supplier name
    reference_products:
      - supplied product
    unit: kilogram
    location: RER
  candidates:
    - name: renamed supplier name
      reference product: supplied product
      unit: kilogram
      location: RER
      reason: Reviewed equivalent activity name in the newer release.
    - name: old supplier name
      reference product: supplied product
      unit: kilogram
      location: RER
      reason: Existing equivalent activity in the older release.
  reason: Explain the evidence for equivalence and any provisional assumptions.
```

Use exact consumer identities where possible. The alternate consumer selector
supports `name_prefix`, `name_contains`, `location`, and optional `unit` and
`reference product`. These predicates select consumers only: supplier lookup is
always exact. Keep selectors narrow. Overlapping rules, duplicate IDs/candidates,
unknown fields and duplicate YAML mapping keys are rejected.

Candidates preserve the exchange's unit and geography. A different reference
product must be explicitly reviewed as a compatible interface; sharing a unit
alone is not evidence of equivalent technology, purity, pressure or temperature.
The adapter is not a general license to replace a supplier with a lower-impact
one. Geographic substitution and quantity-converting process redesign belong
in separate documented modeling changes.

An optional fallback is declared as:

```yaml
  fallback:
    path: ../inventories/compatibility/my_supplier.json
    provider:
      name: my repo-owned supplier
      reference product: supplied product
      unit: kilogram
      location: RER
```

The path is relative to the rule file. The JSON envelope contains
`schema_version: 1`, an `ecoinvent_version`, a `system_model` (`cutoff` or
`consequential`), `datasets`, and `provenance`.
Use complete premise/Brightway-style dictionaries with one matching production
exchange per activity, finite quantities, real technosphere/biosphere burdens,
and explicit supplier identities. Provenance requires `source_description`,
`source_path` (a descriptive identifier, not a runtime dependency) and a source
SHA-256. Optional source links and assumptions can be included. The bundled PV
snapshot demonstrates the format. This loader hashes the local JSON snapshot;
it does not require the original author's source workbook to be installed.
If the new snapshot is to travel with a repository checkout, add a narrow
exception for that reviewed file in `.gitignore`; unrelated inventory workbooks
remain excluded. Check its source licensing before committing or publishing it.

## Protected profiles, audits and caches

Imported consumers carry an `inventory supplier compatibility` receipt. It
records the rule, original/effective supplier identities, source and exchange
positions, amounts before/after, unit, rule hash and used fallback hash. The
receipt freezes each consumer's choice; there is no global "last supplier"
state shared across years or CL/OCE.

The post-update linker normalizes copies of the original workbook's protected
supplier profiles using the same rules and receipts. It therefore preserves the
effective low-voltage or fallback identity instead of restoring an unavailable
medium-voltage supplier. A subsequent displaced but declared alias can be
restored without changing its quantity. Existing fixed renewable-share checks
remain enabled.

Per-scenario supplier-linking JSON reports include these receipts and
`compatibility_targeted_exchanges`. The sources include the rule file and all
existing declared fallback snapshots. Their hashes are checked before/after
processing and during later export/cache recovery, including when the policy
is already cached in memory. Policy version 4 rejects older prepared markers.
Changing a rule or fallback requires rebuilding the affected scenarios and
their downstream matrices/results, not manually editing cache metadata.

## Regression checks

```powershell
python -B -m unittest tests.test_inventory_supplier_compat tests.test_por_supplier_compatibility
python -B -m unittest tests.test_por_supplier_linking
```

The first suites test ordered candidates, non-PV rules, inventory/consumer scope,
duplicates and missing suppliers, unchanged quantities/uncertainty, fallback
validation/closure, frozen decisions, tamper guards, cache recovery and native
product-matching functions extracted from both installed premise versions.
These tests do not build or replace production Brightway databases.
