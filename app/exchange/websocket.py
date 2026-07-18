"""
WebSocket 模块 — 币安实时行情流独立管理

基于 websockets 库，管理以下数据流：
- depth@100ms: 订单簿深度实时更新（100ms 间隔）
- kline_5m: 5分钟K线实时推送

支持自动重连（指数退避）、ping/pong 心跳保持。

模块可独立测试: python -m app.exchange.websocket
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import websockets
from loguru import logger

from app.core.types import AggTrade, OrderBook

# 回调函数类型别名
OrderBookCallback = Callable[[OrderBook], None]
KlineCallback = Callable[[dict[str, Any]], None]
TradeCallback = Callable[[dict[str, Any]], None]
AggTradeCallback = Callable[[AggTrade], None]


class MarketStream:
    """
    币安 WebSocket 行情流管理器

    使用方式:
        stream = MarketStream(symbol="btcusdt", ws_url="wss://testnet.binance.vision/ws")
        stream.on_orderbook(lambda ob: print(ob.spread))
        await stream.start()
        ...
        await stream.stop()

    特性:
        - 自动重连（指数退避，上限 60s）
        - ping/pong 心跳（默认 20s）
        - 回调异常隔离（单个回调异常不影响其他回调）
    """

    def __init__(
        self,
        symbol: str = "btcusdt",
        ws_url: str = "wss://stream.binance.com:9443/ws",
        ping_interval: int = 20,
    ) -> None:
        """
        Args:
            symbol: 交易对（小写，如 "btcusdt"）
            ws_url: WebSocket 基础 URL
            ping_interval: ping 间隔（秒），币安建议 20s
        """
        self.symbol: str = symbol.lower()
        self.ws_url: str = ws_url
        self.ping_interval: int = ping_interval

        # 回调列表
        self._on_orderbook: list[OrderBookCallback] = []
        self._on_kline: list[KlineCallback] = []
        self._on_trade: list[TradeCallback] = []
        self._agg_trade_callbacks: list[AggTradeCallback] = []

        # 运行时状态
        self._last_orderbook: Optional[OrderBook] = None
        self._running: bool = False
        self._tasks: list[asyncio.Task[None]] = []
        self._agg_trade_task: Optional[asyncio.Task[None]] = None

    # ── 回调注册 ────────────────────────────────────

    def on_orderbook(self, callback: OrderBookCallback) -> None:
        """注册订单簿更新回调"""
        self._on_orderbook.append(callback)

    def on_kline(self, callback: KlineCallback) -> None:
        """注册K线更新回调"""
        self._on_kline.append(callback)

    def on_trade(self, callback: TradeCallback) -> None:
        """注册逐笔成交回调"""
        self._on_trade.append(callback)

    def on_agg_trade(self, callback: AggTradeCallback) -> None:
        """注册逐笔成交回调"""
        self._agg_trade_callbacks.append(callback)

    # ── 生命周期 ────────────────────────────────────

    async def start(self) -> None:
        """启动 WebSocket 连接（depth + kline + aggTrade 三流）"""
        self._running = True
        self._tasks = [
            asyncio.create_task(self._depth_stream()),
            asyncio.create_task(self._kline_stream()),
        ]
        self._agg_trade_task = asyncio.create_task(self._run_agg_trade_stream())
        logger.info(f"WebSocket 已启动 | symbol={self.symbol}")

    async def stop(self) -> None:
        """停止所有 WebSocket 连接"""
        self._running = False
        for task in self._tasks:
            task.cancel()
        # 取消 agg_trade 任务
        if self._agg_trade_task is not None:
            self._agg_trade_task.cancel()
        # 等待所有任务取消完成
        all_tasks = list(self._tasks)
        if self._agg_trade_task is not None:
            all_tasks.append(self._agg_trade_task)
        await asyncio.gather(*all_tasks, return_exceptions=True)
        logger.info("WebSocket 已停止")

    # ── Depth 流 ─────────────────────────────────────

    async def _depth_stream(self) -> None:
        """订单簿深度流 (@100ms)"""
        stream_name = f"{self.symbol}@depth@100ms"
        await self._listen(stream_name, self._handle_depth)

    def _handle_depth(self, data: dict[str, Any]) -> None:
        """
        解析 depth 消息并分发给所有注册的回调

        币安 depth 流消息格式:
        {
            "e": "depthUpdate", "E": 123456789,
            "s": "BTCUSDT",
            "b": [["price", "qty"], ...],  // bids
            "a": [["price", "qty"], ...],  // asks
        }
        """
        try:
            orderbook = OrderBook(
                symbol=self.symbol.upper(),
                bids=[(float(b[0]), float(b[1])) for b in data.get("b", [])],
                asks=[(float(a[0]), float(a[1])) for a in data.get("a", [])],
                timestamp=datetime.fromtimestamp(data["E"] / 1000, tz=timezone.utc),
            )
            self._last_orderbook = orderbook

            for callback in self._on_orderbook:
                try:
                    callback(orderbook)
                except Exception as e:
                    logger.error(f"OrderBook 回调异常 [{callback.__name__}]: {e}")

        except Exception as e:
            logger.error(f"Depth 消息解析异常: {e}")

    # ── Kline 流 ─────────────────────────────────────

    async def _kline_stream(self) -> None:
        """5分钟K线流"""
        stream_name = f"{self.symbol}@kline_5m"
        await self._listen(stream_name, self._handle_kline)

    def _handle_kline(self, data: dict[str, Any]) -> None:
        """
        解析 kline 消息并分发给回调

        币安 kline 流消息格式:
        {
            "e": "kline", "E": 123456789,
            "k": {
                "t": 123450000, "T": 123459999,
                "o": "0.0010", "h": "0.0020", "l": "0.0009", "c": "0.0015",
                "v": "100.0", "x": false, ...
            }
        }
        """
        try:
            k = data.get("k", {})
            kline_data: dict[str, Any] = {
                "symbol": self.symbol.upper(),
                "interval": k.get("i", "5m"),
                "open_time": datetime.fromtimestamp(k["t"] / 1000, tz=timezone.utc),
                "close_time": datetime.fromtimestamp(k["T"] / 1000, tz=timezone.utc),
                "open": float(k["o"]),
                "high": float(k["h"]),
                "low": float(k["l"]),
                "close": float(k["c"]),
                "volume": float(k["v"]),
                "is_closed": k.get("x", False),
            }

            for callback in self._on_kline:
                try:
                    callback(kline_data)
                except Exception as e:
                    logger.error(f"Kline 回调异常 [{callback.__name__}]: {e}")

        except Exception as e:
            logger.error(f"Kline 消息解析异常: {e}")

    # ── AggTrade 流 ──────────────────────────────────

    async def _run_agg_trade_stream(self) -> None:
        """订阅 {symbol}@aggTrade 流"""
        stream_name = f"{self.symbol}@aggTrade"
        await self._listen(stream_name, self._handle_agg_trade)

    def _handle_agg_trade(self, data: dict[str, Any]) -> None:
        """
        解析 aggTrade 消息并分发给所有注册的回调

        币安 @aggTrade 流消息格式:
        {
            "e": "aggTrade", "E": 123456789,
            "s": "BTCUSDT",
            "p": "65000.50",    // 成交价格
            "q": "0.001",       // 成交数量
            "m": false,         // is_buyer_maker: true=主动卖出, false=主动买入
            "T": 123456789,     // 成交时间
        }
        """
        try:
            trade = AggTrade(
                symbol=self.symbol.upper(),
                price=float(data["p"]),
                quantity=float(data["q"]),
                is_buyer_maker=data.get("m", False),
                trade_time=datetime.fromtimestamp(data["T"] / 1000, tz=timezone.utc),
                timestamp=datetime.fromtimestamp(data["E"] / 1000, tz=timezone.utc),
            )

            for callback in self._agg_trade_callbacks:
                try:
                    callback(trade)
                except Exception as e:
                    logger.error(f"AggTrade 回调异常 [{callback.__name__}]: {e}")

        except Exception as e:
            logger.error(f"AggTrade 消息解析异常: {e}")

    # ── 连接管理 ────────────────────────────────────

    async def _listen(self, stream: str, handler: Callable[[dict[str, Any]], None]) -> None:
        """
        建立 WebSocket 连接并持续监听，支持自动重连

        重连策略: 指数退避 (1s → 2s → 4s → ... → 60s)

        Args:
            stream: 流名称（如 "btcusdt@depth@100ms"）
            handler: 消息处理函数
        """
        url = f"{self.ws_url}/{stream}"
        retry_delay: int = 1

        while self._running:
            try:
                async with websockets.connect(
                    url,
                    ping_interval=self.ping_interval,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    logger.info(f"WS 已连接: {stream}")
                    retry_delay = 1  # 连接成功，重置重试延迟

                    async for message in ws:
                        if not self._running:
                            break
                        try:
                            data: dict[str, Any] = json.loads(message)
                            handler(data)
                        except json.JSONDecodeError:
                            logger.warning(f"无效 JSON 消息: {str(message)[:200]}")
                        except Exception as e:
                            logger.error(f"WS 消息处理异常: {e}")

            except asyncio.CancelledError:
                break
            except websockets.ConnectionClosed as e:
                logger.warning(f"WS 连接断开 [{stream}]: code={e.code}, {e.reason}")
            except Exception as e:
                logger.error(f"WS 异常 [{stream}]: {e}")

            # 重连等待
            if self._running:
                logger.info(f"WS 重连等待 {retry_delay}s... [{stream}]")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)  # 指数退避，上限 60s

    # ── 属性 ────────────────────────────────────────

    @property
    def latest_orderbook(self) -> Optional[OrderBook]:
        """获取最新订单簿快照"""
        return self._last_orderbook


# ═══════════════════════════════════════════════════════
# 独立测试入口
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    async def _test() -> None:
        print("=" * 50)
        print("WebSocket 模块 — 独立测试")
        print("=" * 50)

        # 测试模块结构
        stream = MarketStream(symbol="btcusdt")
        assert stream.symbol == "btcusdt"

        # 测试回调注册
        received: list[OrderBook] = []

        def on_ob(ob: OrderBook) -> None:
            received.append(ob)

        stream.on_orderbook(on_ob)
        assert len(stream._on_orderbook) == 1
        print("✅ 回调注册正常")

        # 测试 depth 解析（模拟数据）
        mock_depth = {
            "e": "depthUpdate",
            "E": 1700000000000,
            "s": "BTCUSDT",
            "b": [["65000.00", "1.50000000"], ["64999.50", "2.00000000"]],
            "a": [["65001.00", "0.50000000"], ["65002.00", "1.00000000"]],
        }
        stream._handle_depth(mock_depth)

        assert len(received) == 1
        ob = received[0]
        assert ob.symbol == "BTCUSDT"
        assert ob.best_bid == 65000.0
        assert ob.best_ask == 65001.0
        assert abs(ob.spread - 1.0) < 0.01
        print(f"✅ Depth 解析: spread={ob.spread} bps={ob.spread_bps:.2f}")

        # 测试 kline 解析（模拟数据）
        kline_received: list[dict[str, Any]] = []

        def on_kline(kd: dict[str, Any]) -> None:
            kline_received.append(kd)

        stream.on_kline(on_kline)
        mock_kline = {
            "e": "kline", "E": 1700000000000,
            "k": {
                "t": 1700000000000, "T": 1700000299999,
                "s": "BTCUSDT", "i": "5m",
                "o": "65000.00", "h": "65100.00",
                "l": "64900.00", "c": "65050.00",
                "v": "100.5", "x": True,
            },
        }
        stream._handle_kline(mock_kline)

        assert len(kline_received) == 1
        assert kline_received[0]["close"] == 65050.0
        assert kline_received[0]["is_closed"] is True
        print(f"✅ Kline 解析: close={kline_received[0]['close']}")

        # 清理
        stream._running = False

        # 测试 aggTrade 解析（模拟数据）
        agg_received: list[AggTrade] = []

        def on_agg(ag: AggTrade) -> None:
            agg_received.append(ag)

        stream.on_agg_trade(on_agg)
        assert len(stream._agg_trade_callbacks) == 1
        print("✅ AggTrade 回调注册正常")

        mock_agg = {
            "e": "aggTrade",
            "E": 1700000000000,
            "s": "BTCUSDT",
            "p": "65000.50",
            "q": "0.00100000",
            "m": False,
            "T": 1700000000000,
        }
        stream._handle_agg_trade(mock_agg)

        assert len(agg_received) == 1
        at = agg_received[0]
        assert at.symbol == "BTCUSDT"
        assert at.price == 65000.50
        assert at.quantity == 0.001
        assert at.is_buyer_maker is False  # taker buy
        print(f"✅ AggTrade 解析: price={at.price} qty={at.quantity} taker_buy={'是' if not at.is_buyer_maker else '否'}")

        # 测试 is_buyer_maker=True (主动卖出 / taker sell)
        mock_agg_sell = {
            "e": "aggTrade",
            "E": 1700000000000,
            "s": "BTCUSDT",
            "p": "64999.00",
            "q": "0.00200000",
            "m": True,
            "T": 1700000000000,
        }
        stream._handle_agg_trade(mock_agg_sell)
        assert len(agg_received) == 2
        assert agg_received[1].is_buyer_maker is True
        print(f"✅ AggTrade 解析: taker_sell price={agg_received[1].price}")

        print("\n全部测试通过! ✅")
        print("(WebSocket 连接测试需要网络环境，已跳过)")

    asyncio.run(_test())
