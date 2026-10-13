"""Tests for the Florida ZIP reference lookup."""

from __future__ import annotations

import pytest

from app.messenger.florida_zips import (
    describe_mention,
    find_zip_mentions,
    is_florida_zip_range,
    lookup_florida_zip,
)


def test_reference_list_loads_every_row():
    from app.messenger.florida_zips import _reference

    reference = _reference()
    assert len(reference) == 1218
    assert len({info.county for info in reference.values()}) == 29
    assert all(info.state == "FL" for info in reference.values())


@pytest.mark.parametrize(
    "zip_code, city, county",
    [
        ("32114", "Daytona Beach", "Volusia County"),
        ("32209", "Jacksonville", "Duval County"),
        ("34470", "Ocala", "Marion County"),
        ("33602", "Tampa", "Hillsborough County"),
        ("32601", "Gainesville", "Alachua County"),
    ],
)
def test_lookup_returns_city_and_county(zip_code, city, county):
    info = lookup_florida_zip(zip_code)
    assert info is not None
    assert (info.city, info.county, info.state) == (city, county, "FL")


def test_lookup_accepts_zip_plus_four():
    assert lookup_florida_zip("32114-1234").city == "Daytona Beach"


def test_missing_zip_returns_none():
    assert lookup_florida_zip("32999") is None


@pytest.mark.parametrize(
    "zip_code, expected",
    [("32004", True), ("33101", True), ("34997", True), ("34002", False), ("30301", False), ("35203", False)],
)
def test_florida_zip_range(zip_code, expected):
    assert is_florida_zip_range(zip_code) is expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("32114", ["32114"]),
        ("my house is in Daytona, zip 32114", ["32114"]),
        ("Jacksonville, FL 32209", ["32209"]),
        ("13579 Beach Blvd, Jacksonville 32246", ["32246"]),
        ("Ocala 34470 and also 32209", ["34470", "32209"]),
        ("it's in Atlanta GA 30301", ["30301"]),
    ],
)
def test_finds_zip_codes(text, expected):
    assert [m.zip_code for m in find_zip_mentions(text)] == expected


@pytest.mark.parametrize(
    "text",
    [
        "13579 Beach Blvd",  # house number
        "asking 95000, ARV 180000",  # amounts
        "Got a deal: $95000",
        "the lot is 12000 sq ft",
        "call me at 904-555-0100",  # phone number
        "I have 45000 reasons",  # out-of-range number with no ZIP context
        "4218 Hendricks Ave",
        "",
    ],
)
def test_ignores_numbers_that_are_not_zip_codes(text):
    assert find_zip_mentions(text) == []


def test_duplicate_zip_is_reported_once():
    assert [m.zip_code for m in find_zip_mentions("32114, yes 32114")] == ["32114"]


def test_description_of_a_found_zip_names_city_and_county_but_not_as_verified():
    (mention,) = find_zip_mentions("32114")
    text = describe_mention(mention)
    assert "Daytona Beach" in text and "Volusia County" in text
    assert "doesn't verify" in text


def test_description_of_an_unlisted_florida_zip_still_covers_it_without_naming_a_city():
    (mention,) = find_zip_mentions("32999")
    assert mention.info is None and mention.in_florida_range
    text = describe_mention(mention)
    assert "still in our service area" in text
    assert "don't name a city or county" in text


def test_description_of_an_out_of_state_zip():
    (mention,) = find_zip_mentions("Atlanta GA 30301")
    assert not mention.in_florida_range
    assert "not a Florida ZIP" in describe_mention(mention)
