"""RM34/AC25: native fixture failure accounting also works through the actual store."""

from relaymind.assets.sources import SourceFailure
from relaymind.store import RunStore
from relaymind.store.asset_catalog import AssetCatalogStore
from test_asset_sources import capture, skill


def test_native_oplock_helper_records_failed_observation_without_replacing_content(
    tmp_path, monkeypatch
):
    """Exercise the helper's post-capture failure path; native I/O stays in Windows CI."""
    import asset_windows_io_helper as helper

    source, db = tmp_path / "source", tmp_path / "catalog.sqlite"
    skill(source)
    with RunStore(db) as store:
        catalog = AssetCatalogStore(store)
        catalog.publish(capture(source), expected_revision=0)
        original = catalog.snapshot()

    def unavailable(*args, **kwargs):
        raise SourceFailure("source_unavailable")

    # The helper instruments this native constructor in its disposable child;
    # preserve the original when running only the failure-accounting path here.
    monkeypatch.setattr(helper._Api, "__init__", helper._Api.__init__)
    monkeypatch.setattr(helper, "capture", unavailable)
    evidence = helper.capture_oplock(source, db)

    assert evidence["previous_catalog_revision"] == 1
    assert evidence["observed_catalog_revision"] == 2
    assert evidence["content_retained"]
    with RunStore(db) as store:
        catalog = AssetCatalogStore(store)
        observed = catalog.snapshot()
        assert observed.catalog_revision == 2
        assert [a.content_hash for a in observed.assets] == [
            a.content_hash for a in original.assets
        ]
        assert observed.assets[0].stale
        assert observed.sources[0]["rejected_count"] == 1
        assert observed.sources[0]["exclusions"][0]["reason"] == "source_unavailable"
        assert catalog.inventory("example").tombstones == frozenset()
