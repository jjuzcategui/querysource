"""Live end-to-end: QuerySource and MultiQuery over HTTP against a running server.

Every test here is a plain HTTP client talking to ``python run.py``: slug
queries (``/api/v2/services/queries/{slug}``), inline queries
(``/api/v2/test/queries``) and MultiQuery pipelines (``/api/v3/queries``)
with ``Join``, ``Filter``, ``GroupBy`` and ``Transform`` steps. Expected
values are derived from the seed data in ``conftest.py``.
"""
from __future__ import annotations

import csv
import io

import aiohttp
import pytest

from .conftest import FILTER_AMOUNT, Seed

SLUG_ROUTE = "/api/v2/services/queries/{slug}"
INLINE_ROUTE = "/api/v2/test/queries"
MULTI_ROUTE = "/api/v3/queries"
MULTI_SLUG_ROUTE = "/api/v3/queries/{slug}"


async def get_rows(resp: aiohttp.ClientResponse) -> list[dict]:
    """Assert a 200 JSON response and return its list of row dicts."""
    body = await resp.text()
    assert resp.status == 200, f"HTTP {resp.status}: {body[:400]}"
    rows = await resp.json(content_type=None)
    assert isinstance(rows, list), f"expected a JSON list, got {type(rows).__name__}"
    return rows


def ids(rows: list[dict], key: str) -> list:
    return [row[key] for row in rows]


# ---------------------------------------------------------------------------
# Authentication boundary
# ---------------------------------------------------------------------------

class TestAuth:
    async def test_unauthenticated_request_is_rejected(
        self, anonymous_client: aiohttp.ClientSession, seed: Seed
    ) -> None:
        async with anonymous_client.get(SLUG_ROUTE.format(slug=seed.stores_slug)) as resp:
            assert resp.status == 401

    async def test_components_catalog_lists_pipeline_steps(
        self, client: aiohttp.ClientSession
    ) -> None:
        async with client.get("/api/v3/qs/components") as resp:
            body = await resp.text()
            assert resp.status == 200, body[:300]
        assert {"Join", "Filter", "GroupBy", "tOrder"} <= set(body and _names(body))


def _names(body: str) -> set[str]:
    """Collect every component name mentioned in the catalog payload."""
    import json

    payload = json.loads(body)
    found: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("name", "class_name", "component") and isinstance(value, str):
                    found.add(value)
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return found


# ---------------------------------------------------------------------------
# Single queries by slug
# ---------------------------------------------------------------------------

class TestSlugQueries:
    async def test_slug_returns_every_seeded_row(
        self, client: aiohttp.ClientSession, seed: Seed
    ) -> None:
        async with client.get(SLUG_ROUTE.format(slug=seed.stores_slug)) as resp:
            rows = await get_rows(resp)
        assert sorted(ids(rows, "store_id")) == sorted(sid for sid, _, _ in seed.stores)
        assert set(rows[0]) == {"store_id", "store_name", "region"}

    async def test_filter_from_query_string(
        self, client: aiohttp.ClientSession, seed: Seed
    ) -> None:
        async with client.get(
            SLUG_ROUTE.format(slug=seed.stores_slug), params={"region": "EMEA"}
        ) as resp:
            rows = await get_rows(resp)
        expected = sorted(sid for sid, _, region in seed.stores if region == "EMEA")
        assert sorted(ids(rows, "store_id")) == expected

    async def test_filter_in_list_ordering_and_limit_from_json_body(
        self, client: aiohttp.ClientSession, seed: Seed
    ) -> None:
        body = {"region": ["EMEA", "APAC"], "ordering": ["store_id DESC"], "querylimit": 2}
        async with client.post(SLUG_ROUTE.format(slug=seed.stores_slug), json=body) as resp:
            rows = await get_rows(resp)
        matching = sorted(
            (sid for sid, _, region in seed.stores if region in ("EMEA", "APAC")), reverse=True
        )
        assert ids(rows, "store_id") == matching[:2]

    async def test_where_cond_operators(
        self, client: aiohttp.ClientSession, seed: Seed
    ) -> None:
        body = {"where_cond": {"amount": {">=": 80}}, "ordering": ["amount"]}
        async with client.post(SLUG_ROUTE.format(slug=seed.sales_slug), json=body) as resp:
            rows = await get_rows(resp)
        expected = sorted(sale for sale, _, _, amount in seed.sales if amount >= 80)
        assert sorted(ids(rows, "sale_id")) == expected
        assert [float(a) for a in ids(rows, "amount")] == sorted(
            amount for _, _, _, amount in seed.sales if amount >= 80
        )

    async def test_fields_projection(
        self, client: aiohttp.ClientSession, seed: Seed
    ) -> None:
        body = {"fields": ["store_id", "region"]}
        async with client.post(SLUG_ROUTE.format(slug=seed.stores_slug), json=body) as resp:
            rows = await get_rows(resp)
        assert rows and set(rows[0]) == {"store_id", "region"}

    async def test_no_matching_rows_is_204(
        self, client: aiohttp.ClientSession, seed: Seed
    ) -> None:
        async with client.get(
            SLUG_ROUTE.format(slug=seed.stores_slug), params={"region": "NOWHERE"}
        ) as resp:
            assert resp.status == 204

    async def test_unknown_slug_is_an_error(
        self, client: aiohttp.ClientSession, seed: Seed
    ) -> None:
        async with client.get(SLUG_ROUTE.format(slug=f"{seed.stores_slug}_missing")) as resp:
            assert resp.status in (400, 404)

    async def test_csv_output(self, client: aiohttp.ClientSession, seed: Seed) -> None:
        async with client.get(
            SLUG_ROUTE.format(slug=seed.stores_slug), params={"queryformat": "csv"}
        ) as resp:
            text = await resp.text()
            assert resp.status == 200, text[:300]
            assert resp.content_type.startswith("text/csv")
        reader = list(csv.reader(io.StringIO(text)))
        assert reader[0][:3] == ["store_id", "store_name", "region"]
        assert len(reader) - 1 == len(seed.stores)


# ---------------------------------------------------------------------------
# Inline (raw) queries
# ---------------------------------------------------------------------------

class TestInlineQueries:
    async def test_inline_query_with_where_cond(
        self, client: aiohttp.ClientSession, seed: Seed
    ) -> None:
        body = {
            "query": f"SELECT sale_id, product, amount FROM {seed.schema}.sales {{where_cond}}",
            "where_cond": {"product": "phone"},
            "ordering": ["sale_id"],
        }
        async with client.post(INLINE_ROUTE, json=body) as resp:
            rows = await get_rows(resp)
        assert ids(rows, "sale_id") == [
            sale for sale, _, product, _ in seed.sales if product == "phone"
        ]

    async def test_inline_query_without_placeholders(
        self, client: aiohttp.ClientSession, seed: Seed
    ) -> None:
        body = {"query": f"SELECT count(*) AS total FROM {seed.schema}.stores"}
        async with client.post(INLINE_ROUTE, json=body) as resp:
            rows = await get_rows(resp)
        assert rows == [{"total": len(seed.stores)}]


# ---------------------------------------------------------------------------
# MultiQuery: Join + Filter + GroupBy + Transform
# ---------------------------------------------------------------------------

class TestMultiQuery:
    async def test_join_filter_groupby_order_inline(
        self, client: aiohttp.ClientSession, seed: Seed
    ) -> None:
        async with client.post(MULTI_ROUTE, json=seed.pipeline()) as resp:
            rows = await get_rows(resp)
        totals = seed.totals_by_region()
        assert {r["region"]: float(r["amount_sum"]) for r in rows} == totals
        # tOrder descending on the aggregate
        assert ids(rows, "region") == sorted(totals, key=totals.get, reverse=True)

    async def test_same_pipeline_from_stored_slug(
        self, client: aiohttp.ClientSession, seed: Seed
    ) -> None:
        async with client.post(MULTI_SLUG_ROUTE.format(slug=seed.pipeline_slug)) as resp:
            rows = await get_rows(resp)
        assert {r["region"]: float(r["amount_sum"]) for r in rows} == seed.totals_by_region()

    async def test_conditions_are_pushed_down_to_child_queries(
        self, client: aiohttp.ClientSession, seed: Seed
    ) -> None:
        payload = {
            "queries": {
                "stores": {"slug": seed.stores_slug, "region": "EMEA"},
                "sales": {"slug": seed.sales_slug},
            },
            "Join": [{"type": "inner", "left": "stores", "right": "sales", "using": ["store_id"]}],
        }
        async with client.post(MULTI_ROUTE, json=payload) as resp:
            rows = await get_rows(resp)
        expected = sorted(r["sale_id"] for r in seed.joined() if r["region"] == "EMEA")
        assert sorted(ids(rows, "sale_id")) == expected
        assert set(ids(rows, "region")) == {"EMEA"}

    async def test_filter_with_or_operator(
        self, client: aiohttp.ClientSession, seed: Seed
    ) -> None:
        payload = {
            "queries": {"sales": {"slug": seed.sales_slug}},
            "Filter": {
                "operator": "|",
                "conditions": [
                    {"column": "product", "expression": "==", "value": "tablet"},
                    {"column": "amount", "expression": "<", "value": 20},
                ],
            },
        }
        async with client.post(MULTI_ROUTE, json=payload) as resp:
            rows = await get_rows(resp)
        expected = sorted(
            sale for sale, _, product, amount in seed.sales
            if product == "tablet" or amount < 20
        )
        assert sorted(ids(rows, "sale_id")) == expected

    async def test_filter_fields_equality_and_text_expression(
        self, client: aiohttp.ClientSession, seed: Seed
    ) -> None:
        payload = {
            "queries": {"sales": {"slug": seed.sales_slug}},
            "Filter": {
                "fields": {"store_id": 1},
                "conditions": [{"column": "product", "expression": "startswith", "value": "la"}],
            },
        }
        async with client.post(MULTI_ROUTE, json=payload) as resp:
            rows = await get_rows(resp)
        expected = [
            sale for sale, sid, product, _ in seed.sales
            if sid == 1 and product.startswith("la")
        ]
        assert ids(rows, "sale_id") == expected

    async def test_filter_that_drops_every_row_is_204(
        self, client: aiohttp.ClientSession, seed: Seed
    ) -> None:
        """An all-filtering ``Filter`` answers the empty-result 204, not an error."""
        payload = {
            "queries": {"sales": {"slug": seed.sales_slug}},
            "Filter": {"conditions": [{"column": "amount", "expression": ">", "value": 10_000}]},
        }
        async with client.post(MULTI_ROUTE, json=payload) as resp:
            assert resp.status == 204, await resp.text()
            assert "No Data was Found after Filtering" in resp.headers.get("X-Error", "")

    async def test_transform_map_pluck_and_order(
        self, client: aiohttp.ClientSession, seed: Seed
    ) -> None:
        payload = {
            "queries": {
                "stores": {"slug": seed.stores_slug},
                "sales": {"slug": seed.sales_slug},
            },
            "Join": [{"type": "inner", "left": "stores", "right": "sales", "using": ["store_id"]}],
            "Transform": [
                {"Map": {"fields": {"revenue": "amount"}, "replace_columns": True}},
                {"PluckCols": {"columns": ["sale_id", "region", "revenue"]}},
                {"tOrder": {"columns": ["revenue", "sale_id"], "ascending": [False, True]}},
            ],
        }
        async with client.post(MULTI_ROUTE, json=payload) as resp:
            rows = await get_rows(resp)
        assert set(rows[0]) == {"sale_id", "region", "revenue"}
        expected = sorted(seed.joined(), key=lambda r: (-r["amount"], r["sale_id"]))
        assert ids(rows, "sale_id") == [r["sale_id"] for r in expected]
        assert [float(r) for r in ids(rows, "revenue")] == [r["amount"] for r in expected]

    async def test_groupby_without_filter_counts_every_sale(
        self, client: aiohttp.ClientSession, seed: Seed
    ) -> None:
        payload = {
            "queries": {
                "stores": {"slug": seed.stores_slug},
                "sales": {"slug": seed.sales_slug},
            },
            "Join": [{"type": "inner", "left": "stores", "right": "sales", "using": ["store_id"]}],
            "GroupBy": {"by": ["region"], "columns": {"sale_id": "count", "amount": "sum"}},
        }
        async with client.post(MULTI_ROUTE, json=payload) as resp:
            rows = await get_rows(resp)
        joined = seed.joined()
        counts = {r["region"]: 0 for r in joined}
        totals = seed.totals_by_region(min_amount=float("-inf"))
        for r in joined:
            counts[r["region"]] += 1
        assert {r["region"]: r["sale_id_count"] for r in rows} == counts
        assert {r["region"]: float(r["amount_sum"]) for r in rows} == totals

    async def test_multiquery_csv_output(
        self, client: aiohttp.ClientSession, seed: Seed
    ) -> None:
        async with client.post(
            MULTI_ROUTE, json=seed.pipeline(), params={"queryformat": "csv"}
        ) as resp:
            text = await resp.text()
            assert resp.status == 200, text[:300]
            assert resp.content_type.startswith("text/csv")
        reader = list(csv.DictReader(io.StringIO(text)))
        assert {r["region"]: float(r["amount_sum"]) for r in reader} == seed.totals_by_region()

    async def test_empty_payload_is_rejected(self, client: aiohttp.ClientSession) -> None:
        async with client.post(MULTI_ROUTE, json={}) as resp:
            assert resp.status == 400

    async def test_filter_threshold_matches_seed(self, seed: Seed) -> None:
        """Guard: the seed really exercises the Filter (some rows above, some below)."""
        amounts = [amount for _, _, _, amount in seed.sales]
        assert any(a > FILTER_AMOUNT for a in amounts) and any(a <= FILTER_AMOUNT for a in amounts)
