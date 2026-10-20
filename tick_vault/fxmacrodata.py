"""FXMacroData daily reference-rate helpers."""

from datetime import date, datetime
from typing import Any

import pandas as pd
from httpx import AsyncClient

from .constants import FXMACRODATA_API_BASE


def _split_pair(symbol: str) -> tuple[str, str]:
    clean_symbol = symbol.replace("/", "").replace("-", "").upper()
    if len(clean_symbol) != 6:
        raise ValueError(
            "FXMacroData symbols must be six-letter FX pairs, for example EURUSD"
        )
    return clean_symbol[:3], clean_symbol[3:]


def _format_date(value: date | datetime | str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _clean_api_key(api_key: str | None) -> str:
    key = (api_key or "").strip()
    if any(ch.isspace() or not ch.isprintable() for ch in key):
        # Never echo the key itself in the error.
        raise ValueError(
            "FXMacroData API key contains whitespace or control characters"
        )
    return key


def _page_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and ("detail" in payload or "error" in payload):
        raise ValueError("FXMacroData request failed: error response")
    page = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(page, list):
        raise ValueError("FXMacroData returned an unexpected response shape")
    return page


def _daily_rows_to_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["date", "value"])

    data = pd.DataFrame(rows)
    data["date"] = pd.to_datetime(data["date"])
    data["value"] = data["val"].astype(float)
    return data[["date", "value"]].sort_values("date").reset_index(drop=True)


async def fetch_fxmacrodata_daily(
    symbol: str,
    start: date | datetime | str,
    end: date | datetime | str,
    *,
    api_key: str | None = None,
    client: AsyncClient | None = None,
    base_url: str = FXMACRODATA_API_BASE,
) -> pd.DataFrame:
    """
    Fetch daily FXMacroData spot reference rates for a currency pair.

    TickVault stores high-resolution Dukascopy tick data.  This helper provides
    an optional daily reference series that can be used to compare tick-derived
    bars with FXMacroData's official/reference spot history.

    Args:
        symbol: Currency pair such as ``"EURUSD"`` or ``"EUR/USD"``.
        start: Start date in ``YYYY-MM-DD`` form, or a date/datetime object.
        end: End date in ``YYYY-MM-DD`` form, or a date/datetime object.
        api_key: FXMacroData API key; forex data requires one.
        client: Optional ``httpx.AsyncClient`` to reuse connection pools.
        base_url: FXMacroData REST API base URL.

    Returns:
        A pandas DataFrame with ``date`` and ``value`` columns.
    """
    base, quote = _split_pair(symbol)
    api_key = _clean_api_key(api_key)
    close_client = client is None
    if client is None:
        client = AsyncClient(timeout=30.0)

    url = f"{base_url.rstrip('/')}/forex/{base.lower()}/{quote.lower()}"
    params: dict[str, str | int] = {
        "start_date": _format_date(start),
        "end_date": _format_date(end),
        "limit": 100,
        "offset": 0,
    }
    headers = {"X-API-Key": api_key} if api_key else {}

    try:
        # The API returns at most 100 rows per request, so follow the offsets
        # until the whole window has been read.
        rows: list[dict] = []
        while True:
            # Override caller-client defaults so the key cannot follow redirects.
            response = await client.get(
                url, params=params, headers=headers, follow_redirects=False,
            )
            response.raise_for_status()
            payload = response.json()
            page = _page_rows(payload)
            rows.extend(page)
            pagination = payload.get("pagination")
            if pagination is None:
                pagination = {}
            if not isinstance(pagination, dict):
                raise ValueError("FXMacroData returned invalid pagination metadata")
            has_more = pagination.get("has_more", False)
            if not isinstance(has_more, bool):
                raise ValueError("FXMacroData returned invalid pagination metadata")
            if has_more and not page:
                raise ValueError("FXMacroData returned an inconsistent empty page")
            if not has_more:
                break
            next_offset = pagination.get("next_offset")
            if next_offset is None:
                next_offset = params["offset"] + len(page)
            if (
                isinstance(next_offset, bool)
                or not isinstance(next_offset, int)
                or next_offset <= params["offset"]
            ):
                raise ValueError("FXMacroData returned a non-advancing page offset")
            params["offset"] = next_offset
        return _daily_rows_to_frame(rows)
    finally:
        if close_client:
            await client.aclose()
