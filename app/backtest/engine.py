"""
回测引擎 — Edge Score 策略历史回测核心

职责:
  - 逐 tick 遍历历史数据
  - 调用 EdgeCalculator 计算评分 → EdgeStrategy 生成信号
  - 模拟限价单成交（以 tick mid_price 为成交价）
  - 风控检查（复用 RiskManager）
  - 记录每笔交易和权益曲线

模拟假设:
  - 限价单以订单簿中间价成交（保守假设，实际可能更优）
  - 无滑点模拟（历史回测滑点不可知）
  - 手续费: maker 0.02%, taker 0.04%（可配置）
  - 单 tick 只允许一个持仓（不允许加仓）

模块可独立测试: python -m app.backtest.engine
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np
from loguru import logger

from app.core.config import EdgeConfig, RiskConfig, get_config
from app.core.types import (
    Direction, EdgeScores, ExitReason, OrderSide,
    Position, PositionStatus, SignalType, TradeSide,
)
from app.strategy.edge import EdgeCalculator, EdgeStrategy
from app.risk import RiskManager
from app.backtest.data import BacktestTick, KlineData


# ═══════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════

@dataclass
class TradeRecord:
    """单笔交易记录"""
    entry_time: datetime
    exit_time: Optional[datetime] = None
    entry_price: float = 0.0
    exit_price: float = 0.0
    quantity: float = 0.0
    entry_edge_score: float = 0.0
    exit_edge_score: float = 0.0
    exit_reason: str = ""
    return_pct: float = 0.0
    pnl: float = 0.0
    holding_minutes: float = 0.0
    is_win: bool = False

    def close(
        self,
        exit_time: datetime,
        exit_price: float,
        exit_edge_score: float,
        exit_reason: str,
        fee_rate: float = 0.0004,  # taker 0.04%
    ) -> None:
        """
        平仓并计算盈亏

        Args:
            exit_time: 出场时间
            exit_price: 出场价格
            exit_edge_score: 出场时 Edge Score
            exit_reason: 出场原因
            fee_rate: 手续费率
        """
        self.exit_time = exit_time
        self.exit_price = exit_price
        self.exit_edge_score = exit_edge_score
        self.exit_reason = exit_reason

        # 收益率（含手续费）
        gross_return = (exit_price / self.entry_price - 1.0)
        fee_cost = fee_rate * 2  # 入场+出场手续费
        self.return_pct = (gross_return - fee_cost) * 100.0

        self.pnl = self.quantity * self.entry_price * (gross_return - fee_cost)
        self.holding_minutes = (exit_time - self.entry_time).total_seconds() / 60.0
        self.is_win = self.pnl > 0


@dataclass
class BacktestResult:
    """回测结果 — 完整的绩效报告"""
    symbol: str
    interval: str
    start_date: datetime
    end_date: datetime

    # ── 交易统计 ─────────────────────────────────────
    total_ticks: int = 0
    total_signals: int = 0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0

    # ── 收益统计 ─────────────────────────────────────
    total_return_pct: float = 0.0
    annualized_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    max_drawdown_duration_days: float = 0.0

    # ── 风险调整指标 ─────────────────────────────────
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    volatility_pct: float = 0.0

    # ── 交易质量 ─────────────────────────────────────
    win_rate: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_factor: float = 0.0        # 总盈利 / 总亏损
    avg_holding_minutes: float = 0.0
    max_holding_minutes: float = 0.0

    # ── 按出场原因的分布 ────────────────────────────
    exit_reason_dist: dict[str, int] = field(default_factory=dict)

    # ── 原始数据 ─────────────────────────────────────
    trades: list[TradeRecord] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    equity_timestamps: list[datetime] = field(default_factory=list)

    # ── 基准对比 ─────────────────────────────────────
    benchmark_return_pct: float = 0.0    # 买入持有收益
    alpha_pct: float = 0.0               # 超额收益

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def avg_trade_pnl(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.total_pnl / self.total_trades

    def summary(self) -> str:
        """生成文本摘要"""
        lines = [
            "=" * 60,
            f"  回测报告 — {self.symbol} {self.interval}",
            f"  时间范围: {self.start_date.strftime('%Y-%m-%d')} → {self.end_date.strftime('%Y-%m-%d')}",
            "=" * 60,
            "",
            "── 交易统计 ──",
            f"  总信号数:     {self.total_signals}",
            f"  总交易数:     {self.total_trades}",
            f"  盈利交易:     {self.winning_trades}",
            f"  亏损交易:     {self.losing_trades}",
            f"  胜率:         {self.win_rate:.1%}",
            "",
            "── 收益指标 ──",
            f"  总收益率:     {self.total_return_pct:+.2f}%",
            f"  年化收益率:   {self.annualized_return_pct:+.2f}%",
            f"  最大回撤:     {self.max_drawdown_pct:.2f}%",
            f"  基准收益:     {self.benchmark_return_pct:+.2f}% (买入持有)",
            f"  超额收益:     {self.alpha_pct:+.2f}%",
            "",
            "── 风险指标 ──",
            f"  夏普比率:     {self.sharpe_ratio:.2f}",
            f"  索提诺比率:   {self.sortino_ratio:.2f}",
            f"  卡玛比率:     {self.calmar_ratio:.2f}",
            f"  年化波动率:   {self.volatility_pct:.2f}%",
            "",
            "── 交易质量 ──",
            f"  平均盈利:     {self.avg_win_pct:+.3f}%",
            f"  平均亏损:     {self.avg_loss_pct:+.3f}%",
            f"  盈亏比:       {self.profit_factor:.2f}",
            f"  平均持仓:     {self.avg_holding_minutes:.1f} 分钟",
            f"  最大持仓:     {self.max_holding_minutes:.1f} 分钟",
            "",
            "── 出场原因分布 ──",
        ]
        for reason, count in sorted(self.exit_reason_dist.items(), key=lambda x: -x[1]):
            pct = count / max(self.total_trades, 1) * 100
            lines.append(f"  {reason}: {count} 次 ({pct:.1f}%)")

        lines.append("")
        lines.append("=" * 60)
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════
# 回测引擎
# ═══════════════════════════════════════════════════════

class BacktestEngine:
    """
    Edge Score 策略回测引擎

    使用方式:
        engine = BacktestEngine(initial_capital=10000.0)
        result = engine.run(ticks)
        print(result.summary())
    """

    # 手续费率
    MAKER_FEE: float = 0.0002   # 0.02%
    TAKER_FEE: float = 0.0004   # 0.04%

    def __init__(
        self,
        initial_capital: float = 10000.0,
        trade_quantity: float = 0.001,
        edge_config: Optional[EdgeConfig] = None,
        risk_config: Optional[RiskConfig] = None,
        symbol: str = "BTCUSDT",
        interval: str = "5m",
        fee_rate: Optional[float] = None,
    ) -> None:
        """
        Args:
            initial_capital: 初始资金 (USDT)
            trade_quantity: 每笔交易量 (BTC)
            edge_config: Edge Score 配置
            risk_config: 风控配置
            symbol: 交易对
            interval: K 线周期
            fee_rate: 手续费率 (默认 TAKER_FEE)
        """
        self.initial_capital: float = initial_capital
        self.trade_quantity: float = trade_quantity
        self.symbol: str = symbol
        self.interval: str = interval
        self.fee_rate: float = fee_rate if fee_rate is not None else self.TAKER_FEE

        # ── 策略组件 ──────────────────────────────────
        self.edge_config: EdgeConfig = edge_config or EdgeConfig()
        self.risk_config: RiskConfig = risk_config or RiskConfig()

        self.calculator: EdgeCalculator = EdgeCalculator(config=self.edge_config)
        self.strategy: EdgeStrategy = EdgeStrategy(
            config=self.edge_config,
            trade_quantity=self.trade_quantity,
            profit_target_pct=self.risk_config.profit_target_pct,
            max_hold_minutes=self.risk_config.max_hold_minutes,
            base_hedge_ratio=0.9,
        )
        self.risk: RiskManager = RiskManager(
            max_position_size=self.risk_config.max_position_size,
            daily_loss_limit=self.risk_config.daily_loss_limit,
            single_trade_risk_pct=self.risk_config.single_trade_risk_pct,
            max_position_pct=self.risk_config.max_position_pct,
            daily_loss_pct=self.risk_config.daily_loss_pct,
            consecutive_loss_limit=self.risk_config.consecutive_loss_limit,
            pause_minutes=self.risk_config.pause_minutes,
        )

        # ── 运行时状态 ────────────────────────────────
        self._equity: float = initial_capital
        self._equity_curve: list[float] = []
        self._equity_timestamps: list[datetime] = []
        self._trades: list[TradeRecord] = []
        self._current_trade: Optional[TradeRecord] = None
        self._peak_equity: float = initial_capital
        self._max_drawdown: float = 0.0
        self._drawdown_start: Optional[datetime] = None
        self._max_drawdown_duration: float = 0.0

        # 统计
        self._total_signals: int = 0
        self._exit_reason_dist: dict[str, int] = {}

    # ═══════════════════════════════════════════════════
    # 主回测循环
    # ═══════════════════════════════════════════════════

    def run(self, ticks: list[BacktestTick]) -> BacktestResult:
        """
        执行完整回测

        Args:
            ticks: 回测 tick 序列（按时间升序）

        Returns:
            BacktestResult: 完整回测报告
        """
        if not ticks:
            logger.warning("无回测数据")
            return self._empty_result()

        logger.info(f"开始回测 | {len(ticks)} ticks | 初始资金={self.initial_capital:.0f} USDT")

        # 重置状态
        self._reset()

        # 预热期 — 用前 20 个 tick 初始化滚动窗口
        warmup = min(30, len(ticks) // 4)
        for i in range(warmup):
            tick = ticks[i]
            self._compute_and_record(tick, is_warmup=True)

        # 正式回测
        for i in range(warmup, len(ticks)):
            tick = ticks[i]
            self._process_tick(tick, i)

        # 强制平仓（如果回测结束时还有持仓）
        if self._current_trade is not None:
            last_tick = ticks[-1]
            self._close_trade(
                last_tick,
                ExitReason.MAX_HOLD_TIME.value,
                force=True,
            )

        # 计算最终结果
        result = self._build_result(ticks)
        logger.info(f"回测完成 | 交易={result.total_trades} | 收益={result.total_return_pct:+.2f}% | 胜率={result.win_rate:.1%}")
        return result

    # ═══════════════════════════════════════════════════
    # 内部方法
    # ═══════════════════════════════════════════════════

    def _reset(self) -> None:
        """重置回测状态"""
        self.calculator.reset()
        self.strategy = EdgeStrategy(
            config=self.edge_config,
            trade_quantity=self.trade_quantity,
            profit_target_pct=self.risk_config.profit_target_pct,
            max_hold_minutes=self.risk_config.max_hold_minutes,
            base_hedge_ratio=0.9,
        )
        self.risk = RiskManager(
            max_position_size=self.risk_config.max_position_size,
            daily_loss_limit=self.risk_config.daily_loss_limit,
            single_trade_risk_pct=self.risk_config.single_trade_risk_pct,
            max_position_pct=self.risk_config.max_position_pct,
            daily_loss_pct=self.risk_config.daily_loss_pct,
            consecutive_loss_limit=self.risk_config.consecutive_loss_limit,
            pause_minutes=self.risk_config.pause_minutes,
        )
        self._equity = self.initial_capital
        self._equity_curve = [self.initial_capital]
        self._equity_timestamps = []
        self._trades = []
        self._current_trade = None
        self._peak_equity = self.initial_capital
        self._max_drawdown = 0.0
        self._drawdown_start = None
        self._max_drawdown_duration = 0.0
        self._total_signals = 0
        self._exit_reason_dist = {}
        self.risk.update_equity(self.initial_capital)

    def _compute_and_record(self, tick: BacktestTick, is_warmup: bool = False) -> EdgeScores:
        """
        计算 Edge Score 并记录权益曲线

        Args:
            tick: 回测 tick
            is_warmup: 是否预热阶段

        Returns:
            EdgeScores
        """
        scores = self.calculator.compute(
            orderbook=tick.orderbook,
            agg_trades=tick.agg_trades,
            klines=tick.klines,
        )

        if not is_warmup:
            self._equity_curve.append(self._equity)
            self._equity_timestamps.append(tick.timestamp)

        return scores

    def _process_tick(self, tick: BacktestTick, idx: int) -> None:
        """
        处理单个回测 tick

        Args:
            tick: 回测 tick
            idx: tick 索引
        """
        # 计算 Edge Score
        scores = self._compute_and_record(tick)
        mid_price = tick.orderbook.mid_price

        # 更新权益（已平仓盈亏 + 当前持仓浮盈）
        closed_pnl = sum(t.pnl for t in self._trades if t.exit_time is not None)
        unrealized = 0.0
        if self._current_trade is not None and self._current_trade.exit_time is None:
            gross = (mid_price / self._current_trade.entry_price - 1.0)
            unrealized = (gross - self.fee_rate * 2) * self._current_trade.quantity * self._current_trade.entry_price
        self._equity = self.initial_capital + closed_pnl + unrealized

        # 更新峰值和回撤
        self._peak_equity = max(self._peak_equity, self._equity)
        current_drawdown = (self._peak_equity - self._equity) / self._peak_equity
        if current_drawdown > self._max_drawdown:
            self._max_drawdown = current_drawdown

        # 生成信号
        signal = self.strategy.evaluate(scores, tick.orderbook)
        if signal.type != SignalType.NONE:
            self._total_signals += 1

        # 处理入场
        if signal.is_entry and self._current_trade is None:
            self._handle_entry(signal, tick, scores)

        # 处理出场
        elif signal.is_exit and self._current_trade is not None:
            self._handle_exit(signal, tick, scores)

    _idx_counter: int = 0

    def _handle_entry(self, signal, tick: BacktestTick, scores: EdgeScores) -> None:
        """处理入场信号"""
        should_enter, side, limit_price = self.strategy.should_enter(signal)
        if not should_enter:
            return

        # 风控检查
        risk_result = self.risk.check_entry(
            signal, self.strategy.position,
            equity=self._equity,
            current_price=tick.orderbook.mid_price,
        )
        if not risk_result.allowed:
            logger.debug(f"回测入场被风控拒绝: {risk_result.reason}")
            return

        # 创建交易记录（以 mid_price 成交）
        entry_price = tick.orderbook.mid_price

        self._current_trade = TradeRecord(
            entry_time=tick.timestamp,
            entry_price=entry_price,
            quantity=self.trade_quantity,
            entry_edge_score=scores.edge,
        )

        # 更新策略状态
        from app.core.types import Order, OrderStatus, OrderType as OT
        BacktestEngine._idx_counter += 1
        mock_order = Order(
            order_id=f"bt_{BacktestEngine._idx_counter}",
            symbol=self.symbol,
            side=OrderSide.BUY,
            type=OT.LIMIT,
            price=entry_price,
            quantity=self.trade_quantity,
            status=OrderStatus.FILLED,
            filled_qty=self.trade_quantity,
        )
        self.strategy.on_entry_filled(mock_order, signal)

    def _handle_exit(self, signal, tick: BacktestTick, scores: EdgeScores) -> None:
        """处理出场信号"""
        if not self.strategy.should_exit(signal):
            return

        exit_reason = signal.exit_reason.value if signal.exit_reason else ExitReason.EDGE_BELOW_THRESHOLD.value
        self._close_trade(tick, exit_reason, scores.edge)

    def _close_trade(
        self,
        tick: BacktestTick,
        exit_reason: str,
        exit_edge: float = 0.0,
        force: bool = False,
    ) -> None:
        """
        平仓当前交易

        Args:
            tick: 当前 tick
            exit_reason: 出场原因
            exit_edge: 出场时 Edge Score
            force: 是否强制平仓（回测结束时）
        """
        if self._current_trade is None:
            return

        exit_price = tick.orderbook.mid_price

        self._current_trade.close(
            exit_time=tick.timestamp,
            exit_price=exit_price,
            exit_edge_score=exit_edge,
            exit_reason=exit_reason,
            fee_rate=self.fee_rate,
        )

        self._trades.append(self._current_trade)

        # 更新出场原因分布
        self._exit_reason_dist[exit_reason] = self._exit_reason_dist.get(exit_reason, 0) + 1

        # 更新策略状态
        from app.core.types import Order, OrderStatus, OrderType as OT
        BacktestEngine._idx_counter += 1
        mock_order = Order(
            order_id=f"bt_{BacktestEngine._idx_counter}",
            symbol=self.symbol,
            side=OrderSide.SELL,
            type=OT.LIMIT,
            price=exit_price,
            quantity=self.trade_quantity,
            status=OrderStatus.FILLED,
            filled_qty=self.trade_quantity,
        )
        self.strategy.on_exit_filled(mock_order)

        # 更新风控
        if self._current_trade.pnl > 0:
            self.risk.on_trade_win(self._current_trade.pnl)
        else:
            self.risk.on_trade_loss(self._current_trade.pnl)

        self.risk.add_pnl(self._current_trade.pnl)

        logger.debug(
            f"平仓 | {exit_reason} | "
            f"价格={exit_price:.2f} | 收益={self._current_trade.return_pct:+.3f}% | "
            f"持仓={self._current_trade.holding_minutes:.0f}min"
        )

        self._current_trade = None

    def _build_result(self, ticks: list[BacktestTick]) -> BacktestResult:
        """构建回测结果对象"""
        if not ticks:
            return self._empty_result()

        trades = self._trades
        total_trades = len(trades)
        winning = [t for t in trades if t.is_win]
        losing = [t for t in trades if not t.is_win]
        winning_trades = len(winning)
        losing_trades = len(losing)

        # 总收益率
        total_return = (self._equity / self.initial_capital - 1.0) * 100.0

        # 年化收益率
        days = (ticks[-1].timestamp - ticks[0].timestamp).total_seconds() / 86400.0
        if days > 0:
            annualized_return = ((1.0 + total_return / 100.0) ** (365.0 / days) - 1.0) * 100.0
        else:
            annualized_return = 0.0

        # 最大回撤
        max_dd = self._max_drawdown * 100.0

        # 日收益率序列（用于计算夏普比率）
        if len(self._equity_curve) >= 2:
            equity_arr = np.array(self._equity_curve, dtype=np.float64)
            daily_returns: list[float] = []
            # 将 tick 级收益聚合为日收益
            for i in range(1, len(equity_arr)):
                if equity_arr[i - 1] > 0:
                    daily_returns.append(float(equity_arr[i] / equity_arr[i - 1] - 1.0))

            if daily_returns:
                ret_arr = np.array(daily_returns, dtype=np.float64)
                avg_ret = float(np.mean(ret_arr))
                std_ret = float(np.std(ret_arr, ddof=1))
                # 年化夏普（假设 5m K线，日约 288 根）
                periods_per_year = 365.0 * 288.0
                sharpe = (avg_ret / std_ret * np.sqrt(periods_per_year)) if std_ret > 0 else 0.0

                # 索提诺比率（只考虑下行波动）
                downside = ret_arr[ret_arr < 0]
                downside_std = float(np.std(downside, ddof=1)) if len(downside) > 0 else std_ret
                sortino = (avg_ret / downside_std * np.sqrt(periods_per_year)) if downside_std > 0 else 0.0

                # 卡玛比率
                calmar = annualized_return / max_dd if max_dd > 0 else 0.0

                # 年化波动率
                volatility = std_ret * np.sqrt(periods_per_year) * 100.0
            else:
                sharpe = sortino = calmar = volatility = 0.0
        else:
            sharpe = sortino = calmar = volatility = 0.0

        # 交易质量
        win_rate = winning_trades / total_trades if total_trades > 0 else 0.0
        avg_win = float(np.mean([t.return_pct for t in winning])) if winning else 0.0
        avg_loss = float(np.mean([t.return_pct for t in losing])) if losing else 0.0

        total_profit = sum(t.pnl for t in winning)
        total_loss = abs(sum(t.pnl for t in losing))
        profit_factor = total_profit / total_loss if total_loss > 0 else 0.0

        avg_holding = float(np.mean([t.holding_minutes for t in trades])) if trades else 0.0
        max_holding = float(np.max([t.holding_minutes for t in trades])) if trades else 0.0

        # 基准收益（买入持有）
        if len(ticks) >= 2:
            first_price = ticks[0].kline_data.close
            last_price = ticks[-1].kline_data.close
            benchmark_return = (last_price / first_price - 1.0) * 100.0 if first_price > 0 else 0.0
        else:
            benchmark_return = 0.0

        alpha = total_return - benchmark_return

        return BacktestResult(
            symbol=self.symbol,
            interval=self.interval,
            start_date=ticks[0].timestamp,
            end_date=ticks[-1].timestamp,
            total_ticks=len(ticks),
            total_signals=self._total_signals,
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            total_return_pct=total_return,
            annualized_return_pct=annualized_return,
            max_drawdown_pct=max_dd,
            max_drawdown_duration_days=self._max_drawdown_duration,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            calmar_ratio=calmar,
            volatility_pct=volatility,
            win_rate=win_rate,
            avg_win_pct=avg_win,
            avg_loss_pct=avg_loss,
            profit_factor=profit_factor,
            avg_holding_minutes=avg_holding,
            max_holding_minutes=max_holding,
            exit_reason_dist=self._exit_reason_dist,
            trades=trades,
            equity_curve=self._equity_curve,
            equity_timestamps=self._equity_timestamps,
            benchmark_return_pct=benchmark_return,
            alpha_pct=alpha,
        )

    def _empty_result(self) -> BacktestResult:
        """返回空结果"""
        now = datetime.now(timezone.utc)
        return BacktestResult(
            symbol=self.symbol,
            interval=self.interval,
            start_date=now,
            end_date=now,
        )


# ═══════════════════════════════════════════════════════
# 独立测试
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("回测引擎 — 独立测试 (模拟数据)")
    print("=" * 60)

    from datetime import datetime, timezone, timedelta

    # ── 生成模拟 K 线数据 ────────────────────────────
    np.random.seed(42)

    def generate_mock_ticks(n: int = 500, start_price: float = 65000.0) -> list[BacktestTick]:
        """生成模拟回测 tick 数据（随机游走）"""
        ticks: list[BacktestTick] = []
        price = start_price
        base_time = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)

        for i in range(n):
            # 随机游走 + 趋势
            ret = np.random.normal(0.0001, 0.002)  # 均值略正，波动 0.2%
            price *= (1.0 + ret)

            # OHLC
            high = price * (1.0 + abs(np.random.normal(0, 0.001)))
            low = price * (1.0 - abs(np.random.normal(0, 0.001)))
            open_p = price * (1.0 + np.random.normal(0, 0.0005))
            close_p = price

            volume = abs(np.random.normal(5.0, 2.0))
            taker_buy_vol = volume * np.random.uniform(0.3, 0.7)

            kd = KlineData(
                open_time=base_time + timedelta(minutes=5 * i),
                open=open_p, high=high, low=low, close=close_p,
                volume=volume, quote_volume=volume * price,
                trades_count=int(np.random.randint(50, 500)),
                taker_buy_volume=taker_buy_vol,
                taker_buy_quote_volume=taker_buy_vol * price,
            )
            ticks.append(BacktestTick(timestamp=kd.open_time, kline_data=kd))

        return ticks

    # ── 运行回测 ─────────────────────────────────────
    mock_ticks = generate_mock_ticks(500)
    print(f"模拟数据: {len(mock_ticks)} ticks")

    engine = BacktestEngine(
        initial_capital=10000.0,
        trade_quantity=0.001,
    )

    result = engine.run(mock_ticks)

    # ── 验证 ─────────────────────────────────────────
    assert result.total_ticks == 500
    assert result.total_trades >= 0
    assert 0.0 <= result.win_rate <= 1.0
    assert result.max_drawdown_pct >= 0.0
    print(f"✅ 回测完成: {result.total_trades} 笔交易")

    # 打印摘要
    print(result.summary())

    # ── 测试 TradeRecord ─────────────────────────────
    tr = TradeRecord(
        entry_time=datetime.now(timezone.utc),
        entry_price=65000.0,
        quantity=0.001,
        entry_edge_score=75.0,
    )
    tr.close(
        exit_time=datetime.now(timezone.utc) + timedelta(minutes=30),
        exit_price=65200.0,
        exit_edge_score=45.0,
        exit_reason="PROFIT_TARGET",
        fee_rate=0.0004,
    )
    assert tr.is_win
    assert tr.return_pct > 0
    assert tr.pnl > 0
    print(f"✅ TradeRecord: return={tr.return_pct:+.3f}% pnl={tr.pnl:+.2f} USDT")

    # ── 测试空结果 ───────────────────────────────────
    empty = engine._empty_result()
    assert empty.total_trades == 0
    print(f"✅ 空结果: total_trades={empty.total_trades}")

    print("\n全部测试通过! ✅")
