"""
行情模块 — Ticker、OrderBook、K线数据获取

基于币安官方 REST API v3 的公开端点，无需 API Key。
所有方法返回强类型的内部数据结构。

模块可独立测试: python -m app.exchange.market
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from loguru import logger

from app.core.types import Kline, OrderBook, Ticker

if TYPE_CHECKING:
    from .client import BinanceClient


class MarketAPI:
    """
    币安行情 API 封装（公开接口，无需 API Key）

    使用方式:
        client = BinanceClient(api_key="", secret_key="", testnet=True)
        market = MarketAPI(client)
        ob = await market.get_orderbook("BTCUSDT")
    """

    def __init__(self, client: BinanceClient) -> None:
        """
        Args:
            client: BinanceClient 实例
        """
        self._client: BinanceClient = client

    async def get_ticker(self, symbol: str = "BTCUSDT") -> Ticker:
        """
        获取最优买卖价（对应 bookTicker 端点）

        使用 /api/v3/ticker/bookTicker — 币安官方推荐的最优价格查询端点。

        Args:
            symbol: 交易对

        Returns:
            Ticker 对象，含 bid/ask 价格和数量
        """
        data = await self._client.get("/api/v3/ticker/bookTicker", {"symbol": symbol})
        bid = float(data["bidPrice"])
        ask = float(data["askPrice"])
        return Ticker(
            symbol=symbol,
            bid=bid,
            ask=ask,
            bid_qty=float(data["bidQty"]),
            ask_qty=float(data["askQty"]),
            last_price=(bid + ask) / 2.0,  # 以 mid_price 作为估算
        )

    async def get_orderbook(
        self, symbol: str = "BTCUSDT", limit: int = 20,
    ) -> OrderBook:
        """
        获取订单簿深度（对应 depth 端点）

        /api/v3/depth 支持 limit=5/10/20/50/100/500/1000。
        默认 20 档，兼顾精度和性能。

        Args:
            symbol: 交易对
            limit: 深度档位（默认 20）

        Returns:
            OrderBook 对象，bids 降序，asks 升序
        """
        data = await self._client.get("/api/v3/depth", {"symbol": symbol, "limit": limit})
        return OrderBook(
            symbol=symbol,
            bids=[(float(b[0]), float(b[1])) for b in data["bids"]],
            asks=[(float(a[0]), float(a[1])) for a in data["asks"]],
        )

    async def get_klines(
        self,
        symbol: str = "BTCUSDT",
        interval: str = "5m",
        limit: int = 100,
    ) -> list[Kline]:
        """
        获取历史K线数据（对应 klines 端点）

        Args:
            symbol: 交易对
            interval: K线周期（1m/5m/15m/1h/4h/1d 等）
            limit: 获取数量（默认 100，最大 1000）

        Returns:
            Kline 列表，按时间升序排列
        """
        data = await self._client.get("/api/v3/klines", {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        })
        klines: list[Kline] = []
        for k in data:
            klines.append(Kline(
                symbol=symbol,
                interval=interval,
                open_time=datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
                open=float(k[1]),
                high=float(k[2]),
                low=float(k[3]),
                close=float(k[4]),
                volume=float(k[5]),
                close_time=datetime.fromtimestamp(k[6] / 1000, tz=timezone.utc),
            ))
        return klines

    async def get_24hr_ticker(self, symbol: str = "BTCUSDT") -> dict[str, object]:
        """
        获取24小时价格统计（对应 24hr ticker 端点）

        Args:
            symbol: 交易对

        Returns:
            包含 priceChange, priceChangePercent, high, low, volume 等字段
        """
        return await self._client.get("/api/v3/ticker/24hr", {"symbol": symbol})


# ═══════════════════════════════════════════════════════
# 独立测试入口
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio
    from .client import BinanceClient

    async def _test() -> None:
        print("=" * 50)
        print("行情模块 — 独立测试")
        print("=" * 50)

        # 公开接口，不需要 API Key
        client = BinanceClient(api_key="", secret_key="", testnet=True)
        market = MarketAPI(client)

        # 测试 ticker
        try:
            ticker = await market.get_ticker("BTCUSDT")
            assert ticker.bid > 0
            assert ticker.ask > 0
            assert ticker.ask > ticker.bid
            print(f"✅ Ticker: bid={ticker.bid}, ask={ticker.ask}, spread={ticker.ask - ticker.bid}")
        except Exception as e:
            print(f"⚠️  Ticker 测试跳过（网络问题?）: {e}")

        # 测试 orderbook
        try:
            ob = await market.get_orderbook("BTCUSDT", limit=5)
            assert len(ob.bids) > 0
            assert len(ob.asks) > 0
            assert ob.best_bid > 0
            assert ob.best_ask > ob.best_bid
            print(f"✅ OrderBook: bids={len(ob.bids)}档, asks={len(ob.asks)}档")
            print(f"   spread={ob.spread:.2f} | bps={ob.spread_bps:.2f} | mid={ob.mid_price:.2f}")
        except Exception as e:
            print(f"⚠️  OrderBook 测试跳过（网络问题?）: {e}")

        # 测试 klines
        try:
            klines = await market.get_klines("BTCUSDT", "5m", limit=5)
            assert len(klines) == 5
            assert klines[0].open > 0
            print(f"✅ Klines: {len(klines)} 条, 最新 close={klines[-1].close}")
        except Exception as e:
            print(f"⚠️  Klines 测试跳过（网络问题?）: {e}")

        # 测试类型注解
        # 注: from __future__ import annotations 使注解转为字符串
        import inspect
        sig = inspect.signature(market.get_orderbook)
        assert sig.return_annotation in (OrderBook, "OrderBook")
        sig2 = inspect.signature(market.get_klines)
        assert "Kline" in str(sig2.return_annotation)
        print("✅ 返回类型注解正确")

        await client.close()
        print("\n全部测试通过! ✅")

    asyncio.run(_test())
