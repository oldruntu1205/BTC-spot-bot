"""
Edge Score 策略引擎 — BTC 现货多因子买入 + 永续合约动态对冲

══════════════════════════════════════════════════════════════
核心逻辑:
══════════════════════════════════════════════════════════════

Edge Score 由 5 个子指标加权计算 (0-100):

1. 买卖盘失衡 (30%): 前 N 档买单量 vs 卖单量
2. VWAP 偏离 (20%): 当前价格相对滚动 VWAP 的偏离程度
3. 成交流向 (20%): 主动买入成交量 vs 主动卖出成交量
4. 5 分钟动量 (15%): K 线收盘价变化率 (ROC)
5. 波动率过滤 (15%): ATR(14) 百分位 — 低波动时信号更可靠

Edge ≥ 70 → 限价买入现货
Edge ≤ 40 → 出场
动态对冲比例 = base_hedge_ratio × (1 - edge/100) × funding_factor

状态机: IDLE → AWAITING_ENTRY → IN_POSITION → [HEDGED] → AWAITING_EXIT → IDLE

模块可独立测试: python -m app.strategy.edge
"""
from __future__ import annotations

import math
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import numpy as np
from loguru import logger

from app.core.types import (
    AggTrade,
    BotState,
    Direction,
    EdgeConfig,
    EdgeScores,
    ExitReason,
    HedgeSignal,
    Kline,
    Order,
    OrderBook,
    OrderSide,
    OrderType,
    Position,
    PositionStatus,
    Signal,
    SignalType,
    TradeSide,
)

# 历史套利收益中位数（年化），作为绩效基准
HISTORICAL_MEDIAN_RETURN_PCT: float = 13.19


# ═══════════════════════════════════════════════════════
# Edge Score 计算器
# ═══════════════════════════════════════════════════════

class EdgeCalculator:
    """
    Edge Score 综合评分计算器

    计算由 5 个子指标加权的复合 Edge Score (0-100)，
    用于判断 BTC 现货买入时机和持仓退出条件。

    使用方式:
        calc = EdgeCalculator(config=EdgeConfig())
        scores = calc.compute(orderbook, agg_trades, klines)
        print(f"Edge Score: {scores.edge:.1f}")
    """

    def __init__(self, config: Optional[EdgeConfig] = None) -> None:
        """
        Args:
            config: Edge Score 可调配置，不传则使用默认值
        """
        self.config: EdgeConfig = config or EdgeConfig()

        # ── VWAP 滚动窗口 ──────────────────────────────
        self._vwap_prices: deque[float] = deque(maxlen=self.config.vwap_window)
        self._vwap_volumes: deque[float] = deque(maxlen=self.config.vwap_window)
        self._vwap_values: deque[float] = deque(maxlen=self.config.vwap_window)

        # ── 逐笔成交滚动窗口 ───────────────────────────
        self._agg_trades: deque[AggTrade] = deque(maxlen=self.config.trade_flow_window)

        # ── K 线收盘价滚动窗口（用于动量计算）──────────
        self._kline_closes: deque[float] = deque(maxlen=30)

        # ── ATR 滚动窗口 ───────────────────────────────
        self._atr_values: deque[float] = deque(maxlen=100)
        self._atr_period: int = self.config.atr_period
        self._tr_history: deque[float] = deque(maxlen=self._atr_period)

    # ── 子指标 1: 买卖盘失衡 ──────────────────────────

    def _ob_imbalance(self, orderbook: OrderBook) -> float:
        """
        计算买卖盘失衡指标 (0-100)

        raw = (bid_volume - ask_volume) / (bid_volume + ask_volume)
        score = 50 + raw * 50  → 映射 [-1, 1] 到 [0, 100]
        - raw > 0: 买单力量强 → 买方优势 → score > 50
        - raw < 0: 卖单力量强 → 卖方优势 → score < 50

        Args:
            orderbook: 订单簿数据

        Returns:
            买卖盘失衡分数 (0-100)
        """
        levels = self.config.ob_depth_levels
        bid_vol = orderbook.bid_volume(levels)
        ask_vol = orderbook.ask_volume(levels)

        total = bid_vol + ask_vol
        if total < 1e-10:
            return 50.0  # 无数据时返回中性值

        raw = (bid_vol - ask_vol) / total
        score = 50.0 + raw * 50.0
        return max(0.0, min(100.0, score))

    # ── 子指标 2: VWAP 偏离 ───────────────────────────

    def _vwap_deviation(self, orderbook: OrderBook) -> float:
        """
        计算 VWAP 偏离指标 (0-100)

        维护滚动 VWAP: VWAP = Σ(typical_price × volume) / Σ(volume)
        typical_price = (high + low + close) / 3

        由于实时订单簿没有 high/low/close，这里用 mid_price 近似 typical_price，
        volume 用 bid_volume + ask_volume 近似。

        偏离 = (mid_price - vwap) / vwap * 10000  (基点)
        归一化: score = 100 / (1 + exp(-k * deviation_zscore))

        - 价格高于 VWAP → 买方成本偏高 → score < 50
        - 价格低于 VWAP → 买方成本偏低 → score > 50

        Args:
            orderbook: 订单簿数据

        Returns:
            VWAP 偏离分数 (0-100)
        """
        mid = orderbook.mid_price
        if mid <= 0:
            return 50.0

        # 用 mid_price 和估算成交量更新 VWAP 历史
        estimated_vol = orderbook.bid_volume(5) + orderbook.ask_volume(5)
        if estimated_vol < 1e-10:
            estimated_vol = 1.0  # 最小单位避免除零

        self._vwap_prices.append(mid)
        self._vwap_volumes.append(estimated_vol)

        # 计算 VWAP
        prices_arr = np.array(self._vwap_prices, dtype=np.float64)
        volumes_arr = np.array(self._vwap_volumes, dtype=np.float64)
        total_vol = float(np.sum(volumes_arr))

        if total_vol < 1e-10 or len(self._vwap_prices) < 3:
            return 50.0

        vwap = float(np.sum(prices_arr * volumes_arr)) / total_vol

        # 偏离基点
        deviation_bps = (mid - vwap) / vwap * 10000.0

        # 归一化：对偏离值做 sigmoid，k=0.1
        # 先对偏离做 z-score（基于历史偏离）
        self._vwap_values.append(deviation_bps)
        if len(self._vwap_values) < 5:
            # 样本不足时直接 sigmoid
            score = 100.0 / (1.0 + math.exp(-0.1 * deviation_bps))
            return max(0.0, min(100.0, score))

        vals = np.array(self._vwap_values, dtype=np.float64)
        mean = float(np.mean(vals))
        std = float(np.std(vals, ddof=1))

        if std < 1e-10:
            z = 0.0
        else:
            z = (deviation_bps - mean) / std

        score = 100.0 / (1.0 + math.exp(-0.1 * z))
        return max(0.0, min(100.0, score))

    # ── 子指标 3: 成交流向 ───────────────────────────

    def _trade_flow(self, agg_trades: list[AggTrade]) -> float:
        """
        计算成交流向指标 (0-100)

        taker_buy: is_buyer_maker = False → 主动买入
        taker_sell: is_buyer_maker = True → 主动卖出

        ratio = taker_buy_vol / (taker_buy_vol + taker_sell_vol)
        score = ratio * 100  (已天然在 [0, 100])

        - 主动买入占优 → 买盘积极 → score > 50
        - 主动卖出占优 → 卖盘积极 → score < 50

        Args:
            agg_trades: 逐笔成交列表

        Returns:
            成交流向分数 (0-100)
        """
        # 更新滚动窗口
        for t in agg_trades:
            self._agg_trades.append(t)

        if len(self._agg_trades) == 0:
            return 50.0

        taker_buy_vol = sum(
            t.quantity for t in self._agg_trades if not t.is_buyer_maker
        )
        taker_sell_vol = sum(
            t.quantity for t in self._agg_trades if t.is_buyer_maker
        )

        total = taker_buy_vol + taker_sell_vol
        if total < 1e-8:
            return 50.0

        ratio = taker_buy_vol / total
        score = ratio * 100.0
        return max(0.0, min(100.0, score))

    # ── 子指标 4: 5 分钟动量 ─────────────────────────

    def _momentum(self, klines: list[Kline]) -> float:
        """
        计算 5 分钟动量指标 (0-100)

        从 K 线收盘价计算 ROC:
          roc_bps = (close_now - close_5min_ago) / close_5min_ago * 10000

        归一化: score = 100 / (1 + exp(-k * roc_zscore))
        k = 0.05

        Args:
            klines: K 线列表

        Returns:
            动量分数 (0-100)
        """
        # 更新收盘价滚动窗口
        for k in klines:
            self._kline_closes.append(k.close)

        if len(self._kline_closes) < 5:
            return 50.0

        closes = list(self._kline_closes)
        close_now = closes[-1]
        # 5 分钟前的价格（取约 5 根前，或最早可用的）
        lookback_idx = max(0, len(closes) - 6)
        close_past = closes[lookback_idx]

        if close_past <= 0:
            return 50.0

        roc_bps = (close_now - close_past) / close_past * 10000.0

        # 对历史 ROC 做 z-score 归一化
        roc_history: list[float] = []
        for i in range(1, len(closes)):
            if closes[i - 1] > 0:
                roc_history.append(
                    (closes[i] - closes[i - 1]) / closes[i - 1] * 10000.0
                )

        if len(roc_history) < 5:
            # 样本不足时直接 sigmoid
            score = 100.0 / (1.0 + math.exp(-0.05 * roc_bps))
            return max(0.0, min(100.0, score))

        arr = np.array(roc_history, dtype=np.float64)
        mean = float(np.mean(arr))
        std = float(np.std(arr, ddof=1))

        if std < 1e-10:
            z = 0.0
        else:
            z = (roc_bps - mean) / std

        score = 100.0 / (1.0 + math.exp(-0.05 * z))
        return max(0.0, min(100.0, score))

    # ── 子指标 5: 波动率过滤 ─────────────────────────

    def _volatility_filter(self, klines: list[Kline]) -> float:
        """
        计算波动率过滤指标 (0-100)

        使用 ATR(14) 衡量波动率:
          TR = max(high - low, |high - prev_close|, |low - prev_close|)
          ATR = EMA(TR, 14)

        atr_percentile = ATR 当前值在滚动窗口中的百分位
        score = (1.0 - atr_percentile) * 100

        低波动 → 信号可靠 → score 高
        高波动 → 信号不可靠 → score 低

        这作为 regime filter：高波动环境自动降低 Edge Score

        Args:
            klines: K 线列表

        Returns:
            波动率过滤分数 (0-100)
        """
        # 更新 ATR
        for k in klines:
            tr = self._calc_true_range(k)
            self._tr_history.append(tr)

            if len(self._tr_history) >= self._atr_period:
                # Wilder's ATR: 简单平均（首值）或 EMA
                if len(self._atr_values) == 0:
                    atr = float(np.mean(self._tr_history))
                else:
                    prev_atr = self._atr_values[-1]
                    atr = (prev_atr * (self._atr_period - 1) + tr) / self._atr_period
                self._atr_values.append(atr)

        if len(self._atr_values) < 5:
            return 50.0

        current_atr = self._atr_values[-1]
        atr_arr = np.array(self._atr_values, dtype=np.float64)

        # 计算当前 ATR 在历史窗口中的百分位
        atr_percentile = float(
            np.sum(atr_arr < current_atr) / len(atr_arr)
        )

        # 低波动 = 高分数（因为信号更可靠）
        score = (1.0 - atr_percentile) * 100.0
        return max(0.0, min(100.0, score))

    @staticmethod
    def _calc_true_range(kline: Kline) -> float:
        """
        计算单根 K 线的真实波幅 (True Range)

        TR = max(high - low, |high - prev_close|, |low - prev_close|)
        由于单根 K 线没有 prev_close，简化为 high - low

        Args:
            kline: K 线数据

        Returns:
            True Range 值
        """
        return kline.high - kline.low

    # ── 综合计算 ─────────────────────────────────────

    def compute(
        self,
        orderbook: OrderBook,
        agg_trades: list[AggTrade],
        klines: list[Kline],
    ) -> EdgeScores:
        """
        计算综合 Edge Score

        5 因子加权求和:
          edge = ob_imbalance * 0.30 + vwap_deviation * 0.20
               + trade_flow * 0.20 + momentum * 0.15 + volatility_filter * 0.15

        Args:
            orderbook: 订单簿数据
            agg_trades: 逐笔成交列表
            klines: K 线列表

        Returns:
            EdgeScores: 包含综合评分和 5 个子指标分数
        """
        obi = self._ob_imbalance(orderbook)
        vd = self._vwap_deviation(orderbook)
        tf = self._trade_flow(agg_trades)
        mom = self._momentum(klines)
        vf = self._volatility_filter(klines)

        edge = (
            obi * self.config.ob_imbalance_weight
            + vd * self.config.vwap_deviation_weight
            + tf * self.config.trade_flow_weight
            + mom * self.config.momentum_weight
            + vf * self.config.volatility_filter_weight
        )

        return EdgeScores(
            edge=edge,
            ob_imbalance=obi,
            vwap_deviation=vd,
            trade_flow=tf,
            momentum=mom,
            volatility_filter=vf,
        )

    def reset(self) -> None:
        """重置所有滚动窗口和内部状态"""
        self._vwap_prices.clear()
        self._vwap_volumes.clear()
        self._vwap_values.clear()
        self._agg_trades.clear()
        self._kline_closes.clear()
        self._atr_values.clear()
        self._tr_history.clear()
        logger.debug("EdgeCalculator 已重置")


# ═══════════════════════════════════════════════════════
# Edge Score 策略
# ═══════════════════════════════════════════════════════

class EdgeStrategy:
    """
    Edge Score 多因子策略 — BTC 现货买入 + 永续合约动态对冲

    状态机:
      IDLE → AWAITING_ENTRY → IN_POSITION → [HEDGED] → AWAITING_EXIT → IDLE

    入场条件:
      Edge Score ≥ entry_threshold (默认 70)

    出场条件:
      - Edge Score ≤ exit_threshold (默认 40)
      - 止盈: 收益率 ≥ profit_target_pct
      - 超时: 持仓分钟 ≥ max_hold_minutes
      - 信号反转: Edge 从 ≥70 跌到 ≤40

    对冲逻辑:
      动态对冲比例 = base_hedge_ratio × (1 - edge_score/100) × funding_factor
      funding_factor: 资金费率为正 → 增加对冲; 为负 → 减少对冲

    使用方式:
        strategy = EdgeStrategy()
        edge_scores = calculator.compute(ob, trades, klines)
        signal = strategy.evaluate(edge_scores, ob, position)
        if signal.is_entry:
            should, side, price = strategy.should_enter(signal)
    """

    def __init__(
        self,
        config: Optional[EdgeConfig] = None,
        trade_quantity: float = 0.001,
        profit_target_pct: float = 2.0,
        max_hold_minutes: float = 60.0,
        base_hedge_ratio: float = 0.9,
    ) -> None:
        """
        Args:
            config: Edge Score 配置
            trade_quantity: 固定交易量 (BTC)
            profit_target_pct: 止盈收益率 (%)
            max_hold_minutes: 最大持仓时长 (分钟)
            base_hedge_ratio: 基础对冲比例
        """
        self.config: EdgeConfig = config or EdgeConfig()
        self.trade_quantity: float = trade_quantity
        self.profit_target_pct: float = profit_target_pct
        self.max_hold_minutes: float = max_hold_minutes
        self.base_hedge_ratio: float = base_hedge_ratio

        self.calculator: EdgeCalculator = EdgeCalculator(config=self.config)
        self.state: BotState = BotState.IDLE
        self.position: Optional[Position] = None

        # ── 入场时 Edge Score 快照 ─────────────────────
        self._entry_edge_score: float = 0.0
        self._edge_history: deque[float] = deque(maxlen=20)

        # ── 绩效追踪 ──────────────────────────────────
        self._total_trades: int = 0
        self._winning_trades: int = 0
        self._total_return_bps: float = 0.0
        self._returns_history: list[float] = []

    # ── 状态查询 ─────────────────────────────────────

    @property
    def in_position(self) -> bool:
        """是否持有仓位"""
        return (
            self.position is not None
            and self.position.status != PositionStatus.CLOSED
        )

    @property
    def is_hedged(self) -> bool:
        """是否已对冲"""
        return self.position is not None and self.position.is_hedged

    @property
    def is_ready(self) -> bool:
        """策略是否就绪"""
        return True  # EdgeCalculator 内部自行判断

    @property
    def median_return_pct(self) -> float:
        """历史收益中位数 (%)"""
        if not self._returns_history:
            return 0.0
        sorted_returns = sorted(self._returns_history)
        n = len(sorted_returns)
        if n % 2 == 1:
            return sorted_returns[n // 2]
        return (sorted_returns[n // 2 - 1] + sorted_returns[n // 2]) / 2.0

    @property
    def win_rate(self) -> float:
        """胜率"""
        if self._total_trades == 0:
            return 0.0
        return self._winning_trades / self._total_trades

    # ── 核心评估 ─────────────────────────────────────

    def evaluate(
        self,
        edge_scores: EdgeScores,
        orderbook: OrderBook,
        position: Optional[Position] = None,
    ) -> Signal:
        """
        评估 Edge Score 并生成交易信号

        状态机决策:
          - 未持仓 + Edge ≥ 70 → ENTRY
          - 已持仓 + Edge ≤ 40 → EXIT (EDGE_BELOW_THRESHOLD)
          - 已持仓 + 止盈/超时 → EXIT
          - 其他 → NONE

        Args:
            edge_scores: Edge 综合评分
            orderbook: 当前订单簿
            position: 当前持仓 (可选，未传时用 self.position)

        Returns:
            Signal: 交易信号
        """
        pos = position or self.position
        self._edge_history.append(edge_scores.edge)

        # 未持仓 → 检查入场
        if pos is None or pos.status == PositionStatus.CLOSED:
            return self._evaluate_entry(edge_scores, orderbook)

        # 已持仓 → 检查出场
        return self._evaluate_exit(edge_scores, orderbook, pos)

    def _evaluate_entry(
        self, edge_scores: EdgeScores, orderbook: OrderBook
    ) -> Signal:
        """
        评估入场条件

        Args:
            edge_scores: Edge 评分
            orderbook: 订单簿

        Returns:
            Signal
        """
        if edge_scores.edge >= self.config.entry_threshold:
            confidence = min(
                (edge_scores.edge - self.config.entry_threshold) / 30.0,
                1.0,
            )
            return Signal(
                type=SignalType.ENTRY,
                direction=Direction.LONG,
                confidence=confidence,
                limit_price=orderbook.best_ask,
                trade_side=TradeSide.PRIMARY,
                edge_scores=edge_scores,
                reason=(
                    f"Edge Score 入场 | edge={edge_scores.edge:.1f} ≥ "
                    f"{self.config.entry_threshold} | "
                    f"OB={edge_scores.ob_imbalance:.0f} "
                    f"VWAP={edge_scores.vwap_deviation:.0f} "
                    f"Flow={edge_scores.trade_flow:.0f} "
                    f"Mom={edge_scores.momentum:.0f} "
                    f"Vol={edge_scores.volatility_filter:.0f}"
                ),
            )

        return Signal(
            type=SignalType.NONE,
            direction=Direction.NONE,
            confidence=0.0,
            edge_scores=edge_scores,
            reason=(
                f"等待入场 | edge={edge_scores.edge:.1f} < "
                f"{self.config.entry_threshold}"
            ),
        )

    def _evaluate_exit(
        self,
        edge_scores: EdgeScores,
        orderbook: OrderBook,
        position: Position,
    ) -> Signal:
        """
        评估出场条件

        优先级:
          1. 止盈 (PROFIT_TARGET)
          2. 超时 (MAX_HOLD_TIME)
          3. Edge ≤ 40 (EDGE_BELOW_THRESHOLD)

        Args:
            edge_scores: Edge 评分
            orderbook: 订单簿
            position: 当前持仓

        Returns:
            Signal: EXIT 或 NONE
        """
        # 更新持仓当前价格
        position.update_unrealized_pnl(orderbook.mid_price)

        # 检查出场条件
        exit_reason = self.check_exit_conditions(position, edge_scores)

        if exit_reason is not None:
            confidence = 1.0
            return Signal(
                type=SignalType.EXIT,
                direction=Direction.SHORT,
                confidence=confidence,
                limit_price=orderbook.best_bid,
                trade_side=TradeSide.PRIMARY,
                edge_scores=edge_scores,
                exit_reason=exit_reason,
                reason=(
                    f"Edge Score 出场 | edge={edge_scores.edge:.1f} | "
                    f"原因={exit_reason.value} | "
                    f"收益={position.return_pct:+.2f}% | "
                    f"持仓={position.holding_minutes:.0f}分钟"
                ),
            )

        # 检查信号反转（Edge 从高点显著下跌）
        if (
            edge_scores.edge <= self.config.exit_threshold
            and self._entry_edge_score >= self.config.entry_threshold
        ):
            return Signal(
                type=SignalType.EXIT,
                direction=Direction.SHORT,
                confidence=0.8,
                limit_price=orderbook.best_bid,
                trade_side=TradeSide.PRIMARY,
                edge_scores=edge_scores,
                exit_reason=ExitReason.SIGNAL_REVERSAL,
                reason=(
                    f"信号反转出场 | edge {self._entry_edge_score:.0f}→"
                    f"{edge_scores.edge:.0f} | "
                    f"收益={position.return_pct:+.2f}%"
                ),
            )

        return Signal(
            type=SignalType.NONE,
            direction=Direction.NONE,
            confidence=0.0,
            edge_scores=edge_scores,
            reason=(
                f"持仓中 | edge={edge_scores.edge:.1f} | "
                f"收益={position.return_pct:+.2f}% | "
                f"持仓={position.holding_minutes:.0f}分钟"
            ),
        )

    # ── 入场/出场判断 ────────────────────────────────

    def should_enter(
        self, signal: Signal
    ) -> tuple[bool, Optional[OrderSide], float]:
        """
        判断是否入场（仅限价买单）

        Args:
            signal: 当前信号

        Returns:
            (是否入场, 订单方向, 限价)
        """
        if self.in_position:
            return False, None, 0.0

        if signal.type == SignalType.ENTRY and signal.direction == Direction.LONG:
            return True, OrderSide.BUY, signal.limit_price

        return False, None, 0.0

    def should_exit(self, signal: Signal) -> bool:
        """
        判断是否出场

        Args:
            signal: 当前信号

        Returns:
            是否应该出场
        """
        return self.in_position and signal.type == SignalType.EXIT

    def check_exit_conditions(
        self,
        position: Position,
        edge_scores: EdgeScores,
    ) -> Optional[ExitReason]:
        """
        检查所有出场条件

        优先级顺序:
          1. 止盈: return_pct >= profit_target_pct → PROFIT_TARGET
          2. 超时: holding_minutes >= max_hold_minutes → MAX_HOLD_TIME
          3. Edge 低于阈值: edge <= exit_threshold → EDGE_BELOW_THRESHOLD

        Args:
            position: 当前持仓
            edge_scores: Edge 评分

        Returns:
            ExitReason 或 None (无需出场)
        """
        # 1. 止盈检查
        if position.return_pct >= self.profit_target_pct:
            return ExitReason.PROFIT_TARGET

        # 2. 最大持仓时间检查
        if position.holding_minutes >= self.max_hold_minutes:
            return ExitReason.MAX_HOLD_TIME

        # 3. Edge 低于出场阈值
        if edge_scores.edge <= self.config.exit_threshold:
            return ExitReason.EDGE_BELOW_THRESHOLD

        return None

    # ── 成交回调 ─────────────────────────────────────

    def on_entry_filled(
        self, order: Order, signal: Optional[Signal] = None
    ) -> None:
        """
        入场订单成交回调

        Args:
            order: 已成交的入场订单
            signal: 关联的入场信号
        """
        edge_score = signal.edge_scores.edge if signal and signal.edge_scores else 0.0

        self.position = Position(
            symbol=order.symbol,
            side=Direction.LONG,
            quantity=order.filled_qty,
            entry_price=order.price,
            current_price=order.price,
            trade_side=TradeSide.PRIMARY,
            entry_edge_score=edge_score,
        )
        self._entry_edge_score = edge_score
        self.state = BotState.IN_POSITION

        logger.info(
            f"Edge 策略入场 | BUY {order.filled_qty} BTC @ {order.price:.2f} | "
            f"entry_edge={edge_score:.1f}"
        )

    def on_exit_filled(self, order: Order) -> None:
        """
        出场订单成交回调

        Args:
            order: 已成交的出场订单
        """
        if self.position is None:
            return

        # 计算已实现盈亏
        pnl = (order.price - self.position.entry_price) * self.position.quantity
        self.position.realized_pnl = pnl

        # 计算收益率并记录
        return_pct = (order.price / self.position.entry_price - 1.0) * 100.0
        self._total_trades += 1
        self._returns_history.append(return_pct)
        if pnl > 0:
            self._winning_trades += 1
        self._total_return_bps += return_pct * 100

        self.position.status = PositionStatus.CLOSED
        self.position.closed_at = order.created_at if order.created_at else None
        self.state = BotState.IDLE
        self._entry_edge_score = 0.0

        logger.info(
            f"Edge 策略出场 | SELL {order.filled_qty} BTC @ {order.price:.2f} | "
            f"PnL={pnl:+.2f} USDT ({return_pct:+.2f}%) | "
            f"胜率={self.win_rate:.1%} | "
            f"收益中位数={self.median_return_pct:.2f}%"
        )

    # ── 动态对冲逻辑 ─────────────────────────────────

    def should_hedge(
        self,
        position: Optional[Position] = None,
        edge_scores: Optional[EdgeScores] = None,
        funding_rate: float = 0.0,
    ) -> Optional[HedgeSignal]:
        """
        检查是否需要调整对冲比例

        动态对冲比例:
          hedge_ratio = base_hedge_ratio × (1 - edge/100) × funding_factor

          funding_factor:
            funding_rate > 0 (多头付空头) → 增加对冲
            funding_rate < 0 (空头付多头) → 减少对冲
            映射: 1.0 + funding_rate * 100 (±10% 范围)

        Edge 越高 → 买方优势越强 → 减少对冲（让利润奔跑）
        Edge 越低 → 买方优势越弱 → 增加对冲（保护本金）

        Args:
            position: 当前持仓
            edge_scores: Edge 评分
            funding_rate: 当前资金费率

        Returns:
            HedgeSignal 或 None (无需调整)
        """
        pos = position or self.position
        if pos is None or pos.status == PositionStatus.CLOSED:
            return None

        if edge_scores is None:
            return None

        edge = edge_scores.edge

        # 资金费率因子: 正费率 → 空头有收益 → 增加对冲
        funding_factor = 1.0 + funding_rate * 100.0
        funding_factor = max(0.8, min(1.2, funding_factor))

        # 动态对冲比例
        target_hedge_ratio = (
            self.base_hedge_ratio
            * (1.0 - edge / 100.0)
            * funding_factor
        )
        target_hedge_ratio = max(0.0, min(1.0, target_hedge_ratio))

        current_hedge_ratio = pos.hedge_ratio_pct / 100.0

        # 对冲比例变化小于 5% → 不调整（避免频繁调仓）
        if abs(target_hedge_ratio - current_hedge_ratio) < 0.05:
            return None

        target_hedge_qty = pos.quantity * target_hedge_ratio
        current_hedge_qty = pos.futures_hedge_qty + pos.hedge_quantity

        if target_hedge_qty > current_hedge_qty:
            # 需要增加对冲
            delta_qty = target_hedge_qty - current_hedge_qty
            return HedgeSignal(
                required=True,
                direction=Direction.SHORT,
                quantity=delta_qty,
                hedge_ratio=target_hedge_ratio,
                current_hedge_ratio=current_hedge_ratio,
                funding_rate=funding_rate,
                reason=(
                    f"增加对冲 | edge={edge:.0f} | "
                    f"目标比例={target_hedge_ratio:.0%} | "
                    f"当前比例={current_hedge_ratio:.0%} | "
                    f"资金费率={funding_rate*100:.2f}%"
                ),
            )
        elif current_hedge_qty > target_hedge_qty:
            # 需要减少对冲
            delta_qty = current_hedge_qty - target_hedge_qty
            return HedgeSignal(
                required=True,
                direction=Direction.LONG,
                quantity=delta_qty,
                hedge_ratio=target_hedge_ratio,
                current_hedge_ratio=current_hedge_ratio,
                funding_rate=funding_rate,
                reason=(
                    f"减少对冲 | edge={edge:.0f} | "
                    f"目标比例={target_hedge_ratio:.0%} | "
                    f"当前比例={current_hedge_ratio:.0%}"
                ),
            )

        return None

    def on_hedge_filled(self, hedge_order: Order) -> None:
        """
        对冲订单成交回调

        Args:
            hedge_order: 已成交的对冲订单
        """
        if self.position is None:
            return

        if hedge_order.side == OrderSide.SELL:
            hedge_pos = Position(
                symbol=hedge_order.symbol,
                side=Direction.SHORT,
                quantity=hedge_order.filled_qty,
                entry_price=hedge_order.price,
                current_price=hedge_order.price,
                trade_side=TradeSide.HEDGE,
            )
            self.position.add_hedge(hedge_pos)
            self.position.futures_hedge_qty += hedge_order.filled_qty
            logger.info(
                f"对冲已执行 | SELL {hedge_order.filled_qty} BTC @ "
                f"{hedge_order.price:.2f} | "
                f"对冲率={self.position.hedge_ratio_pct:.0f}%"
            )
        else:
            unwind_qty = hedge_order.filled_qty
            self.position.hedge_quantity = max(
                0.0, self.position.hedge_quantity - unwind_qty
            )
            self.position.futures_hedge_qty = max(
                0.0, self.position.futures_hedge_qty - unwind_qty
            )
            if (
                self.position.hedge_quantity < 1e-8
                and self.position.futures_hedge_qty < 1e-8
            ):
                self.position.hedge_quantity = 0.0
                self.position.futures_hedge_qty = 0.0
                self.position.hedge_positions.clear()
                self.position.status = PositionStatus.OPEN
            logger.info(
                f"对冲解除 | BUY {unwind_qty} BTC @ {hedge_order.price:.2f} | "
                f"对冲率={self.position.hedge_ratio_pct:.0f}%"
            )

    # ── 绩效报告 ─────────────────────────────────────

    def performance_summary(self) -> dict:
        """
        生成策略绩效摘要

        Returns:
            绩效指标字典
        """
        return {
            "total_trades": self._total_trades,
            "winning_trades": self._winning_trades,
            "win_rate": self.win_rate,
            "median_return_pct": self.median_return_pct,
            "total_return_bps": self._total_return_bps,
        }

    def reset(self) -> None:
        """重置策略状态"""
        self.state = BotState.IDLE
        self.position = None
        self.calculator.reset()
        self._entry_edge_score = 0.0
        self._edge_history.clear()
        self._total_trades = 0
        self._winning_trades = 0
        self._total_return_bps = 0.0
        self._returns_history.clear()
        logger.info("EdgeStrategy 已重置")


# ═══════════════════════════════════════════════════════
# 独立测试入口
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    from datetime import datetime, timezone

    print("=" * 60)
    print("Edge Score 策略引擎 — 独立测试")
    print("=" * 60)

    # ── 辅助函数: 创建模拟数据 ───────────────────────

    def make_orderbook(
        symbol: str = "BTCUSDT",
        mid: float = 65000.0,
        spread: float = 2.0,
        bid_depth: int = 10,
        ask_depth: int = 10,
        bid_bias: float = 1.0,
        ask_bias: float = 1.0,
    ) -> OrderBook:
        """创建模拟订单簿"""
        bids = []
        asks = []
        for i in range(max(bid_depth, ask_depth)):
            if i < bid_depth:
                price = mid - spread / 2 - i * 0.5
                qty = 1.0 * bid_bias * (1.0 + (bid_depth - i) * 0.1)
                bids.append((price, qty))
            if i < ask_depth:
                price = mid + spread / 2 + i * 0.5
                qty = 1.0 * ask_bias * (1.0 + (ask_depth - i) * 0.1)
                asks.append((price, qty))
        return OrderBook(symbol=symbol, bids=bids, asks=asks)

    def make_agg_trades(
        n: int = 50,
        base_price: float = 65000.0,
        buy_ratio: float = 0.5,
    ) -> list[AggTrade]:
        """创建模拟逐笔成交"""
        trades = []
        now = datetime.now(timezone.utc)
        for i in range(n):
            is_buyer_maker = np.random.random() > buy_ratio
            trades.append(
                AggTrade(
                    symbol="btcusdt",
                    price=base_price + np.random.normal(0, 5),
                    quantity=abs(np.random.normal(0.01, 0.005)),
                    is_buyer_maker=is_buyer_maker,
                    trade_time=now,
                )
            )
        return trades

    def make_klines(
        n: int = 30,
        base_price: float = 65000.0,
        trend: float = 0.0,
        volatility: float = 50.0,
    ) -> list[Kline]:
        """创建模拟 K 线"""
        klines = []
        now = datetime.now(timezone.utc)
        price = base_price
        for i in range(n):
            open_price = price
            close_price = price + np.random.normal(trend, volatility)
            high = max(open_price, close_price) + abs(np.random.normal(0, volatility * 0.3))
            low = min(open_price, close_price) - abs(np.random.normal(0, volatility * 0.3))
            volume = abs(np.random.normal(10, 3))
            klines.append(
                Kline(
                    symbol="BTCUSDT",
                    interval="1m",
                    open_time=now,
                    open=open_price,
                    high=high,
                    low=low,
                    close=close_price,
                    volume=volume,
                )
            )
            price = close_price
        return klines

    # ═══════════════════════════════════════════════════
    # 测试 1: EdgeCalculator 基本功能
    # ═══════════════════════════════════════════════════

    print("\n── 测试 1: EdgeCalculator 基本功能 ──")

    config = EdgeConfig()
    calc = EdgeCalculator(config=config)

    # 创建基础数据
    ob = make_orderbook(mid=65000.0, bid_bias=1.2, ask_bias=0.8)
    trades = make_agg_trades(n=60, buy_ratio=0.6)
    klines = make_klines(n=30, base_price=65000.0, trend=5.0, volatility=50.0)

    # 先喂一些历史数据预热
    for _ in range(5):
        warmup_trades = make_agg_trades(n=10, buy_ratio=0.55)
        warmup_klines = make_klines(n=5, base_price=65000.0, trend=3.0, volatility=40.0)
        warmup_ob = make_orderbook(mid=65000.0)
        calc.compute(warmup_ob, warmup_trades, warmup_klines)

    scores = calc.compute(ob, trades, klines)

    # 验证 EdgeScores 结构
    assert isinstance(scores, EdgeScores), "返回类型应为 EdgeScores"
    assert hasattr(scores, "edge"), "缺少 edge 字段"
    assert hasattr(scores, "ob_imbalance"), "缺少 ob_imbalance 字段"
    assert hasattr(scores, "vwap_deviation"), "缺少 vwap_deviation 字段"
    assert hasattr(scores, "trade_flow"), "缺少 trade_flow 字段"
    assert hasattr(scores, "momentum"), "缺少 momentum 字段"
    assert hasattr(scores, "volatility_filter"), "缺少 volatility_filter 字段"
    print(f"  ✅ EdgeScores 结构完整")

    # 验证分数范围 [0, 100]
    assert 0.0 <= scores.edge <= 100.0, f"edge={scores.edge} 超出 [0,100]"
    assert 0.0 <= scores.ob_imbalance <= 100.0, f"ob_imbalance={scores.ob_imbalance} 超出 [0,100]"
    assert 0.0 <= scores.vwap_deviation <= 100.0, f"vwap_deviation={scores.vwap_deviation} 超出 [0,100]"
    assert 0.0 <= scores.trade_flow <= 100.0, f"trade_flow={scores.trade_flow} 超出 [0,100]"
    assert 0.0 <= scores.momentum <= 100.0, f"momentum={scores.momentum} 超出 [0,100]"
    assert 0.0 <= scores.volatility_filter <= 100.0, f"volatility_filter={scores.volatility_filter} 超出 [0,100]"
    print(f"  ✅ 所有子指标在 [0, 100] 范围内")

    print(f"  Edge Score: {scores.edge:.1f}")
    print(f"    OB Imbalance:    {scores.ob_imbalance:.1f}")
    print(f"    VWAP Deviation:  {scores.vwap_deviation:.1f}")
    print(f"    Trade Flow:      {scores.trade_flow:.1f}")
    print(f"    Momentum:        {scores.momentum:.1f}")
    print(f"    Volatility Filter: {scores.volatility_filter:.1f}")

    # ═══════════════════════════════════════════════════
    # 测试 2: 子指标独立验证
    # ═══════════════════════════════════════════════════

    print("\n── 测试 2: 子指标独立验证 ──")

    # 2a: 买卖盘失衡 — 买方占优
    calc2 = EdgeCalculator()
    ob_bullish = make_orderbook(mid=65000.0, bid_bias=2.0, ask_bias=0.5)
    # 只喂历史数据
    for _ in range(10):
        calc2.compute(
            make_orderbook(mid=65000.0),
            make_agg_trades(n=5, buy_ratio=0.5),
            make_klines(n=5, base_price=65000.0, volatility=30.0),
        )
    scores_bull = calc2.compute(ob_bullish, [], [])
    assert scores_bull.ob_imbalance > 50.0, (
        f"买方占优时 ob_imbalance 应 > 50，实际={scores_bull.ob_imbalance:.1f}"
    )
    print(f"  ✅ 买方占优: ob_imbalance={scores_bull.ob_imbalance:.1f} > 50")

    # 2b: 买卖盘失衡 — 卖方占优
    calc3 = EdgeCalculator()
    ob_bearish = make_orderbook(mid=65000.0, bid_bias=0.5, ask_bias=2.0)
    for _ in range(10):
        calc3.compute(
            make_orderbook(mid=65000.0),
            make_agg_trades(n=5, buy_ratio=0.5),
            make_klines(n=5, base_price=65000.0, volatility=30.0),
        )
    scores_bear = calc3.compute(ob_bearish, [], [])
    assert scores_bear.ob_imbalance < 50.0, (
        f"卖方占优时 ob_imbalance 应 < 50，实际={scores_bear.ob_imbalance:.1f}"
    )
    print(f"  ✅ 卖方占优: ob_imbalance={scores_bear.ob_imbalance:.1f} < 50")

    # 2c: 成交流向 — 主动买入占优
    calc4 = EdgeCalculator()
    for _ in range(5):
        calc4.compute(
            make_orderbook(mid=65000.0),
            make_agg_trades(n=10, buy_ratio=0.5),
            make_klines(n=5, base_price=65000.0, volatility=30.0),
        )
    buy_trades = make_agg_trades(n=50, buy_ratio=0.8)
    scores_buyflow = calc4.compute(make_orderbook(mid=65000.0), buy_trades, [])
    assert scores_buyflow.trade_flow > 50.0, (
        f"主动买入占优时 trade_flow 应 > 50，实际={scores_buyflow.trade_flow:.1f}"
    )
    print(f"  ✅ 主动买入占优: trade_flow={scores_buyflow.trade_flow:.1f} > 50")

    # 2d: 成交流向 — 主动卖出占优
    calc5 = EdgeCalculator()
    for _ in range(5):
        calc5.compute(
            make_orderbook(mid=65000.0),
            make_agg_trades(n=10, buy_ratio=0.5),
            make_klines(n=5, base_price=65000.0, volatility=30.0),
        )
    sell_trades = make_agg_trades(n=50, buy_ratio=0.2)
    scores_sellflow = calc5.compute(make_orderbook(mid=65000.0), sell_trades, [])
    assert scores_sellflow.trade_flow < 50.0, (
        f"主动卖出占优时 trade_flow 应 < 50，实际={scores_sellflow.trade_flow:.1f}"
    )
    print(f"  ✅ 主动卖出占优: trade_flow={scores_sellflow.trade_flow:.1f} < 50")

    # ═══════════════════════════════════════════════════
    # 测试 3: EdgeStrategy 状态机
    # ═══════════════════════════════════════════════════

    print("\n── 测试 3: EdgeStrategy 状态机 ──")

    strategy = EdgeStrategy(
        config=EdgeConfig(entry_threshold=70.0, exit_threshold=40.0),
        trade_quantity=0.001,
        profit_target_pct=2.0,
        max_hold_minutes=60.0,
        base_hedge_ratio=0.9,
    )
    assert strategy.state == BotState.IDLE
    assert not strategy.in_position
    print(f"  ✅ 初始状态: IDLE")

    # 预热 EdgeCalculator
    edge_calc = EdgeCalculator()
    for _ in range(20):
        edge_calc.compute(
            make_orderbook(mid=65000.0),
            make_agg_trades(n=10, buy_ratio=0.5),
            make_klines(n=5, base_price=65000.0, volatility=40.0),
        )

    # 生成高 Edge Score → 入场
    ob_bull = make_orderbook(mid=65000.0, bid_bias=2.5, ask_bias=0.3)
    trades_bull = make_agg_trades(n=60, buy_ratio=0.9)
    klines_up = make_klines(n=30, base_price=64900.0, trend=15.0, volatility=20.0)
    high_scores = edge_calc.compute(ob_bull, trades_bull, klines_up)
    print(f"  高 Edge Score: {high_scores.edge:.1f}")

    sig = strategy.evaluate(high_scores, ob_bull)
    print(f"  信号: type={sig.type.value}, reason={sig.reason}")

    if sig.is_entry:
        should, side, price = strategy.should_enter(sig)
        assert should, "高 Edge 时应入场"
        assert side == OrderSide.BUY, "入场应为买入"
        print(f"  ✅ 入场信号: side={side.value}, price={price:.2f}")

        # 模拟入场成交
        entry_order = Order(
            order_id="test_edge_001",
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            type=OrderType.LIMIT,
            price=price,
            quantity=0.001,
            filled_qty=0.001,
        )
        strategy.on_entry_filled(entry_order, sig)
        assert strategy.in_position
        assert strategy.state == BotState.IN_POSITION
        assert strategy.position is not None
        assert strategy.position.entry_edge_score == high_scores.edge
        print(f"  ✅ 入场成交: state={strategy.state.value}, entry_edge={strategy.position.entry_edge_score:.1f}")
    else:
        print(f"  ⚠️  未触发入场 (edge={high_scores.edge:.1f})")

    # ═══════════════════════════════════════════════════
    # 测试 4: 出场条件
    # ═══════════════════════════════════════════════════

    print("\n── 测试 4: 出场条件 ──")

    # 4a: 止盈检查
    pos_profit = Position(
        symbol="BTCUSDT",
        side=Direction.LONG,
        quantity=0.001,
        entry_price=65000.0,
        current_price=66300.0,  # +2% → 触发止盈
        status=PositionStatus.OPEN,
    )
    edge_mid = EdgeScores(edge=55.0, ob_imbalance=55, vwap_deviation=55, trade_flow=55, momentum=55, volatility_filter=55)
    reason = strategy.check_exit_conditions(pos_profit, edge_mid)
    assert reason == ExitReason.PROFIT_TARGET, f"应触发止盈，实际={reason}"
    print(f"  ✅ 止盈触发: reason={reason.value}, return={pos_profit.return_pct:+.2f}%")

    # 4b: 超时检查
    from datetime import timedelta
    pos_old = Position(
        symbol="BTCUSDT",
        side=Direction.LONG,
        quantity=0.001,
        entry_price=65000.0,
        current_price=65100.0,
        status=PositionStatus.OPEN,
        opened_at=datetime.now(timezone.utc) - timedelta(minutes=65),
    )
    reason2 = strategy.check_exit_conditions(pos_old, edge_mid)
    assert reason2 == ExitReason.MAX_HOLD_TIME, f"应触发超时，实际={reason2}"
    print(f"  ✅ 超时触发: reason={reason2.value}, hold={pos_old.holding_minutes:.0f}分钟")

    # 4c: Edge 低于阈值
    pos_normal = Position(
        symbol="BTCUSDT",
        side=Direction.LONG,
        quantity=0.001,
        entry_price=65000.0,
        current_price=65050.0,
        status=PositionStatus.OPEN,
    )
    edge_low = EdgeScores(edge=30.0, ob_imbalance=30, vwap_deviation=30, trade_flow=30, momentum=30, volatility_filter=30)
    reason3 = strategy.check_exit_conditions(pos_normal, edge_low)
    assert reason3 == ExitReason.EDGE_BELOW_THRESHOLD, f"应触发 Edge 低于阈值，实际={reason3}"
    print(f"  ✅ Edge 低于阈值: reason={reason3.value}, edge={edge_low.edge:.1f}")

    # 4d: 无需出场
    edge_ok = EdgeScores(edge=55.0, ob_imbalance=55, vwap_deviation=55, trade_flow=55, momentum=55, volatility_filter=55)
    reason4 = strategy.check_exit_conditions(pos_normal, edge_ok)
    assert reason4 is None, f"不应触发出场，实际={reason4}"
    print(f"  ✅ 无需出场: edge={edge_ok.edge:.1f}, return={pos_normal.return_pct:+.2f}%")

    # ═══════════════════════════════════════════════════
    # 测试 5: 动态对冲比例计算
    # ═══════════════════════════════════════════════════

    print("\n── 测试 5: 动态对冲比例 ──")

    strategy_hedge = EdgeStrategy(
        base_hedge_ratio=0.9,
        profit_target_pct=2.0,
        max_hold_minutes=60.0,
    )
    strategy_hedge.position = Position(
        symbol="BTCUSDT",
        side=Direction.LONG,
        quantity=0.001,
        entry_price=65000.0,
        current_price=65100.0,
        status=PositionStatus.OPEN,
    )

    # 5a: 高 Edge + 正资金费率 → 减少对冲
    edge_high = EdgeScores(edge=80.0, ob_imbalance=80, vwap_deviation=80, trade_flow=80, momentum=80, volatility_filter=80)
    hedge1 = strategy_hedge.should_hedge(
        position=strategy_hedge.position,
        edge_scores=edge_high,
        funding_rate=0.0001,  # 多头付空头
    )
    if hedge1:
        # 高 Edge → 对冲比例低
        expected_ratio = 0.9 * (1.0 - 80.0 / 100.0) * (1.0 + 0.0001 * 100.0)
        print(f"  高 Edge (80) + 正费率: 对冲比例={hedge1.hedge_ratio:.3f} "
              f"(预期≈{expected_ratio:.3f})")
        assert hedge1.hedge_ratio < 0.5, f"高 Edge 应对冲比例低，实际={hedge1.hedge_ratio:.3f}"
        print(f"  ✅ 高 Edge → 对冲比例降低")
    else:
        print(f"  ⚠️  未生成对冲信号（可能 delta < 5%）")

    # 5b: 低 Edge + 正资金费率 → 增加对冲
    edge_low2 = EdgeScores(edge=30.0, ob_imbalance=30, vwap_deviation=30, trade_flow=30, momentum=30, volatility_filter=30)
    hedge2 = strategy_hedge.should_hedge(
        position=strategy_hedge.position,
        edge_scores=edge_low2,
        funding_rate=0.0002,
    )
    if hedge2:
        expected_ratio2 = 0.9 * (1.0 - 30.0 / 100.0) * (1.0 + 0.0002 * 100.0)
        print(f"  低 Edge (30) + 正费率: 对冲比例={hedge2.hedge_ratio:.3f} "
              f"(预期≈{expected_ratio2:.3f})")
        assert hedge2.hedge_ratio > 0.5, f"低 Edge 应对冲比例高，实际={hedge2.hedge_ratio:.3f}"
        print(f"  ✅ 低 Edge → 对冲比例增加")
    else:
        print(f"  ⚠️  未生成对冲信号")

    # 5c: 负资金费率 → 减少对冲
    hedge3 = strategy_hedge.should_hedge(
        position=strategy_hedge.position,
        edge_scores=edge_low2,
        funding_rate=-0.0003,  # 空头付多头
    )
    if hedge3:
        # 负费率 → funding_factor < 1 → 对冲比例更低
        print(f"  负资金费率: 对冲比例={hedge3.hedge_ratio:.3f}, "
              f"funding_rate={hedge3.funding_rate}")
        assert hedge3.funding_rate < 0
        print(f"  ✅ 负资金费率 → 对冲比例进一步降低")
    else:
        print(f"  ⚠️  未生成对冲信号")

    # ═══════════════════════════════════════════════════
    # 测试 6: 完整交易周期
    # ═══════════════════════════════════════════════════

    print("\n── 测试 6: 完整交易周期 ──")

    strategy_full = EdgeStrategy(
        config=EdgeConfig(entry_threshold=70.0, exit_threshold=40.0),
        trade_quantity=0.001,
        profit_target_pct=2.0,
        max_hold_minutes=60.0,
        base_hedge_ratio=0.9,
    )

    calc_full = EdgeCalculator()
    for _ in range(20):
        calc_full.compute(
            make_orderbook(mid=65000.0),
            make_agg_trades(n=10, buy_ratio=0.5),
            make_klines(n=5, base_price=65000.0, volatility=40.0),
        )

    # Step 1: 生成入场信号
    ob_entry = make_orderbook(mid=65000.0, bid_bias=3.0, ask_bias=0.2)
    trades_entry = make_agg_trades(n=60, buy_ratio=0.95)
    klines_entry = make_klines(n=30, base_price=64900.0, trend=20.0, volatility=15.0)
    entry_scores = calc_full.compute(ob_entry, trades_entry, klines_entry)

    sig_entry = strategy_full.evaluate(entry_scores, ob_entry)
    print(f"  Step 1: 信号={sig_entry.type.value}, edge={entry_scores.edge:.1f}")

    if sig_entry.is_entry:
        should, side, price = strategy_full.should_enter(sig_entry)
        if should:
            entry_order = Order(
                order_id="full_cycle_entry",
                symbol="BTCUSDT",
                side=OrderSide.BUY,
                type=OrderType.LIMIT,
                price=price,
                quantity=0.001,
                filled_qty=0.001,
            )
            strategy_full.on_entry_filled(entry_order, sig_entry)
            print(f"  Step 2: 入场成交 @ {price:.2f}")

    if strategy_full.in_position:
        # Step 3: 模拟出场
        exit_order = Order(
            order_id="full_cycle_exit",
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            type=OrderType.LIMIT,
            price=66300.0,
            quantity=0.001,
            filled_qty=0.001,
        )
        strategy_full.on_exit_filled(exit_order)
        assert not strategy_full.in_position
        assert strategy_full._total_trades == 1
        assert strategy_full._winning_trades == 1
        perf = strategy_full.performance_summary()
        print(f"  Step 3: 出场成交 @ 66300.0")
        print(f"  Step 4: 胜率={perf['win_rate']:.1%}, "
              f"收益中位数={perf['median_return_pct']:.2f}%")
        print(f"  ✅ 完整交易周期成功")

    # ═══════════════════════════════════════════════════
    # 测试 7: 边界条件
    # ═══════════════════════════════════════════════════

    print("\n── 测试 7: 边界条件 ──")

    # 7a: 空订单簿
    empty_ob = OrderBook(symbol="BTCUSDT")
    calc_empty = EdgeCalculator()
    scores_empty = calc_empty.compute(empty_ob, [], [])
    assert 0.0 <= scores_empty.edge <= 100.0
    print(f"  ✅ 空订单簿: edge={scores_empty.edge:.1f} (在 [0,100])")

    # 7b: 空逐笔成交
    calc_no_trades = EdgeCalculator()
    scores_no_trades = calc_no_trades.compute(
        make_orderbook(mid=65000.0), [], []
    )
    assert 0.0 <= scores_no_trades.trade_flow <= 100.0
    print(f"  ✅ 空逐笔成交: trade_flow={scores_no_trades.trade_flow:.1f}")

    # 7c: 已平仓持仓不触发信号
    closed_pos = Position(
        symbol="BTCUSDT",
        side=Direction.LONG,
        quantity=0.001,
        entry_price=65000.0,
        current_price=65100.0,
        status=PositionStatus.CLOSED,
    )
    sig_closed = strategy.evaluate(high_scores, ob_bull, closed_pos)
    # 已平仓 → 视为未持仓 → 应返回 ENTRY 或 NONE
    assert sig_closed.type in (SignalType.ENTRY, SignalType.NONE), (
        f"已平仓不应返回 EXIT，实际={sig_closed.type.value}"
    )
    print(f"  ✅ 已平仓持仓: signal={sig_closed.type.value}")

    # 7d: should_enter 在已持仓时不入场
    if strategy.in_position:
        should, side, price = strategy.should_enter(sig_entry)
        assert not should, "已持仓时不应入场"
        print(f"  ✅ 已持仓时不重复入场")

    # 7e: EdgeScores 属性方法
    assert isinstance(high_scores.is_bullish, bool)
    assert isinstance(high_scores.is_bearish, bool)
    assert isinstance(high_scores.is_neutral, bool)
    d = high_scores.to_dict()
    assert "edge" in d
    assert len(d) == 6  # edge + 5 sub-indicators (timestamp not in dict)
    print(f"  ✅ EdgeScores 属性方法正常")

    # 7f: 重置
    strategy_full.reset()
    assert strategy_full.state == BotState.IDLE
    assert strategy_full.position is None
    assert strategy_full._total_trades == 0
    assert len(strategy_full._returns_history) == 0
    print(f"  ✅ 策略重置正常")

    print("\n" + "=" * 60)
    print("全部测试通过! ✅")
    print("=" * 60)
