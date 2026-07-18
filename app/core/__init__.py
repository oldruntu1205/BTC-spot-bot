"""
核心模块导出

策略定位: BTC 现货单向买入套利 + 方向性对冲风控

模块可独立测试:
  python -m app.core.types
  python -m app.core.event_bus
  python -m app.core.config
"""
from .types import (
    AggTrade, BotState, Direction, EdgeScores, Event, EventType, ExitReason,
    FundingRateData, HedgeSignal,
    Kline, MarkPriceData, OpenInterestData, Order, OrderBook, OrderSide, OrderStatus, OrderType,
    Portfolio, Position, PositionStatus, RiskResult,
    Signal, SignalType, SpreadStats, Ticker, TradeSide,
)
from .event_bus import EventBus, event_bus
from .config import AppSettings, EdgeConfig, FuturesConfig, get_config, load_config

__all__ = [
    # 类型
    "AggTrade", "BotState", "Direction", "EdgeScores", "Event", "EventType", "ExitReason",
    "FundingRateData", "HedgeSignal",
    "Kline", "MarkPriceData", "OpenInterestData", "Order", "OrderBook", "OrderSide", "OrderStatus", "OrderType",
    "Portfolio", "Position", "PositionStatus", "RiskResult",
    "Signal", "SignalType", "SpreadStats", "Ticker", "TradeSide",
    # 基础设施
    "EventBus", "event_bus",
    # 配置
    "AppSettings", "EdgeConfig", "FuturesConfig", "get_config", "load_config",
]
