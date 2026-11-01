"""Owned inference cost coverage; missing components stay unknown (RM40/AC31)."""

from collections.abc import Mapping, Sequence

from relaymind.canonical import canonical_json

METRICS = (
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "reasoning_tokens",
    "calls",
    "cpu_ms",
    "peak_bytes",
)


def _owned_cost_groups(row: Mapping) -> list[list[Mapping]]:
    groups = (
        [list(row["observed_costs"])]
        if row["observed_costs"]
        else [list(attempt.get("cost_observations", [])) for attempt in row["attempts"]]
    )
    for group in groups:
        provenance = [observation["provenance"] for observation in group]
        if len(provenance) != len(set(provenance)) or any(
            p not in ("observed", "reported") for p in provenance
        ):
            raise ValueError("duplicate_cost_observation")
    return groups or [[]]


def row_cost_observations(row: Mapping) -> list[dict]:
    """Retain each owned attempt/whole-slot observation once per provenance."""
    return [dict(observation) for group in _owned_cost_groups(row) for observation in group]


def summarize_asset_costs(rows: Sequence[Mapping]) -> list[dict]:
    """Complete totals require every owned component; subtotals preserve partials."""
    result = []
    owned_groups = [_owned_cost_groups(row) for row in rows]
    for provenance in ("observed", "reported"):
        for metric in METRICS:
            known, components = 0, []
            for row, groups in zip(rows, owned_groups, strict=True):
                # Coverage is established independently for every owned attempt,
                # never by counting flattened observations from another attempt.
                selected = [
                    next(
                        (o["value"].get(metric) for o in group if o["provenance"] == provenance),
                        None,
                    )
                    for group in groups
                ]
                expected = len(groups)
                resources = row["resources"]
                if (
                    metric in ("cpu_ms", "peak_bytes")
                    and provenance == "observed"
                    and resources.get(metric) is not None
                ):
                    selected = [resources[metric]]
                    expected = 1
                components.extend(v for v in selected if v is not None)
                complete = len(selected) >= expected and all(v is not None for v in selected)
                if metric in ("cpu_ms", "peak_bytes") and resources.get(metric) is not None:
                    complete = complete and resources["coverage"] == "complete"
                # The verified built-in has exactly one query invocation and
                # its attempt contains that observation inclusively. Count it
                # once; arbitrary unbound embedding arrays stay incomplete.
                native = [a for a in row["attempts"] if a.get("mechanism") == "hybrid_weighted_rrf"]
                inclusive = (
                    len(native) == 1
                    and len(row["embeddings"]) == 1
                    and canonical_json(native[0]["cost_observations"])
                    == canonical_json(row["embeddings"][0]["cost_observations"])
                )
                if row["embeddings"] and not row["observed_costs"] and not inclusive:
                    # V1 has no attempt/embedding containment identity. Preserve
                    # admitted attempt components once, but require a verified
                    # whole-slot observation before asserting complete coverage.
                    resource_complete = (
                        metric in ("cpu_ms", "peak_bytes")
                        and provenance == "observed"
                        and resources.get(metric) is not None
                        and resources["coverage"] == "complete"
                    )
                    complete = complete and resource_complete
                known += bool(complete)
            subtotal = (
                (max(components) if metric == "peak_bytes" else sum(components))
                if components
                else None
            )
            result.append(
                dict(
                    metric=metric,
                    provenance=provenance,
                    known=known,
                    unknown=len(rows) - known,
                    total=subtotal if rows and known == len(rows) else None,
                    known_subtotal=subtotal,
                )
            )
    return result
