"""
回测系统 — 基于历史 K 线数据的 Edge Score 策略回测引擎

══════════════════════════════════════════════════════════════
回测流程:
══════════════════════════════════════════════════════════════
  1. 数据获取 — 从 Binance REST API 拉取历史 5m K 线 (OHLCV)
  2. 数据预处理 — 生成模拟订单簿/AggTrade，构造回测 tick
  3. 逐 tick 回测 — EdgeCalculator 计算评分 → EdgeStrategy 决策
  4. 风控检查 — RiskManager 模拟限价单成交
  5. 绩效报告 — 收益率曲线、夏普比率、最大回撤、胜率等

核心假设 (回测简化):
  - 订单簿由 K 线 OHLC 近似: bid ≈ low, ask ≈ high, 成交量按比例分配
  - AggTrade 由 K 线 volume 近似: taker_buy 比例 = 50% (中性假设)
  - 限价单以 mid_price 成交 (忽略滑点，保守使用 orderbook 中间价)
  - 无手续费模拟 (可配置)

模块可独立测试: python -m app.backtest
"""
from app.backtest.engine import BacktestEngine, BacktestResult, TradeRecord
from app.backtest.data import DataLoader, BacktestTick, KlineData

__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "TradeRecord",
    "DataLoader",
    "BacktestTick",
    "KlineData",
]
