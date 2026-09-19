"""Language and author-affiliation-country constraints.

Two questions are kept strictly apart throughout, because conflating them would be a
scientific error rather than a bug:

    publication language  !=  author affiliation country

and neither is the publisher's country, the country a study was carried out in, or the
geographic subject of the research. Nothing here derives any of those from the others.

The provider behaviour these tests encode was verified against the live OpenAlex API
on 2026-08-18 and is recorded in ``docs/providers.md`` section 1.9.
"""

from __future__ import annotations

import pytest

from harvester.errors import ConfigurationError
from harvester.providers.openalex import (
    AFFILIATION_COUNTRY_FILTER,
    LANGUAGE_FILTER,
    OpenAlexQuery,
)
from harvester.vocabulary import (
    COUNTRIES,
    LANGUAGES,
    country_name,
    language_name,
    normalize_countries,
    normalize_languages,
)


def filters_of(query: OpenAlexQuery) -> dict[str, str]:
    return dict(clause.split(":", 1) for clause in query.filter_string().split(","))


# ==================================================== 1./4. defaults mean "any"


def test_the_default_query_applies_no_language_and_no_country_filter():
    query = OpenAlexQuery(search="moral psychology")
    assert query.languages == []
    assert query.affiliation_countries == []
    assert LANGUAGE_FILTER not in filters_of(query)
    assert AFFILIATION_COUNTRY_FILTER not in filters_of(query)


def test_the_default_filter_string_is_byte_identical_to_the_previous_behaviour():
    """Any/Any must leave the query exactly as it was before this feature."""
    assert OpenAlexQuery(search="x").filter_string() == "has_doi:true,is_oa:true"
    assert (
        OpenAlexQuery(search="x", topic_id="T10159", from_publication_year=2020).filter_string()
        == "from_publication_date:2020-01-01,has_doi:true,is_oa:true,topics.id:T10159"
    )


@pytest.mark.parametrize("empty", [[], None, "", ()])
def test_every_spelling_of_empty_means_any(empty):
    query = OpenAlexQuery(search="x", languages=empty, affiliation_countries=empty)
    assert query.filter_string() == "has_doi:true,is_oa:true"


# ======================================================== 2./3. language values


def test_a_single_language_becomes_one_provider_filter():
    query = OpenAlexQuery(search="x", languages=["de"])
    assert filters_of(query)[LANGUAGE_FILTER] == "de"


def test_several_languages_are_or_ed_in_one_filter():
    """OR within a key uses the pipe; it must not become several AND-ed clauses."""
    query = OpenAlexQuery(search="x", languages=["en", "de", "fr"])
    assert filters_of(query)[LANGUAGE_FILTER] == "de|en|fr"
    assert query.filter_string().count(f"{LANGUAGE_FILTER}:") == 1


def test_languages_are_canonicalised_whatever_the_caller_supplies():
    assert OpenAlexQuery(languages=["DE", "en ", "de"], search="x").languages == ["de", "en"]


def test_language_selection_order_does_not_change_the_query():
    a = OpenAlexQuery(search="x", languages=["fr", "de", "en"])
    b = OpenAlexQuery(search="x", languages=["en", "fr", "de"])
    assert a.filter_string() == b.filter_string()


# ========================================================= 5./6. country values


def test_a_single_affiliation_country_becomes_one_provider_filter():
    query = OpenAlexQuery(search="x", affiliation_countries=["DE"])
    assert filters_of(query)[AFFILIATION_COUNTRY_FILTER] == "DE"


def test_several_countries_are_or_ed_in_one_filter():
    query = OpenAlexQuery(search="x", affiliation_countries=["DE", "AT", "CH"])
    assert filters_of(query)[AFFILIATION_COUNTRY_FILTER] == "AT|CH|DE"
    assert query.filter_string().count(f"{AFFILIATION_COUNTRY_FILTER}:") == 1


def test_countries_are_canonicalised_to_upper_case_alpha_2():
    assert OpenAlexQuery(search="x", affiliation_countries=["de", "At ", "DE"]).affiliation_countries == ["AT", "DE"]


# =============================================== 7. provider request translation


def test_the_two_constraints_use_the_documented_provider_keys():
    assert LANGUAGE_FILTER == "language"
    # Deliberately the institution-backed key, not ``authorships.countries``, which
    # also carries countries inferred from raw affiliation strings.
    assert AFFILIATION_COUNTRY_FILTER == "authorships.institutions.country_code"


def test_both_constraints_combine_with_the_existing_ones_by_and():
    query = OpenAlexQuery(
        search="x",
        topic_id="T10159",
        from_publication_year=2015,
        oa_status="gold",
        languages=["en", "de"],
        affiliation_countries=["DE", "AT"],
    )
    assert query.filter_string() == (
        "authorships.institutions.country_code:AT|DE,"
        "from_publication_date:2015-01-01,"
        "has_doi:true,is_oa:true,"
        "language:de|en,"
        "open_access.oa_status:gold,"
        "topics.id:T10159"
    )


def test_the_two_constraints_stay_independent_of_each_other():
    """A language selection must never imply a country, or the reverse."""
    language_only = OpenAlexQuery(search="x", languages=["de"])
    assert language_only.affiliation_countries == []
    assert AFFILIATION_COUNTRY_FILTER not in filters_of(language_only)

    country_only = OpenAlexQuery(search="x", affiliation_countries=["DE"])
    assert country_only.languages == []
    assert LANGUAGE_FILTER not in filters_of(country_only)


# ============================================= 9. no silent ignoring of a value


@pytest.mark.parametrize("bad", ["xx", "klingon", "eng", "de-DE", "12", "!"])
def test_an_unrecognised_language_is_refused_rather_than_dropped(bad):
    with pytest.raises(ConfigurationError, match="unknown language code"):
        OpenAlexQuery(search="x", languages=[bad])


@pytest.mark.parametrize("bad", ["ZZ", "GER", "Germany", "d", "49"])
def test_an_unrecognised_country_is_refused_rather_than_dropped(bad):
    with pytest.raises(ConfigurationError, match="unknown affiliation country code"):
        OpenAlexQuery(search="x", affiliation_countries=[bad])


def test_one_bad_value_never_silently_narrows_a_selection():
    """The whole selection is refused; it is not quietly reduced to the good half."""
    with pytest.raises(ConfigurationError):
        OpenAlexQuery(search="x", languages=["de", "xx"])
    with pytest.raises(ConfigurationError):
        OpenAlexQuery(search="x", affiliation_countries=["DE", "ZZ"])


def test_an_active_constraint_always_reaches_the_filter_string():
    """Guard against a future edit that accepts a value and then forgets to send it."""
    for code in sorted(LANGUAGES)[:25]:
        assert f"{LANGUAGE_FILTER}:{code}" in OpenAlexQuery(search="x", languages=[code]).filter_string()
    for code in sorted(COUNTRIES)[:25]:
        query = OpenAlexQuery(search="x", affiliation_countries=[code])
        assert f"{AFFILIATION_COUNTRY_FILTER}:{code}" in query.filter_string()


# ============================ 10. backward compatibility of stored search state


def test_a_query_stored_before_this_feature_still_loads_and_means_any():
    stored = {
        "topic_id": "T10159",
        "primary_topic_only": False,
        "search": "moral psychology",
        "publication_year": None,
        "from_publication_year": 2015,
        "to_publication_year": None,
        "is_oa": True,
        "has_doi": True,
        "oa_status": None,
        "extra_filters": [],
        "filter": "from_publication_date:2015-01-01,has_doi:true,is_oa:true,topics.id:T10159",
    }
    restored = OpenAlexQuery.from_dict(stored)
    assert restored.languages == []
    assert restored.affiliation_countries == []
    # A resumed run must reproduce exactly the scope it originally had.
    assert restored.filter_string() == stored["filter"]


def test_a_stored_query_with_null_constraints_also_means_any():
    restored = OpenAlexQuery.from_dict(
        {"search": "x", "languages": None, "affiliation_countries": None}
    )
    assert restored.languages == []
    assert restored.affiliation_countries == []


def test_the_new_constraints_survive_a_full_round_trip():
    query = OpenAlexQuery(search="x", languages=["de", "en"], affiliation_countries=["CH", "DE"])
    restored = OpenAlexQuery.from_dict(query.to_dict())
    assert restored.to_dict() == query.to_dict()
    assert restored.filter_string() == query.filter_string()


def test_to_dict_reports_both_constraints_for_run_provenance():
    payload = OpenAlexQuery(search="x", languages=["de"], affiliation_countries=["AT"]).to_dict()
    assert payload["languages"] == ["de"]
    assert payload["affiliation_countries"] == ["AT"]
    assert "language:de" in payload["filter"]
    assert "authorships.institutions.country_code:AT" in payload["filter"]


# ================================================== the controlled vocabularies


def test_the_vocabularies_come_from_the_provider_and_use_the_published_standards():
    assert len(LANGUAGES) == 181 and len(COUNTRIES) == 247
    assert all(len(code) == 2 and code.islower() for code in LANGUAGES)
    assert all(len(code) == 2 and code.isupper() for code in COUNTRIES)
    assert LANGUAGES["en"] == "English" and LANGUAGES["de"] == "German"
    assert COUNTRIES["DE"] == "Germany" and COUNTRIES["AT"] == "Austria"


def test_display_names_exist_for_the_ui_and_fall_back_visibly():
    assert language_name("fr") == "French"
    assert country_name("CH") == "Switzerland"
    # An unknown code is shown as itself rather than as an invented name.
    assert language_name("zz") == "zz"
    assert country_name("ZZ") == "ZZ"


def test_normalisers_accept_a_delimited_string_as_well_as_a_list():
    assert normalize_languages("de,en") == ["de", "en"]
    assert normalize_languages("de|en") == ["de", "en"]
    assert normalize_countries("DE, at") == ["AT", "DE"]


def test_a_code_that_exists_in_only_one_vocabulary_is_refused_by_the_other():
    assert "at" not in LANGUAGES and "AT" in COUNTRIES      # Austria is not a language
    assert "cs" in LANGUAGES and "CS" not in COUNTRIES       # Czech is not a country
    with pytest.raises(ConfigurationError, match="unknown language code"):
        normalize_languages(["AT"])
    with pytest.raises(ConfigurationError, match="unknown affiliation country code"):
        normalize_countries(["cs"])


def test_colliding_codes_keep_their_separate_meanings():
    """110 codes are spelled the same in both vocabularies and mean different things.

    ``de`` is the German *language*; ``DE`` is *Germany*. Selecting one must never be
    read as the other — the whole point of keeping these constraints apart.
    """
    collisions = {code for code in LANGUAGES if code.upper() in COUNTRIES}
    assert len(collisions) == 110
    for code in ("de", "fr", "it", "pt", "id", "no"):
        assert code in collisions
        assert LANGUAGES[code] != COUNTRIES[code.upper()]

    german_language = OpenAlexQuery(search="x", languages=["de"])
    assert german_language.affiliation_countries == []
    assert filters_of(german_language) == {
        "has_doi": "true", "is_oa": "true", LANGUAGE_FILTER: "de"
    }

    german_institutions = OpenAlexQuery(search="x", affiliation_countries=["DE"])
    assert german_institutions.languages == []
    assert filters_of(german_institutions) == {
        "has_doi": "true", "is_oa": "true", AFFILIATION_COUNTRY_FILTER: "DE"
    }
