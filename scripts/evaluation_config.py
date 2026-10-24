"""Shared, validated runner configurations (RM03, RM25–RM26, RM29, RM31–RM32)."""

import argparse
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from relaymind.orchestrator.budgets import LocalBudgets
from relaymind.orchestrator.performance import PerformanceSettings
from relaymind.orchestrator.placement import PlacementSettings
from relaymind.selection.settings import SelectionSettings


def add_configuration_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--performance", nargs="+", action="extend", default=[], metavar="KEY=VALUE"
    )
    parser.add_argument("--budgets", nargs="+", action="extend", default=[], metavar="KEY=VALUE")
    parser.add_argument("--selection-chain", nargs="+", action="extend", metavar="SELECTOR")
    parser.add_argument("--no-local-model", action="store_true")
    parser.add_argument("--placement", nargs="+", action="extend", default=[], metavar="KEY=VALUE")
    parser.add_argument("--timing-samples", type=str)
    parser.add_argument("--checkpoint", type=str)


def _settings(values: list[str], defaults: dict[str, Any], name: str) -> dict[str, Any]:
    result = {}
    for item in values:
        key, separator, raw = item.partition("=")
        key, raw = key.strip(), raw.strip()
        if not separator or not raw or key not in defaults:
            raise ValueError(f"{name}: expected a known key=value, got {item!r}")
        if key in result:
            raise ValueError(f"{name}: duplicate key {key}")
        kind = type(defaults[key])
        if kind is bool:
            if raw not in ("true", "false"):
                raise ValueError(f"{name}.{key} must be true or false")
            result[key] = raw == "true"
        else:
            try:
                result[key] = kind(raw)
            except ValueError as exc:
                raise ValueError(f"{name}.{key}: invalid value {raw!r}") from exc
    return result


@dataclass(frozen=True)
class RunnerConfiguration:
    performance: PerformanceSettings
    budgets: LocalBudgets
    selection: SelectionSettings
    local_model_configured: bool
    placement: PlacementSettings = field(default_factory=PlacementSettings)

    def gateway_options(self) -> dict[str, Any]:
        return {
            "performance": self.performance.to_json_object(),
            "budgets": self.budgets.to_json_object(),
            "selection": {**asdict(self.selection), "chain": list(self.selection.chain)},
            "local_model": self.local_model_configured,
            "placement": asdict(self.placement),
        }

    def to_json_object(self) -> dict[str, Any]:
        return {
            "performance": self.performance.to_json_object(),
            "budgets": self.budgets.to_json_object(),
            "selection": {**asdict(self.selection), "chain": list(self.selection.chain)},
            "local_model_configured": self.local_model_configured,
            "placement": asdict(self.placement),
            "enabled_workflows": [],
            "routing_cache": False,
        }


def configuration_from_args(args: argparse.Namespace) -> RunnerConfiguration:
    performance = _settings(
        getattr(args, "performance", []), PerformanceSettings().to_json_object(), "performance"
    )
    if getattr(args, "early_headers", False):
        if performance.get("early_headers") is False:
            raise ValueError("--early-headers conflicts with early_headers=false")
        performance["early_headers"] = True
    local = not getattr(args, "no_local_model", False)
    chain = getattr(args, "selection_chain", None) or (
        ["model_router"] if local else ["example_match"]
    )
    if not local and "model_router" in chain:
        raise ValueError("model_router requires a local model")
    return RunnerConfiguration(
        PerformanceSettings(**performance),
        LocalBudgets(
            **_settings(getattr(args, "budgets", []), LocalBudgets().to_json_object(), "budgets")
        ),
        SelectionSettings(chain=tuple(chain)),
        local,
        PlacementSettings(
            **_settings(getattr(args, "placement", []), asdict(PlacementSettings()), "placement")
        ),
    )


def label_report(report: str, configuration: dict[str, Any] | None) -> str:
    heading, _, body = report.partition("\n")
    label = (
        "Configuration: not recorded in this export.\n"
        if configuration is None
        else "Configuration (reported separately):\n\n```json\n"
        + json.dumps(configuration, indent=2, sort_keys=True)
        + "\n```\n"
    )
    return heading + "\n\n" + label + "\n" + body.lstrip("\n")
