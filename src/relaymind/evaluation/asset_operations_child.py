"""Evaluation-only gateway child with a standard-input control channel (#241; RM38/AC29).

The operations run kit needs the AC29 workload's source rescan and derived
rebuild hooks inside a separately running gateway process. This entry runs the
unchanged ``relaymind serve`` launcher and adds, for the kit only, one JSON
command per standard-input line that calls the gateway's own background owner:

- ``{"id": n, "op": "rescan", "source_id": s}`` awaits
  ``AssetCatalogService.rescan(s)``;
- ``{"id": n, "op": "rebuild"}`` awaits ``AssetCatalogService.rebuild()``.

Each reply is one ``Control-Reply:`` line on standard output with the
publication revision, ``jobs`` (the stored outcome and event chain of every job
of that source that was not finished when the request began, newest first) and
``job_id``, the one job the request awaited. The catalog owner runs one job at
a time and answers a request when its job is recorded finished, so that job is
the latest-finished of ``jobs``; a periodic or repair job that started after it
is still running when the reply is built. When no such job, or more than one
with the same finish time, exists, ``job_id`` is null and ``binding`` says
``job_missing`` or ``job_ambiguous``: the child never guesses. When the job
store cannot be read, the reply is
``{"ok": false, "error": "job_evidence_unavailable", "evidence_error": ...}``:
missing evidence, not a refused command. A closed standard input stops the server. This is
not a product API: it opens no socket and adds no endpoint, and only the
parent process that owns the pipes can use it.
"""

import asyncio
import json
import sys
import threading
from collections.abc import Mapping

from relaymind.api.server import build_server

CONTROL_PREFIX = "Control-Reply: "


def _new_jobs(catalog, before: set[str], source_id: str | None) -> list[dict]:
    """Newest first, each with its stored event chain."""
    store = catalog.store
    return [
        dict(job, events=list(store.job_events(job["job_id"])))
        for job in store.job_outcomes()
        if job["job_id"] not in before and job["source_id"] == source_id
    ]


def _finished_before(catalog) -> set[str]:
    """Jobs already finished when the request began; a running job may be the one it joins."""
    return {job["job_id"] for job in catalog.store.job_outcomes() if job.get("state") != "running"}


def bound_job(jobs: list[dict]) -> tuple[str | None, str | None]:
    """The awaited job's ID, or None with ``job_missing`` / ``job_ambiguous``."""
    finished = [job for job in jobs if job.get("state") != "running" and job.get("finished_at")]
    if not finished:
        return None, "job_missing"
    last = max(job["finished_at"] for job in finished)
    candidates = [job["job_id"] for job in finished if job["finished_at"] == last]
    if len(candidates) != 1:
        return None, "job_ambiguous"
    return candidates[0], None


EVIDENCE_UNAVAILABLE = "job_evidence_unavailable"


def _evidence_unavailable(reply: dict, error: Exception) -> dict:
    return dict(reply, error=EVIDENCE_UNAVAILABLE, evidence_error=type(error).__name__)


async def handle(app, command: Mapping) -> dict:
    """Run one control command against the app's asset catalog owner."""
    reply: dict = {"id": command.get("id"), "ok": False}
    catalog = getattr(app.state, "asset_catalog", None)
    if catalog is None:
        return dict(reply, error="no_asset_catalog")
    op = command.get("op")
    if op not in ("rescan", "rebuild"):
        return dict(reply, error="unknown_op")
    source_id = command.get("source_id") if op == "rescan" else None
    # Every failure is answered at once so the parent never waits for its
    # control timeout. A failed job store read is the closed evidence error
    # ``job_evidence_unavailable``, never a refused rescan or rebuild.
    try:
        before = _finished_before(catalog)
    except Exception as error:
        return _evidence_unavailable(reply, error)
    try:
        if op == "rescan":
            publication = await catalog.rescan(source_id)
        else:
            publication = await catalog.rebuild()
    except Exception as error:
        return dict(reply, error=type(error).__name__, reason=getattr(error, "reason", None))
    try:
        jobs = _new_jobs(catalog, before, source_id)
    except Exception as error:
        return _evidence_unavailable(reply, error)
    job_id, binding = bound_job(jobs)
    reply = dict(
        reply,
        ok=True,
        revision=publication.revision,
        changed=publication.changed,
        jobs=jobs,
        job_id=job_id,
    )
    return reply if binding is None else dict(reply, binding=binding)


def _write(reply: Mapping) -> None:
    sys.stdout.write(CONTROL_PREFIX + json.dumps(reply, ensure_ascii=True, default=str) + "\n")
    sys.stdout.flush()


def forward_lines(stream, loop: asyncio.AbstractEventLoop, lines: asyncio.Queue) -> None:
    """Hand each line, then None at EOF, to the loop; stop quietly once it is closed."""

    def put(item) -> bool:
        if loop.is_closed():
            return False
        try:
            loop.call_soon_threadsafe(lines.put_nowait, item)
        except RuntimeError:  # Closed between the check and the call.
            return False
        return True

    for line in stream:
        if not put(line):
            return
    put(None)


def controlled_serve(app, *, host: str, port: int, on_started=None) -> None:
    """The launcher's serve function plus the control loop on the same event loop."""
    server = build_server(app, host=host, port=port, on_started=on_started)

    async def run() -> None:
        loop = asyncio.get_running_loop()
        lines: asyncio.Queue = asyncio.Queue()

        threading.Thread(
            target=forward_lines,
            args=(sys.stdin, loop, lines),
            name="operations-control",
            daemon=True,
        ).start()
        replies: set[asyncio.Task] = set()

        async def answer(line: str) -> None:
            try:
                command = json.loads(line)
                if not isinstance(command, dict):
                    raise ValueError
            except ValueError:
                _write({"id": None, "ok": False, "error": "invalid_command"})
                return
            _write(await handle(app, command))

        async def control() -> None:
            while (line := await lines.get()) is not None:
                task = asyncio.create_task(answer(line))
                replies.add(task)
                task.add_done_callback(replies.discard)
            server.should_exit = True  # The parent closed the channel.

        controller = asyncio.create_task(control())
        try:
            await server.serve()
        finally:
            controller.cancel()
            for task in (controller, *replies):
                task.cancel()

    asyncio.run(run())


def main(argv: list[str] | None = None) -> int:
    from relaymind.launcher import main as launch

    return launch(argv, serve_fn=controlled_serve)


if __name__ == "__main__":
    raise SystemExit(main())
