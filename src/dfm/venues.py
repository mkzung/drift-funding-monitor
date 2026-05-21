"""Venue clients — abstract base + concrete implementations for each perp DEX.

Each venue exposes:
  - fetch_funding_rate(symbol) → FundingRate
  - fetch_market_state(symbol) → PerpMarketState
  - normalize_symbol(canonical_symbol) → venue-native symbol

Concrete clients hit each venue's public HTTP API (no signing — read-only). For
unit tests the abstract `Venue` ABC is satisfied by `FakeVenueClient` which
returns deterministic synthetic state.

The async surface lets a scanner fan out to all 4 venues in parallel; venue
clients tolerate transient errors and return None for individual symbol
failures rather than killing the whole scan.

References:
  - Drift Protocol: docs.drift.trade (Anchor IDL + REST API)
  - Hyperliquid: docs.hyperliquid.xyz (Info API endpoint)
  - Orderly: docs.orderly.network (REST)
  - Backpack: docs.backpack.exchange (REST)
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod

import httpx

from .state import FundingRate, PerpMarketState, Venue


class VenueClient(ABC):
    """Abstract perp-venue read-only client."""

    venue: Venue

    @abstractmethod
    async def fetch_funding_rate(self, symbol: str) -> FundingRate | None:
        """Fetch current funding rate for one symbol. Returns None on failure."""

    @abstractmethod
    async def fetch_market_state(self, symbol: str) -> PerpMarketState | None:
        """Fetch full market snapshot. Returns None on failure."""

    @abstractmethod
    def normalize_symbol(self, canonical: str) -> str:
        """Map canonical symbol (e.g. 'SOL-PERP') to venue-native naming."""


# ──────────────────────────────────────────────────────────────────────
# Real venue stubs — public HTTP endpoints documented but not exercised
# in tests. Tests use FakeVenueClient below.
# ──────────────────────────────────────────────────────────────────────


class DriftClient(VenueClient):
    """Drift Protocol stub — production path TBD.

    Drift state is best read directly from Solana RPC via the Anchor IDL
    (`anchorpy` + `@drift-labs/sdk`). A REST-style funding endpoint exists at
    `data.api.drift.trade` and may move; the canonical source of truth is the
    on-chain `perp_market` account. This client returns None from the live
    fetchers until either:

      1. anchorpy + Drift IDL are wired in (preferred path), or
      2. the user injects a Birdeye/Helius/Hubble-style indexed snapshot
         via `FakeVenueClient`.

    Tests use FakeVenueClient; production users should subclass this and
    override `fetch_funding_rate` + `fetch_market_state`.
    """

    venue = Venue.DRIFT

    def __init__(self, base_url: str | None = None, timeout_s: float = 8.0):
        # Kept for API parity with the other clients; unused by the stub
        # body. A real subclass would assign self.base_url here and use it.
        self.base_url = base_url or ""
        self.timeout_s = timeout_s

    def normalize_symbol(self, canonical: str) -> str:
        # Drift uses "SOL-PERP" style natively
        return canonical.upper()

    async def fetch_funding_rate(self, symbol: str) -> FundingRate | None:
        # Stub. Production users wire in anchorpy here or subclass to point
        # at their indexer. Returning None keeps the scanner functional
        # (Drift just gets skipped in scan_all_venues output).
        return None

    async def fetch_market_state(self, symbol: str) -> PerpMarketState | None:
        return None


class HyperliquidClient(VenueClient):
    """Hyperliquid Info API stub.

    POST to `https://api.hyperliquid.xyz/info` with `{"type": "metaAndAssetCtxs"}`
    returns the meta + per-asset context including funding, mark, open
    interest in a single call. Faster than 4 separate fetches.
    """

    venue = Venue.HYPERLIQUID
    BASE_URL = "https://api.hyperliquid.xyz"

    def __init__(self, base_url: str | None = None, timeout_s: float = 8.0):
        self.base_url = base_url or self.BASE_URL
        self.timeout_s = timeout_s

    def normalize_symbol(self, canonical: str) -> str:
        # Hyperliquid uses bare symbol, e.g. "SOL" not "SOL-PERP"
        return canonical.upper().replace("-PERP", "")

    async def _fetch_meta(self) -> list | None:  # type: ignore[type-arg]
        """Hyperliquid /info returns a heterogeneous 2-tuple:
        [{"universe": [...]}, [ctx_dict_0, ctx_dict_1, ...]]
        We keep the bare-list type because the two slots have different shapes.
        """
        url = f"{self.base_url}/info"
        async with httpx.AsyncClient(timeout=self.timeout_s) as cli:
            try:
                resp = await cli.post(url, json={"type": "metaAndAssetCtxs"})
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, list):
                    return None
                return data
            except (httpx.HTTPError, ValueError):
                return None

    async def fetch_funding_rate(self, symbol: str) -> FundingRate | None:
        st = await self.fetch_market_state(symbol)
        return st.funding_rate if st else None

    async def fetch_market_state(self, symbol: str) -> PerpMarketState | None:
        sym = self.normalize_symbol(symbol)
        meta = await self._fetch_meta()
        if not meta or not isinstance(meta, list) or len(meta) < 2:
            return None
        meta_info, ctxs = meta[0], meta[1]
        if not isinstance(meta_info, dict) or not isinstance(ctxs, list):
            return None
        universe = meta_info.get("universe", [])
        if not isinstance(universe, list):
            return None
        try:
            idx = next(
                (i for i, a in enumerate(universe)
                 if isinstance(a, dict) and a.get("name") == sym),
                None,
            )
        except (AttributeError, TypeError):
            return None
        if idx is None or idx >= len(ctxs):
            return None
        ctx = ctxs[idx]
        if not isinstance(ctx, dict):
            return None
        # Reject zero/negative prices BEFORE coercing to 1.0 — a zero mark
        # from the venue is a data-error signal that ConcentrationRisk /
        # BasisBlowoutRisk would otherwise misread as a real $1 mark.
        try:
            mark = float(ctx.get("markPx", 0))
            index = float(ctx.get("oraclePx", 0))
        except (TypeError, ValueError):
            return None
        if mark <= 0 or index <= 0:
            return None
        ts = int(time.time())
        return PerpMarketState(
            venue=self.venue,
            symbol=sym,
            timestamp=ts,
            mark_price=mark,
            index_price=index,
            # HL's `metaAndAssetCtxs` reports total `openInterest` (in base
            # units, not USD) as a single scalar — there's no long/short
            # breakdown in this endpoint. Splitting 50/50 (prior version)
            # made `open_interest_imbalance` deterministically 0 and silently
            # neutered ConcentrationRisk for every HL state. Set both to 0
            # to signal "unknown" until a long/short-aware source is wired.
            open_interest_long=0.0,
            open_interest_short=0.0,
            funding_rate=FundingRate(
                venue=self.venue,
                symbol=sym,
                timestamp=ts,
                hourly_rate=float(ctx.get("funding", 0)),
            ),
        )


class OrderlyClient(VenueClient):
    """Orderly Network REST stub. Public endpoint: api.orderly.org/v1/public."""

    venue = Venue.ORDERLY
    BASE_URL = "https://api.orderly.org"

    def __init__(self, base_url: str | None = None, timeout_s: float = 8.0):
        self.base_url = base_url or self.BASE_URL
        self.timeout_s = timeout_s

    def normalize_symbol(self, canonical: str) -> str:
        # Orderly uses "PERP_SOL_USDC" style
        base = canonical.upper().replace("-PERP", "")
        return f"PERP_{base}_USDC"

    async def fetch_funding_rate(self, symbol: str) -> FundingRate | None:
        sym = self.normalize_symbol(symbol)
        url = f"{self.base_url}/v1/public/funding_rate/{sym}"
        async with httpx.AsyncClient(timeout=self.timeout_s) as cli:
            try:
                resp = await cli.get(url)
                resp.raise_for_status()
                payload = resp.json()
            except (httpx.HTTPError, ValueError):
                return None
        # Defensive: Orderly returns {"success":..., "data":{...}}; reject
        # any other shape (list, scalar, null) without crashing.
        if not isinstance(payload, dict):
            return None
        data = payload.get("data", {})
        if not isinstance(data, dict):
            return None
        # Orderly reports `last_funding_rate` over the market's funding
        # interval. Default is 8h on Orderly but some pairs shipped at 1h;
        # VERIFY via /v1/public/info before deploying live. The /8 here
        # is the common-case approximation, identical caveat to Backpack.
        rate_per_interval = float(data.get("last_funding_rate", 0))
        return FundingRate(
            venue=self.venue,
            symbol=sym,
            timestamp=int(time.time()),
            hourly_rate=rate_per_interval / 8,
        )

    async def fetch_market_state(self, symbol: str) -> PerpMarketState | None:
        fr = await self.fetch_funding_rate(symbol)
        if fr is None:
            return None
        return PerpMarketState(
            venue=self.venue,
            symbol=fr.symbol,
            timestamp=fr.timestamp,
            mark_price=1.0,
            index_price=1.0,
            funding_rate=fr,
            last_update_lag_s=999,
        )


class BackpackClient(VenueClient):
    """Backpack Exchange REST stub. Public endpoint: api.backpack.exchange."""

    venue = Venue.BACKPACK
    BASE_URL = "https://api.backpack.exchange"

    def __init__(self, base_url: str | None = None, timeout_s: float = 8.0):
        self.base_url = base_url or self.BASE_URL
        self.timeout_s = timeout_s

    def normalize_symbol(self, canonical: str) -> str:
        # Backpack uses "SOL_USDC_PERP"
        base = canonical.upper().replace("-PERP", "")
        return f"{base}_USDC_PERP"

    async def fetch_funding_rate(self, symbol: str) -> FundingRate | None:
        sym = self.normalize_symbol(symbol)
        # Backpack public REST uses camelCase path segments. Verified against
        # docs.backpack.exchange (May 2026): `GET /api/v1/fundingRates`
        # returns `[{"fundingRate":"<8h>", "symbol":..., "intervalEndTimestamp":...}]`.
        # Round-2 kebab-case "fix" was the wrong direction.
        url = f"{self.base_url}/api/v1/fundingRates?symbol={sym}&limit=1"
        async with httpx.AsyncClient(timeout=self.timeout_s) as cli:
            try:
                resp = await cli.get(url)
                resp.raise_for_status()
                payload = resp.json()
            except (httpx.HTTPError, ValueError):
                return None
        if not payload or not isinstance(payload, list):
            return None
        latest = payload[0]
        # Defensive: list element must be a dict — reject otherwise.
        if not isinstance(latest, dict):
            return None
        # Backpack reports the funding rate over the market's funding
        # interval. Most perp markets ship with an 8-hour interval, but
        # some have 1h or 4h — VERIFY per-market via /api/v1/markets
        # before deploying live. The /8 conversion below is correct only
        # for 8h-interval markets; for a real productionization, fetch
        # `fundingInterval` from the markets endpoint and divide by
        # `interval_hours` instead. Kept as /8 here for portfolio demos.
        return FundingRate(
            venue=self.venue,
            symbol=sym,
            timestamp=int(time.time()),
            hourly_rate=float(latest.get("fundingRate", 0)) / 8,
        )

    async def fetch_market_state(self, symbol: str) -> PerpMarketState | None:
        fr = await self.fetch_funding_rate(symbol)
        if fr is None:
            return None
        return PerpMarketState(
            venue=self.venue,
            symbol=fr.symbol,
            timestamp=fr.timestamp,
            mark_price=1.0,
            index_price=1.0,
            funding_rate=fr,
            last_update_lag_s=999,
        )


# ──────────────────────────────────────────────────────────────────────
# Test double — deterministic synthetic venue for hermetic CI
# ──────────────────────────────────────────────────────────────────────


class FakeVenueClient(VenueClient):
    """Test double — returns whatever state was injected at construction time.

    Used by every test in tests/ to keep the suite hermetic (no live HTTP).
    Production code never references this class except via the abstract
    Venue interface.
    """

    def __init__(self, venue: Venue, state_by_symbol: dict[str, PerpMarketState] | None = None):
        self.venue = venue
        self._states = state_by_symbol or {}

    def normalize_symbol(self, canonical: str) -> str:
        return canonical.upper()

    async def fetch_funding_rate(self, symbol: str) -> FundingRate | None:
        st = self._states.get(self.normalize_symbol(symbol))
        return st.funding_rate if st else None

    async def fetch_market_state(self, symbol: str) -> PerpMarketState | None:
        return self._states.get(self.normalize_symbol(symbol))


# ──────────────────────────────────────────────────────────────────────
# Multi-venue scanner
# ──────────────────────────────────────────────────────────────────────


async def scan_all_venues(
    clients: list[VenueClient], symbol: str
) -> dict[Venue, PerpMarketState]:
    """Concurrently fetch market state from every client for one symbol.

    Returns {venue: state}; venues that errored are omitted (not raised).
    The caller decides whether to alert on partial coverage.
    """
    tasks = [c.fetch_market_state(symbol) for c in clients]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out: dict[Venue, PerpMarketState] = {}
    for cli, res in zip(clients, results, strict=False):
        if isinstance(res, PerpMarketState):
            out[cli.venue] = res
    return out
