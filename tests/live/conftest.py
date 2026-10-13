"""Fixtures for the live end-to-end suite: a real QuerySource server over HTTP.

Unlike ``tests/e2e`` (dry-run, no database) this suite talks to a running
QuerySource instance — normally started by hand with ``python run.py`` — using
an ``aiohttp`` client, exactly as an external consumer would. Nothing inside
the server process is patched.

What the fixtures provide:

* ``live_server``: the base URL of a reachable server. If nothing listens on
  the configured host/port the suite is skipped, unless ``QS_LIVE_SPAWN=1`` is
  set, in which case ``python run.py`` is spawned for the session.
* ``auth_headers``: a Bearer token obtained from ``POST /api/v1/login`` with
  ``QS_LIVE_USER``/``QS_LIVE_PASSWORD`` (falling back to the ``AUTH_TEST_EMAIL``
  / ``AUTH_TEST_PASSWORD`` navconfig values), or ``QS_LIVE_TOKEN`` verbatim.
* ``seed``: a throw-away schema (``qs_live_<hex>``) with ``stores``/``sales``
  tables plus three slugs registered in ``public.queries`` (two single
  queries and one stored MultiQuery pipeline). Everything is dropped at the
  end of the session.
* ``client``: a per-test ``aiohttp.ClientSession`` bound to the server with
  the auth header already set.

Run with ``pytest tests/live -m live``; the ``live`` marker is deselected by
default (see ``pytest.ini``).
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Iterator
from urllib.parse import urlsplit

import aiohttp
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Tag every test under ``tests/live`` with the ``live`` marker."""
    here = Path(__file__).parent
    for item in items:
        if here in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.live)


# ---------------------------------------------------------------------------
# Seed data — the single source of truth for every expected value in the tests
# ---------------------------------------------------------------------------

STORES: list[tuple[int, str, str]] = [
    # store_id, store_name, region
    (1, "Downtown", "EMEA"),
    (2, "Harbor", "EMEA"),
    (3, "Airport", "APAC"),
    (4, "Mall", "LATAM"),
    (5, "Outlet", "APAC"),  # no sales: dropped by an inner join
]

SALES: list[tuple[int, int, str, float]] = [
    # sale_id, store_id, product, amount
    (1, 1, "laptop", 120.0),
    (2, 1, "phone", 80.0),
    (3, 2, "phone", 45.0),
    (4, 3, "tablet", 210.0),
    (5, 3, "laptop", 10.0),
    (6, 4, "phone", 65.0),
]

FILTER_AMOUNT = 50  # pipeline keeps sales strictly above this amount


@dataclass
class Seed:
    """Names created for this session and helpers to compute expected results."""

    schema: str
    stores_slug: str
    sales_slug: str
    pipeline_slug: str
    stores: list[tuple[int, str, str]] = field(default_factory=lambda: list(STORES))
    sales: list[tuple[int, int, str, float]] = field(default_factory=lambda: list(SALES))

    @property
    def regions(self) -> dict[int, str]:
        return {store_id: region for store_id, _, region in self.stores}

    def joined(self) -> list[dict]:
        """Inner join of sales with stores, as the Join operator produces it."""
        return [
            {
                "sale_id": sale_id,
                "store_id": store_id,
                "product": product,
                "amount": amount,
                "region": self.regions[store_id],
            }
            for sale_id, store_id, product, amount in self.sales
            if store_id in self.regions
        ]

    def totals_by_region(self, min_amount: float = FILTER_AMOUNT) -> dict[str, float]:
        totals: dict[str, float] = {}
        for row in self.joined():
            if row["amount"] > min_amount:
                totals[row["region"]] = totals.get(row["region"], 0.0) + row["amount"]
        return totals

    def pipeline(self) -> dict:
        """The MultiQuery payload used both inline and as the stored slug."""
        return {
            "queries": {
                "stores": {"slug": self.stores_slug},
                "sales": {"slug": self.sales_slug},
            },
            "Join": [
                {"type": "inner", "left": "stores", "right": "sales", "using": ["store_id"]}
            ],
            "Filter": {
                "conditions": [
                    {"column": "amount", "expression": ">", "value": FILTER_AMOUNT}
                ]
            },
            "GroupBy": {"by": ["region"], "columns": {"amount": "sum"}},
            "Transform": [{"tOrder": {"columns": ["amount_sum"], "ascending": False}}],
        }


# ---------------------------------------------------------------------------
# Server discovery / spawning
# ---------------------------------------------------------------------------

def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            return sock.connect_ex((host, port)) == 0
        except OSError:
            return False


def _configured_base_url() -> str:
    """``QS_LIVE_URL`` or the server's own ``APP_HOST``/``APP_PORT``."""
    if url := os.environ.get("QS_LIVE_URL"):
        return url.rstrip("/")
    from navconfig import config

    host = config.get("APP_HOST", fallback="localhost")
    port = config.getint("APP_PORT", fallback=5000)
    return f"http://{host}:{port}"


@pytest.fixture(scope="session")
def live_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Base URL of a reachable QuerySource server (spawning one if asked to)."""
    base_url = _configured_base_url()
    parts = urlsplit(base_url)
    host, port = parts.hostname or "localhost", parts.port or 80
    if _port_open(host, port):
        yield base_url
        return
    if os.environ.get("QS_LIVE_SPAWN", "").lower() not in ("1", "true", "yes"):
        pytest.skip(
            f"No QuerySource server listening on {base_url}. Start one with "
            "`python run.py` (or set QS_LIVE_SPAWN=1 to let the suite spawn it)."
        )
    log_path = tmp_path_factory.mktemp("qs-live") / "server.log"
    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "run.py"],
            cwd=REPO_ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline and proc.poll() is None:
        if _port_open(host, port):
            break
        time.sleep(0.5)
    else:
        proc.kill()
        pytest.fail(f"python run.py did not open {base_url} in time; see {log_path}")
    time.sleep(1.0)  # let on_startup hooks (pools, scheduler) finish
    try:
        yield base_url
    finally:
        os.killpg(proc.pid, signal.SIGINT)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _credentials() -> tuple[str | None, str | None]:
    user = os.environ.get("QS_LIVE_USER")
    password = os.environ.get("QS_LIVE_PASSWORD")
    if user and password:
        return user, password
    from navconfig import config

    return (
        config.get("AUTH_TEST_EMAIL", fallback=None),
        config.get("AUTH_TEST_PASSWORD", fallback=None),
    )


async def _login(base_url: str, user: str, password: str) -> dict:
    async with aiohttp.ClientSession(base_url=base_url) as session:
        async with session.post(
            "/api/v1/login", json={"username": user, "password": password}
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                pytest.fail(f"Login failed with HTTP {resp.status}: {body[:300]}")
            return json.loads(body)


@pytest.fixture(scope="session")
def auth_headers(live_server: str) -> dict[str, str]:
    """``Authorization`` header for every authenticated request."""
    if token := os.environ.get("QS_LIVE_TOKEN"):
        return {"Authorization": f"Bearer {token}"}
    user, password = _credentials()
    if not (user and password):
        pytest.skip(
            "No credentials: set QS_LIVE_USER/QS_LIVE_PASSWORD, QS_LIVE_TOKEN, "
            "or AUTH_TEST_EMAIL/AUTH_TEST_PASSWORD in the navconfig environment."
        )
    payload = asyncio.run(_login(live_server, user, password))
    scheme = payload.get("token_type") or "Bearer"
    return {"Authorization": f"{scheme} {payload['token']}"}


# ---------------------------------------------------------------------------
# Database seed (same database the server uses: querysource.conf.default_dsn)
# ---------------------------------------------------------------------------

def _dsn() -> str:
    from querysource.conf import default_dsn

    return default_dsn


def _slug_row_sql() -> str:
    return (
        "INSERT INTO public.queries (query_slug, description, query_raw, provider, parser, "
        "is_raw, is_cached, cache_timeout, cache_refresh, program_id, program_slug, created_at) "
        "VALUES ($1, $2, $3, $4, $5, false, false, 0, 0, 1, 'default', now())"
    )


async def _create_seed(seed: Seed) -> None:
    from asyncdb import AsyncDB

    db = AsyncDB("pg", dsn=_dsn())
    async with await db.connection() as conn:
        statements = [
            f"CREATE SCHEMA {seed.schema}",
            f"CREATE TABLE {seed.schema}.stores "
            "(store_id integer PRIMARY KEY, store_name text NOT NULL, region text NOT NULL)",
            f"CREATE TABLE {seed.schema}.sales "
            "(sale_id integer PRIMARY KEY, store_id integer NOT NULL, "
            "product text NOT NULL, amount numeric(12,2) NOT NULL)",
            f"INSERT INTO {seed.schema}.stores (store_id, store_name, region) VALUES "
            + ", ".join(f"({sid}, '{name}', '{region}')" for sid, name, region in seed.stores),
            f"INSERT INTO {seed.schema}.sales (sale_id, store_id, product, amount) VALUES "
            + ", ".join(
                f"({sale}, {sid}, '{product}', {amount})"
                for sale, sid, product, amount in seed.sales
            ),
        ]
        for sql in statements:
            _, error = await conn.execute(sql)
            assert not error, f"{sql[:60]}...: {error}"
        rows = [
            (
                seed.stores_slug, "qs live e2e: stores",
                f"SELECT * FROM {seed.schema}.stores {{where_cond}}", "db", "pgSQLParser",
            ),
            (
                seed.sales_slug, "qs live e2e: sales",
                f"SELECT * FROM {seed.schema}.sales {{where_cond}}", "db", "pgSQLParser",
            ),
            (
                seed.pipeline_slug, "qs live e2e: multiquery pipeline",
                json.dumps(seed.pipeline()), "multi", "pgSQLParser",
            ),
        ]
        for row in rows:
            _, error = await conn.execute(_slug_row_sql(), *row)
            assert not error, f"inserting slug {row[0]}: {error}"


async def _drop_seed(seed: Seed) -> None:
    from asyncdb import AsyncDB

    db = AsyncDB("pg", dsn=_dsn())
    async with await db.connection() as conn:
        await conn.execute(
            "DELETE FROM public.queries WHERE query_slug IN ($1, $2, $3)",
            seed.stores_slug, seed.sales_slug, seed.pipeline_slug,
        )
        await conn.execute(f"DROP SCHEMA IF EXISTS {seed.schema} CASCADE")


@pytest.fixture(scope="session")
def seed(live_server: str) -> Iterator[Seed]:
    """Create the throw-away schema and slugs; drop them when the session ends."""
    from querysource.conf import DBHOST, DBPORT

    if not _port_open(DBHOST, int(DBPORT)):
        pytest.skip(f"PostgreSQL not reachable on {DBHOST}:{DBPORT}")
    suffix = secrets.token_hex(4)
    data = Seed(
        schema=f"qs_live_{suffix}",
        stores_slug=f"qs_live_{suffix}_stores",
        sales_slug=f"qs_live_{suffix}_sales",
        pipeline_slug=f"qs_live_{suffix}_pipeline",
    )
    asyncio.run(_create_seed(data))
    try:
        yield data
    finally:
        asyncio.run(_drop_seed(data))


# ---------------------------------------------------------------------------
# HTTP clients
# ---------------------------------------------------------------------------

@pytest.fixture
async def client(
    live_server: str, auth_headers: dict[str, str], seed: Seed
) -> AsyncIterator[aiohttp.ClientSession]:
    """Authenticated client; depends on ``seed`` so slugs exist before any call."""
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(
        base_url=live_server, headers=auth_headers, timeout=timeout
    ) as session:
        yield session


@pytest.fixture
async def anonymous_client(live_server: str) -> AsyncIterator[aiohttp.ClientSession]:
    """Client without credentials, to assert the server rejects it."""
    async with aiohttp.ClientSession(
        base_url=live_server, timeout=aiohttp.ClientTimeout(total=30)
    ) as session:
        yield session
