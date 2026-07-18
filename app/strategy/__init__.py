"""
策略模块 — Edge Score 多因子综合评分 + 永续合约动态对冲

导出 Edge Score 计算引擎和策略状态机。
完全替代旧的 Z-score 价差套利策略。

模块可独立测试:
  python -m app.strategy          # 基础测试
  python -m app.strategy.edge     # Edge Score 完整测试
"""
from .edge import EdgeCalculator, EdgeStrategy, HISTORICAL_MEDIAN_RETURN_PCT

__all__ = [
    "EdgeCalculator",
    "EdgeStrategy",
    "HISTORICAL_MEDIAN_RETURN_PCT",
]
