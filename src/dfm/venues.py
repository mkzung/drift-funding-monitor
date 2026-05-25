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
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx

from .state import FundingRate, PerpMarketState, Venue

logger = logging.getLogger(__name__)

# Default funding interval (hours) used as a fallback when the venue API
# either refuses to surface it or returns it in an unexpected shape. All
# four supported venues currently default to 8h on most markets, but
# individual markets ship at 1h / 4h. Always log a WARNING when this
# fallback is exercised so the operator notices.
_DEFAULT_FUNDING_INTERVAL_HOURS = 8.0


async def _get_with_retry(
    url: str,
    timeout_s: float,
    *,
    max_retries: int = 3,
    backoff_base_s: float = 0.5,
    venue_name: str = "",
) -> httpx.Response | None:
    """GET with exponential-backoff retry on 429 / 5xx. Returns None on
    persistent failure (after `max_retries`). Logs WARNING on each retry
    and on terminal failure with status code + URL for operator triage.

    Non-retryable errors (4xx other than 429, ValueError, transport errors
    that aren't TimeoutException/ConnectError) return None immediately and
    log a single WARNING.
    """
    attempt = 0
    async with httpx.AsyncClient(timeout=timeout_s) as cli:
        while attempt <= max_retries:
            try:
                resp = await cli.get(url)
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    if attempt >= max_retries:
                        logger.warning(
                            "%s GET %s failed status=%d after %d retries",
                            venue_name, url, resp.status_code, attempt,
                        )
                        return None
                    sleep_s = backoff_base_s * (2 ** attempt)
                    logger.warning(
                        "%s GET %s status=%d retry=%d sleep=%.2fs",
                        venue_name, url, resp.status_code, attempt + 1, sleep_s,
                    )
                    await asyncio.sleep(sleep_s)
                    attempt += 1
                    continue
                resp.raise_for_status()
                return resp
            except httpx.HTTPStatusError as e:
                logger.warning(
                    "%s GET %s HTTP error status=%d",
                    venue_name, url, e.response.status_code,
                )
                return None
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                if attempt >= max_retries:
                    logger.warning(
                        "%s GET %s transient %s after %d retries",
                        venue_name, url, type(e).__name__, attempt,
                    )
                    return None
                sleep_s = backoff_base_s * (2 ** attempt)
                logger.warning(
                    "%s GET %s %s retry=%d sleep=%.2fs",
                    venue_name, url, type(e).__name__, attempt + 1, sleep_s,
                )
                await asyncio.sleep(sleep_s)
                attempt += 1
            except httpx.HTTPError as e:
                logger.warning("%s GET %s %s", venue_name, url, type(e).__name__)
                return None
    return None


async def _post_with_retry(
    url: str,
    json: dict[str, Any],
    timeout_s: float,
    *,
    max_retries: int = 3,
    backoff_base_s: float = 0.5,
    venue_name: str = "",
) -> httpx.Response | None:
    """POST counterpart of `_get_with_retry`. Same semantics."""
    attempt = 0
    async with httpx.AsyncClient(timeout=timeout_s) as cli:
        while attempt <= max_retries:
            try:
                resp = await cli.post(url, json=json)
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    if attempt >= max_retries:
                        logger.warning(
                            "%s POST %s failed status=%d after %d retries",
                            venue_name, url, resp.status_code, attempt,
                        )
                        return None
                    sleep_s = backoff_base_s * (2 ** attempt)
                    logger.warning(
                        "%s POST %s status=%d retry=%d sleep=%.2fs",
                        venue_name, url, resp.status_code, attempt + 1, sleep_s,
                    )
                    await asyncio.sleep(sleep_s)
                    attempt += 1
                    continue
                resp.raise_for_status()
                return resp
            except httpx.HTTPStatusError as e:
                logger.warning(
                    "%s POST %s HTTP error status=%d",
                    venue_name, url, e.response.status_code,
                )
                return None
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                if attempt >= max_retries:
                    logger.warning(
                        "%s POST %s transient %s after %d retries",
                        venue_name, url, type(e).__name__, attempt,
                    )
                    return None
                sleep_s = backoff_base_s * (2 ** attempt)
                logger.warning(
                    "%s POST %s %s retry=%d sleep=%.2fs",
                    venue_name, url, type(e).__name__, attempt + 1, sleep_s,
                )
                await asyncio.sleep(sleep_s)
                attempt += 1
            except httpx.HTTPError as e:
                logger.warning("%s POST %s %s", venue_name, url, type(e).__name__)
                return None
    return None


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
        resp = await _post_with_retry(
            url, {"type": "metaAndAssetCtxs"}, self.timeout_s,
            venue_name="hyperliquid",
        )
        if resp is None:
            return None
        try:
            data = resp.json()
        except ValueError:
            logger.warning("hyperliquid POST %s returned non-JSON body", url)
            return None
        if not isinstance(data, list):
            return None
        return data

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
    """Orderly Network REST stub. Public endpoint: api.orderly.org/v1/public.

    NOTE on production-readiness: this client now fetches the per-symbol
    funding interval from `/v1/public/funding_rate_history` (which carries
    the interval used for the historical samples) and caches the result
    per-session, instead of hardcoding /8. Mark and index prices are still
    not exposed by the funding endpoints — `fetch_market_state` returns
    a state with mark=index=1.0 and a sentinel lag, which is why this
    venue is documented as scaffolded-only in README.
    """

    venue = Venue.ORDERLY
    BASE_URL = "https://api.orderly.org"

    def __init__(self, base_url: str | None = None, timeout_s: float = 8.0):
        self.base_url = base_url or self.BASE_URL
        self.timeout_s = timeout_s
        self._interval_cache: dict[str, float] = {}

    def normalize_symbol(self, canonical: str) -> str:
        # Orderly uses "PERP_SOL_USDC" style
        base = canonical.upper().replace("-PERP", "")
        return f"PERP_{base}_USDC"

    async def _fetch_funding_interval_hours(self, sym: str) -> float:
        """Fetch funding interval (hours) from Orderly's funding_rate_history
        endpoint. Cached per (client, symbol). Falls back to 8h with WARNING
        if the API returns an unexpected shape or fails.
        """
        if sym in self._interval_cache:
            return self._interval_cache[sym]
        url = f"{self.base_url}/v1/public/funding_rate_history?symbol={sym}"
        resp = await _get_with_retry(url, self.timeout_s, venue_name="orderly")
        interval_h = _DEFAULT_FUNDING_INTERVAL_HOURS
        if resp is not None:
            try:
                payload = resp.json()
            except ValueError:
                payload = None
            # Orderly wraps in {"success":..,"data":{"rows":[...]}} where each
            # row carries `funding_rate` and `funding_period_hours` (or the
            # interval is encoded between successive `next_funding_time` deltas).
            if isinstance(payload, dict):
                data = payload.get("data", {})
                if isinstance(data, dict):
                    rows = data.get("rows", [])
                    if isinstance(rows, list) and rows:
                        first = rows[0]
                        if isinstance(first, dict):
                            v = first.get("funding_period_hours") \
                                or first.get("fundingIntervalHours")
                            if isinstance(v, (int, float)) and v > 0:
                                interval_h = float(v)
                            elif len(rows) >= 2 and isinstance(rows[1], dict):
                                t1 = first.get("funding_rate_timestamp")
                                t2 = rows[1].get("funding_rate_timestamp")
                                if (isinstance(t1, (int, float))
                                        and isinstance(t2, (int, float))
                                        and t1 != t2):
                                    # Orderly timestamps are ms
                                    delta_h = abs(t1 - t2) / 1000 / 3600
                                    if 0 < delta_h < 48:
                                        interval_h = delta_h
        if interval_h == _DEFAULT_FUNDING_INTERVAL_HOURS:
            logger.warning(
                "orderly funding_interval for %s unavailable; falling back to %.0fh",
                sym, interval_h,
            )
        self._interval_cache[sym] = interval_h
        return interval_h

    async def fetch_funding_rate(self, symbol: str) -> FundingRate | None:
        sym = self.normalize_symbol(symbol)
        url = f"{self.base_url}/v1/public/funding_rate/{sym}"
        resp = await _get_with_retry(url, self.timeout_s, venue_name="orderly")
        if resp is None:
            return None
        try:
            payload = resp.json()
        except ValueError:
            logger.warning("orderly GET %s returned non-JSON body", url)
            return None
        # Defensive: Orderly returns {"success":..., "data":{...}}; reject
        # any other shape (list, scalar, null) without crashing.
        if not isinstance(payload, dict):
            return None
        data = payload.get("data", {})
        if not isinstance(data, dict):
            return None
        # Orderly reports `last_funding_rate` over the market's funding
        # interval. We now fetch the interval per-symbol instead of
        # hardcoding /8 — see _fetch_funding_interval_hours.
        rate_per_interval = float(data.get("last_funding_rate", 0))
        interval_h = await self._fetch_funding_interval_hours(sym)
        return FundingRate(
            venue=self.venue,
            symbol=sym,
            timestamp=int(time.time()),
            hourly_rate=rate_per_interval / interval_h,
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
    """Backpack Exchange REST stub. Public endpoint: api.backpack.exchange.

    NOTE on production-readiness: this client now fetches the per-symbol
    funding interval from `/api/v1/markets` (each market record carries
    `fundingInterval` in MILLISECONDS) and caches the result per-session,
    instead of hardcoding /8. Mark and index prices are still not exposed
    by the funding endpoints — `fetch_market_state` returns a state with
    mark=index=1.0 and a sentinel lag, which is why this venue is
    documented as scaffolded-only in README.
    """

    venue = Venue.BACKPACK
    BASE_URL = "https://api.backpack.exchange"

    def __init__(self, base_url: str | None = None, timeout_s: float = 8.0):
        self.base_url = base_url or self.BASE_URL
        self.timeout_s = timeout_s
        self._interval_cache: dict[str, float] = {}

    def normalize_symbol(self, canonical: str) -> str:
        # Backpack uses "SOL_USDC_PERP"
        base = canonical.upper().replace("-PERP", "")
        return f"{base}_USDC_PERP"

    async def _fetch_funding_interval_hours(self, sym: str) -> float:
        """Fetch funding interval (hours) from Backpack's /api/v1/markets
        endpoint. Cached per (client, symbol). Falls back to 8h with WARNING
        if the API returns an unexpected shape or fails.

        Backpack reports `fundingInterval` in MILLISECONDS on each market
        record. We divide by 3_600_000 to get hours.
        """
        if sym in self._interval_cache:
            return self._interval_cache[sym]
        url = f"{self.base_url}/api/v1/markets"
        resp = await _get_with_retry(url, self.timeout_s, venue_name="backpack")
        interval_h = _DEFAULT_FUNDING_INTERVAL_HOURS
        if resp is not None:
            try:
                payload = resp.json()
            except ValueError:
                payload = None
            if isinstance(payload, list):
                for m in payload:
                    if isinstance(m, dict) and m.get("symbol") == sym:
                        v = m.get("fundingInterval")
                        if isinstance(v, (int, float)) and v > 0:
                            interval_h = float(v) / 3_600_000
                        break
        if interval_h == _DEFAULT_FUNDING_INTERVAL_HOURS:
            logger.warning(
                "backpack funding_interval for %s unavailable; falling back to %.0fh",
                sym, interval_h,
            )
        self._interval_cache[sym] = interval_h
        return interval_h

    async def fetch_funding_rate(self, symbol: str) -> FundingRate | None:
        sym = self.normalize_symbol(symbol)
        # Backpack public REST uses camelCase path segments. Verified against
        # docs.backpack.exchange (May 2026): `GET /api/v1/fundingRates`
        # returns `[{"fundingRate":"<rate>", "symbol":..., "intervalEndTimestamp":...}]`.
        url = f"{self.base_url}/api/v1/fundingRates?symbol={sym}&limit=1"
        resp = await _get_with_retry(url, self.timeout_s, venue_name="backpack")
        if resp is None:
            return None
        try:
            payload = resp.json()
        except ValueError:
            logger.warning("backpack GET %s returned non-JSON body", url)
            return None
        if not payload or not isinstance(payload, list):
            return None
        latest = payload[0]
        # Defensive: list element must be a dict — reject otherwise.
        if not isinstance(latest, dict):
            return None
        interval_h = await self._fetch_funding_interval_hours(sym)
        return FundingRate(
            venue=self.venue,
            symbol=sym,
            timestamp=int(time.time()),
            hourly_rate=float(latest.get("fundingRate", 0)) / interval_h,
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
