"""Tests for src.version — the single canonical version literal + semver helpers.

These pin the single-source contract Phase 11/12/13 wire into: exactly one
"2.1.3" literal, a real packaging.version compare (so 2.10.0 > 2.9.0 and a
leading-v never misfires), and a 4-int VSVersionInfo tuple for the spec.
"""

from src.version import __version__, parse_version, version_tuple


def test_version_literal_is_current():
    """The single literal is exactly "2.1.3" (PEP 440, no leading v)."""
    assert __version__ == "2.1.3"


def test_parse_tolerates_leading_v_on_either_side():
    """parse_version strips a leading v/V so a git tag compares equal to code."""
    assert parse_version("v2.1.0") == parse_version("2.1.0")
    assert parse_version("V2.1.0") == parse_version("2.1.0")


def test_parse_uses_real_semver_not_string_compare():
    """2.10.0 > 2.9.0 — the naive string compare would get this wrong."""
    assert parse_version("2.10.0") > parse_version("2.9.0")


def test_parse_defaults_to_module_version():
    """parse_version() with no arg parses __version__."""
    assert parse_version() == parse_version("2.1.3")


def test_version_tuple_pads_release_to_four_ints():
    """version_tuple() -> (2, 1, 3, 0): release padded with 0 to length 4."""
    assert version_tuple() == (2, 1, 3, 0)


def test_version_tuple_tolerates_v_and_pads_short():
    """version_tuple('v3.4') -> (3, 4, 0, 0): leading v tolerated, padded."""
    assert version_tuple("v3.4") == (3, 4, 0, 0)
