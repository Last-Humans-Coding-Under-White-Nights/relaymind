"""Sandbox demo data service and the seven demo tools (RM23, spec section 9).

The service reads a seed fixture (synthetic data, never a production
resource) and keeps a disposable working copy of it in its work
directory. Read tools answer from that copy; delete_demo_database is the
only tool that changes it. Every read and the delete's compare and write
run under a cross-process file lock in the work directory (fcntl on
POSIX, msvcrt on Windows; spec section 3), so services in several
processes that share one working copy see one consistent state and a
stale revision is a conflict with no mutation. A new service keeps an
existing working copy; ``reset`` (or ``reset=True``) copies the seed
again.

The fixture has one ``revision``. Every listed database reports it,
delete_demo_database compares expected_revision with it under the lock,
and every delete and every reset of an existing working copy bumps it,
so a revision read before any fixture change is stale afterwards
(section 9). A delete returns the revision it stored.

Each tool's output is a pure function of a state dict. The service runs
those functions on the working copy, and the load check runs the same
functions on the seed and validates every output against its published
schema.

Every output carries the fixture scenario and the observation time
(section 9). An ``unavailable`` health status is a successful
observation; a tool listed in the fixture's ``unreachable_tools`` raises
ToolUnavailableError, which the registry records as a failed ToolResult.

Tool schemas are published in ``schemas/demo/<tool>.v1.json``.
"""

import contextlib
import json
import os
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from relaymind.schema_util import first_schema_error, load_packaged_schema
from relaymind.store import TraceClock
from relaymind.tools.registry import (
    MAX_TOOL_TIMEOUT_SECONDS,
    SideEffect,
    StrictValidator,
    ToolConflictError,
    ToolDeclaration,
    ToolRegistry,
    ToolUnavailableError,
    utc_timestamp,
)

# Authorization rules for the demo tools (RM08): reads and the sandbox
# mutation need different caller permissions.
READ_PERMISSION = "demo.read"
MUTATE_PERMISSION = "demo.mutate"

# -- tool outputs as pure functions of a state dict ---------------------------

Output = Callable[[Mapping[str, Any], Mapping[str, Any]], dict[str, Any]]


def _health(state: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
    health = state["services"][args["service"]]["health"]
    return {
        "service": args["service"],
        "status": health["status"],
        "checked_at": health["checked_at"],
    }


def _deployment(state: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
    deployment = state["services"][args["service"]]["latest_deployment"] or {}
    return {
        "service": args["service"],
        "deployment_id": deployment.get("deployment_id"),
        "deployed_at": deployment.get("deployed_at"),
        "revision": deployment.get("revision"),
    }


def _incidents(state: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
    window = args["window_minutes"]
    counts = state["services"][args["service"]]["incidents"][str(window)]
    return {
        "service": args["service"],
        "window_minutes": window,
        **{key: counts[key] for key in ("total", "critical", "warning", "info")},
    }


def _metrics(state: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
    window = args["window_minutes"]
    samples = state["services"][args["service"]]["metrics"][str(window)]
    return {"service": args["service"], "window_minutes": window, "samples": samples}


def _logs(state: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
    """The newest entries first, whatever the fixture order (section 9).

    Timestamps are fixed-width UTC strings, so text order is time order.
    """
    entries = sorted(
        state["services"][args["service"]]["logs"],
        key=lambda entry: entry["timestamp"],
        reverse=True,
    )
    limit = args["limit"]
    return {
        "service": args["service"],
        "entries": entries[:limit],
        "truncated": len(entries) > limit,
    }


def _databases(state: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
    """Each database with the one fixture revision (section 9)."""
    revision = state["revision"]
    return {"databases": [{**db, "revision": revision} for db in state["databases"]]}


# The read tools' outputs, without the fixture metadata. The state is
# parsed fresh from the working copy for every call, and the registry
# copies handler output, so these functions return parts of it uncopied.
_OUTPUTS: dict[str, Output] = {
    "get_service_health": _health,
    "get_latest_deployment": _deployment,
    "get_incident_counts": _incidents,
    "get_metrics": _metrics,
    "get_recent_logs": _logs,
    "list_demo_databases": _databases,
}


def _deleted(database_id: str, revision: int) -> dict[str, Any]:
    """delete_demo_database output, without the fixture metadata.

    revision is the fixture revision stored by the delete.
    """
    return {"database_id": database_id, "deleted": True, "revision": revision}


def _check_reachable(state: Mapping[str, Any], tool: str) -> None:
    if tool in state["unreachable_tools"]:
        raise ToolUnavailableError(f"sandbox tool {tool} is not reachable")


# Every demo tool: its side-effect class and how validated arguments
# reach the service (RM08 handlers).
_TOOLS: dict[str, tuple[SideEffect, Callable[["SandboxDataService", dict[str, Any]], Any]]] = {
    **{
        name: (SideEffect.READ_ONLY, lambda svc, args, tool=name: svc._observe(tool, args))
        for name in _OUTPUTS
    },
    "delete_demo_database": (
        SideEffect.MUTATING,
        lambda svc, args: svc.delete_database(
            args["database_id"], expected_revision=args["expected_revision"]
        ),
    ),
}

DEMO_TOOL_NAMES: frozenset[str] = frozenset(_TOOLS)

# The argument that names what a mutating tool changes (RM11, section 9).
_CONFIRMATION_TARGETS: dict[str, str] = {"delete_demo_database": "database_id"}

DEMO_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    name: load_packaged_schema(__package__, f"demo/{name}.v1.json") for name in DEMO_TOOL_NAMES
}

# The service IDs and windows come from the published input schemas.
_COUNT_INPUT = DEMO_TOOL_SCHEMAS["get_incident_counts"]["input_schema"]["properties"]
DEMO_SERVICES: tuple[str, ...] = tuple(_COUNT_INPUT["service"]["enum"])
DEMO_WINDOWS: tuple[int, ...] = tuple(_COUNT_INPUT["window_minutes"]["enum"])

_OUTPUT_VALIDATORS = {
    name: StrictValidator(schema["output_schema"]) for name, schema in DEMO_TOOL_SCHEMAS.items()
}

_STATE_FILE = "sandbox_state.json"
_TEMP_PREFIX = "sandbox_state."
_TEMP_SUFFIX = ".partial"
_LOCK_FILE = "sandbox_state.lock"
_PROBE_META = {"scenario": "probe", "observed_at": "2026-01-01T00:00:00Z"}


class SandboxFixtureError(ValueError):
    """The seed fixture does not match the section 9 tool contract."""


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on path that other processes honor.

    The lock is not reentrant: do not take it twice in one call chain.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            while True:
                try:
                    # LK_LOCK gives up after ten one-second tries; keep waiting.
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    continue
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class SandboxDataService:
    """Deterministic sandbox data for the demo tools (RM23)."""

    def __init__(
        self,
        seed_path: Path,
        *,
        work_dir: Path,
        clock: TraceClock | None = None,
        reset: bool = False,
    ) -> None:
        """Open the working copy in work_dir.

        The seed is read and checked first. An existing working copy is
        kept, so services that share work_dir share state. It is created
        from the seed when missing, or reset when reset is true.
        """
        self.seed_path = Path(seed_path)
        self.state_path = Path(work_dir) / _STATE_FILE
        self.lock_path = Path(work_dir) / _LOCK_FILE
        self._clock = clock or TraceClock()
        seed = self._load_seed()
        with self._locked():
            if reset or not self.state_path.exists():
                self._replace_with(seed)

    # -- state ---------------------------------------------------------------

    def reset(self) -> None:
        """Replace the working copy with the seed fixture (section 9).

        The seed file is read again and checked like at construction; a
        seed that fails the check raises SandboxFixtureError and leaves
        the working copy as it was. The scenario and label then come from
        the new copy.
        """
        seed = self._load_seed()
        with self._locked():
            self._replace_with(seed)

    def _load_seed(self) -> dict[str, Any]:
        seed: dict[str, Any] = json.loads(self.seed_path.read_text("utf-8"))
        _check_fixture(seed)
        return seed

    def _replace_with(self, seed: Mapping[str, Any]) -> None:
        """Write the seed as the working copy; call under the lock.

        A reset is a fixture change, so over an existing copy the fixture
        revision moves past both the stored and the seed revision, and a
        revision read before the reset conflicts (section 9).
        """
        state = dict(seed)
        if self.state_path.exists():
            stored = self._read()["revision"]
            state["revision"] = max(stored, seed["revision"]) + 1
        self._write(state)

    @property
    def scenario(self) -> str:
        """The scenario of the working copy, as every output reports it."""
        with self._locked():
            return self._read()["scenario"]

    @property
    def label(self) -> str:
        """The sandbox label of the working copy (RM23)."""
        with self._locked():
            return self._read()["label"]

    def state_bytes(self) -> bytes:
        """The working copy as stored, for evidence that nothing changed."""
        with self._locked():
            return self.state_path.read_bytes()

    def _locked(self) -> contextlib.AbstractContextManager[None]:
        return file_lock(self.lock_path)

    def _read(self) -> dict[str, Any]:
        # Always read under the file lock: another process may have
        # committed a delete since the last call.
        return json.loads(self.state_path.read_text("utf-8"))

    def _write(self, state: Mapping[str, Any]) -> None:
        """Replace the working copy through a unique temp file in its directory."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            prefix=_TEMP_PREFIX, suffix=_TEMP_SUFFIX, dir=self.state_path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(state, indent=2, ensure_ascii=False))
            os.replace(tmp, self.state_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def _fixture_meta(self, state: Mapping[str, Any]) -> dict[str, str]:
        return {"scenario": state["scenario"], "observed_at": utc_timestamp(self._clock.utc_now())}

    def _observe(self, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """Run one read tool against the working copy."""
        with self._locked():
            state = self._read()
        _check_reachable(state, tool)
        return {**_OUTPUTS[tool](state, args), "fixture": self._fixture_meta(state)}

    # -- read tools ----------------------------------------------------------

    def service_health(self, service: str) -> dict[str, Any]:
        return self._observe("get_service_health", {"service": service})

    def latest_deployment(self, service: str) -> dict[str, Any]:
        return self._observe("get_latest_deployment", {"service": service})

    def incident_counts(self, service: str, window_minutes: int) -> dict[str, Any]:
        args = {"service": service, "window_minutes": window_minutes}
        return self._observe("get_incident_counts", args)

    def metrics(self, service: str, window_minutes: int) -> dict[str, Any]:
        return self._observe("get_metrics", {"service": service, "window_minutes": window_minutes})

    def recent_logs(self, service: str, limit: int) -> dict[str, Any]:
        return self._observe("get_recent_logs", {"service": service, "limit": limit})

    def list_databases(self) -> list[dict[str, Any]]:
        """The sandbox databases that still exist, in fixture order, each
        with the fixture revision."""
        with self._locked():
            return _databases(self._read(), {})["databases"]

    def databases_output(self) -> dict[str, Any]:
        """list_demo_databases output data."""
        return self._observe("list_demo_databases", {})

    # -- mutating tool -------------------------------------------------------

    def delete_database(self, database_id: str, *, expected_revision: int) -> dict[str, Any]:
        """Delete one sandbox database if the fixture revision still matches.

        The read, the compare with the fixture revision, and the write
        happen under one cross-process lock; on a mismatch or an unknown
        ID nothing is written. The revision is compared before the ID is
        looked up, so a stale revision is always revision_conflict. A
        delete bumps the fixture revision and returns the value it stored
        (section 9).
        """
        with self._locked():
            state = self._read()
            _check_reachable(state, "delete_demo_database")
            databases = state["databases"]
            # Section 9: the revision compare comes first, so a stale
            # revision is a conflict even for a database already deleted.
            if state["revision"] != expected_revision:
                raise ToolConflictError(
                    "revision_conflict",
                    f"the sandbox fixture is at revision {state['revision']}, "
                    f"not {expected_revision}",
                )
            match = next((db for db in databases if db["database_id"] == database_id), None)
            if match is None:
                raise ToolConflictError(
                    "database_not_found", f"sandbox database {database_id!r} does not exist"
                )
            state["databases"] = [db for db in databases if db is not match]
            state["revision"] += 1
            self._write(state)
        return {**_deleted(database_id, state["revision"]), "fixture": self._fixture_meta(state)}


def _probe_calls(seed: Mapping[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every read tool call whose output depends on the seed."""
    for service in DEMO_SERVICES:
        yield "get_service_health", {"service": service}
        yield "get_latest_deployment", {"service": service}
        for window in DEMO_WINDOWS:
            yield "get_incident_counts", {"service": service, "window_minutes": window}
            yield "get_metrics", {"service": service, "window_minutes": window}
        yield "get_recent_logs", {"service": service, "limit": 100}
    yield "list_demo_databases", {}


def _check_output(name: str, output: dict[str, Any]) -> None:
    error = first_schema_error(_OUTPUT_VALIDATORS[name], {**output, "fixture": _PROBE_META})
    if error is not None:
        raise SandboxFixtureError(f"fixture data for {name} is invalid: {error.message}")


def _check_fixture(seed: dict[str, Any]) -> None:
    """Reject a seed whose data could not satisfy the section 9 output schemas."""
    for key in ("scenario", "label", "revision", "services", "databases", "unreachable_tools"):
        if key not in seed:
            raise SandboxFixtureError(f"sandbox fixture is missing '{key}'")
    services = set(seed["services"])
    if services != set(DEMO_SERVICES):
        raise SandboxFixtureError(
            f"sandbox fixture must define exactly {', '.join(DEMO_SERVICES)}; "
            f"missing {', '.join(sorted(set(DEMO_SERVICES) - services)) or 'none'}, "
            f"unexpected {', '.join(sorted(services - set(DEMO_SERVICES))) or 'none'}"
        )
    unknown_tools = set(seed["unreachable_tools"]) - DEMO_TOOL_NAMES
    if unknown_tools:
        raise SandboxFixtureError(f"unknown unreachable_tools: {', '.join(sorted(unknown_tools))}")

    for name, arguments in _probe_calls(seed):
        try:
            output = _OUTPUTS[name](seed, arguments)
        except (KeyError, TypeError) as exc:
            raise SandboxFixtureError(f"fixture data for {name} is incomplete: {exc}") from exc
        _check_output(name, output)
        if name == "get_incident_counts":
            if output["critical"] + output["warning"] + output["info"] != output["total"]:
                raise SandboxFixtureError(
                    f"{arguments['service']} incidents for {arguments['window_minutes']} "
                    "minutes: categories do not sum to total"
                )
    for db in seed["databases"]:
        _check_output("delete_demo_database", _deleted(db["database_id"], seed["revision"]))
    ids = [db["database_id"] for db in seed["databases"]]
    if len(ids) != len(set(ids)):
        raise SandboxFixtureError("sandbox database IDs must be unique")


def register_demo_tools(registry: ToolRegistry, service: SandboxDataService) -> None:
    """Register the seven section 9 tools against one sandbox service."""
    for name in sorted(DEMO_TOOL_NAMES):
        schema = DEMO_TOOL_SCHEMAS[name]
        side_effect, call = _TOOLS[name]
        registry.register(
            ToolDeclaration(
                name=name,
                description=schema["description"],
                input_schema=schema["input_schema"],
                output_schema=schema["output_schema"],
                timeout_seconds=MAX_TOOL_TIMEOUT_SECONDS,
                side_effect=side_effect,
                handler=lambda args, call=call: call(service, args),
                required_permission=(
                    MUTATE_PERMISSION if side_effect is SideEffect.MUTATING else READ_PERMISSION
                ),
                # RM11, section 9: a confirmation binds the database ID.
                confirmation_target=_CONFIRMATION_TARGETS.get(name),
            )
        )
