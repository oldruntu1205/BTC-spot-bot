"""
回测数据模块 — 历史数据获取与预处理

职责:
  - 从 Binance 公开 API 拉取历史 5m K 线数据（无需 API Key）
  - 将 K 线数据转换为回测 tick 格式
  - 用 OHLCV 数据近似生成订单簿和 AggTrade

模块可独立测试: python -m app.backtest.data
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import numpy as np
import requests
from loguru import logger

from app.core.types import AggTrade, Kline, OrderBook


# ═══════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════

@dataclass
class KlineData:
    """
    单根 K 线原始数据 — 对应 Binance GET /api/v3/klines 返回
    """
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float           # 成交量 (BTC)
    quote_volume: float     # 成交额 (USDT)
    trades_count: int = 0   # 成交笔数
    taker_buy_volume: float = 0.0      # 主动买入成交量
    taker_buy_quote_volume: float = 0.0  # 主动买入成交额

    def to_kline(self, symbol: str = "BTCUSDT", interval: str = "5m") -> Kline:
        """转换为策略引擎使用的 Kline 对象"""
        return Kline(
            symbol=symbol,
            interval=interval,
            open_time=self.open_time,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            close_time=self.open_time + timedelta(minutes=5),
        )

    def to_orderbook(self, symbol: str = "BTCUSDT") -> OrderBook:
        """
        用 K 线数据近似构造订单簿

        近似逻辑:
          - best_bid ≈ low (买方能拿到的最低价)
          - best_ask ≈ high (卖方能拿到的最高价)
          - bids 模拟: [low, volume*0.5], [low*0.999, volume*0.3], ...
          - asks 模拟: [high, volume*0.5], [high*1.001, volume*0.3], ...
          - 中间价 mid = (open+high+low+close)/4 (典型价格)
        """
        mid = (self.open + self.high + self.low + self.close) / 4.0
        vol = max(self.volume * 0.3, 0.1)  # 单档深度约 30% 成交量

        bids = [
            (self.low, vol),
            (self.low * 0.9995, vol * 0.6),
            (self.low * 0.9990, vol * 0.4),
            (self.low * 0.9985, vol * 0.3),
            (self.low * 0.9980, vol * 0.2),
            (self.low * 0.9975, vol * 0.15),
            (self.low * 0.9970, vol * 0.1),
            (self.low * 0.9965, vol * 0.08),
            (self.low * 0.9960, vol * 0.06),
            (self.low * 0.9955, vol * 0.05),
        ]
        asks = [
            (self.high, vol),
            (self.high * 1.0005, vol * 0.6),
            (self.high * 1.0010, vol * 0.4),
            (self.high * 1.0015, vol * 0.3),
            (self.high * 1.0020, vol * 0.2),
            (self.high * 1.0025, vol * 0.15),
            (self.high * 1.0030, vol * 0.1),
            (self.high * 1.0035, vol * 0.08),
            (self.high * 1.0040, vol * 0.06),
            (self.high * 1.0045, vol * 0.05),
        ]

        return OrderBook(symbol=symbol, bids=bids, asks=asks)

    def to_agg_trades(self, symbol: str = "btcusdt") -> list[AggTrade]:
        """
        用 K 线成交量近似生成逐笔成交

        将 K 线成交量拆分为 N 笔虚拟成交:
          - 主动买入: taker_buy_volume / trades_count
          - 主动卖出: (volume - taker_buy_volume) / trades_count

        如果 trades_count=0，则按 50:50 拆分。
        """
        trades: list[AggTrade] = []
        n = max(self.trades_count, 10)  # 至少拆分 10 笔

        # 主动买入量
        taker_buy_vol = self.taker_buy_volume if self.taker_buy_volume > 0 else self.volume * 0.5
        taker_sell_vol = self.volume - taker_buy_vol

        buy_count = max(1, int(n * taker_buy_vol / self.volume)) if self.volume > 0 else n // 2
        sell_count = n - buy_count

        # 价格在 [low, high] 区间均匀分布
        price_step = (self.high - self.low) / max(n, 1)

        for i in range(buy_count):
            price = self.low + price_step * (i + 0.5)
            qty = taker_buy_vol / buy_count if buy_count > 0 else 0.001
            trades.append(AggTrade(
                symbol=symbol, price=price, quantity=qty,
                is_buyer_maker=False,  # 主动买入
                trade_time=self.open_time,
            ))

        for i in range(sell_count):
            price = self.low + price_step * (buy_count + i + 0.5)
            qty = taker_sell_vol / sell_count if sell_count > 0 else 0.001
            trades.append(AggTrade(
                symbol=symbol, price=price, quantity=qty,
                is_buyer_maker=True,  # 主动卖出
                trade_time=self.open_time,
            ))

        return trades


@dataclass
class BacktestTick:
    """
    回测最小时间单位 — 单根 K 线对应的完整行情快照

    包含策略引擎 compute() 所需的所有数据:
      - orderbook: 模拟订单簿
      - agg_trades: 模拟逐笔成交
      - klines: 当前 K 线列表
    """
    timestamp: datetime
    kline_data: KlineData
    orderbook: OrderBook = field(init=False)
    agg_trades: list[AggTrade] = field(init=False)
    klines: list[Kline] = field(init=False)

    def __post_init__(self) -> None:
        self.orderbook = self.kline_data.to_orderbook()
        self.agg_trades = self.kline_data.to_agg_trades()
        self.klines = [self.kline_data.to_kline()]


# ═══════════════════════════════════════════════════════
# 数据加载器
# ═══════════════════════════════════════════════════════

class DataLoader:
    """
    历史 K 线数据加载器 — 通过 Binance 公开 API 获取数据

    无需 API Key，使用公开的 GET /api/v3/klines 端点。

    使用方式:
        loader = DataLoader(symbol="BTCUSDT", interval="5m")
        klines = loader.fetch(start_date="2026-01-01", end_date="2026-07-01")
        ticks = loader.to_ticks(klines)
    """

    BASE_URL: str = "https://api.binance.com"
    KLINE_ENDPOINT: str = "/api/v3/klines"
    MAX_LIMIT: int = 1000  # 单次请求最多 1000 根 K 线

    def __init__(
        self,
        symbol: str = "BTCUSDT",
        interval: str = "5m",
        cache_dir: Optional[str] = None,
    ) -> None:
        """
        Args:
            symbol: 交易对
            interval: K 线周期 (1m, 5m, 15m, 1h, 4h, 1d, ...)
            cache_dir: 缓存目录（可选），用于避免重复请求
        """
        self.symbol: str = symbol
        self.interval: str = interval
        self.cache_dir: Optional[str] = cache_dir

    def fetch(
        self,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 500,
    ) -> list[KlineData]:
        """
        拉取历史 K 线数据

        Args:
            start_date: 起始日期 (YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS)
            end_date: 结束日期 (YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS)
            limit: K 线数量（最多 1000）

        Returns:
            KlineData 列表（按时间升序）
        """
        params: dict = {
            "symbol": self.symbol,
            "interval": self.interval,
            "limit": min(limit, self.MAX_LIMIT),
        }

        if start_date:
            start_ms = self._parse_date_to_ms(start_date)
            params["startTime"] = start_ms

        if end_date:
            end_ms = self._parse_date_to_ms(end_date)
            params["endTime"] = end_ms

        logger.info(f"拉取 {self.symbol} {self.interval} K线 | {start_date or '最新'} → {end_date or '最新'} | limit={limit}")

        try:
            resp = requests.get(
                f"{self.BASE_URL}{self.KLINE_ENDPOINT}",
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            raw_data = resp.json()
        except requests.RequestException as e:
            logger.error(f"K 线数据请求失败: {e}")
            return []

        klines = [self._parse_kline(row) for row in raw_data]
        logger.info(f"已获取 {len(klines)} 根 K 线 | {klines[0].open_time} → {klines[-1].open_time}")
        return klines

    def fetch_range(
        self,
        start_date: str,
        end_date: str,
    ) -> list[KlineData]:
        """
        拉取指定日期范围的完整 K 线数据（自动分页）

        Args:
            start_date: 起始日期
            end_date: 结束日期

        Returns:
            完整 KlineData 列表
        """
        all_klines: list[KlineData] = []
        current_start = start_date

        while True:
            batch = self.fetch(start_date=current_start, end_date=end_date, limit=1000)
            if not batch:
                break

            all_klines.extend(batch)

            # 如果批次 < 1000，说明已到末尾
            if len(batch) < 1000:
                break

            # 下一页从最后一根 K 线之后开始
            last_time = batch[-1].open_time + timedelta(minutes=5)
            current_start = last_time.strftime("%Y-%m-%d %H:%M:%S")

        # 去重排序
        seen: set[datetime] = set()
        unique: list[KlineData] = []
        for k in sorted(all_klines, key=lambda x: x.open_time):
            if k.open_time not in seen:
                seen.add(k.open_time)
                unique.append(k)

        logger.info(f"完整拉取完成: {len(unique)} 根 K 线")
        return unique

    def to_ticks(self, klines: list[KlineData]) -> list[BacktestTick]:
        """
        将 K 线数据转换为回测 tick 序列

        Args:
            klines: K 线数据列表

        Returns:
            BacktestTick 列表
        """
        return [BacktestTick(timestamp=k.open_time, kline_data=k) for k in klines]

    # ── 私有方法 ─────────────────────────────────────

    @staticmethod
    def _parse_date_to_ms(date_str: str) -> int:
        """将日期字符串转换为毫秒时间戳"""
        # 尝试多种格式
        formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
        ]
        for fmt in formats:
            try:
                dt = datetime.strptime(date_str, fmt)
                return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
            except ValueError:
                continue
        raise ValueError(f"无法解析日期: {date_str}")

    @staticmethod
    def _parse_kline(row: list) -> KlineData:
        """
        解析 Binance K 线 API 返回的单行数据

        API 返回格式:
          [
            0: open_time (ms),
            1: open,
            2: high,
            3: low,
            4: close,
            5: volume,
            6: close_time (ms),
            7: quote_volume,
            8: trades_count,
            9: taker_buy_volume,
            10: taker_buy_quote_volume,
            11: ignore,
          ]
        """
        return KlineData(
            open_time=datetime.fromtimestamp(row[0] / 1000.0, tz=timezone.utc),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            quote_volume=float(row[7]),
            trades_count=int(row[8]),
            taker_buy_volume=float(row[9]),
            taker_buy_quote_volume=float(row[10]),
        )


# ═══════════════════════════════════════════════════════
# 独立测试
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("回测数据模块 — 独立测试")
    print("=" * 60)

    # ── 测试 KlineData 转换 ───────────────────────────
    from datetime import datetime, timezone

    kd = KlineData(
        open_time=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
        open=65000.0, high=65100.0, low=64900.0, close=65050.0,
        volume=10.0, quote_volume=650500.0,
        trades_count=500, taker_buy_volume=6.0, taker_buy_quote_volume=390300.0,
    )

    # Kline 转换
    kline = kd.to_kline()
    assert kline.open == 65000.0
    assert kline.close == 65050.0
    assert kline.volume == 10.0
    print(f"✅ Kline 转换: O={kline.open} H={kline.high} L={kline.low} C={kline.close}")

    # OrderBook 转换
    ob = kd.to_orderbook()
    assert len(ob.bids) == 10
    assert len(ob.asks) == 10
    assert ob.best_bid == kd.low
    assert ob.best_ask == kd.high
    assert ob.mid_price > 0
    print(f"✅ OrderBook 近似: bid={ob.best_bid:.2f} ask={ob.best_ask:.2f} mid={ob.mid_price:.2f}")

    # AggTrade 转换
    trades = kd.to_agg_trades()
    assert len(trades) > 0
    taker_buy_count = sum(1 for t in trades if not t.is_buyer_maker)
    taker_sell_count = sum(1 for t in trades if t.is_buyer_maker)
    print(f"✅ AggTrade 近似: {len(trades)} 笔 | taker_buy={taker_buy_count} taker_sell={taker_sell_count}")

    # ── 测试 BacktestTick ────────────────────────────
    tick = BacktestTick(
        timestamp=kd.open_time,
        kline_data=kd,
    )
    assert tick.orderbook is not None
    assert len(tick.agg_trades) > 0
    assert len(tick.klines) == 1
    print(f"✅ BacktestTick: ob_spread={tick.orderbook.spread:.2f} trades={len(tick.agg_trades)}")

    # ── 测试 DataLoader 日期解析 ─────────────────────
    ms = DataLoader._parse_date_to_ms("2026-07-01")
    assert ms > 0
    ms2 = DataLoader._parse_date_to_ms("2026-07-01 12:00:00")
    assert ms2 > ms
    print(f"✅ 日期解析: 2026-07-01 → {ms}, 2026-07-01 12:00:00 → {ms2}")

    print("\n全部测试通过! ✅")
    print("(DataLoader.fetch 需要网络连接，跳过在线测试)")
