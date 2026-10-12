# Repo-owned supplier fallback snapshots

These auxiliary inventories are loaded only by explicit rules in
`configuration_file/inventory_supplier_compatibility.yaml`. They do not replace
the main PoR inventory workbook or introduce new ITOM production variables.

`por_pv_ma.json` preserves the existing Moroccan PV recipe and its necessary
legacy auxiliaries as ecoinvent-3.12 dictionaries. It is used only when neither
declared native Moroccan commercial-PV supplier exists. Provenance, yield,
lifetime, proxy technology shares and the lossless voltage interface are recorded
inside the file. See `docs/inventory_supplier_compatibility.md` for use and checks.

Source inventories and derivatives retain their applicable licenses, including
ecoinvent restrictions. Do not interpret the external PoR datapackage's general
CC0 declaration as permission to redistribute this snapshot. No repository
commit, push or external publication is performed by the adapter.
