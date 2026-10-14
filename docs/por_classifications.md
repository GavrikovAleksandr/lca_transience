# Activity classifications and complete contribution totals

Classification labels describe the activities behind an impact result. They do
not change exchange amounts, electricity recipes, carbon credits or
characterization factors. CPC is a product/service classification; ISIC describes
the economic activity. Neither establishes that an inventory is geographically
accurate or environmentally preferable.

## Why the repository handles this explicitly

Two inspected Pathways installations treat missing classifications differently.
One allocates both `unclassified` and `undefined` result coordinates; the other
allocates only `unclassified`, although its grouping code still uses `undefined`.
The latter can silently omit contributions when building its sparse aggregation
matrix. This includes negative contributions, not just positive burdens.

There is also an export issue: premise can release its in-memory inventories or
keep them in native inventory stores/checkpoints before writing classification
metadata. An exporter that only reads `scenario['database']` can then create a
header-only `classifications.csv`, despite exporting valid numerical matrices.

The repository addresses both issues without editing installed packages:

- `por_classifications.py` captures classification metadata before matrix export
  and supplies the populated resource to the package exporter.
- `pathways_classification_compat.py` retains both fallback labels and checks that
  category/location grouping assigns every activity column exactly once. It
  installs the same checks in Windows multiprocessing workers.

An unclassified contribution is retained in full. A classification lookup must
never decide whether its impact exists.

## Workflow in the notebooks

In `1_export_packages.ipynb`, the existing
`attach_por_pathways_export(ndb, stage='pathways')` call now captures classifications
after scenario updating/supplier linking and before matrix export. Both legacy
list/cache layouts and newer native store/checkpoint layouts are supported. The
native reader uses activity metadata, not exchange graph materialization. Legacy
classification metadata sidecars are read without deleting them.

The completed package contains the standard four-column classification resource:
`name`, `reference product`, `classification_system`, `classification_code`.
The notebook displays a summary and audit preview. Detailed CSV/JSON audits are
saved under `export/por_classifications/<snapshot hash>/`, outside the package's
temporary resources. The audit records sources, provisional rules, reasons,
unresolved/conflicting labels and source fingerprints.

In `2_calc_impacts.ipynb`, the guard is installed before constructing `Pathways`.
`prepare_pathways_classifications(p, package_path=name_zip_file)` then repairs the
classification map **in memory** using the package's actual matrix-index identities.
It can therefore repair an existing header-only package for calculation without
rebuilding its matrices. It does not overwrite the ZIP. The notebook displays the
coverage and a table of provisional/unresolved assignments before calculation.

Restart the kernel after updating these helpers and rerun notebook 2 from the
beginning. Previously saved results with omitted contributions cannot be repaired
by relabelling their remaining rows: they must be recalculated. Rerun notebook 1
when you next export a package, so its classification resource is complete too.
Do not use an already-running package builder to test newly edited hooks.

## How missing classifications are assigned

The lookup retains existing scenario or explicitly packaged metadata first.
It then uses verified exact name/product/unit reference metadata, constrained
equivalent-name normalization and explicit foreground rules. Unitless bundled
fallback catalogs cannot resolve a known conflict in the typed reference data.
Human-selected Pathways reporting mappings are retained.

`configuration_file/por_classifications.yaml` contains the reviewable foreground
rules. Their inferred assignments are marked `provisional`, with a source and
reason. They cover resin production, aromatic upgrading/imports, olefins,
methanol, hydrogen, electricity, steam and related custom markets. Existing
ecoinvent labels are not rewritten merely because a newly inferred category
would seem more intuitive.

`por_classification_references.json` contains selected native reference labels.
`ecoinvent_312_classification_catalog.json` provides a metadata-only stock 3.12
recovery catalog. Regional source labels must agree for a shared identity/system
to be accepted. Pathways keys classifications by name/product, not location/unit;
incompatible labels for that shared key are therefore reported as ambiguous,
not resolved by choosing the first or closest region.

Genuine unknowns remain `undefined`. The historical combined CCS service
activities receive a descriptive `PoR activity category` of
`CO2 compression, transport and storage`, but no invented official CPC/ISIC code.
This extra system is available in `p.classification_details`; it does not alter
the selected CPC/ISIC grouping or credit treatment. See
[the rule sources and interpretation](por_classification_rules_sources.md) for
category definitions, boundary decisions and licensing restrictions.

Inherited metadata can also contain **several incompatible codes for the same
system inside one activity**, not just different codes across regional copies.
For example, an allocated gypsum co-product of petroleum-coke production can
carry both CPC 33500 and 15200. The reader records both labels in the audit and
uses `undefined` for that system only; its valid ISIC label and all numerical
contributions remain intact. This also applies to legacy metadata sidecars and
older package CSV rows. Descriptions that differ but retain the same official
numeric code are not conflicts. Malformed metadata and contradictory explicitly
authored classification rules still raise errors rather than being silently
accepted.

## Reviewing or extending the rules

1. Inspect `classification_review` in notebook 2, or the export audit CSV.
2. Check the activity's actual reference product, unit and process boundary.
3. Add a narrowly matched YAML rule, using a verified equivalent reference or
   documented official classifications. Include a unique `id`, `status`, `source`
   and `reason`; do not classify a whole name family without product constraints.
4. Restart/reload the helpers, inspect the changed audit and rerun the tests.
   Overlapping incompatible assignments fail rather than silently selecting one.
5. Recalculate results; compare signed totals separately from their redistribution
   between categories. Classification recovery is not an inventory-quality check.

For a comparison between environments, also hold the ZIP, LCIA method/version,
scenario, year, demand and calculation settings constant. The notebooks print
the LCIA ecoinvent version, but this change does not alter its current selection.
Availability of 3.12-specific methods is a separate issue.

## Check against the current package

The read-only check on 12 October 2026 recovered CPC labels for 14,043 of the
14,047 activity/product pairs in the inspected twelve-scenario/year package.
The four unresolved pairs are three historical combined CCS-service variants
and one gypsum co-product with conflicting native reference metadata. These are
retained, not assigned a misleading official category. ISIC has more unresolved
entries because a product/co-product does not always establish a manufacturing
industry.

The CL 2025 grouping check assigned all 42,988 numerical matrix columns exactly
once. An artificial signed contribution vector conserved its total through the
installed newer Pathways aggregation code. This verifies classification coverage
and aggregation, not a new production LCIA result; the notebook must still be
rerun to produce corrected impact results.

Focused tests cover both installed environments, populated export after inventory
release, metadata-only native checkpoints, source conflicts/provenance, legacy
sidecars, complete activity coverage and signed-total conservation in a spawned
worker. No production Brightway databases are changed by these tests.
