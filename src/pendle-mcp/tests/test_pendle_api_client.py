import httpx
import pytest

import pendle_mcp.pendle_api as pendle_api
from pendle_mcp.pendle_api import (
    PendleApiClient,
    PendleApiError,
    PendleAssetType,
    TransactionAction,
    TransactionType,
)


@pytest.mark.asyncio
async def test_get_chains_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.scheme == "https"
        assert request.url.host == "api-v2.pendle.finance"
        assert request.url.path == "/core/v1/chains"
        return httpx.Response(200, json={"chainIds": [1, 42161]})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        data = await client.get_chains()

    assert data == {"chainIds": [1, 42161]}


@pytest.mark.asyncio
async def test_non_2xx_raises_pendle_api_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "rate limited"})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core",
        transport=transport,
        max_retries=1,
        retry_backoff_seconds=0,
        retry_jitter_ratio=0,
    ) as client:
        with pytest.raises(PendleApiError) as excinfo:
            await client.get_chains()

    assert excinfo.value.status_code == 429
    assert excinfo.value.error_type == "rate_limited"
    assert excinfo.value.attempts == 2
    assert excinfo.value.retries_exhausted is True
    message = str(excinfo.value)
    assert "error_type=rate_limited" in message
    assert "status_code=429" in message
    assert "/core/v1/chains" in message
    assert "rate limited" in message


@pytest.mark.asyncio
async def test_retries_on_429() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"message": "rate limited"})
        return httpx.Response(200, json={"chainIds": [1]})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core",
        transport=transport,
        max_retries=1,
        retry_backoff_seconds=0,
        retry_jitter_ratio=0,
    ) as client:
        data = await client.get_chains()

    assert calls == 2
    assert data == {"chainIds": [1]}


@pytest.mark.asyncio
async def test_retry_after_is_respected_for_429(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(pendle_api.asyncio, "sleep", fake_sleep)

    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                json={"message": "rate limited"},
                headers={"Retry-After": "1"},
            )
        return httpx.Response(200, json={"chainIds": [1]})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core",
        transport=transport,
        max_retries=1,
        retry_backoff_seconds=0,
        retry_jitter_ratio=0,
    ) as client:
        data = await client.get_chains()

    assert data == {"chainIds": [1]}
    assert sleeps == [1.0]


@pytest.mark.asyncio
async def test_invalid_json_raises_pendle_api_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"not-json",
            headers={"content-type": "application/json"},
        )

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        with pytest.raises(PendleApiError) as excinfo:
            await client.get_chains()

    assert "invalid JSON" in str(excinfo.value)


@pytest.mark.asyncio
async def test_error_detail_is_truncated_with_custom_limit() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="0123456789abcdef")

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core",
        transport=transport,
        max_retries=0,
        error_detail_max_chars=10,
    ) as client:
        with pytest.raises(PendleApiError) as excinfo:
            await client.get_chains()

    assert "detail=0123456789…(truncated)" in str(excinfo.value)


@pytest.mark.asyncio
async def test_from_env_reads_error_detail_max_chars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PENDLE_API_ERROR_DETAIL_MAX_CHARS", "10")
    async with PendleApiClient.from_env() as client:
        assert client._error_detail_max_chars == 10


@pytest.mark.asyncio
async def test_get_markets_all_query_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/core/v1/markets/all"
        assert request.url.params.get("chainId") == "42161"
        assert request.url.params.get("isActive") == "true"
        assert request.url.params.get("ids") == "m1,m2"
        return httpx.Response(200, json={"results": []})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        data = await client.get_markets_all(
            chain_id=42161,
            ids=["m1", "m2"],
            is_active=True,
        )

    assert data == {"results": []}


@pytest.mark.asyncio
async def test_get_assets_all_query_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/core/v1/assets/all"
        assert request.url.params.get("chainId") == "1"
        assert request.url.params.get("skip") == "10"
        assert request.url.params.get("limit") == "5"
        assert request.url.params.get("type") == "PT"
        assert request.url.params.get("ids") == "a,b"
        return httpx.Response(200, json={"results": []})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        data = await client.get_assets_all(
            chain_id=1,
            skip=10,
            limit=5,
            asset_type=PendleAssetType.PT,
            ids=["a", "b"],
        )

    assert data == {"results": []}


@pytest.mark.asyncio
async def test_retries_on_5xx() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"chainIds": [1]})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core",
        transport=transport,
        max_retries=1,
        retry_backoff_seconds=0,
    ) as client:
        data = await client.get_chains()

    assert calls == 2
    assert data == {"chainIds": [1]}


@pytest.mark.asyncio
async def test_get_markets_points_market_query_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/core/v1/markets/points-market"
        assert request.url.params.get("chainId") == "42161"
        assert request.url.params.get("isActive") == "false"
        return httpx.Response(200, json={"markets": []})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        data = await client.get_markets_points_market(chain_id=42161, is_active=False)

    assert data == {"markets": []}


@pytest.mark.asyncio
async def test_get_market_data_v2_query_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/core/v2/42161/markets/0xmarket/data"
        assert request.url.params.get("timestamp") == "2026-01-11T00:00:00Z"
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        data = await client.get_market_data_v2(
            chain_id=42161,
            address="0xmarket",
            timestamp="2026-01-11T00:00:00Z",
        )

    assert data == {"ok": True}


@pytest.mark.asyncio
async def test_get_market_historical_data_v2_query_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/core/v2/42161/markets/0xmarket/historical-data"
        assert request.url.params.get("time_frame") == "day"
        assert request.url.params.get("timestamp_start") == "2026-01-01T00:00:00Z"
        assert request.url.params.get("timestamp_end") == "2026-01-11T00:00:00Z"
        assert request.url.params.get("fields") == "ptApy,ytApy"
        assert request.url.params.get("includeFeeBreakdown") == "true"
        return httpx.Response(200, json={"rows": []})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        data = await client.get_market_historical_data_v2(
            chain_id=42161,
            address="0xmarket",
            time_frame="1d",
            timestamp_start="2026-01-01T00:00:00Z",
            timestamp_end="2026-01-11T00:00:00Z",
            fields=["ptApy", "ytApy"],
            include_fee_breakdown=True,
        )

    assert data == {"rows": []}


@pytest.mark.asyncio
async def test_get_prices_ohlcv_v4_query_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/core/v4/42161/prices/0xasset/ohlcv"
        assert request.url.params.get("time_frame") == "hour"
        assert request.url.params.get("timestamp_start") == "2026-01-01T00:00:00Z"
        assert request.url.params.get("timestamp_end") == "2026-01-02T00:00:00Z"
        return httpx.Response(200, json={"candles": []})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        data = await client.get_prices_ohlcv_v4(
            chain_id=42161,
            address="0xasset",
            time_frame="1h",
            timestamp_start="2026-01-01T00:00:00Z",
            timestamp_end="2026-01-02T00:00:00Z",
        )

    assert data == {"candles": []}


@pytest.mark.asyncio
async def test_time_frame_is_validated_before_request() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"candles": []})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        with pytest.raises(ValueError) as excinfo:
            await client.get_prices_ohlcv_v4(
                chain_id=42161,
                address="0xasset",
                time_frame="1x",
            )

    assert calls == 0
    assert "time_frame" in str(excinfo.value)
    assert "hour/day/week" in str(excinfo.value)


@pytest.mark.asyncio
async def test_get_user_pnl_transactions_query_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/core/v1/pnl/transactions"
        assert request.url.params.get("user") == "0xuser"
        assert request.url.params.get("chainId") == "42161"
        assert request.url.params.get("market") == "0xmarket"
        assert request.url.params.get("skip") == "5"
        assert request.url.params.get("limit") == "20"
        return httpx.Response(200, json={"items": []})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        data = await client.get_user_pnl_transactions(
            user="0xuser",
            chain_id=42161,
            market="0xmarket",
            skip=5,
            limit=20,
        )

    assert data == {"items": []}


@pytest.mark.asyncio
async def test_get_market_transactions_v5_query_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/core/v5/42161/transactions/0xmarket"
        assert request.url.params.get("type") == "TRADES"
        assert request.url.params.get("action") == "LONG_YIELD"
        assert request.url.params.get("minValue") == "123.4"
        assert request.url.params.get("txOrigin") == "0xorigin"
        assert request.url.params.get("resumeToken") == "resume"
        assert request.url.params.get("limit") == "50"
        assert request.url.params.get("skip") == "0"
        return httpx.Response(200, json={"txs": []})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        data = await client.get_market_transactions_v5(
            chain_id=42161,
            address="0xmarket",
            transaction_type=TransactionType.TRADES,
            action=TransactionAction.LONG_YIELD,
            min_value=123.4,
            tx_origin="0xorigin",
            resume_token="resume",
            limit=50,
            skip=0,
        )

    assert data == {"txs": []}


@pytest.mark.asyncio
async def test_get_user_positions_query_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/core/v1/dashboard/positions/database/0xuser"
        assert request.url.params.get("filterUsd") == "123.45"
        return httpx.Response(200, json={"positions": []})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        data = await client.get_user_positions(user="0xuser", filter_usd=123.45)

    assert data == {"positions": []}


@pytest.mark.asyncio
async def test_get_merkle_claimed_rewards_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/core/v1/dashboard/merkle-claimed-rewards/0xuser"
        return httpx.Response(200, json={"rewards": []})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        data = await client.get_merkle_claimed_rewards(user="0xuser")

    assert data == {"rewards": []}


@pytest.mark.asyncio
async def test_get_limit_orders_all_v2_query_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/core/v2/limit-orders"
        assert request.url.params.get("chainId") == "42161"
        assert request.url.params.get("limit") == "50"
        assert request.url.params.get("maker") == "0xmaker"
        assert request.url.params.get("yt") == "0xyt"
        assert request.url.params.get("timestamp_start") == "2026-01-01T00:00:00Z"
        assert request.url.params.get("timestamp_end") == "2026-01-02T00:00:00Z"
        assert request.url.params.get("resumeToken") == "resume"
        return httpx.Response(200, json={"orders": []})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        data = await client.get_limit_orders_all_v2(
            chain_id=42161,
            limit=50,
            maker="0xmaker",
            yt="0xyt",
            timestamp_start="2026-01-01T00:00:00Z",
            timestamp_end="2026-01-02T00:00:00Z",
            resume_token="resume",
        )

    assert data == {"orders": []}


@pytest.mark.asyncio
async def test_get_limit_orders_archived_v2_query_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/core/v2/limit-orders/archived"
        assert request.url.params.get("chainId") == "42161"
        assert request.url.params.get("limit") == "50"
        assert request.url.params.get("maker") == "0xmaker"
        assert request.url.params.get("yt") == "0xyt"
        assert request.url.params.get("timestamp_start") == "2026-01-01T00:00:00Z"
        assert request.url.params.get("timestamp_end") == "2026-01-02T00:00:00Z"
        assert request.url.params.get("resumeToken") == "resume"
        return httpx.Response(200, json={"orders": []})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        data = await client.get_limit_orders_archived_v2(
            chain_id=42161,
            limit=50,
            maker="0xmaker",
            yt="0xyt",
            timestamp_start="2026-01-01T00:00:00Z",
            timestamp_end="2026-01-02T00:00:00Z",
            resume_token="resume",
        )

    assert data == {"orders": []}


@pytest.mark.asyncio
async def test_get_limit_orders_book_v2_query_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/core/v2/limit-orders/book/42161"
        assert request.url.params.get("precisionDecimal") == "6"
        assert request.url.params.get("market") == "0xmarket"
        assert request.url.params.get("limit") == "100"
        assert request.url.params.get("includeAmm") == "true"
        return httpx.Response(200, json={"bids": [], "asks": []})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        data = await client.get_limit_orders_book_v2(
            chain_id=42161,
            precision_decimal=6,
            market="0xmarket",
            limit=100,
            include_amm=True,
        )

    assert data == {"bids": [], "asks": []}


@pytest.mark.asyncio
async def test_get_limit_orders_maker_limit_orders_query_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/core/v1/limit-orders/makers/limit-orders"
        assert request.url.params.get("chainId") == "42161"
        assert request.url.params.get("maker") == "0xmaker"
        assert request.url.params.get("skip") == "10"
        assert request.url.params.get("limit") == "20"
        assert request.url.params.get("yt") == "0xyt"
        assert request.url.params.get("type") == "1"
        assert request.url.params.get("isActive") == "false"
        return httpx.Response(200, json={"orders": []})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        data = await client.get_limit_orders_maker_limit_orders(
            chain_id=42161,
            maker="0xmaker",
            skip=10,
            limit=20,
            yt="0xyt",
            order_type=1,
            is_active=False,
        )

    assert data == {"orders": []}


@pytest.mark.asyncio
async def test_get_limit_orders_taker_limit_orders_query_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/core/v1/limit-orders/takers/limit-orders"
        assert request.url.params.get("chainId") == "42161"
        assert request.url.params.get("yt") == "0xyt"
        assert request.url.params.get("type") == "1"
        assert request.url.params.get("skip") == "0"
        assert request.url.params.get("limit") == "50"
        assert request.url.params.get("sortBy") == "price"
        assert request.url.params.get("sortOrder") == "desc"
        return httpx.Response(200, json={"orders": []})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        data = await client.get_limit_orders_taker_limit_orders(
            chain_id=42161,
            yt="0xyt",
            order_type=1,
            skip=0,
            limit=50,
            sort_by="price",
            sort_order="desc",
        )

    assert data == {"orders": []}


@pytest.mark.asyncio
async def test_get_supported_aggregators_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/core/v1/sdk/42161/supported-aggregators"
        assert dict(request.url.params) == {}
        return httpx.Response(200, json={"aggregators": []})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        data = await client.get_supported_aggregators(chain_id=42161)

    assert data == {"aggregators": []}


@pytest.mark.asyncio
async def test_get_market_tokens_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/core/v1/sdk/42161/markets/0xmarket/tokens"
        assert dict(request.url.params) == {}
        return httpx.Response(200, json={"tokens": []})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        data = await client.get_market_tokens(chain_id=42161, market="0xmarket")

    assert data == {"tokens": []}


@pytest.mark.asyncio
async def test_get_swapping_prices_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/core/v1/sdk/42161/markets/0xmarket/swapping-prices"
        assert dict(request.url.params) == {}
        return httpx.Response(200, json={"prices": {}})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        data = await client.get_swapping_prices(chain_id=42161, market="0xmarket")

    assert data == {"prices": {}}


@pytest.mark.asyncio
async def test_get_pt_cross_chain_metadata_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/core/v1/sdk/42161/cross-chain-pt-metadata/0xpt"
        assert dict(request.url.params) == {}
        return httpx.Response(200, json={"pt": {}})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        data = await client.get_pt_cross_chain_metadata(chain_id=42161, pt="0xpt")

    assert data == {"pt": {}}


@pytest.mark.asyncio
async def test_convert_v2_query_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/core/v2/sdk/42161/convert"
        assert request.url.params.get("slippage") == "0.005"
        assert request.url.params.get("tokensIn") == "0xt1,0xt2"
        assert request.url.params.get("amountsIn") == "1,2"
        assert request.url.params.get("tokensOut") == "0xto"
        assert request.url.params.get("receiver") == "0xreceiver"
        assert request.url.params.get("enableAggregator") == "true"
        assert request.url.params.get("aggregators") == "agg1,agg2"
        assert request.url.params.get("redeemRewards") == "false"
        assert request.url.params.get("needScale") == "true"
        assert request.url.params.get("additionalData") == "0xdeadbeef"
        assert request.url.params.get("useLimitOrder") == "false"
        return httpx.Response(200, json={"tx": {"to": "0x0"}})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        data = await client.convert_v2(
            chain_id=42161,
            slippage=0.005,
            tokens_in=["0xt1", "0xt2"],
            amounts_in=["1", "2"],
            tokens_out=["0xto"],
            receiver="0xreceiver",
            enable_aggregator=True,
            aggregators=["agg1", "agg2"],
            redeem_rewards=False,
            need_scale=True,
            additional_data="0xdeadbeef",
            use_limit_order=False,
        )

    assert data == {"tx": {"to": "0x0"}}


@pytest.mark.asyncio
async def test_convert_v2_redacts_additional_data_in_error_params() -> None:
    additional_data = "0x" + ("a" * 1000)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request")

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core",
        transport=transport,
        max_retries=0,
    ) as client:
        with pytest.raises(PendleApiError) as excinfo:
            await client.convert_v2(
                chain_id=42161,
                slippage=0.005,
                tokens_in=["0xt1"],
                amounts_in=["1"],
                tokens_out=["0xto"],
                additional_data=additional_data,
            )

    err = excinfo.value
    assert err.error_type == "client_error"
    assert err.params is not None
    redacted = err.params.get("additionalData")
    assert redacted is not None
    assert redacted.startswith("0x")
    assert "len=" in redacted
    assert "…" in redacted
    assert additional_data not in str(err)


@pytest.mark.asyncio
async def test_convert_v2_rejects_decimal_amounts_in_before_request() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        with pytest.raises(ValueError) as excinfo:
            await client.convert_v2(
                chain_id=42161,
                slippage=0.005,
                tokens_in=["0xt1"],
                amounts_in=["0.001"],
                tokens_out=["0xto"],
            )

    assert calls == 0
    message = str(excinfo.value)
    assert "amounts_in" in message
    assert "smallest unit" in message


@pytest.mark.asyncio
async def test_get_ve_pendle_data_v2_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/core/v2/ve-pendle/data"
        assert dict(request.url.params) == {}
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        data = await client.get_ve_pendle_data_v2()

    assert data == {"ok": True}


@pytest.mark.asyncio
async def test_get_ve_pendle_market_fees_chart_query_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/core/v1/ve-pendle/market-fees-chart"
        assert request.url.params.get("timestamp_start") == "2026-01-01T00:00:00Z"
        assert request.url.params.get("timestamp_end") == "2026-01-02T00:00:00Z"
        return httpx.Response(200, json={"rows": []})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        data = await client.get_ve_pendle_market_fees_chart(
            timestamp_start="2026-01-01T00:00:00Z",
            timestamp_end="2026-01-02T00:00:00Z",
        )

    assert data == {"rows": []}


@pytest.mark.asyncio
async def test_get_distinct_user_from_token_query_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/core/v1/statistics/get-distinct-user-from-token"
        assert request.url.params.get("token") == "0xtoken"
        assert request.url.params.get("chainId") == "42161"
        return httpx.Response(200, json={"count": 123})

    transport = httpx.MockTransport(handler)
    async with PendleApiClient(
        base_url="https://api-v2.pendle.finance/core", transport=transport
    ) as client:
        data = await client.get_distinct_user_from_token(
            token="0xtoken",
            chain_id=42161,
        )

    assert data == {"count": 123}
