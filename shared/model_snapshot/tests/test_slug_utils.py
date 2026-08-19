"""F-020-19/22: slug headroom + race-safe collision suffixing."""
from __future__ import annotations

import pytest

from shared.model_snapshot.slug_utils import (
    SLUG_MAX_LEN,
    _parse_suffix_number,
    resolve_slug_collision,
    slug_with_suffix,
    slugify,
    validate_bi_safe_slug,
)


class TestSlugify:
    def test_bounds_to_64(self):
        assert len(slugify("a" * 200)) == SLUG_MAX_LEN

    def test_fallback_on_empty(self):
        assert slugify("", fallback="x") == "x"

    def test_underscore_separator(self):
        assert slugify("My Cube Name", separator="_") == "my_cube_name"

    def test_hyphen_separator(self):
        assert slugify("My Model", separator="-") == "my-model"


class TestSuffixHeadroom:
    def test_64char_base_plus_suffix_stays_within_64(self):
        base = "a" * 64
        # F-020-19: naive f"{base}_{n}" would be 66 chars and overflow String(64).
        out = slug_with_suffix(base, 12)
        assert len(out) <= SLUG_MAX_LEN
        assert out.endswith("_12")

    def test_n1_returns_bounded_base_no_suffix(self):
        assert slug_with_suffix("model", 1) == "model"


class TestResolveCollision:
    def test_no_collision_returns_base(self):
        assert resolve_slug_collision("sales", set()) == "sales"

    def test_collision_suffixes(self):
        existing = {"sales", "sales_2"}
        assert resolve_slug_collision("sales", existing) == "sales_3"

    def test_collision_headroom_on_long_slug(self):
        long = "x" * 64
        existing = {long}
        out = resolve_slug_collision(long, existing)
        assert len(out) <= SLUG_MAX_LEN
        assert out != long


class TestParseSuffixNumber:
    """Bug-5870: _parse_suffix_number extracts the suffix so the retry loop
    continues from the right number instead of resetting to 2."""

    def test_bare_slug_returns_2(self):
        assert _parse_suffix_number("sales") == 2

    def test_suffix_2_returns_3(self):
        assert _parse_suffix_number("sales_2") == 3

    def test_high_suffix_returns_next(self):
        assert _parse_suffix_number("sales_51") == 52

    def test_suffix_with_truncated_base(self):
        # When the base was truncated for headroom the suffix is still parsed.
        assert _parse_suffix_number("aaaa_99") == 100

    def test_no_numeric_suffix_returns_2(self):
        assert _parse_suffix_number("sales_model") == 2


class TestModelSlugBiSafety:
    """Bug-5513: model slug must reject BI-unsafe characters at create/update."""

    def test_create_rejects_hyphen(self):
        import pytest
        from pydantic import ValidationError
        from shared.schemas.domains.models_sources import ModelCreate
        with pytest.raises(ValidationError, match="letter or underscore"):
            ModelCreate(slug="tpcds-retail")

    def test_create_rejects_leading_digit(self):
        import pytest
        from pydantic import ValidationError
        from shared.schemas.domains.models_sources import ModelCreate
        with pytest.raises(ValidationError, match="letter or underscore"):
            ModelCreate(slug="1sales")

    def test_create_accepts_valid_slug(self):
        from shared.schemas.domains.models_sources import ModelCreate
        m = ModelCreate(slug="sales_model_v2")
        assert m.slug == "sales_model_v2"

    def test_create_accepts_uppercase(self):
        from shared.schemas.domains.models_sources import ModelCreate
        m = ModelCreate(slug="Sales_Model")
        assert m.slug == "Sales_Model"

    def test_create_accepts_underscore_start(self):
        from shared.schemas.domains.models_sources import ModelCreate
        m = ModelCreate(slug="_private")
        assert m.slug == "_private"

    def test_update_rejects_hyphen(self):
        import pytest
        from pydantic import ValidationError
        from shared.schemas.domains.models_sources import ModelUpdate
        with pytest.raises(ValidationError, match="letter or underscore"):
            ModelUpdate(slug="bad-slug")

    def test_update_accepts_none(self):
        from shared.schemas.domains.models_sources import ModelUpdate
        m = ModelUpdate(slug=None)
        assert m.slug is None

    def test_update_accepts_valid_slug(self):
        from shared.schemas.domains.models_sources import ModelUpdate
        m = ModelUpdate(slug="good_slug")
        assert m.slug == "good_slug"


class TestBiSafeSlugValidation:
    """Bug-6291: validate_bi_safe_slug enforces the BI-safe contract at the
    import chokepoint (insert_model_with_slug_retry)."""

    def test_valid_slug_passes(self):
        validate_bi_safe_slug("sales_model")

    def test_valid_slug_uppercase_passes(self):
        validate_bi_safe_slug("Sales_Model")

    def test_valid_slug_underscore_start_passes(self):
        validate_bi_safe_slug("_private")

    def test_hyphenated_slug_rejected(self):
        with pytest.raises(ValueError, match="not BI-safe"):
            validate_bi_safe_slug("sales-model")

    def test_leading_digit_rejected(self):
        with pytest.raises(ValueError, match="not BI-safe"):
            validate_bi_safe_slug("2024_orders")

    def test_space_rejected(self):
        with pytest.raises(ValueError, match="not BI-safe"):
            validate_bi_safe_slug("my model")

    def test_special_char_rejected(self):
        with pytest.raises(ValueError, match="not BI-safe"):
            validate_bi_safe_slug("my!model")

    def test_empty_string_rejected(self):
        with pytest.raises(ValueError, match="not BI-safe"):
            validate_bi_safe_slug("")

    def test_custom_label_in_error(self):
        with pytest.raises(ValueError, match="Persona slug"):
            validate_bi_safe_slug("bad-slug", label="Persona slug")


class TestSlugifyAlwaysBiSafe:
    """Bug-7622: slugify() must ALWAYS return a BI-safe slug so its output can
    feed insert_model_with_slug_retry / validate_bi_safe_slug without raising a
    ValueError that ecosystem/catalog import endpoints would surface as a 500.

    The property under test: for ANY input name, ``validate_bi_safe_slug(
    slugify(name))`` does not raise.
    """

    @pytest.mark.parametrize("name", [
        "123sales",          # digit-leading
        "2024_orders",       # digit-leading with underscore
        "42",                # digits only
        "$$$",               # symbol-only -> empty -> fallback
        "!!!",               # symbol-only
        "   ",               # whitespace only -> empty -> fallback
        "",                  # empty
        "3.14 metric",       # digit-leading after collapse
        "9lives",            # digit-leading alpha
        "café_sales",        # non-ascii collapses
        "-leading-hyphen",   # strips to alpha
        "a" * 200,           # overlong -> bounded, still alpha start
    ])
    def test_slugify_output_is_bi_safe(self, name):
        slug = slugify(name)
        # Must not raise — this is the exact chokepoint the import path hits.
        validate_bi_safe_slug(slug)
        assert len(slug) <= SLUG_MAX_LEN

    def test_digit_leading_prefixed_with_underscore(self):
        assert slugify("123sales") == "_123sales"

    def test_symbol_only_uses_fallback(self):
        assert slugify("$$$", fallback="catalog_model") == "catalog_model"

    def test_distinct_digit_names_stay_distinct(self):
        # Prefixing (not dropping) the leading digit preserves distinctness.
        assert slugify("1_sales") != slugify("2_sales")

    def test_digit_leading_within_length_bound_after_prefix(self):
        # A 64-char digit-leading slug + the "_" prefix must still fit in 64.
        out = slugify("1" + "a" * 100)
        assert len(out) <= SLUG_MAX_LEN
        validate_bi_safe_slug(out)
