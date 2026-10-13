# Sources and interpretation of PoR reporting classifications

`configuration_file/por_classifications.yaml` fills classification gaps for
explicitly named foreground activities. It is not an amendment to ecoinvent's
master-data classifications. Existing activity metadata and an exact
name/reference-product match in a reference catalog have priority. Unmatched
activities retain `undefined` and remain in reported totals.

Every foreground rule is marked `provisional`: an official category definition
can be verified while applying it to a custom modelled activity is still a
reporting inference. Sources were checked on 12 October 2026 against the UN
Statistics Division's CPC 2.1 and ISIC Rev.4 registry and the local ecoinvent 3.12
metadata. The YAML contains the source URL and reason for each rule. Matching
uses both an explicit activity name/family and an exact reference product;
reference selectors also identify the product and unit. No rule assigns all
chemicals, all electricity-related names, or all waste-related names to one
industry.

`configuration_file/por_classification_references.json` stores classification
metadata for 14 selected reference identities used by the foreground rules.
Additional exact references are available in the complete stock catalog below.
It was extracted in read-only mode from the local native ecoinvent 3.12 cutoff
database on 12 October 2026. Every matching regional record agrees on both
recorded systems. Each entry records its source regions and the number of
matching regional datasets; it contains no exchange inventory, activity code or
database address. Keeping this small metadata snapshot allows the rules to
resolve consistently when a bundled premise classification catalog omits a
reference such as nylon or butanediol. The rules' use of these verified native
labels for a custom foreground activity remains `provisional`.

`configuration_file/ecoinvent_312_classification_catalog.json` is the complete
stock classification metadata snapshot for recovery of legacy exports. It
projects 26,533 native ecoinvent 3.12 cutoff activity records into 11,669 exact
name/reference-product/unit identities. It preserves original labels, including
their existing abbreviations and unusual product assignments. Each system is
retained only when its nonempty values agree across matching source records;
13 identities have a conflicting CPC or ISIC system, which is recorded under
`ambiguous_systems` and omitted from that identity's assigned classifications.
Missing source labels are also left missing. Source regions, source record
counts, classification record counts and a deterministic hash of the source
metadata projection provide the extraction audit. This catalog contains no
exchange inventories, graph edges, activity codes, UUIDs or database addresses.

The full stock catalog is a native-label recovery source, separate from the
reviewed foreground rules and their selected reference metadata. Existing
scenario metadata remains authoritative. An unresolved conflict in the stock
catalog must not be resolved by selecting the first region or a lower-priority
sparse catalog. Regional source records may still be authoritative for an
activity when that activity retains its own classification metadata.

The ecoinvent-derived snapshots remain subject to the project's applicable
ecoinvent licence; a metadata-only projection does not imply public
redistribution rights. Keep them with the licensed project and check the
[ecoinvent EULA](https://support.ecoinvent.org/eula) before external distribution.
The [ecoinvent sharing guidance](https://support.ecoinvent.org/faqs-guide)
distinguishes shareable calculated results from raw database information.

CPC classifies products and services; ISIC classifies economic activities. An
electricity co-product of a chemical or waste-treatment process therefore must
not automatically receive the electric-power-generation industry. Similarly,
captured CO2 used as chemical feedstock differs from the service of transporting
and storing already captured CO2. These distinctions are supported by the UN
definitions of [basic chemical manufacture](https://unstats.un.org/unsd/classifications/Econ/Detail/EN/27/2011)
and [electric power generation](https://unstats.un.org/unsd/classifications/Econ/Detail/EN/27/3510).

The custom PLA, PBS and PET resin rules use the UN
[polyethers and polyesters category, CPC 34740](https://unstats.un.org/unsd/classifications/Econ/Detail/EN/1074/34740).
The custom PBR rule uses the UN
[synthetic-rubber category, CPC 34800](https://unstats.un.org/unsd/classifications/Econ/Detail/EN/1074/34800).
Nylon and other primary-form polymers use the corresponding reference metadata
or [CPC 34790](https://unstats.un.org/unsd/classifications/Econ/Detail/EN/1074/34790).
All use the applicable
[primary-form plastics and synthetic rubber industry, ISIC 2013](https://unstats.un.org/unsd/classifications/Econ/Detail/EN/27/2013).
The workbook describes PLA polymerisation, PBS polycondensation and lignin
oxypropylation; the latter receives the broad polymer group because its product
is only named `polyol`. MDI is a monomer, and its proxy intentionally retains the
ecoinvent MDI activity's existing product-sector convention.

Native ecoinvent labels can differ from a newly inferred product category.
The inspected native PET activity carries CPC 34710, native polybutadiene carries
CPC 34790, and several benzene co-products carry CPC 33100. These original
assignments are preserved wherever present. A missing label on a newly modelled
foreground activity does not justify copying a coke category onto a benzene
upgrading process. The latter uses the UN
[hydrocarbon product family, CPC 34110](https://unstats.un.org/unsd/classifications/Econ/Detail/EN/1074/34110)
with a provisional basic-chemical manufacturing label. The raw-stream rules are
broad family proxies and make no claim that their streams are purified.

Import and market rules are supply proxies. Where an equivalent native market
is used, its ISIC label represents ecoinvent's product-sector reporting
convention, not the legal industry of an importer or terminal operator. For raw
aromatic imports and the custom benzene/p-xylene aggregation rules no unique
industry is inferred, and ISIC stays unresolved.

The three historical activities whose names mention MTO, plastic-waste
gasification and plastic-waste pyrolysis all have the reference product
`carbon dioxide, stored`. The inventory contains them as combined CO2 service
activities; their names do not make them waste-treatment or electricity
production activities. The separate `PoR activity category` label reports
`CO2 compression, transport and storage`. CPC and ISIC remain unresolved for
this service. [CPC 34210](https://unstats.un.org/unsd/classifications/Econ/Detail/EN/1074/34210)
defines industrial gas products and is not used to assert an official code for
the combined storage service. The rules change neither its inventory nor its
legacy carbon-credit treatment.

The renewable-power and steam rules use
[CPC 17100](https://unstats.un.org/unsd/classifications/Econ/Detail/EN/1074/17100)
and [CPC 17300](https://unstats.un.org/unsd/classifications/Econ/Detail/EN/1074/17300)
with their relevant supply activities. Gaseous hydrogen and captured CO2 chemical
feedstock use CPC 34210 and ISIC 2011. Methanol and bio-based butanediol retain
the alcohol product family or verified equivalent metadata. These classifications
do not establish net carbon removal, renewable certification, storage permanence,
or inventory quality.
