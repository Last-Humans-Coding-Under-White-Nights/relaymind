"""RM37/RM39 AC28/AC30: ordinary registered native channel keeps exact contract/scope gates."""

import hashlib

import pytest

from relaymind.recommendations.errors import RecommendationError
from relaymind.recommendations.registrations import (
    ActionRegistrations,
    Registration,
    RegistrationSnapshot,
    production_qualification,
)
from relaymind.store import RunStore


def test_default_registration_admits_only_reviewed_contract_and_rechecks_scope(tmp_path):
    store = RunStore(tmp_path / "native.sqlite3")
    token = "fixture-native-channel-" + "a" * 48
    registered = ActionRegistrations(store)
    try:
        record = Registration(
            "operator-channel",
            "u",
            "w",
            hashlib.sha256(token.encode()).hexdigest(),
            True,
            "operator-host",
            "operator-mapping",
            "openclaw-native-2026.10.7.v1",
        )
        registered.replace(RegistrationSnapshot((record,)))
        assert (
            registered.authenticate(token, {"user_id": "u", "workspace_id": "w"}).adapter_id
            == "operator-channel"
        )
        with pytest.raises(RecommendationError):
            registered.authenticate(token, {"user_id": "foreign", "workspace_id": "w"})
        registered.replace(RegistrationSnapshot(()))
        with pytest.raises(RecommendationError):
            registered.authenticate(token, {"user_id": "u", "workspace_id": "w"})
    finally:
        store.close()


@pytest.mark.parametrize(
    "contract",
    [
        "fixture-contract",
        "claim",
        "openclaw-native-2026.10.8.v1",
        "openclaw-native-2026.10.7.v2",
        "",
    ],
)
def test_unreviewed_contracts_never_qualify(contract):
    assert not production_qualification("opaque-channel", "opaque-host", "opaque-mapping", contract)


@pytest.mark.parametrize("value", ["", "   ", "x" * 257, None])
def test_missing_or_unbounded_operator_references_do_not_qualify(value):
    assert not production_qualification("channel", value, "mapping", "openclaw-native-2026.10.7.v1")
