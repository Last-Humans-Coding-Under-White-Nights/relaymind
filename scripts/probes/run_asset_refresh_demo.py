"""Run two actual scheduled 300-second source jobs, never a shortened clock (RM43)."""

import argparse
import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from relaymind import launcher
from relaymind.assets.catalog import JOB_SECONDS
from relaymind.assets.remote_contracts import PUBLIC_SKILLS


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def validate_config(config: launcher.LauncherConfig, output: Path) -> None:
    sources = config.assets.sources
    if (
        len(sources) != 1
        or sources[0].kind != "qualified_remote"
        or sources[0].adapter_contract != PUBLIC_SKILLS.profile_id
        or config.assets.refresh.remote_seconds != 300
        or not config.recommendations.enabled
        or config.recommendations.primary != "lexical"
    ):
        raise ValueError(
            "demo requires explicit installed profile, lexical service and 300-second interval"
        )
    try:
        config.store_path.resolve().relative_to(output.resolve())
    except ValueError:
        raise ValueError("demo store must be disposable under output") from None
    if config.store_path.exists():
        raise ValueError("demo requires a new disposable store")


async def run(config_path: Path, output: Path) -> dict:
    config = launcher.load_config(config_path)
    validate_config(config, output)
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    report = dict(
        profile="qualified_source_refresh_demo",
        qualification="provisional",
        acceptance_eligible=False,
        source_profile=PUBLIC_SKILLS.profile_id,
        specification_version="0.59",
        capture_started_at=datetime.now(UTC).isoformat(),
        production_configuration_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),
        remote_seconds=300,
        default_remote_seconds=3600,
        job_trigger="existing_worker_periodic",
        clock="actual_monotonic_and_UTC",
        status="running",
        observations=[],
        limits=[
            "six_source_skills_not_300_asset_gate",
            "no_supported_platform_or_resource_pass",
            "no_real_source_change_or_outage_claim_without_observation",
        ],
    )

    def save():
        report["elapsed_ms"] = (time.monotonic() - started) * 1000
        (output / "observations.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    launched = launcher.build_app(config)
    app = launched.app
    worker = app.state.asset_catalog
    source_id = config.assets.sources[0].source_id
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1",
                headers={"Authorization": "Bearer " + app.state.application_token},
            ) as client:
                seen = set()
                while time.monotonic() - started < 300 + 3 * JOB_SECONDS:
                    jobs = worker.store.job_outcomes()
                    for job in reversed(jobs):
                        if (
                            job["source_id"] != source_id
                            or job["state"] != "succeeded"
                            or job["job_id"] in seen
                        ):
                            continue
                        seen.add(job["job_id"])
                        response = await client.post(
                            "/relay/v1/recommendations",
                            headers={"Idempotency-Key": f"demo-{len(seen)}"},
                            json=dict(
                                schema_version=1,
                                task_text="Report current health status for staging-api.",
                                conversation=[],
                                requested_kinds=["skill", "mcp_tool"],
                                selection_scope="discovery",
                            ),
                        )
                        snapshot = worker.store.snapshot().to_json_object()
                        observation = dict(
                            observed_elapsed_ms=(time.monotonic() - started) * 1000,
                            job=job,
                            events=worker.store.job_events(job["job_id"]),
                            catalog=snapshot,
                            readiness=(
                                await client.get("/relay/v1/recommendations/readiness")
                            ).json(),
                            query_status=response.status_code,
                            query=response.json(),
                        )
                        report["observations"].append(observation)
                        print(
                            json.dumps(
                                dict(
                                    successful_source_jobs=len(seen),
                                    job_id=job["job_id"],
                                    catalog_assets=len(snapshot["assets"]),
                                    query_status=response.status_code,
                                    elapsed_seconds=(time.monotonic() - started),
                                )
                            ),
                            flush=True,
                        )
                        save()
                    if len(seen) >= 2:
                        one, two = report["observations"][:2]
                        interval = (
                            datetime.fromisoformat(two["job"]["started_at"])
                            - datetime.fromisoformat(one["job"]["started_at"])
                        ).total_seconds()
                        if interval < 300:
                            raise RuntimeError("source jobs did not honor actual profile interval")
                        report["actual_job_start_interval_seconds"] = interval
                        report["source_change_observed"] = (
                            one["catalog"]["assets"] != two["catalog"]["assets"]
                        )
                        report["live_add_update_remove"] = (
                            "observed" if report["source_change_observed"] else "not_observed"
                        )
                        report["status"] = "completed"
                        break
                    save()
                    await asyncio.sleep(0.25)
                if report["status"] != "completed":
                    report["status"] = "incomplete"
                    report["source_jobs"] = jobs
    finally:
        save()
        launched.orchestrator.store.close()
    return report


def main() -> int:
    args = arguments()
    result = asyncio.run(run(args.config, args.output))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
