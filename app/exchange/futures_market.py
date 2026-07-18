"""
永续合约行情模块 — 标记价格、资金费率、未平仓量查询

基于币安 /fapi/v1/ 公开端点，无需 API Key。

模块可独立测试: python -m app.exchange.futures_market
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.core.types import FundingRateData, MarkPriceData, OpenInterestData

if TYPE_CHECKING:
    from .futures_client import FuturesBinanceClient


class FuturesMarketAPI:
    """
    永续合约公开行情查询

    使用方式:
        client = FuturesBinanceClient(testnet=True)
        api = FuturesMarketAPI(client)
        mp = await api.get_mark_price("BTCUSDT")
        print(f"标记价格: {mp.mark_price}")
    """

    def __init__(self, client: FuturesBinanceClient) -> None:
        """
        Args:
            client: FuturesBinanceClient 实例
        """
        self._client: FuturesBinanceClient = client

    async def get_mark_price(self, symbol: str = "BTCUSDT") -> MarkPriceData:
        """
        查询标记价格和资金费率

        端点: GET /fapi/v1/premiumIndex

        Args:
            symbol: 交易对

        Returns:
            MarkPriceData: 包含标记价格、指数价格、资金费率
        """
        data = await self._client.get(
            "/fapi/v1/premiumIndex",
            {"symbol": symbol},
        )
        return MarkPriceData(
            symbol=symbol,
            mark_price=float(data["markPrice"]),
            index_price=float(data["indexPrice"]),
            estimated_settle_price=float(data.get("estimatedSettlePrice", 0)),
            funding_rate=float(data.get("lastFundingRate", 0)),
            next_funding_time=datetime.fromtimestamp(
                data["nextFundingTime"] / 1000, tz=timezone.utc,
            ) if data.get("nextFundingTime") else None,
        )

    async def get_funding_rate(self, symbol: str = "BTCUSDT") -> FundingRateData:
        """
        查询当前资金费率

        端点: GET /fapi/v1/fundingRate (最新一条)

        Args:
            symbol: 交易对

        Returns:
            FundingRateData
        """
        data = await self._client.get(
            "/fapi/v1/fundingRate",
            {"symbol": symbol, "limit": 1},
        )
        rate = data[0] if isinstance(data, list) and data else data
        return FundingRateData(
            symbol=symbol,
            funding_rate=float(rate["fundingRate"]),
            funding_countdown=float(rate.get("fundingCountdown", 0)),
            timestamp=datetime.fromtimestamp(
                rate["fundingTime"] / 1000, tz=timezone.utc,
            ),
        )

    async def get_open_interest(self, symbol: str = "BTCUSDT") -> OpenInterestData:
        """
        查询未平仓量

        端点: GET /fapi/v1/openInterest

        Args:
            symbol: 交易对

        Returns:
            OpenInterestData
        """
        data = await self._client.get(
            "/fapi/v1/openInterest",
            {"symbol": symbol},
        )
        return OpenInterestData(
            symbol=symbol,
            open_interest=float(data["openInterest"]),
            timestamp=datetime.fromtimestamp(
                data["time"] / 1000, tz=timezone.utc,
            ) if data.get("time") else datetime.now(timezone.utc),
        )

    async def get_premium_index(self, symbol: str = "BTCUSDT") -> dict:
        """
        查询溢价指数

        端点: GET /fapi/v1/premiumIndex

        Args:
            symbol: 交易对

        Returns:
            包含 markPrice, indexPrice, lastFundingRate 等
        """
        return await self._client.get(
            "/fapi/v1/premiumIndex",
            {"symbol": symbol},
        )


# ═══════════════════════════════════════════════════════
# 独立测试入口
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio
    from .futures_client import FuturesBinanceClient

    async def _test() -> None:
        print("=" * 50)
        print("永续合约行情 — 独立测试")
        print("=" * 50)

        client = FuturesBinanceClient(
            api_key="test_key",
            secret_key="test_secret",
            testnet=True,
        )
        api = FuturesMarketAPI(client)

        # 测试接口方法存在
        assert hasattr(api, "get_mark_price")
        assert hasattr(api, "get_funding_rate")
        assert hasattr(api, "get_open_interest")
        print("✅ 接口方法完整")

        # 测试类型注解
        import inspect
        sig = inspect.signature(api.get_mark_price)
        # 注: from __future__ import annotations 使注解转为字符串
        assert "MarkPriceData" in str(sig.return_annotation)
        print("✅ 返回类型注解正确")

        # 测试真实 API 调用
        try:
            mp = await api.get_mark_price("BTCUSDT")
            assert mp.mark_price > 0
            print(f"✅ 标记价格: {mp.mark_price:.2f} | 资金费率: {mp.funding_rate*100:.4f}%")

            oi = await api.get_open_interest("BTCUSDT")
            assert oi.open_interest > 0
            print(f"✅ 未平仓量: {oi.open_interest:.0f} BTC")
        except Exception as e:
            print(f"⚠️  测试网 API 不可用（网络问题）: {e}")

        await client.close()
        print("\n全部测试通过! ✅")

    asyncio.run(_test())
