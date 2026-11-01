"""Evaluation-only gateway child with a standard-input control channel (#241; RM38/AC29).

The operations run kit needs the AC29 workload's source rescan and derived
rebuild hooks inside a separately running gateway process. This entry runs the
unchanged ``relaymind serve`` launcher and adds, for the kit only, one JSON
command per standard-input line that calls the gateway's own background owner:

- ``{"id": n, "op": "rescan", "source_id": s}`` awaits
  ``AssetCatalogService.rescan(s)``;
- ``{"id": n, "op": "rebuild"}`` awaits ``AssetCatalogService.rebuild()``.

Each reply is one ``Control-Reply:`` line on standard output with the
publication revision and the stored outcome and event chain of every job that
started for that request. A closed standard input stops the server. This is
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
    before = {job["job_id"] for job in catalog.store.job_outcomes()}
    try:
        if op == "rescan":
            publication = await catalog.rescan(source_id)
        else:
            publication = await catalog.rebuild()
    except Exception as error:
        return dict(reply, error=type(error).__name__, reason=getattr(error, "reason", None))
    return dict(
        reply,
        ok=True,
        revision=publication.revision,
        changed=publication.changed,
        jobs=_new_jobs(catalog, before, source_id),
    )


def _write(reply: Mapping) -> None:
    sys.stdout.write(CONTROL_PREFIX + json.dumps(reply, ensure_ascii=True, default=str) + "\n")
    sys.stdout.flush()


def controlled_serve(app, *, host: str, port: int, on_started=None) -> None:
    """The launcher's serve function plus the control loop on the same event loop."""
    server = build_server(app, host=host, port=port, on_started=on_started)

    async def run() -> None:
        loop = asyncio.get_running_loop()
        lines: asyncio.Queue = asyncio.Queue()

        def read() -> None:
            for line in sys.stdin:
                loop.call_soon_threadsafe(lines.put_nowait, line)
            loop.call_soon_threadsafe(lines.put_nowait, None)

        threading.Thread(target=read, name="operations-control", daemon=True).start()
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
