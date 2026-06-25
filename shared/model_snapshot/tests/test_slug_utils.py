"""F-020-19/22: slug headroom + race-safe collision suffixing."""
from __future__ import annotations

from shared.model_snapshot.slug_utils import (
    SLUG_MAX_LEN,
    resolve_slug_collision,
    slug_with_suffix,
    slugify,
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
