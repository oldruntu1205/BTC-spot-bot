"""
永续合约 WebSocket 流 — 标记价格、资金费率实时推送

订阅:
  - markPrice@1s: 标记价格 + 指数价格
  - 资金费率和 OI 通过 REST 定时查询 (每5分钟)

模块可独立测试: python -m app.exchange.futures_websocket
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import websockets
from loguru import logger

from app.core.types import MarkPriceData

# 回调类型别名
MarkPriceCallback = Callable[[MarkPriceData], None]


class FuturesMarketStream:
    """
    永续合约 WebSocket 行情流

    使用方式:
        stream = FuturesMarketStream(symbol="btcusdt")
        stream.on_mark_price(lambda mp: print(mp.mark_price))
        await stream.start()
        ...
        await stream.stop()
    """

    def __init__(
        self,
        symbol: str = "btcusdt",
        ws_url: str = "wss://fstream.binance.com/ws",
        ping_interval: int = 20,
    ) -> None:
        """
        Args:
            symbol: 交易对 (小写)
            ws_url: WebSocket 基础 URL
            ping_interval: ping 间隔 (秒)
        """
        self.symbol: str = symbol.lower()
        self.ws_url: str = ws_url
        self.ping_interval: int = ping_interval

        self._on_mark_price: list[MarkPriceCallback] = []
        self._running: bool = False
        self._tasks: list[asyncio.Task[None]] = []

    # ── 回调注册 ────────────────────────────────────

    def on_mark_price(self, callback: MarkPriceCallback) -> None:
        """注册标记价格回调"""
        self._on_mark_price.append(callback)

    # ── 生命周期 ────────────────────────────────────

    async def start(self) -> None:
        """启动 WebSocket 连接"""
        self._running = True
        self._tasks = [
            asyncio.create_task(self._mark_price_stream()),
        ]
        logger.info(f"永续合约 WS 已启动 | symbol={self.symbol}")

    async def stop(self) -> None:
        """停止所有连接"""
        self._running = False
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("永续合约 WS 已停止")

    # ── Mark Price 流 ────────────────────────────────

    async def _mark_price_stream(self) -> None:
        """标记价格流 (@1s)"""
        stream_name = f"{self.symbol}@markPrice@1s"
        await self._listen(stream_name, self._handle_mark_price)

    def _handle_mark_price(self, data: dict[str, Any]) -> None:
        """
        解析 markPrice 消息

        币安 @markPrice 流格式:
        {
            "e": "markPriceUpdate", "E": 123456789,
            "s": "BTCUSDT",
            "p": "65000.00",        // 标记价格
            "i": "64999.50",        // 指数价格
            "P": "65050.00",        // 预估结算价
            "r": "0.00010000",      // 资金费率
            "T": 123456789000,      // 下次资金费时间
        }
        """
        try:
            mp = MarkPriceData(
                symbol=self.symbol.upper(),
                mark_price=float(data["p"]),
                index_price=float(data["i"]),
                estimated_settle_price=float(data.get("P", 0)),
                funding_rate=float(data.get("r", 0)),
                next_funding_time=datetime.fromtimestamp(
                    data["T"] / 1000, tz=timezone.utc,
                ) if data.get("T") else None,
                timestamp=datetime.fromtimestamp(data["E"] / 1000, tz=timezone.utc),
            )

            for callback in self._on_mark_price:
                try:
                    callback(mp)
                except Exception as e:
                    logger.error(f"MarkPrice 回调异常 [{callback.__name__}]: {e}")

        except Exception as e:
            logger.error(f"MarkPrice 消息解析异常: {e}")

    # ── 连接管理 ────────────────────────────────────

    async def _listen(
        self,
        stream: str,
        handler: Callable[[dict[str, Any]], None],
    ) -> None:
        """
        建立 WebSocket 连接并持续监听

        Args:
            stream: 流名称
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
                    logger.info(f"Futures WS 已连接: {stream}")
                    retry_delay = 1

                    async for message in ws:
                        if not self._running:
                            break
                        try:
                            data: dict[str, Any] = json.loads(message)
                            handler(data)
                        except json.JSONDecodeError:
                            logger.warning(f"无效 JSON: {str(message)[:200]}")
                        except Exception as e:
                            logger.error(f"WS 消息处理异常: {e}")

            except asyncio.CancelledError:
                break
            except websockets.ConnectionClosed as e:
                logger.warning(f"Futures WS 断开 [{stream}]: code={e.code}")
            except Exception as e:
                logger.error(f"Futures WS 异常 [{stream}]: {e}")

            if self._running:
                logger.info(f"Futures WS 重连等待 {retry_delay}s...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)


# ═══════════════════════════════════════════════════════
# 独立测试入口
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    async def _test() -> None:
        print("=" * 50)
        print("永续合约 WebSocket — 独立测试")
        print("=" * 50)

        stream = FuturesMarketStream(symbol="btcusdt")
        assert stream.symbol == "btcusdt"

        # 测试回调注册
        received: list[MarkPriceData] = []

        def on_mp(mp: MarkPriceData) -> None:
            received.append(mp)

        stream.on_mark_price(on_mp)
        assert len(stream._on_mark_price) == 1
        print("✅ 回调注册正常")

        # 测试 markPrice 解析
        mock_data = {
            "e": "markPriceUpdate",
            "E": 1700000000000,
            "s": "BTCUSDT",
            "p": "65000.00",
            "i": "64999.50",
            "P": "65050.00",
            "r": "0.00010000",
            "T": 1700000300000,
        }
        stream._handle_mark_price(mock_data)

        assert len(received) == 1
        mp = received[0]
        assert mp.symbol == "BTCUSDT"
        assert mp.mark_price == 65000.00
        assert mp.index_price == 64999.50
        assert mp.funding_rate == 0.0001
        print(f"✅ MarkPrice 解析: mark={mp.mark_price} index={mp.index_price} rate={mp.funding_rate*100:.2f}%")

        # 清理
        stream._running = False
        print("\n全部测试通过! ✅")
        print("(WebSocket 连接测试需要网络环境，已跳过)")

    asyncio.run(_test())
