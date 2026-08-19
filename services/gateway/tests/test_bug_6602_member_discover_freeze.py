"""Bug-6602: Excel freezes when selecting/expanding a dimension over XMLA.

Root cause (Fable diagnostic ``fable-xmla-excel-cube-shape.md`` §3): the XMLA
MDSCHEMA_MEMBERS discover handler
(``xmla_server._load_discover_member_data``) narrowed the dimensions it
fetches ONLY on ``DIMENSION_UNIQUE_NAME`` / ``HIERARCHY_UNIQUE_NAME``
restrictions. MSOLAP member requests issued by Excel when expanding a
hierarchy restrict by ``MEMBER_UNIQUE_NAME`` (+ ``TREE_OP``) or
``LEVEL_UNIQUE_NAME`` alone -- both ignored -- so ONE expand fanned out to a
full ``SELECT DISTINCT ... LIMIT 100000`` source scan for EVERY dimension in
the model, uncached, and streamed a multi-MB SOAP payload synchronously.

These tests pin the three quick-win fixes (all gateway ``src/dax`` only):

1. Restriction-aware narrowing -- a MEMBER/LEVEL-only restricted discover
   queries only the owning dimension, not all dimensions (kills the fan-out).
2. Short-TTL member + metadata caches -- a repeat discover during one Excel
   gesture does not re-run the source scan / metadata N+1.
3. Bounded first-page enumeration -- a high-cardinality flat dimension is
   capped to a bounded page instead of streaming the whole 100k result.

Correctness invariants preserved: the correct members for the requested
hierarchy still come back, persona scoping (persona_id) still flows through,
and the cache is keyed by (model, persona, dimension, restriction) so no
cross-persona leak.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.dax import xmla_server  # noqa: E402
from src.dax import member_cache  # noqa: E402

# Note: the member/metadata caches are reset before/after every test by the
# autouse fixture in ``tests/conftest.py`` (process-global cache isolation), so
# no per-file reset fixture is needed here.


_DIMS = [
    {"name": "Region", "source": "column"},
    {"name": "Product", "source": "column"},
    {"name": "Customer", "source": "column"},
]


def _patch_flat_members(monkeypatch, calls, *, count=1):
    async def fake_get_dimension_members(
        model_id, dimension_name, tenant_slug, jwt_token, *, persona_id=None,
    ):
        calls.append((dimension_name, persona_id))
        return {
            "members": [{"name": f"m{i}"} for i in range(count)],
            "levels": [dimension_name],
        }

    monkeypatch.setattr(
        xmla_server, "get_dimension_members", fake_get_dimension_members,
    )


class TestNarrowing:
    """Quick win 1 -- restriction-aware narrowing kills the all-dims fan-out."""

    @pytest.mark.asyncio
    async def test_member_unique_name_narrows_to_one_dimension(self, monkeypatch):
        """A MEMBER_UNIQUE_NAME-only restriction (what Excel sends on expand)
        must fetch members for ONLY the owning dimension, not every dimension.
        """
        calls: list[tuple[str, str | None]] = []
        _patch_flat_members(monkeypatch, calls)

        result = await xmla_server._load_discover_member_data(
            model_id="m1",
            project_id="p1",
            dimensions=_DIMS,
            tenant_slug="acme",
            jwt_token="tok",
            restrictions={"MEMBER_UNIQUE_NAME": ["[Region].[Region].[West]"]},
        )

        fetched = [c[0] for c in calls]
        assert fetched == ["Region"], (
            f"Expected narrowing to only [Region]; fanned out to {fetched}"
        )
        assert set(result.keys()) == {"Region"}

    @pytest.mark.asyncio
    async def test_level_unique_name_narrows_to_one_dimension(self, monkeypatch):
        """A LEVEL_UNIQUE_NAME-only restriction narrows to the owning dim."""
        calls: list[tuple[str, str | None]] = []
        _patch_flat_members(monkeypatch, calls)

        await xmla_server._load_discover_member_data(
            model_id="m1",
            project_id="p1",
            dimensions=_DIMS,
            tenant_slug="acme",
            jwt_token="tok",
            restrictions={"LEVEL_UNIQUE_NAME": ["[Product].[Product].[Product]"]},
        )

        fetched = [c[0] for c in calls]
        assert fetched == ["Product"], (
            f"Expected narrowing to only [Product]; fanned out to {fetched}"
        )

    @pytest.mark.asyncio
    async def test_dimension_unique_name_still_narrows(self, monkeypatch):
        """Regression guard: the pre-existing DIMENSION_UNIQUE_NAME narrowing
        must keep working."""
        calls: list[tuple[str, str | None]] = []
        _patch_flat_members(monkeypatch, calls)

        await xmla_server._load_discover_member_data(
            model_id="m1",
            project_id="p1",
            dimensions=_DIMS,
            tenant_slug="acme",
            jwt_token="tok",
            restrictions={"DIMENSION_UNIQUE_NAME": ["[Customer]"]},
        )

        assert [c[0] for c in calls] == ["Customer"]

    @pytest.mark.asyncio
    async def test_dotted_dimension_name_narrows_correctly(self, monkeypatch):
        """R2 finding: narrowing must be bracket-aware. A dimension whose name
        contains a literal '.' must still resolve from a MEMBER_UNIQUE_NAME
        restriction (a naive split('.') would parse the wrong name and return
        zero members)."""
        calls: list[tuple[str, str | None]] = []
        _patch_flat_members(monkeypatch, calls)
        dims = [
            {"name": "Sales.Region", "source": "column"},
            {"name": "Product", "source": "column"},
        ]

        result = await xmla_server._load_discover_member_data(
            model_id="m1",
            project_id="p1",
            dimensions=dims,
            tenant_slug="acme",
            jwt_token="tok",
            restrictions={
                "MEMBER_UNIQUE_NAME": ["[Sales.Region].[Sales.Region].[West]"],
            },
        )

        assert [c[0] for c in calls] == ["Sales.Region"], (
            f"Dotted dimension name must resolve; fetched {[c[0] for c in calls]}"
        )
        assert set(result.keys()) == {"Sales.Region"}

    @pytest.mark.asyncio
    async def test_no_restriction_still_enumerates_all(self, monkeypatch):
        """With no dim/hier/member/level restriction the handler still
        enumerates every dimension (the legitimate 'browse all' case)."""
        calls: list[tuple[str, str | None]] = []
        _patch_flat_members(monkeypatch, calls)

        await xmla_server._load_discover_member_data(
            model_id="m1",
            project_id="p1",
            dimensions=_DIMS,
            tenant_slug="acme",
            jwt_token="tok",
            restrictions={},
        )

        assert sorted(c[0] for c in calls) == ["Customer", "Product", "Region"]

    @pytest.mark.asyncio
    async def test_measures_level_restriction_fetches_no_dimensions(self, monkeypatch):
        """Bug-6802: a LEVEL_UNIQUE_NAME=[Measures] restriction (a legitimate
        MSOLAP probe that names NO real Tessallite dimension) must fetch NO
        dimension members — never fan a full DISTINCT source scan out to every
        dimension. Failing NARROW: a restriction owning no dimension owns no
        members."""
        calls: list[tuple[str, str | None]] = []
        _patch_flat_members(monkeypatch, calls)

        result = await xmla_server._load_discover_member_data(
            model_id="m1",
            project_id="p1",
            dimensions=_DIMS,
            tenant_slug="acme",
            jwt_token="tok",
            restrictions={"LEVEL_UNIQUE_NAME": ["[Measures]"]},
        )

        assert calls == [], (
            f"a [Measures] level restriction must fan out to NO dimension; "
            f"fetched {[c[0] for c in calls]} (Bug-6802 fail-wide)"
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_unparseable_member_restriction_fetches_no_dimensions(self, monkeypatch):
        """Bug-6802: any MEMBER_UNIQUE_NAME the grammar cannot parse to a
        [Dim].[Hier] owner must fetch NO dimension members, not every one."""
        calls: list[tuple[str, str | None]] = []
        _patch_flat_members(monkeypatch, calls)

        result = await xmla_server._load_discover_member_data(
            model_id="m1",
            project_id="p1",
            dimensions=_DIMS,
            tenant_slug="acme",
            jwt_token="tok",
            # A single-bracket / malformed reference -> grammar "invalid".
            restrictions={"MEMBER_UNIQUE_NAME": ["[NotARealDimensionRef]"]},
        )

        assert calls == [], (
            f"an unparseable member restriction must fan out to NO dimension; "
            f"fetched {[c[0] for c in calls]} (Bug-6802 fail-wide)"
        )
        assert result == {}


class TestMemberCache:
    """Quick win 2 -- short-TTL member cache de-dups the discover burst."""

    @pytest.mark.asyncio
    async def test_repeat_discover_hits_cache(self, monkeypatch):
        """A second identical discover within the TTL must not re-run the
        source fetch."""
        calls: list[tuple[str, str | None]] = []
        _patch_flat_members(monkeypatch, calls)

        restrictions = {"MEMBER_UNIQUE_NAME": ["[Region].[Region].[West]"]}
        kw = dict(
            model_id="m1", project_id="p1", dimensions=_DIMS,
            tenant_slug="acme", jwt_token="tok", restrictions=restrictions,
        )
        first = await xmla_server._load_discover_member_data(**kw)
        second = await xmla_server._load_discover_member_data(**kw)

        assert len(calls) == 1, (
            f"Second discover should hit cache; got {len(calls)} fetches"
        )
        assert first == second

    @pytest.mark.asyncio
    async def test_cache_keyed_by_persona(self, monkeypatch):
        """Two personas must NOT share cached member data -- a restricted
        persona must never see another persona's enumeration."""
        calls: list[tuple[str, str | None]] = []
        _patch_flat_members(monkeypatch, calls)

        restrictions = {"MEMBER_UNIQUE_NAME": ["[Region].[Region].[West]"]}
        base = dict(
            model_id="m1", project_id="p1", dimensions=_DIMS,
            tenant_slug="acme", jwt_token="tok", restrictions=restrictions,
        )
        await xmla_server._load_discover_member_data(persona_id="persona-a", **base)
        await xmla_server._load_discover_member_data(persona_id="persona-b", **base)

        personas = [c[1] for c in calls]
        assert personas == ["persona-a", "persona-b"], (
            "Different personas must each trigger their own scoped fetch "
            f"(no cross-persona cache reuse); got {personas}"
        )

    @pytest.mark.asyncio
    async def test_cache_keyed_by_principal_no_cross_user_rls_leak(self, monkeypatch):
        """CRITICAL (Bug-6602 R1 finding 1): member discovery compiles ROW-LEVEL
        SECURITY from the caller's Principal, not the persona. Two DIFFERENT
        users sharing the SAME persona (here the business base, persona_id=None)
        must NOT share a cache entry, or one user's row-filtered member list
        would be served to another user -- a cross-user data leak. Different
        JWTs (identities) must each trigger their own scoped fetch.
        """
        fetches: list[str] = []

        async def fake_get_dimension_members(
            model_id, dimension_name, tenant_slug, jwt_token, *, persona_id=None,
        ):
            # Return identity-specific members to make a leak observable.
            fetches.append(jwt_token)
            return {
                "members": [{"name": f"{jwt_token}-member"}],
                "levels": [dimension_name],
            }

        monkeypatch.setattr(
            xmla_server, "get_dimension_members", fake_get_dimension_members,
        )

        restrictions = {"MEMBER_UNIQUE_NAME": ["[Region].[Region].[West]"]}
        base = dict(
            model_id="m1", project_id="p1", dimensions=_DIMS,
            tenant_slug="acme", restrictions=restrictions, persona_id=None,
        )
        r_user_a = await xmla_server._load_discover_member_data(
            jwt_token="jwt-user-a", **base,
        )
        r_user_b = await xmla_server._load_discover_member_data(
            jwt_token="jwt-user-b", **base,
        )

        assert fetches == ["jwt-user-a", "jwt-user-b"], (
            "Each distinct principal must fetch its own row-security-scoped "
            f"member list (no cross-user cache reuse); got {fetches}"
        )
        # User B must receive B's members, never A's cached list.
        assert r_user_b["Region"]["members"][0]["name"] == "jwt-user-b-member"
        assert r_user_a["Region"]["members"][0]["name"] == "jwt-user-a-member"


class TestBoundedEnumeration:
    """Quick win 3 -- a high-cardinality flat dim browse is capped to a page,
    but a targeted specific-member lookup is never truncated."""

    @pytest.mark.asyncio
    async def test_flat_browse_bounded_to_page_limit(self, monkeypatch):
        """A whole-level BROWSE of a flat dimension returning more than the page
        limit is truncated to the bounded first page (no multi-MB payload)."""
        limit = xmla_server._member_page_limit()
        calls: list[tuple[str, str | None]] = []
        _patch_flat_members(monkeypatch, calls, count=limit + 500)

        # DIMENSION_UNIQUE_NAME browse (no specific member) -> capped.
        result = await xmla_server._load_discover_member_data(
            model_id="m1",
            project_id="p1",
            dimensions=_DIMS,
            tenant_slug="acme",
            jwt_token="tok",
            restrictions={"DIMENSION_UNIQUE_NAME": ["[Region]"]},
        )

        members = result["Region"]["members"]
        assert len(members) == limit, (
            f"A flat browse must be capped to the page limit {limit}; "
            f"got {len(members)}"
        )

    @pytest.mark.asyncio
    async def test_specific_member_lookup_not_truncated(self, monkeypatch):
        """R1 finding 2: a targeted MEMBER_UNIQUE_NAME lookup must NOT be capped,
        or a flat member beyond the page boundary would silently vanish from the
        pivot. The whole level is returned (downstream _rows_members filters to
        the one requested member)."""
        limit = xmla_server._member_page_limit()
        calls: list[tuple[str, str | None]] = []
        _patch_flat_members(monkeypatch, calls, count=limit + 500)

        result = await xmla_server._load_discover_member_data(
            model_id="m1",
            project_id="p1",
            dimensions=_DIMS,
            tenant_slug="acme",
            jwt_token="tok",
            restrictions={"MEMBER_UNIQUE_NAME": ["[Region].[Region].[West]"]},
        )

        members = result["Region"]["members"]
        assert len(members) == limit + 500, (
            "A specific-member lookup must not be truncated so a member beyond "
            f"the browse page is still resolvable; got {len(members)}"
        )


class TestCacheEviction:
    """Bounded memory (R2 Fable finding 1): one-shot keys (per-token principal,
    per-drill restriction) are never re-read to trigger read-time expiry, so
    ``put`` must sweep + bound the cache or it grows without limit."""

    def test_member_cache_bounded_by_max_entries(self, monkeypatch):
        monkeypatch.setattr(member_cache, "_MEMBER_MAX_ENTRIES", 3)
        for i in range(50):
            member_cache.put_member_data(f"key-{i}", {"members": [i]})
        assert len(member_cache._member_cache) <= 3, (
            f"member cache must stay bounded; grew to "
            f"{len(member_cache._member_cache)}"
        )
        # The most recent writes survive (oldest evicted first).
        assert member_cache.get_member_data("key-49") == {"members": [49]}

    def test_metadata_cache_bounded_by_max_entries(self, monkeypatch):
        monkeypatch.setattr(member_cache, "_METADATA_MAX_ENTRIES", 2)
        for i in range(20):
            member_cache.put_metadata(f"mkey-{i}", ([], [], []))
        assert len(member_cache._metadata_cache) <= 2


def _discover_xml(request_type: str, catalog: str, restriction_xml: str = "") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <Discover xmlns="urn:schemas-microsoft-com:xml-analysis">
      <RequestType>{request_type}</RequestType>
      <Restrictions><RestrictionList>{restriction_xml}</RestrictionList></Restrictions>
      <Properties><PropertyList><Catalog>{catalog}</Catalog></PropertyList></Properties>
    </Discover>
  </soap:Body>
</soap:Envelope>"""


class TestMetadataCache:
    """Quick win 4 -- short-TTL metadata cache eliminates the per-Discover
    hierarchy-detail N+1. Cross-PRINCIPAL isolation (the real caller-variance
    guard) lives in ``TestMetadataCacheCallerVariance``; the tests here use one
    JWT and vary only the CATALOG persona, so they verify that the gateway's
    catalog-persona trimming is applied AFTER the (per-principal) cache read.
    """

    def _patch_metadata(self, monkeypatch, counters, persona_for_catalog):
        from src.dax import xmla_server as xs
        from defusedxml import ElementTree as ET

        _DIMENSIONS = [
            {"id": "r1", "name": "Region", "source": "column"},
            {"id": "r2", "name": "Product", "source": "column"},
            {"id": "r3", "name": "Customer", "source": "column"},
        ]

        async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
            return "meta-model-1", "proj-1", persona_for_catalog(catalog), None

        async def fake_measures(model_id, tenant_slug, jwt_token, **kw):
            counters["measures"] += 1
            return [{"id": "m1", "name": "Revenue", "default_agg": "sum"}]

        async def fake_dimensions(model_id, tenant_slug, jwt_token, **kw):
            counters["dimensions"] += 1
            return [dict(d) for d in _DIMENSIONS]

        async def fake_hierarchies(model_id, tenant_slug, jwt_token, **kw):
            counters["hierarchies"] += 1
            return []

        async def fake_list_all(tenant_slug, jwt_token):
            return [{"id": "meta-model-1", "project_id": "proj-1", "trust_meta": {}}]

        monkeypatch.setattr(xs, "_resolve_model_id", fake_resolve_model_id)
        monkeypatch.setattr(xs, "get_model_measures", fake_measures)
        monkeypatch.setattr(xs, "get_model_dimensions", fake_dimensions)
        monkeypatch.setattr(xs, "get_model_hierarchies", fake_hierarchies)
        monkeypatch.setattr(xs, "list_all_models_for_tenant", fake_list_all)
        return xs, ET

    @pytest.mark.asyncio
    async def test_repeat_discover_skips_metadata_n_plus_one(self, monkeypatch):
        """Two Discovers for the same (tenant, model) must fetch measures /
        dimensions / hierarchies only ONCE -- the second hits the metadata cache,
        eliminating the per-hierarchy N+1."""
        counters = {"measures": 0, "dimensions": 0, "hierarchies": 0}
        xs, ET = self._patch_metadata(
            monkeypatch, counters, persona_for_catalog=lambda c: None,
        )

        xml = _discover_xml("MDSCHEMA_DIMENSIONS", "meta_model")
        for _ in range(2):
            root = ET.fromstring(xml)
            method_el = xs._find_method(root)
            await xs._handle_discover(method_el, tenant_slug="acme", jwt_token="tok")

        assert counters == {"measures": 1, "dimensions": 1, "hierarchies": 1}, (
            f"Metadata must be fetched once and cached; got {counters}"
        )

    @pytest.mark.asyncio
    async def test_catalog_persona_trim_applied_after_cache_read(self, monkeypatch):
        """Ordering guard for the gateway's CATALOG-persona trimming (distinct
        from the per-principal isolation in TestMetadataCacheCallerVariance):
        with ONE JWT, two Discovers to different catalog personas share the
        cached snapshot, and the persona allow-list trimming is applied per
        request AFTER the read — a persona-restricted Discover must NOT poison
        the shared entry so a later business-base Discover sees a trimmed
        catalogue. (The model-service filtering by JWT is modelled in
        TestMetadataCacheCallerVariance; here the fake returns a constant list.)"""
        counters = {"measures": 0, "dimensions": 0, "hierarchies": 0}

        def persona_for_catalog(catalog):
            if catalog.endswith("_restricted"):
                # Restricted persona: only Region (id r1) is allowed.
                return {
                    "id": "persona-restricted",
                    "slug": "restricted",
                    "includes_hidden_columns": False,
                    "included_dimension_ids": ["r1"],
                    "included_measure_ids": [],
                }
            return None  # business base

        xs, ET = self._patch_metadata(monkeypatch, counters, persona_for_catalog)

        # Bug-6603: MDSCHEMA_DIMENSIONS now collapses standalone attribute dims into
        # one [Dimensions] group node, so the per-dimension trim is asserted against
        # MDSCHEMA_HIERARCHIES instead — each attribute keeps its own [Name].[Name]
        # hierarchy, and the SAME persona-trimmed cube list drives both rowsets, so
        # this is the same raw-before-trim guard.

        # 1) Restricted persona Discover first -- would cache a trimmed list if
        #    trimming ran before the cache write.
        restricted = _discover_xml("MDSCHEMA_HIERARCHIES", "meta_model_restricted")
        root = ET.fromstring(restricted)
        resp_r = await xs._handle_discover(
            xs._find_method(root), tenant_slug="acme", jwt_token="tok",
        )
        body_r = resp_r.body.decode()
        assert "[Region].[Region]" in body_r
        assert "[Product]" not in body_r, "restricted persona must not see Product"

        # 2) Business-base Discover second -- must see the FULL catalogue, proving
        #    the cache held raw (untrimmed) metadata.
        base = _discover_xml("MDSCHEMA_HIERARCHIES", "meta_model")
        root = ET.fromstring(base)
        resp_b = await xs._handle_discover(
            xs._find_method(root), tenant_slug="acme", jwt_token="tok",
        )
        body_b = resp_b.body.decode()
        assert "[Region].[Region]" in body_b
        assert "[Product].[Product]" in body_b, (
            "business base must see all dimensions; a persona-trimmed list "
            "leaked through the metadata cache (raw-before-trim violated)"
        )
        assert "[Customer].[Customer]" in body_b
        # Bug-6628: persona_id is now part of the cache key, so the
        # restricted persona catalog (persona_id='persona-restricted') and
        # the business base catalog (persona_id=None) are separate cache
        # entries. Two fetches is correct because the model-service
        # returns persona-scoped metadata server-side. The N+1 elimination
        # still holds for the SAME catalog's connect burst.
        assert counters["dimensions"] == 2, (
            f"metadata must be fetched once per persona variant; got {counters}"
        )

    @pytest.mark.asyncio
    async def test_metadata_helper_partial_preserve_and_no_cache_on_failure(
        self, monkeypatch,
    ):
        """Deliberate, fast pin (R3 Fable finding 3) for the consolidated helper:
        when the hierarchy fetch fails, measures + dimensions that already
        succeeded must still be returned (partial-preserve), the partial result
        must NOT be cached, and the next call must re-fetch. This replaces
        reliance on a real HTTP timeout to exercise the failure leg."""
        from src.dax import xmla_server as xs

        counters = {"measures": 0, "dimensions": 0, "hierarchies": 0}

        async def fake_measures(model_id, tenant_slug, jwt_token, **kw):
            counters["measures"] += 1
            return [{"id": "m1", "name": "Revenue"}]

        async def fake_dimensions(model_id, tenant_slug, jwt_token, **kw):
            counters["dimensions"] += 1
            return [{"id": "d1", "name": "Region"}]

        async def failing_hierarchies(model_id, tenant_slug, jwt_token, **kw):
            counters["hierarchies"] += 1
            raise RuntimeError("simulated hierarchy-detail fetch failure")

        monkeypatch.setattr(xs, "get_model_measures", fake_measures)
        monkeypatch.setattr(xs, "get_model_dimensions", fake_dimensions)
        monkeypatch.setattr(xs, "get_model_hierarchies", failing_hierarchies)

        kw = dict(
            model_id="mm-1", project_id="p1", tenant_slug="acme", jwt_token="tok",
        )
        measures, dims, hiers = await xs._load_model_metadata_cached(**kw)

        # Partial-preserve: the successful fetches survive the later failure.
        assert measures == [{"id": "m1", "name": "Revenue"}]
        assert dims == [{"id": "d1", "name": "Region"}]
        assert hiers == []

        # Not cached: a partial (failed) result must not be frozen for the TTL.
        # The key must match the production key exactly (per-principal, F-1), or
        # this probes a key that is never written and passes vacuously.
        meta_key = member_cache.metadata_key(
            tenant_slug="acme", model_id="mm-1",
            principal_key=member_cache.principal_fingerprint("tok"),
        )
        assert member_cache.get_metadata(meta_key) is None

        # Next call re-fetches (no stale/partial cache hit).
        await xs._load_model_metadata_cached(**kw)
        assert counters["measures"] == 2, (
            f"a failed fetch must not be cached; re-fetch expected, got {counters}"
        )


class TestMetadataCacheCallerVariance:
    """F-1 (Fable 2026-07-07, HIGH): the model-service list endpoints
    AUTO-RESOLVE the caller's effective persona from the JWT and apply the
    persona allow-list AND CLS restricted-column exclusion (Bug-6141) BEFORE
    responding, so the SAME ``(tenant, model)`` endpoint returns DIFFERENT
    measure/dimension NAMES per caller. Keying the metadata cache on
    ``(tenant, model)`` alone let an admin-primed (unfiltered) snapshot serve
    CLS-restricted names to a restricted viewer — or a viewer-primed (trimmed)
    snapshot truncate the admin's catalogue — for the TTL window. The cache must
    be scoped per calling principal.

    These tests model the model-service caller-dependence the earlier suites
    could not see (they returned a constant list regardless of JWT): the fakes
    below vary their result by ``jwt_token`` exactly as ``resolve_effective_persona``
    + CLS filtering do in production.
    """

    def _patch(self, monkeypatch, *, counters=None):
        from src.dax import xmla_server as xs

        def _measures_for(jwt):
            # Admin (unrestricted) sees the CLS-backed measure "SecretMargin";
            # the restricted viewer's persona excludes it (Bug-6141).
            if jwt == "jwt-admin":
                return [
                    {"id": "m1", "name": "Revenue"},
                    {"id": "m2", "name": "SecretMargin"},
                ]
            return [{"id": "m1", "name": "Revenue"}]

        async def fake_measures(model_id, tenant_slug, jwt_token, **kw):
            if counters is not None:
                counters["measures"] += 1
            return [dict(m) for m in _measures_for(jwt_token)]

        async def fake_dimensions(model_id, tenant_slug, jwt_token, **kw):
            if counters is not None:
                counters["dimensions"] += 1
            return [{"id": "d1", "name": "Region"}]

        async def fake_hierarchies(model_id, tenant_slug, jwt_token, **kw):
            if counters is not None:
                counters["hierarchies"] += 1
            return []

        monkeypatch.setattr(xs, "get_model_measures", fake_measures)
        monkeypatch.setattr(xs, "get_model_dimensions", fake_dimensions)
        monkeypatch.setattr(xs, "get_model_hierarchies", fake_hierarchies)
        return xs

    @pytest.mark.asyncio
    async def test_admin_primed_cache_never_leaks_restricted_name_to_viewer(
        self, monkeypatch,
    ):
        xs = self._patch(monkeypatch)
        kw = dict(model_id="mm-1", project_id="p1", tenant_slug="acme")

        # 1) Admin primes the cache with the UNFILTERED list.
        admin_m, _, _ = await xs._load_model_metadata_cached(
            jwt_token="jwt-admin", **kw,
        )
        assert any(m["name"] == "SecretMargin" for m in admin_m)

        # 2) Restricted viewer reads within the TTL — must get ITS OWN filtered
        #    list, never the admin's cached CLS-restricted name.
        viewer_m, _, _ = await xs._load_model_metadata_cached(
            jwt_token="jwt-viewer", **kw,
        )
        names = {m["name"] for m in viewer_m}
        assert "SecretMargin" not in names, (
            "CLS-restricted measure name leaked to a restricted viewer via the "
            "admin-primed metadata cache (F-1)"
        )
        assert names == {"Revenue"}

    @pytest.mark.asyncio
    async def test_viewer_primed_cache_does_not_truncate_admin_catalogue(
        self, monkeypatch,
    ):
        """Reverse prime order: a restricted viewer must not poison the cache so
        that a later admin gets a silently truncated catalogue."""
        xs = self._patch(monkeypatch)
        kw = dict(model_id="mm-1", project_id="p1", tenant_slug="acme")

        viewer_m, _, _ = await xs._load_model_metadata_cached(
            jwt_token="jwt-viewer", **kw,
        )
        assert {m["name"] for m in viewer_m} == {"Revenue"}

        admin_m, _, _ = await xs._load_model_metadata_cached(
            jwt_token="jwt-admin", **kw,
        )
        assert any(m["name"] == "SecretMargin" for m in admin_m), (
            "admin catalogue truncated by a viewer-primed metadata cache (F-1)"
        )

    @pytest.mark.asyncio
    async def test_same_principal_still_shares_cache_n_plus_one_preserved(
        self, monkeypatch,
    ):
        """The freeze fix must survive: two fetches by the SAME principal in one
        connect burst hit the cache once (N+1 elimination intact)."""
        counters = {"measures": 0, "dimensions": 0, "hierarchies": 0}
        xs = self._patch(monkeypatch, counters=counters)
        kw = dict(model_id="mm-1", project_id="p1", tenant_slug="acme")

        await xs._load_model_metadata_cached(jwt_token="jwt-admin", **kw)
        await xs._load_model_metadata_cached(jwt_token="jwt-admin", **kw)

        assert counters == {"measures": 1, "dimensions": 1, "hierarchies": 1}, (
            f"same-principal burst must reuse the cache; got {counters}"
        )


class TestHierarchyLevelSkew:
    """F-2 (Fable 2026-07-07): the model-service preview removes persona-excluded
    levels and indexes the served level into that FILTERED list, so for a caller
    whose auto-resolved persona differs from the catalog persona the level it
    returns can differ from the gateway's ``expand_level`` (computed against the
    UNFILTERED level list). Members must be labelled by the level the preview
    ACTUALLY served, never filed under the wrong (persona-excluded) level.
    """

    _DIM = {
        "name": "Calendar",
        "source": "hierarchy",
        "hierarchy_id": "h1",
        "levels": [
            {"name": "Year", "ordinal": 0},
            {"name": "Quarter", "ordinal": 1},
            {"name": "Month", "ordinal": 2},
        ],
    }

    @pytest.mark.asyncio
    async def test_members_labelled_by_preview_served_level(self, monkeypatch):
        async def fake_preview(**kw):
            # Persona excluded Quarter; asked for gateway index 1 (Quarter) the
            # model-service serves MONTH members from its filtered [Year, Month].
            return {
                "members": [
                    {"key_value": "Jan", "caption": "Jan", "level_name": "Month"},
                ],
                "levels": ["Year", "Month"],
            }

        monkeypatch.setattr(xmla_server, "get_hierarchy_preview", fake_preview)

        result = await xmla_server._load_hierarchy_member_data(
            model_id="m1",
            project_id="p1",
            dimension=dict(self._DIM),
            tenant_slug="acme",
            jwt_token="tok",
            restrictions={"LEVEL_UNIQUE_NAME": ["[Calendar].[Calendar].[Quarter]"]},
            persona_id="persona-x",
        )

        by_level = result["members_by_level"]
        # Fix: Month members land at the Month index (2), NOT the Quarter index
        # (1) the gateway asked for.
        assert "2" in by_level and by_level["2"][0]["level"] == "Month", (
            f"members must be filed under the served level; got {by_level}"
        )
        assert "1" not in by_level, (
            "Month members were mislabelled under the Quarter level (F-2 skew)"
        )

    @pytest.mark.asyncio
    async def test_common_path_unchanged_when_preview_matches(self, monkeypatch):
        """No-skew regression: when the preview serves the requested level, the
        label is unchanged (members stay at ``expand_level``)."""
        async def fake_preview(**kw):
            return {
                "members": [
                    {"key_value": "Q1", "caption": "Q1", "level_name": "Quarter"},
                ],
                "levels": ["Year", "Quarter", "Month"],
            }

        monkeypatch.setattr(xmla_server, "get_hierarchy_preview", fake_preview)

        result = await xmla_server._load_hierarchy_member_data(
            model_id="m1",
            project_id="p1",
            dimension=dict(self._DIM),
            tenant_slug="acme",
            jwt_token="tok",
            restrictions={"LEVEL_UNIQUE_NAME": ["[Calendar].[Calendar].[Quarter]"]},
            persona_id=None,
        )
        by_level = result["members_by_level"]
        assert "1" in by_level and by_level["1"][0]["level"] == "Quarter"


class TestCacheDisable:
    """R3 Fable finding 1: a non-positive TTL must FULLY disable a cache -- put
    must no-op too, or a 'disabled' cache silently accumulates dead entries."""

    def test_put_member_no_ops_when_ttl_disabled(self, monkeypatch):
        monkeypatch.setattr(member_cache, "_MEMBER_TTL_SECONDS", 0)
        member_cache.put_member_data("k", {"members": [1]})
        assert len(member_cache._member_cache) == 0
        assert member_cache.get_member_data("k") is None

    def test_put_metadata_no_ops_when_ttl_disabled(self, monkeypatch):
        monkeypatch.setattr(member_cache, "_METADATA_TTL_SECONDS", 0)
        member_cache.put_metadata("k", ([], [], []))
        assert len(member_cache._metadata_cache) == 0
