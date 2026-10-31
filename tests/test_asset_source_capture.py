"""RM34/RM40: explicit source acquisition pins bytes without executing content."""

import asyncio
import hashlib
import importlib
from contextlib import contextmanager

import httpx
import pytest


@contextmanager
def capture_client(*, transport):
    client = httpx.AsyncClient(transport=transport)
    try:
        yield client
    finally:
        asyncio.run(client.aclose())


def api():
    try:
        return importlib.import_module("relaymind.evaluation.asset_source_capture")
    except ModuleNotFoundError:
        pytest.fail("RM40 bounded pinned-source acquisition is missing")


def recipe(payload=b"original", url="https://source.example/file"):
    return {
        "schema_version": 1,
        "dataset_id": "source-benchmark",
        "files": [
            {
                "id": "original",
                "url": url,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
            }
        ],
    }


def test_rm40_capture_exact_bytes_and_reuse_without_network(tmp_path):
    payload = b"Original bytes; never execute."
    with capture_client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=httpx.ByteStream(payload))
        )
    ) as client:
        result = api().acquire_recipe(recipe(payload), tmp_path / "cache", client=client)
    assert result[0]["status"] == "captured"
    assert (
        tmp_path / "cache" / (hashlib.sha256(payload).hexdigest() + ".original")
    ).read_bytes() == payload
    with capture_client(
        transport=httpx.MockTransport(
            lambda request: pytest.fail("cached original was fetched again")
        )
    ) as client:
        assert (
            api().acquire_recipe(recipe(payload), tmp_path / "cache", client=client)[0]["status"]
            == "reused"
        )


@pytest.mark.parametrize(
    "kind", ["digest", "length", "oversize", "encoding", "redirect", "credentials"]
)
def test_rm40_capture_rejects_tamper_unsafe_redirect_and_unsupported_transport(tmp_path, kind):
    data = recipe()

    def response(request):
        if kind == "redirect":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
        if kind == "oversize":
            return httpx.Response(
                200, headers={"content-length": str(10 * 1024 * 1024 + 1)}, content=b""
            )
        if kind == "encoding":
            return httpx.Response(
                200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(b"not gzip")
            )
        return httpx.Response(
            200, stream=httpx.ByteStream(b"changed" if kind == "digest" else b"original!")
        )

    if kind == "credentials":
        data["files"][0]["url"] = "https://user:secret@source.example/file"
    with capture_client(transport=httpx.MockTransport(response)) as client:
        result = api().acquire_recipe(data, tmp_path / "cache", client=client)
    assert result[0]["status"] == "excluded"
    assert result[0]["reason"]
    assert not list((tmp_path / "cache").glob("*.original"))


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@source.example/file",
        "https://0x7f000001/file",
        "https://127.1/file",
        "http://source.example/file",
    ],
)
def test_rm40_cache_hit_cannot_bypass_public_url_and_credential_rules(tmp_path, url):
    payload = b"original"
    data = recipe(payload, url)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / (hashlib.sha256(payload).hexdigest() + ".original")).write_bytes(payload)
    result = api().acquire_recipe(data, cache)
    assert result[0]["status"] == "excluded"
    assert "final_url" not in result[0]


def test_rm40_cached_verification_still_obeys_total_job_deadline(tmp_path, monkeypatch):
    payload = b"original"
    data = recipe(payload)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / (hashlib.sha256(payload).hexdigest() + ".original")).write_bytes(payload)
    with capture_client(
        transport=httpx.MockTransport(lambda request: pytest.fail("expired job fetched data"))
    ) as client:
        elapsed = [0.0]
        original_read = api().read_local

        def slow_cache_read(*args):
            value = original_read(*args)
            elapsed[0] = 61
            return value

        monkeypatch.setattr(api().time, "monotonic", lambda: elapsed[0])
        monkeypatch.setattr(api(), "read_local", slow_cache_read)
        result = api().acquire_recipe(data, cache, client=client)
    assert result[0]["status"] == "excluded" and result[0]["reason"] == "capture_job_limit"


@pytest.mark.parametrize("chunk_seconds,expected", [(6, "excluded"), (2, "captured")])
def test_rm40_exchange_deadline_includes_all_trickle_body_chunks(
    tmp_path, monkeypatch, chunk_seconds, expected
):
    """RM34/RM40: individually timely reads cannot exceed ten seconds in aggregate."""
    elapsed = [0.0]
    payload = b"x" * (3 * 65536)

    class Trickle(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(3):
                elapsed[0] += chunk_seconds
                yield b"x" * 65536

    monkeypatch.setattr(api().time, "monotonic", lambda: elapsed[0])
    with capture_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Trickle()))
    ) as client:
        result = api().acquire_recipe(recipe(payload), tmp_path / "cache", client=client)
    assert result[0]["status"] == expected
    if expected == "excluded":
        assert result[0]["reason"] == "capture_exchange_limit"
        assert not list((tmp_path / "cache").glob("*.original"))


def test_rm40_exchange_deadline_interrupts_a_blocking_next_read(tmp_path):
    """RM34/RM40: no next body read can outlive the absolute exchange deadline."""
    cancelled = []

    class Blocked(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                await asyncio.sleep(10.3)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            yield b"original"

    with capture_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Blocked()))
    ) as client:
        result = api().acquire_recipe(recipe(), tmp_path / "cache", client=client)
    assert result[0]["status"] == "excluded"
    assert result[0]["reason"] == "capture_exchange_limit"
    assert cancelled == [True]
    assert not list((tmp_path / "cache").glob("*.original"))


def test_rm40_sync_capture_inside_active_loop_fails_before_any_io(tmp_path):
    async def caller():
        with pytest.raises(ValueError, match="capture_requires_sync_context"):
            api().acquire_recipe(recipe(), tmp_path / "cache")

    asyncio.run(caller())
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize(
    "host", ["abc.de", "cafe.de", "feed.cafe", "face.de", "ab.ca", "bad.ad", "beef.de"]
)
def test_rm40_hex_only_domains_are_public_hosts(tmp_path, host):
    # RM40/AC31: a hostname made only of hex letters is still a DNS name.
    payload = b"original"
    data = recipe(payload, f"https://{host}/file")
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / (hashlib.sha256(payload).hexdigest() + ".original")).write_bytes(payload)
    result = api().acquire_recipe(data, cache)
    assert result[0]["status"] == "reused"


@pytest.mark.parametrize("files", [None, "files", {}])
def test_rm40_non_list_files_are_a_recipe_error_not_a_limit(tmp_path, files):
    data = recipe()
    data["files"] = files
    with pytest.raises(api().AssetCorpusError, match="invalid_source_recipe"):
        api().acquire_recipe(data, tmp_path / "cache")


@pytest.mark.parametrize("hosts", [None, "github.com", [1], [""], {}])
def test_rm40_redirect_hosts_must_be_a_list_of_host_names(tmp_path, hosts):
    data = recipe()
    data["redirect_hosts"] = hosts
    with pytest.raises(api().AssetCorpusError, match="invalid_source_recipe"):
        api().acquire_recipe(data, tmp_path / "cache")


def test_rm40_one_unsafe_url_does_not_exclude_other_rows(tmp_path):
    """RM40: a rejected source is excluded alone; valid rows are still acquired."""
    payload = b"valid original"
    data = recipe(payload)
    data["files"].append(
        {
            "id": "unsafe",
            "url": "http://source.example/plain",
            "sha256": hashlib.sha256(b"other").hexdigest(),
            "bytes": 5,
        }
    )
    seen = []

    def respond(request):
        seen.append(str(request.url))
        return httpx.Response(200, stream=httpx.ByteStream(payload))

    with capture_client(transport=httpx.MockTransport(respond)) as client:
        results = api().acquire_recipe(data, tmp_path / "cache", client=client)
    by_id = {r["id"]: r for r in results}
    assert by_id["original"]["status"] == "captured"
    assert by_id["unsafe"]["status"] == "excluded"
    assert by_id["unsafe"]["reason"] == "unsafe_source_url"
    assert seen == ["https://source.example/file"]
