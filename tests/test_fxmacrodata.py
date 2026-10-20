from datetime import date

import pytest
import httpx

from tick_vault.fxmacrodata import fetch_fxmacrodata_daily
from tick_vault import fxmacrodata


class _FakeResponse:
    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return {
            "data": [
                {"date": "2026-01-03", "val": 1.11},
                {"date": "2026-01-02", "val": 1.10},
            ]
        }


class _FakeClient:
    def __init__(self) -> None:
        self.url = ""
        self.params = {}
        self.headers = {}

    async def get(self, url: str, params: dict, headers: dict, follow_redirects: bool = False) -> _FakeResponse:
        self.url = url
        self.params = params
        self.headers = headers
        return _FakeResponse()


@pytest.mark.asyncio
async def test_fetch_fxmacrodata_daily_builds_request_and_frame() -> None:
    client = _FakeClient()

    data = await fetch_fxmacrodata_daily(
        "EUR/USD",
        date(2026, 1, 1),
        "2026-01-04",
        api_key="test-key",
        client=client,
    )

    assert client.url == "https://api.fxmacrodata.com/v1/forex/eur/usd"
    assert client.params == {
        "start_date": "2026-01-01",
        "end_date": "2026-01-04",
        "limit": 100,
        "offset": 0,
    }
    assert client.headers == {"X-API-Key": "test-key"}
    assert data["value"].tolist() == [1.10, 1.11]


@pytest.mark.asyncio
async def test_fetch_fxmacrodata_daily_omits_key_header_without_key() -> None:
    client = _FakeClient()

    await fetch_fxmacrodata_daily("EURUSD", "2026-01-01", "2026-01-04", client=client)

    assert client.headers == {}


class _PagedResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


class _PagedClient:
    def __init__(self, total: int) -> None:
        self.rows = [
            {"date": f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", "val": 1 + i / 1000}
            for i in range(total)
        ][::-1]
        self.offsets: list[int] = []

    async def get(self, url: str, params: dict, headers: dict, follow_redirects: bool = False) -> _PagedResponse:
        offset = params["offset"]
        self.offsets.append(offset)
        page = self.rows[offset : offset + params["limit"]]
        more = offset + len(page) < len(self.rows)
        return _PagedResponse(
            {
                "data": page,
                "pagination": {"has_more": more, "next_offset": offset + len(page)},
            }
        )


@pytest.mark.asyncio
async def test_fetch_fxmacrodata_daily_reads_every_page() -> None:
    client = _PagedClient(total=250)

    data = await fetch_fxmacrodata_daily(
        "EURUSD", "2026-01-01", "2026-12-31", api_key="test-key", client=client
    )

    assert client.offsets == [0, 100, 200]
    assert len(data) == 250
    assert data["date"].is_monotonic_increasing


@pytest.mark.asyncio
async def test_fetch_fxmacrodata_daily_strips_and_rejects_bad_keys() -> None:
    client = _FakeClient()

    await fetch_fxmacrodata_daily(
        "EURUSD", "2026-01-01", "2026-01-04", api_key=" test-key\n", client=client
    )
    assert client.headers == {"X-API-Key": "test-key"}

    with pytest.raises(ValueError) as excinfo:
        await fetch_fxmacrodata_daily(
            "EURUSD",
            "2026-01-01",
            "2026-01-04",
            api_key="test-key\r\nX-Other: 1",
            client=_FakeClient(),
        )
    assert "test-key" not in str(excinfo.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"detail": "Not Found"}, [], {"data": {}}])
async def test_fetch_fxmacrodata_daily_rejects_malformed_payload(
    payload: object,
) -> None:
    class _Client:
        async def get(self, url: str, params: dict, headers: dict, follow_redirects: bool = False) -> _PagedResponse:
            return _PagedResponse(payload)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="FXMacroData"):
        await fetch_fxmacrodata_daily(
            "EURUSD", "2026-01-01", "2026-01-04", api_key="test-key", client=_Client()
        )


@pytest.mark.asyncio
async def test_redirect_enabled_caller_client_cannot_forward_key():
    requests = []

    def handler(request):
        requests.append((request.url.host, request.headers.get("X-API-Key")))
        if request.url.host == "origin.example":
            return httpx.Response(302, headers={"Location": "https://other.example/rates"})
        return httpx.Response(200, json={"data": []})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True,
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_fxmacrodata_daily(
                "EURUSD", "2026-01-01", "2026-01-04", api_key="fixture-key",
                client=client, base_url="https://origin.example/v1",
            )
        assert not client.is_closed
    assert requests == [("origin.example", "fixture-key")]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"detail": "fixture-private-value"}, {"error": "fixture-private-value"}, {},
])
async def test_error_payload_never_returns_partial_history_or_echoes_details(payload):
    pages = iter([
        {"data": [{"date": "2026-01-01", "val": 1.0}],
         "pagination": {"has_more": True, "next_offset": 1}},
        payload,
    ])

    class Client:
        async def get(self, *args, **kwargs):
            return _PagedResponse(next(pages))

    with pytest.raises(ValueError) as raised:
        await fetch_fxmacrodata_daily("EURUSD", "2026-01-01", "2026-01-04", client=Client())
    assert "fixture-private-value" not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("pagination", [
    {"has_more": True, "next_offset": 0},
    {"has_more": True, "next_offset": -1},
    {"has_more": True, "next_offset": "bad"},
    {"has_more": True, "next_offset": True},
    {"has_more": True, "next_offset": 1.5},
    {"has_more": "false"}, "bad",
])
async def test_invalid_pagination_is_rejected_before_repeating_page(pagination):
    class Client:
        def __init__(self):
            self.calls = 0

        async def get(self, *args, **kwargs):
            self.calls += 1
            if self.calls > 1:
                raise AssertionError("attempted another page with invalid pagination")
            return _PagedResponse({
                "data": [{"date": "2026-01-01", "val": 1.0}],
                "pagination": pagination,
            })

    with pytest.raises(ValueError):
        await fetch_fxmacrodata_daily("EURUSD", "2026-01-01", "2026-01-04", client=Client())


@pytest.mark.asyncio
async def test_missing_next_offset_uses_page_progress():
    class Client:
        def __init__(self):
            self.offsets = []

        async def get(self, url, params, **kwargs):
            self.offsets.append(params["offset"])
            if len(self.offsets) == 1:
                return _PagedResponse({
                    "data": [{"date": "2026-01-01", "val": 1.0}],
                    "pagination": {"has_more": True},
                })
            return _PagedResponse({"data": []})

    client = Client()
    frame = await fetch_fxmacrodata_daily("EURUSD", "2026-01-01", "2026-01-04", client=client)
    assert client.offsets == [0, 1]
    assert len(frame) == 1


@pytest.mark.asyncio
async def test_empty_page_with_more_data_is_not_accepted_as_complete():
    class Client:
        async def get(self, *args, **kwargs):
            return _PagedResponse({"data": [], "pagination": {"has_more": True}})

    with pytest.raises(ValueError):
        await fetch_fxmacrodata_daily("EURUSD", "2026-01-01", "2026-01-04", client=Client())


@pytest.mark.asyncio
async def test_owned_client_is_closed_on_invalid_response(monkeypatch):
    class Client:
        closed = False

        async def get(self, *args, **kwargs):
            return _PagedResponse({"data": {}})

        async def aclose(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr(fxmacrodata, "AsyncClient", lambda **kwargs: client)
    with pytest.raises(ValueError):
        await fetch_fxmacrodata_daily("EURUSD", "2026-01-01", "2026-01-04")
    assert client.closed
