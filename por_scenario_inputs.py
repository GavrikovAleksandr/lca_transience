"""Fresh premise inputs without carrying completed exports into a new build.

premise mutates the dictionaries supplied to NewDatabase, and Pathways copies
those dictionaries across years. Reconstruct inputs rather than copying an
updated dictionary, which can retain applied-sector flags and inventory stores.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping


_INPUT_FIELDS = (
    "model", "pathway", "year", "filepath", "exclude", "external scenarios",
)
_RUNTIME_FIELDS = frozenset({
    "applied functions", "cache", "configurations", "database",
    "database filepath", "database metadata cache filepath",
    "database metadata filepath", "database name", "external data",
    "heat diagnostics", "iam data", "index",
    "inventories metadata cache filepath", "mapping", "por supplier linking", "metal steel policy",
    "unmatched category flows", "additional valid regions",
})


def _copy_input_containers(value):
    """Copy plain input containers without copying opaque resource objects."""
    if type(value) is dict:
        return {key: _copy_input_containers(item) for key, item in value.items()}
    if type(value) is list:
        return [_copy_input_containers(item) for item in value]
    if type(value) is tuple:
        return tuple(_copy_input_containers(item) for item in value)
    if type(value) is set:
        return {_copy_input_containers(item) for item in value}
    if type(value) is frozenset:
        return frozenset(_copy_input_containers(item) for item in value)
    return value


def fresh_scenario_definitions(scenarios: Iterable[Mapping]) -> list[dict]:
    """Return independent user inputs for each new Brightway/Pathways build.

    Public fields supported by the installed legacy and native-store premise
    versions are retained, including optional ``filepath`` and ``exclude``.
    Known premise outputs and private runtime fields are discarded without
    reading their values. Unknown public fields raise instead of silently
    discarding a potential configuration option after a premise upgrade.

    External entry dictionaries and their input containers are copied, but
    each ``data`` resource is kept by identity; it may contain open readers or
    large datapackage objects. No premise import, cache access or DB write is
    needed. The source definitions are never modified.
    """
    fresh = []
    for position, scenario in enumerate(scenarios):
        if not isinstance(scenario, Mapping):
            raise TypeError(f"Scenario {position} must be a mapping.")
        unknown = [
            key for key in scenario
            if key not in _INPUT_FIELDS and key not in _RUNTIME_FIELDS
            and not (isinstance(key, str) and key.startswith("_"))
        ]
        if unknown:
            raise ValueError(
                f"Scenario {position} has unsupported public input fields: "
                f"{unknown!r}. Supported fields are {_INPUT_FIELDS!r}; review "
                "new premise options before adding them to this adapter."
            )

        definition = {
            key: _copy_input_containers(scenario[key])
            for key in _INPUT_FIELDS
            if key != "external scenarios" and key in scenario
        }
        if "external scenarios" in scenario:
            external = scenario["external scenarios"]
            if not isinstance(external, list):
                raise TypeError(f"Scenario {position}: external scenarios must be a list.")
            entries = []
            for entry_position, entry in enumerate(external):
                if not isinstance(entry, Mapping):
                    raise TypeError(
                        f"Scenario {position}, external entry {entry_position} "
                        "must be a mapping."
                    )
                entries.append({
                    key: item if key == "data" else _copy_input_containers(item)
                    for key, item in entry.items()
                })
            definition["external scenarios"] = entries
        fresh.append(definition)
    return fresh
