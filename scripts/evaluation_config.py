"""Shared, validated runner configurations (RM03, RM25–RM26, RM29, RM31–RM32)."""

import argparse
import json
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

from relaymind.orchestrator.budgets import LocalBudgets
from relaymind.orchestrator.performance import PerformanceSettings
from relaymind.selection.settings import SelectionSettings


class ConfigurationError(ValueError):
    """Runner options that do not describe a valid configuration: a usage error.

    Only option checks raise it, before anything starts, so a runner reports it as a
    usage error and lets any other failure keep its traceback.
    """


def add_configuration_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--performance", nargs="+", action="extend", default=[], metavar="KEY=VALUE"
    )
    parser.add_argument("--budgets", nargs="+", action="extend", default=[], metavar="KEY=VALUE")
    parser.add_argument("--selection-chain", nargs="+", action="extend", metavar="SELECTOR")
    parser.add_argument("--no-local-model", action="store_true")
    parser.add_argument("--external-base-url")
    parser.add_argument("--external-model")
    parser.add_argument("--external-context-limit", type=int)
    parser.add_argument(
        "--external-region", help="the endpoint region, if known (needs an external endpoint)"
    )


def _settings(values: list[str], defaults: dict[str, Any], name: str) -> dict[str, Any]:
    result = {}
    for item in values:
        key, separator, raw = item.partition("=")
        key, raw = key.strip(), raw.strip()
        if not separator or not raw or key not in defaults:
            raise ConfigurationError(f"{name}: expected a known key=value, got {item!r}")
        if key in result:
            raise ConfigurationError(f"{name}: duplicate key {key}")
        kind = type(defaults[key])
        if kind is bool:
            if raw not in ("true", "false"):
                raise ConfigurationError(f"{name}.{key} must be true or false")
            result[key] = raw == "true"
        else:
            try:
                result[key] = kind(raw)
            except ValueError as exc:
                raise ConfigurationError(f"{name}.{key}: invalid value {raw!r}") from exc
    return result


@dataclass(frozen=True)
class ExternalLabel:
    """The external endpoint a run used, as its label names it: never a credential.

    host is the base URL's host and port only; user info, path, and query are left out.
    """

    model: str
    host: str
    region: str | None = None


def external_label(base_url: str, model: str, region: str | None = None) -> ExternalLabel:
    try:
        parts = urlsplit(base_url)
        port = parts.port
    except ValueError as exc:
        raise ConfigurationError(f"--external-base-url: invalid URL {base_url!r}") from exc
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigurationError("--external-base-url must be an absolute http or https URL")
    host = parts.hostname if port is None else f"{parts.hostname}:{port}"
    return ExternalLabel(model, host, region)


@dataclass(frozen=True)
class RunnerConfiguration:
    performance: PerformanceSettings
    budgets: LocalBudgets
    selection: SelectionSettings
    local_model_configured: bool
    external: ExternalLabel | None = None

    def gateway_options(self) -> dict[str, Any]:
        return {
            "performance": self.performance.to_json_object(),
            "budgets": self.budgets.to_json_object(),
            "selection": {**asdict(self.selection), "chain": list(self.selection.chain)},
            "local_model": self.local_model_configured,
        }

    def to_json_object(self) -> dict[str, Any]:
        return {
            "performance": self.performance.to_json_object(),
            "budgets": self.budgets.to_json_object(),
            "selection": {**asdict(self.selection), "chain": list(self.selection.chain)},
            "local_model_configured": self.local_model_configured,
            "external": None if self.external is None else asdict(self.external),
            "placement": {"policy": "local_first"},
            "enabled_workflows": [],
            "routing_cache": False,
        }


def _external_from_args(args: argparse.Namespace) -> ExternalLabel | None:
    base_url = getattr(args, "external_base_url", None)
    model = getattr(args, "external_model", None)
    limit = getattr(args, "external_context_limit", None)
    region = getattr(args, "external_region", None)
    given = (base_url, model, limit)
    if any(value is not None for value in given) and not all(given):
        raise ConfigurationError(
            "--external-base-url, --external-model, and --external-context-limit go together"
        )
    if base_url is None:
        if region is not None:
            raise ConfigurationError("--external-region needs an external endpoint")
        return None
    return external_label(base_url, model, region)


def configuration_from_args(args: argparse.Namespace) -> RunnerConfiguration:
    """The validated configuration; every option error is a ConfigurationError."""
    performance = _settings(
        getattr(args, "performance", []), PerformanceSettings().to_json_object(), "performance"
    )
    if getattr(args, "early_headers", False):
        if performance.get("early_headers") is False:
            raise ConfigurationError("--early-headers conflicts with early_headers=false")
        performance["early_headers"] = True
    local = not getattr(args, "no_local_model", False)
    chain = getattr(args, "selection_chain", None) or (
        ["model_router"] if local else ["example_match"]
    )
    if not local and "model_router" in chain:
        raise ConfigurationError("model_router requires a local model")
    budgets = _settings(getattr(args, "budgets", []), LocalBudgets().to_json_object(), "budgets")
    try:
        return RunnerConfiguration(
            PerformanceSettings(**performance),
            LocalBudgets(**budgets),
            SelectionSettings(chain=tuple(chain)),
            local,
            _external_from_args(args),
        )
    except ConfigurationError:
        raise
    except ValueError as exc:
        raise ConfigurationError(str(exc)) from exc


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
