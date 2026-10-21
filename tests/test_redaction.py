"""Secret-field redaction (RM12, sections 5 and 8).

RM12: logs and traces redact configured secret fields. Section 8: events
that contain tool results are redacted before persistence. Section 5:
secrets are excluded from the store. The fields are configuration.
"""

import pytest

from relaymind.redaction import DEFAULT_SECRET_FIELDS, REDACTED, Redactor, redact_text


def test_secret_fields_are_redacted_at_any_depth_without_regard_to_case():
    redactor = Redactor(secret_fields={"api_key", "token"})
    value = {
        "service": "staging-api",
        "API_Key": "k-1",
        "nested": [{"token": {"inner": "t-1"}}, {"keep": "x"}],
    }
    assert redactor.redact(value) == {
        "service": "staging-api",
        "API_Key": REDACTED,
        "nested": [{"token": REDACTED}, {"keep": "x"}],
    }
    assert value["API_Key"] == "k-1", "redact must return a copy"


def test_secret_values_are_collected_for_scrubbing_free_text():
    redactor = Redactor(secret_fields={"password"})
    found = redactor.secret_values({"password": ["p-1", {"x": "p-2"}], "other": "o"})
    assert found == frozenset({"p-1", "p-2"})
    assert redact_text("say p-1 and p-2 and o", found) == f"say {REDACTED} and {REDACTED} and o"


def test_the_default_configuration_covers_common_credential_keys():
    assert {"api_key", "authorization", "password", "token"} <= DEFAULT_SECRET_FIELDS
    assert Redactor().redact({"authorization": "Bearer x"}) == {"authorization": REDACTED}


@pytest.mark.parametrize("fields", ["token", [""], [None]])
def test_an_unusable_configuration_is_rejected(fields):
    with pytest.raises(ValueError):
        Redactor(secret_fields=fields)


def test_a_short_secret_is_not_scrubbed_from_inside_other_words():
    """RM12: free-text scrubbing replaces a secret value as a whole token,
    so a short value such as "1" or "true" does not corrupt unrelated
    numbers and words in the answer; the value on its own is still
    replaced."""
    assert redact_text("10 retries, 1 failed", {"1"}) == f"10 retries, {REDACTED} failed"
    assert redact_text("untrue, but true", {"true"}) == f"untrue, but {REDACTED}"
    assert redact_text("Bearer sk-abc.", {"sk-abc"}) == f"Bearer {REDACTED}."


def test_a_secret_repeated_in_free_text_of_the_same_value_is_scrubbed():
    """RM12, section 8 (spec 0.12): the value is replaced where it is repeated, not only by key."""
    redactor = Redactor(secret_fields={"source"})
    value = {
        "entries": [
            {"message": "use svc-key-7 to log in", "source": "svc-key-7"},
            {"message": "svc-key-70 is another word", "source": "svc-key-7"},
        ],
        "count": 2,
    }
    assert redactor.redact_with_repeats(value) == {
        "entries": [
            {"message": f"use {REDACTED} to log in", "source": REDACTED},
            {"message": "svc-key-70 is another word", "source": REDACTED},
        ],
        "count": 2,
    }
    assert value["entries"][0]["source"] == "svc-key-7", "must return a copy"


def test_a_numeric_secret_repeated_in_free_text_is_scrubbed():
    """RM12, section 8 (spec 0.12): a number under a secret field is a secret value too."""
    redactor = Redactor(secret_fields={"pin"})
    value = {"pin": 7731, "message": "login with pin 7731", "count": 77310}
    assert redactor.secret_values(value) == frozenset({"7731"})
    assert redactor.redact_with_repeats(value) == {
        "pin": REDACTED,
        "message": f"login with pin {REDACTED}",
        "count": 77310,
    }


def test_known_secrets_of_the_run_are_scrubbed_from_a_value_without_the_field():
    """RM12, section 8 (spec 0.12): a secret seen earlier in the run is scrubbed later too."""
    redactor = Redactor(secret_fields={"source"})
    value = {"service": "svc-key-7", "limit": 1}
    assert redactor.redact_with_repeats(value, {"svc-key-7"}) == {
        "service": REDACTED,
        "limit": 1,
    }


def test_the_redaction_marker_is_never_scrubbed_again():
    """RM12: a secret that is a word of the marker leaves earlier replacements whole."""
    assert redact_text("use longer-secret", {"longer-secret", "redacted"}) == f"use {REDACTED}"
