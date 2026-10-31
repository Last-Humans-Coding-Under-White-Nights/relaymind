"""RM37/RM42 AC28/AC33: owned private buffers fence before reuse/publication."""

import pytest


def api():
    from relaymind.recommendations import adaptation_buffers

    return adaptation_buffers


def test_erasure_clears_owned_buffers_and_prevents_late_registration():
    from relaymind.assets.embeddings import EmbeddingError

    owner = api().PrivateAdaptationBuffers()
    lease = owner.acquire({"user_id": "u", "workspace_id": "w"})
    text = bytearray(b"private correction")
    history = ["private retained text"]
    lease.register(text)
    lease.register(history)
    owner.invalidate({"user_id": "u", "workspace_id": "w"})
    assert text == bytearray(len(text)) and history == [] and lease.fenced
    late = bytearray(b"late")
    with pytest.raises(EmbeddingError):
        lease.register(late)
    assert late == bytearray(4)
    lease.close()


def test_scope_and_successful_cleanup_have_no_cross_effects():
    owner = api().PrivateAdaptationBuffers()
    left = owner.acquire({"user_id": "u", "workspace_id": "left"})
    right = owner.acquire({"user_id": "u", "workspace_id": "right"})
    a, b = bytearray(b"a"), bytearray(b"b")
    left.register(a)
    right.register(b)
    owner.invalidate({"user_id": "u", "workspace_id": "left"})
    assert a == b"\x00" and b == b"b" and not right.fenced
    left.close()
    right.close()
    assert b == b"\x00" and owner.active == 0


def _close_against_real_feedback_mutation(directory, operation):
    """Subprocess-owned threads make an old deadlock fail without stranding pytest."""
    import asyncio
    import os
    import threading
    from pathlib import Path

    from recommendation_helpers import BODY
    from test_asset_adaptation_store import correct
    from test_asset_user_actions import action_client

    async def run():
        async with action_client(Path(directory)) as (client, app, service, runs, catalog, _):
            parent = (await client.post("/relay/v1/recommendations", json=BODY)).json()
            receipt = await correct(client, parent, 0, "correction")
            feedback = app.state.feedback_store
            store_entered, remove_entered = threading.Event(), threading.Event()
            close_done = threading.Event()

            def before_invalidation(scope):
                store_entered.set()
                assert remove_entered.wait(2)

            feedback.register_invalidator(before_invalidation)
            owner = api().PrivateAdaptationBuffers(feedback)
            lease = owner.acquire(service.scope)
            private = lease.register(bytearray(b"private intent"))
            original_remove = owner._remove

            def remove():
                remove_entered.set()
                original_remove()

            owner._remove = remove

            def close():
                owner.close()
                close_done.set()

            if operation == "reset":
                mutation = asyncio.create_task(
                    client.post(
                        "/relay/v1/feedback/reset",
                        json=dict(schema_version=1, expected_feedback_revision=1),
                        headers={"Idempotency-Key": "concurrent-reset"},
                    )
                )
            else:
                mutation = asyncio.create_task(
                    client.delete(
                        "/relay/v1/feedback/" + receipt["feedback_id"],
                        params={"expected_feedback_revision": 1},
                        headers={"Idempotency-Key": "concurrent-delete"},
                    )
                )
            assert await asyncio.to_thread(store_entered.wait, 2)
            thread = threading.Thread(target=close, daemon=True)
            thread.start()
            for _ in range(500):
                if close_done.is_set() and mutation.done():
                    break
                await asyncio.sleep(0.002)
            if not close_done.is_set() or not mutation.done():
                print("close/reset lock cycle", flush=True)
                os._exit(2)
            assert (await mutation).status_code == 200
            thread.join(1)
            assert lease.fenced and not any(private) and owner.active == 0
            owner.close()
            from relaymind.assets.embeddings import EmbeddingError

            with pytest.raises(EmbeddingError):
                owner.acquire(service.scope)

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["reset", "delete"])
def test_close_and_real_feedback_mutation_complete_with_private_leases_fenced(tmp_path, operation):
    """RM37/RM42 AC28/AC33: actual store transaction and close use one lock order."""
    import subprocess
    import sys
    from pathlib import Path

    command = (
        "import sys;sys.path.insert(0,'tests');"
        "from test_adaptation_buffers import _close_against_real_feedback_mutation as run;"
        "run(sys.argv[1],sys.argv[2])"
    )
    result = subprocess.run(
        [sys.executable, "-c", command, str(tmp_path), operation],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert result.returncode == 0, result.stdout + result.stderr
