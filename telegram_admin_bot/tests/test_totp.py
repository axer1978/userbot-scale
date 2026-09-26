"""TOTP against the RFC 6238 reference values, so any authenticator app agrees."""

from __future__ import annotations

import pytest

import totp

# RFC 6238 Appendix B, SHA-1: the ASCII key "12345678901234567890", 8 digits.
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


@pytest.mark.parametrize("unix_time, expected", [
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
])
def test_matches_the_rfc_reference_values(unix_time, expected):
    assert totp.code_at(RFC_SECRET, unix_time // 30, digits=8) == expected


def test_a_current_code_is_accepted_and_tells_which_step():
    now = 1_700_000_000.0
    code = totp.code_at(RFC_SECRET, int(now // 30))
    assert totp.matching_counter(RFC_SECRET, code, now=now) == int(now // 30)


def test_one_step_of_clock_drift_is_tolerated_but_not_two():
    now = 1_700_000_000.0
    step = int(now // 30)
    assert totp.matching_counter(RFC_SECRET, totp.code_at(RFC_SECRET, step - 1), now=now) == step - 1
    assert totp.matching_counter(RFC_SECRET, totp.code_at(RFC_SECRET, step + 1), now=now) == step + 1
    assert totp.matching_counter(RFC_SECRET, totp.code_at(RFC_SECRET, step - 2), now=now) is None


def test_spaces_in_a_typed_code_are_fine_but_wrong_lengths_are_not():
    now = 1_700_000_000.0
    code = totp.code_at(RFC_SECRET, int(now // 30))
    assert totp.matching_counter(RFC_SECRET, f"{code[:3]} {code[3:]}", now=now) is not None
    assert totp.matching_counter(RFC_SECRET, code[:5], now=now) is None
    assert totp.matching_counter(RFC_SECRET, "", now=now) is None


def test_new_secrets_are_usable_and_distinct():
    a, b = totp.new_secret(), totp.new_secret()
    assert a != b
    totp.validate_secret(a)
    assert "secret=" + a in totp.provisioning_uri(a)


@pytest.mark.parametrize("bad", ["not base32!", "ABCD"])
def test_unusable_secrets_are_rejected(bad):
    with pytest.raises(ValueError):
        totp.validate_secret(bad)
