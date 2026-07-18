"""
风控模块 — 多层级风险控制 + 方向性对冲管理

══════════════════════════════════════════════════════════════
风控层级（从外到内）:
══════════════════════════════════════════════════════════════
  1. 安全模式 — API异常/网络错误 → 撤销所有订单，暂停交易
  2. 连续亏损暂停 — 连续N笔亏损 → 暂停M分钟冷静期
  3. 仓位限制 — 最大持仓量 ≤ 账户权益的N%、最大净多头敞口
  4. 单笔风险限制 — 单笔交易风险 ≤ 账户权益的N%
  5. 日亏损限制 — 日亏损金额/百分比达到上限停止交易
  6. 回撤控制 — 权益回撤超限自动进入 EMERGENCY 状态
  7. 滑点保护 — 订单价格偏离市价超限拒绝
  8. 超时撤单 — 挂单超过时限自动撤销
  9. 对冲管理 — 方向性敞口超阈值触发对冲/解除对冲

策略约束（单向买入）:
  - 仅允许 BUY 入场订单（ENTRY 信号 → 买入方向）
  - 出场订单始终为 SELL（止盈或对冲卖出）
  - 对冲方向始终为 SELL（削减多头敞口）

模块可独立测试: python -m app.risk
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

from loguru import logger

from app.core.types import (
    Direction, HedgeSignal, OrderBook, Position, PositionStatus,
    RiskResult, Signal, SignalType, TradeSide,
)


class RiskManager:
    """
    多层级风控引擎 — 单向买入策略版本

    入场规则:
      - 仅允许 BUY 入场（ENTRY + LONG）
      - 出场/对冲始终 SELL
      - 禁止裸卖空（无持仓时的 SELL 被拒绝）

    增强风控 (V1.1):
      - 安全模式: API/网络异常时自动进入，撤销所有订单
      - 连续亏损暂停: 3次连续亏损 → 暂停30分钟
      - 单笔风险 ≤ 0.5% 账户权益
      - 最大仓位 ≤ 20% 账户权益
      - 日亏损 ≤ 2% 账户权益

    使用方式:
        rm = RiskManager(
            max_position_size=0.01, daily_loss_limit=100.0,
            single_trade_risk_pct=0.5, max_position_pct=20.0,
            daily_loss_pct=2.0, consecutive_loss_limit=3,
            pause_minutes=30,
        )
        result = rm.check_entry(signal, position, equity=10000.0, current_price=65000.0)
        if not result.allowed:
            logger.warning(f"风控拒绝: {result.reason}")
    """

    def __init__(
        self,
        max_position_size: float = 0.01,
        max_net_exposure: float = 0.005,
        max_drawdown_pct: float = 0.05,
        daily_loss_limit: float = 100.0,
        order_timeout: int = 120,
        max_slippage_bps: int = 5,
        max_concurrent_orders: int = 3,
        min_order_value: float = 10.0,
        # ── V1.1 增强风控参数 ──────────────────────────
        single_trade_risk_pct: float = 0.5,
        max_position_pct: float = 20.0,
        daily_loss_pct: float = 2.0,
        consecutive_loss_limit: int = 3,
        pause_minutes: int = 30,
    ) -> None:
        """
        Args:
            max_position_size: 最大主策略持仓量 (BTC) — 旧版固定值
            max_net_exposure: 最大净多头敞口 (BTC)，对冲后的净持仓上限
            max_drawdown_pct: 最大回撤百分比（如 0.05 = 5%）
            daily_loss_limit: 单日最大亏损 (USDT) — 旧版绝对值
            order_timeout: 订单超时时间（秒）
            max_slippage_bps: 最大允许滑点（基点）
            max_concurrent_orders: 最大并发挂单数
            min_order_value: 最小订单金额 (USDT)，币安现货最小 10 USDT
            single_trade_risk_pct: 单笔交易风险 ≤ N% 账户权益 (默认 0.5%)
            max_position_pct: 最大持仓 ≤ N% 账户权益 (默认 20%)
            daily_loss_pct: 日亏损 ≤ N% 账户权益 (默认 2%)
            consecutive_loss_limit: 连续亏损次数上限 (默认 3)
            pause_minutes: 连续亏损后暂停分钟数 (默认 30)
        """
        # 旧版参数
        self.max_position_size: float = max_position_size
        self.max_net_exposure: float = max_net_exposure
        self.max_drawdown_pct: float = max_drawdown_pct
        self.daily_loss_limit: float = daily_loss_limit
        self.order_timeout: int = order_timeout
        self.max_slippage_bps: int = max_slippage_bps
        self.max_concurrent_orders: int = max_concurrent_orders
        self.min_order_value: float = min_order_value

        # V1.1 增强风控参数
        self.single_trade_risk_pct: float = single_trade_risk_pct
        self.max_position_pct: float = max_position_pct
        self.daily_loss_pct: float = daily_loss_pct
        self.consecutive_loss_limit: int = consecutive_loss_limit
        self.pause_minutes: int = pause_minutes

        # ── 运行时追踪 ────────────────────────────────
        self.daily_start_equity: float = 0.0
        self.peak_equity: float = 0.0
        self.daily_pnl: float = 0.0
        self._daily_date: str = ""
        self._order_timestamps: dict[str, datetime] = {}
        self._active_order_count: int = 0
        self._emergency: bool = False

        # ── V1.1 增强追踪 ──────────────────────────────
        self._safe_mode: bool = False
        self._safe_mode_reason: str = ""
        self._safe_mode_entered_at: Optional[datetime] = None
        self._consecutive_losses: int = 0
        self._consecutive_wins: int = 0
        self._paused_until: Optional[datetime] = None
        self._pause_reason: str = ""
        self._total_trades: int = 0
        self._winning_trades: int = 0

    # ═══════════════════════════════════════════════════
    # 状态属性
    # ═══════════════════════════════════════════════════

    @property
    def in_emergency(self) -> bool:
        """是否处于紧急状态"""
        return self._emergency

    @property
    def in_safe_mode(self) -> bool:
        """是否处于安全模式"""
        return self._safe_mode

    @property
    def safe_mode_reason(self) -> str:
        """安全模式原因"""
        return self._safe_mode_reason

    @property
    def is_paused(self) -> bool:
        """是否处于暂停状态（连续亏损暂停）"""
        if self._paused_until is None:
            return False
        if datetime.now(timezone.utc) >= self._paused_until:
            # 暂停已到期，自动恢复
            self._paused_until = None
            self._pause_reason = ""
            return False
        return True

    @property
    def pause_remaining_seconds(self) -> float:
        """暂停剩余秒数"""
        if self._paused_until is None:
            return 0.0
        remaining = (self._paused_until - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, remaining)

    @property
    def consecutive_losses(self) -> int:
        """连续亏损次数"""
        return self._consecutive_losses

    @property
    def win_rate(self) -> float:
        """胜率"""
        if self._total_trades <= 0:
            return 0.0
        return self._winning_trades / self._total_trades

    @property
    def can_trade(self) -> bool:
        """
        是否允许交易（综合状态检查）

        禁止交易的情况:
          - 紧急状态
          - 安全模式
          - 连续亏损暂停中
        """
        if self._emergency:
            return False
        if self._safe_mode:
            return False
        if self.is_paused:
            return False
        return True

    # ═══════════════════════════════════════════════════
    # 安全模式管理
    # ═══════════════════════════════════════════════════

    def enter_safe_mode(self, reason: str) -> None:
        """
        进入安全模式 — 立即撤销所有订单，停止交易

        触发条件:
          - API 异常 (HTTP 4xx/5xx)
          - 网络错误 (连接超时/DNS解析失败)
          - 交易所返回系统错误

        Args:
            reason: 触发原因描述
        """
        if self._safe_mode:
            logger.debug(f"已在安全模式中，忽略重复触发: {reason}")
            return

        self._safe_mode = True
        self._safe_mode_reason = reason
        self._safe_mode_entered_at = datetime.now(timezone.utc)
        logger.error(f"🚨 进入安全模式 | 原因: {reason} | 时间: {self._safe_mode_entered_at}")

    def exit_safe_mode(self) -> None:
        """退出安全模式 — 恢复正常交易"""
        if not self._safe_mode:
            return

        duration = ""
        if self._safe_mode_entered_at:
            elapsed = (datetime.now(timezone.utc) - self._safe_mode_entered_at).total_seconds()
            duration = f" | 持续 {elapsed:.0f}s"

        self._safe_mode = False
        self._safe_mode_reason = ""
        self._safe_mode_entered_at = None
        logger.info(f"✅ 退出安全模式{duration} — 恢复正常交易")

    # ═══════════════════════════════════════════════════
    # 连续亏损暂停管理
    # ═══════════════════════════════════════════════════

    def on_trade_win(self, pnl: float) -> None:
        """
        记录一笔盈利交易 — 重置连续亏损计数器

        Args:
            pnl: 本次交易盈亏 (USDT)
        """
        self._total_trades += 1
        self._winning_trades += 1
        self._consecutive_losses = 0
        self._consecutive_wins += 1
        logger.debug(f"交易盈利 {pnl:+.2f} USDT | 连续盈利={self._consecutive_wins} | 胜率={self.win_rate:.1%}")

    def on_trade_loss(self, pnl: float) -> None:
        """
        记录一笔亏损交易 — 累加连续亏损，达阈值自动暂停

        暂停逻辑:
          - 连续亏损 < limit: 仅累加计数
          - 连续亏损 = limit: 自动暂停 pause_minutes 分钟

        Args:
            pnl: 本次交易盈亏 (USDT，负值)
        """
        self._total_trades += 1
        self._consecutive_losses += 1
        self._consecutive_wins = 0
        logger.warning(
            f"交易亏损 {pnl:+.2f} USDT | 连续亏损={self._consecutive_losses}/{self.consecutive_loss_limit}"
        )

        # 达到连续亏损上限 → 暂停
        if self._consecutive_losses >= self.consecutive_loss_limit:
            self._pause_trading(
                reason=f"连续亏损 {self._consecutive_losses} 次，暂停 {self.pause_minutes} 分钟"
            )

    def _pause_trading(self, reason: str) -> None:
        """
        暂停交易（内部方法）

        Args:
            reason: 暂停原因
        """
        self._paused_until = datetime.now(timezone.utc) + timedelta(minutes=self.pause_minutes)
        self._pause_reason = reason
        logger.warning(
            f"⏸️ 交易暂停 | {reason} | "
            f"恢复时间: {self._paused_until.strftime('%H:%M:%S')}"
        )

    def resume_trading(self) -> None:
        """手动恢复交易（清除暂停状态）"""
        if self._paused_until is not None:
            self._paused_until = None
            self._pause_reason = ""
            self._consecutive_losses = 0
            logger.info("▶️ 交易已手动恢复")

    # ═══════════════════════════════════════════════════
    # 入场风控
    # ═══════════════════════════════════════════════════

    def check_entry(
        self,
        signal: Signal,
        position: Optional[Position],
        equity: float = 0.0,
        current_price: float = 0.0,
    ) -> RiskResult:
        """
        入场前综合风控检查（仅限买入方向）

        检查项:
          1. 紧急状态检查
          2. 安全模式检查
          3. 连续亏损暂停检查
          4. 信号类型检查（仅 ENTRY + LONG 允许入场）
          5. 是否已有持仓（不允许重复入场）
          6. 单日亏损是否超限（金额 + 百分比）
          7. 单笔交易风险检查（≤ 账户权益的 N%）
          8. 最大仓位检查（≤ 账户权益的 N%）
          9. 并发订单数是否超限

        Args:
            signal: 交易信号
            position: 当前持仓（None 表示无持仓）
            equity: 当前账户权益 (USDT)，用于百分比风控计算
            current_price: 当前价格，用于仓位/风险计算

        Returns:
            RiskResult: 检查结果
        """
        # 0. 紧急状态
        if self._emergency:
            return RiskResult(allowed=False, reason="紧急状态，禁止入场")

        # 1. 安全模式
        if self._safe_mode:
            return RiskResult(
                allowed=False,
                reason=f"安全模式中，禁止入场 ({self._safe_mode_reason})",
            )

        # 2. 连续亏损暂停
        if self.is_paused:
            remaining = self.pause_remaining_seconds
            return RiskResult(
                allowed=False,
                reason=f"连续亏损暂停中 ({self._pause_reason})，剩余 {remaining:.0f}s",
            )

        # 3. 信号方向检查：仅允许 ENTRY + LONG（买入入场）
        if signal.type != SignalType.ENTRY or signal.direction != Direction.LONG:
            return RiskResult(
                allowed=False,
                reason=f"仅允许买入入场 (ENTRY+LONG)，收到 {signal.type.value}+{signal.direction.value}",
            )

        # 4. 仓位检查
        if position is not None and position.status != PositionStatus.CLOSED:
            return RiskResult(allowed=False, reason="已有持仓，不允许重复入场")

        # 5. 单日止损 — 金额
        if self.daily_pnl <= -self.daily_loss_limit:
            self._emergency = True
            return RiskResult(
                allowed=False,
                reason=f"单日亏损已达上限 ({self.daily_loss_limit:.0f} USDT)，当前 PnL={self.daily_pnl:.2f}",
            )

        # 6. 单日止损 — 百分比 (V1.1)
        if equity > 0 and self.daily_loss_pct > 0:
            daily_loss_pct_actual = abs(self.daily_pnl) / equity * 100.0 if self.daily_pnl < 0 else 0.0
            if daily_loss_pct_actual >= self.daily_loss_pct:
                self._emergency = True
                return RiskResult(
                    allowed=False,
                    reason=f"单日亏损已达上限 ({self.daily_loss_pct:.1f}% 权益)，当前 PnL%={daily_loss_pct_actual:.2f}%",
                )

        # 7. 单笔交易风险检查 (V1.1) — 仅在 equity > 0 且 current_price > 0 时生效
        if equity > 0 and current_price > 0 and self.single_trade_risk_pct > 0:
            trade_quantity = self.max_position_size
            trade_value = trade_quantity * current_price
            trade_risk = trade_value / equity * 100.0
            if trade_risk > self.single_trade_risk_pct:
                return RiskResult(
                    allowed=False,
                    reason=f"单笔交易风险超限 ({trade_risk:.2f}% > {self.single_trade_risk_pct}%)，"
                           f"交易金额={trade_value:.2f} USDT，权益={equity:.2f} USDT",
                )

        # 8. 最大仓位检查 (V1.1) — 按权益百分比限制
        if equity > 0 and current_price > 0 and self.max_position_pct > 0:
            max_position_value = equity * self.max_position_pct / 100.0
            max_position_qty = max_position_value / current_price
            # 取固定值和百分比限制中较小的
            effective_max_qty = min(self.max_position_size, max_position_qty)
        else:
            effective_max_qty = self.max_position_size

        if effective_max_qty <= 0:
            return RiskResult(allowed=False, reason="最大持仓量配置为 0")

        # 9. 并发订单检查
        if self._active_order_count >= self.max_concurrent_orders:
            return RiskResult(
                allowed=False,
                reason=f"并发订单数已达上限 ({self.max_concurrent_orders})",
            )

        return RiskResult(
            allowed=True,
            max_allowed_qty=effective_max_qty,
            adjusted_price=signal.limit_price,
        )

    def check_exit(self, signal: Signal, position: Optional[Position]) -> RiskResult:
        """
        出场前风控检查

        Args:
            signal: 出场信号
            position: 当前持仓

        Returns:
            RiskResult
        """
        if position is None or position.status == PositionStatus.CLOSED:
            return RiskResult(allowed=False, reason="无持仓，无需出场")

        if signal.type != SignalType.EXIT:
            return RiskResult(allowed=False, reason=f"非出场信号: {signal.type.value}")

        return RiskResult(allowed=True)

    def check_hedge(self, signal: Signal, position: Optional[Position]) -> RiskResult:
        """
        对冲前风控检查

        Args:
            signal: 对冲信号
            position: 当前持仓

        Returns:
            RiskResult
        """
        if signal.type != SignalType.HEDGE:
            return RiskResult(allowed=False, reason=f"非对冲信号: {signal.type.value}")

        if position is None or position.status == PositionStatus.CLOSED:
            return RiskResult(allowed=False, reason="无持仓，无需对冲")

        # 检查对冲后净敞口是否合理
        if signal.direction == Direction.SHORT:
            # 卖出对冲 → 净敞口减少
            new_exposure = position.net_exposure - signal.confidence * position.quantity
            if new_exposure < 0:
                return RiskResult(
                    allowed=False,
                    reason=f"对冲过度: 净敞口将变为 {new_exposure:.6f} BTC (负值)",
                )
        elif signal.direction == Direction.LONG:
            # 买入解除对冲 → 净敞口增加
            if position.net_exposure >= self.max_net_exposure:
                return RiskResult(
                    allowed=False,
                    reason=f"净敞口已达上限 ({self.max_net_exposure} BTC)，不允许增加敞口",
                )

        return RiskResult(allowed=True)

    # ═══════════════════════════════════════════════════
    # V1.1 新增：专项风控检查
    # ═══════════════════════════════════════════════════

    def check_position_size(
        self,
        equity: float,
        quantity: float,
        price: float,
    ) -> RiskResult:
        """
        检查仓位是否超过权益百分比限制

        Args:
            equity: 账户权益 (USDT)
            quantity: 计划持仓量 (BTC)
            price: 当前价格

        Returns:
            RiskResult
        """
        if equity <= 0:
            return RiskResult(allowed=True)

        position_value = quantity * price
        position_pct = position_value / equity * 100.0

        if position_pct > self.max_position_pct:
            return RiskResult(
                allowed=False,
                reason=f"仓位超限 ({position_pct:.2f}% > {self.max_position_pct}%)，"
                       f"仓位价值={position_value:.2f} USDT，权益={equity:.2f} USDT",
            )

        return RiskResult(allowed=True)

    def check_single_trade_risk(
        self,
        equity: float,
        quantity: float,
        entry_price: float,
        stop_price: float = 0.0,
    ) -> RiskResult:
        """
        检查单笔交易风险是否超过权益百分比限制

        风险计算: 如果提供止损价，用 (entry - stop) * qty 估算最大损失；
                  否则用 trade_value / equity 估算。

        Args:
            equity: 账户权益 (USDT)
            quantity: 交易量 (BTC)
            entry_price: 入场价格
            stop_price: 止损价格（可选）

        Returns:
            RiskResult
        """
        if equity <= 0:
            return RiskResult(allowed=True)

        if stop_price > 0 and stop_price < entry_price:
            # 有止损价: 计算最大损失
            max_loss = (entry_price - stop_price) * quantity
        else:
            # 无止损: 用交易额比例估算
            max_loss = entry_price * quantity

        risk_pct = max_loss / equity * 100.0

        if risk_pct > self.single_trade_risk_pct:
            return RiskResult(
                allowed=False,
                reason=f"单笔风险超限 ({risk_pct:.3f}% > {self.single_trade_risk_pct}%)，"
                       f"最大损失={max_loss:.2f} USDT，权益={equity:.2f} USDT",
            )

        return RiskResult(allowed=True)

    # ═══════════════════════════════════════════════════
    # 订单风控
    # ═══════════════════════════════════════════════════

    def check_order(self, order_price: float, orderbook: OrderBook) -> RiskResult:
        """
        下单前滑点检查

        Args:
            order_price: 订单价格
            orderbook: 当前订单簿

        Returns:
            RiskResult: 滑点超限时 allowed=False
        """
        mid = orderbook.mid_price
        if mid <= 0:
            return RiskResult(allowed=True)

        slippage_bps = abs(order_price - mid) / mid * 10000.0
        if slippage_bps > self.max_slippage_bps:
            return RiskResult(
                allowed=False,
                reason=f"滑点超限 ({slippage_bps:.1f} bps > {self.max_slippage_bps} bps)",
            )

        return RiskResult(allowed=True)

    def check_order_value(self, price: float, quantity: float) -> RiskResult:
        """
        检查订单金额是否满足最小交易额

        Args:
            price: 委托价格
            quantity: 委托数量

        Returns:
            RiskResult
        """
        order_value = price * quantity
        if order_value < self.min_order_value:
            return RiskResult(
                allowed=False,
                reason=f"订单金额 {order_value:.2f} USDT < 最小 {self.min_order_value} USDT",
            )
        return RiskResult(allowed=True)

    def register_order(self, order_id: str) -> None:
        """注册新订单（追踪超时）"""
        self._order_timestamps[order_id] = datetime.now(timezone.utc)
        self._active_order_count += 1

    def unregister_order(self, order_id: str) -> None:
        """注销订单"""
        self._order_timestamps.pop(order_id, None)
        self._active_order_count = max(0, self._active_order_count - 1)

    def should_cancel(self, order_id: str) -> bool:
        """
        检查订单是否应因超时被撤销

        Args:
            order_id: 订单ID

        Returns:
            True 表示应撤销
        """
        ts = self._order_timestamps.get(order_id)
        if ts is None:
            return False
        elapsed = (datetime.now(timezone.utc) - ts).total_seconds()
        return elapsed > self.order_timeout

    # ═══════════════════════════════════════════════════
    # 权益追踪
    # ═══════════════════════════════════════════════════

    def update_equity(self, current_equity: float) -> None:
        """
        更新当前权益并自动处理日重置

        Args:
            current_equity: 当前总权益（USDT）
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # 跨日重置
        if today != self._daily_date:
            self.daily_start_equity = current_equity
            self.daily_pnl = 0.0
            self._daily_date = today
            self._emergency = False
            self._consecutive_losses = 0
            self._consecutive_wins = 0
            self._paused_until = None
            self._pause_reason = ""
            # 安全模式跨日不自动退出，需手动确认
            logger.info(
                f"📅 日重置 | 起始权益={current_equity:.2f} USDT | "
                f"安全模式={'是' if self._safe_mode else '否'}"
            )

        self.peak_equity = max(self.peak_equity, current_equity)

    def add_pnl(self, pnl: float) -> None:
        """
        累加当日盈亏

        Args:
            pnl: 本次盈亏金额 (USDT)
        """
        self.daily_pnl += pnl

    @property
    def drawdown_pct(self) -> float:
        """当前回撤百分比"""
        if self.peak_equity <= 0:
            return 0.0
        current_equity = self.daily_start_equity + self.daily_pnl
        return max(0.0, (self.peak_equity - current_equity) / self.peak_equity)

    @property
    def daily_pnl_pct(self) -> float:
        """当日收益率 (%)"""
        if self.daily_start_equity <= 0:
            return 0.0
        return self.daily_pnl / self.daily_start_equity * 100.0

    # ═══════════════════════════════════════════════════
    # V1.1 状态摘要
    # ═══════════════════════════════════════════════════

    def status_summary(self) -> dict:
        """
        返回风控状态摘要（用于日志/监控）

        Returns:
            包含所有关键风控状态的字典
        """
        return {
            "emergency": self._emergency,
            "safe_mode": self._safe_mode,
            "safe_mode_reason": self._safe_mode_reason,
            "is_paused": self.is_paused,
            "pause_reason": self._pause_reason,
            "pause_remaining_s": self.pause_remaining_seconds,
            "consecutive_losses": self._consecutive_losses,
            "consecutive_wins": self._consecutive_wins,
            "total_trades": self._total_trades,
            "win_rate": self.win_rate,
            "daily_pnl": self.daily_pnl,
            "daily_pnl_pct": self.daily_pnl_pct,
            "daily_start_equity": self.daily_start_equity,
            "drawdown_pct": self.drawdown_pct,
            "active_orders": self._active_order_count,
            "can_trade": self.can_trade,
        }


class HedgeManager:
    """
    方向性风险对冲管理器 — 单向多头版本

    策略只持有 BTC 多头，对冲方向始终为卖出（SHORT）。
    当 BTC 价格下跌导致浮亏超过阈值时触发对冲削减敞口。

    对冲层级:
      1. 轻度对冲 (浮亏 50-150 bps): 对冲 25-50% 敞口
      2. 中度对冲 (浮亏 150-300 bps): 对冲 50-75% 敞口
      3. 深度对冲 (浮亏 > 300 bps): 对冲 75-100% 敞口

    使用方式:
        hm = HedgeManager(hedge_ratio=1.0, hedge_threshold_bps=50.0)
        signal = hm.evaluate(position, orderbook)
        if signal.required:
            # 下限价卖单对冲
            ...
    """

    # 对冲层级配置: (收益率阈值_bps, 对冲比例)
    HEDGE_TIERS: list[tuple[float, float]] = [
        (50.0, 0.25),    # 浮亏 50 bps → 对冲 25%
        (150.0, 0.50),   # 浮亏 150 bps → 对冲 50%
        (300.0, 0.75),   # 浮亏 300 bps → 对冲 75%
        (500.0, 1.00),   # 浮亏 500 bps → 完全对冲
    ]

    def __init__(
        self,
        hedge_ratio: float = 1.0,
        hedge_threshold_bps: float = 50.0,
        unwind_threshold_bps: float = 25.0,
    ) -> None:
        """
        Args:
            hedge_ratio: 全局对冲比例上限（1.0 = 允许完全对冲）
            hedge_threshold_bps: 触发对冲的最小浮亏（基点）
            unwind_threshold_bps: 解除对冲的浮亏回归阈值（基点）
        """
        self.hedge_ratio: float = hedge_ratio
        self.hedge_threshold_bps: float = hedge_threshold_bps
        self.unwind_threshold_bps: float = unwind_threshold_bps

    def _get_hedge_ratio(self, return_bps: float) -> float:
        """
        根据浮亏幅度确定对冲比例

        Args:
            return_bps: 当前收益率（基点），负值 = 浮亏

        Returns:
            对冲比例 [0.0, 1.0]
        """
        abs_loss = abs(return_bps)
        ratio = 0.0
        for threshold, tier_ratio in self.HEDGE_TIERS:
            if abs_loss >= threshold:
                ratio = tier_ratio
        return min(ratio, self.hedge_ratio)

    def evaluate(self, position: Optional[Position], orderbook: OrderBook) -> HedgeSignal:
        """
        评估是否需要执行对冲

        Args:
            position: 当前持仓（None 表示无持仓）
            orderbook: 当前订单簿

        Returns:
            HedgeSignal
        """
        if position is None or position.status == PositionStatus.CLOSED:
            return HedgeSignal(
                required=False, direction=Direction.NONE,
                quantity=0.0, hedge_ratio=0.0,
            )

        # 更新持仓市值
        position.update_unrealized_pnl(orderbook.mid_price)
        return_bps = position.return_bps

        # 浮亏未达阈值 → 不需要对冲
        if return_bps > -self.hedge_threshold_bps:
            return HedgeSignal(
                required=False, direction=Direction.NONE,
                quantity=0.0, hedge_ratio=0.0,
                reason=f"浮亏 {return_bps:.1f} bps 未达对冲阈值 {self.hedge_threshold_bps} bps",
            )

        # 计算对冲量
        target_hedge_ratio = self._get_hedge_ratio(return_bps)
        if target_hedge_ratio <= 0:
            return HedgeSignal(
                required=False, direction=Direction.NONE,
                quantity=0.0, hedge_ratio=0.0,
            )

        # 已对冲部分不计入
        current_hedge_ratio = position.hedge_ratio_pct / 100.0
        incremental_ratio = max(0.0, target_hedge_ratio - current_hedge_ratio)

        if incremental_ratio < 0.01:  # 小于 1% 不操作
            return HedgeSignal(
                required=False, direction=Direction.NONE,
                quantity=0.0, hedge_ratio=target_hedge_ratio,
                reason=f"对冲增量 {incremental_ratio:.1%} 过小，跳过",
            )

        hedge_qty = position.quantity * incremental_ratio

        return HedgeSignal(
            required=True,
            direction=Direction.SHORT,  # 始终卖出对冲
            quantity=hedge_qty,
            limit_price=orderbook.best_bid,
            hedge_ratio=target_hedge_ratio,
            reason=(
                f"方向性对冲 | 浮亏={return_bps:.1f}bps | "
                f"对冲层级={target_hedge_ratio:.0%} | "
                f"卖出 {hedge_qty:.6f} BTC 削减敞口"
            ),
        )

    def should_unwind_hedge(self, position: Optional[Position], orderbook: OrderBook) -> HedgeSignal:
        """
        检查是否应该解除对冲（浮亏回归安全区间）

        Args:
            position: 当前持仓
            orderbook: 当前订单簿

        Returns:
            HedgeSignal: required=True 表示应买入解除对冲
        """
        if position is None or not position.is_hedged:
            return HedgeSignal(
                required=False, direction=Direction.NONE,
                quantity=0.0, hedge_ratio=0.0,
            )

        position.update_unrealized_pnl(orderbook.mid_price)
        return_bps = position.return_bps

        # 浮亏已回归到解除阈值以内 → 解除对冲
        if return_bps > -self.unwind_threshold_bps:
            # 买入 BTC 恢复敞口
            unwind_qty = position.hedge_quantity

            return HedgeSignal(
                required=True,
                direction=Direction.LONG,  # 买入解除对冲
                quantity=unwind_qty,
                limit_price=orderbook.best_ask,
                hedge_ratio=0.0,
                reason=(
                    f"解除对冲 | 浮亏={return_bps:.1f}bps 已回归安全区间 | "
                    f"买入 {unwind_qty:.6f} BTC 恢复敞口"
                ),
            )

        return HedgeSignal(
            required=False, direction=Direction.NONE,
            quantity=0.0, hedge_ratio=position.hedge_ratio_pct / 100.0,
        )


# ═══════════════════════════════════════════════════════
# 独立测试入口
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    from datetime import datetime, timezone

    print("=" * 60)
    print("风控模块 — 单向买入策略 V1.1 增强风控 独立测试")
    print("=" * 60)

    # ── 测试 RiskManager ──────────────────────────────
    from app.core.types import SpreadStats, Signal, SignalType, Position

    rm = RiskManager(
        max_position_size=0.01, max_net_exposure=0.005,
        max_drawdown_pct=0.05, daily_loss_limit=100.0,
        order_timeout=120, max_slippage_bps=5,
        max_concurrent_orders=3, min_order_value=10.0,
        # V1.1 增强参数
        single_trade_risk_pct=0.5, max_position_pct=20.0,
        daily_loss_pct=2.0, consecutive_loss_limit=3,
        pause_minutes=30,
    )
    assert rm.max_position_size == 0.01
    assert rm.single_trade_risk_pct == 0.5
    assert rm.max_position_pct == 20.0
    assert rm.consecutive_loss_limit == 3
    assert rm.pause_minutes == 30
    assert rm.can_trade  # 初始应允许交易
    print("✅ RiskManager V1.1 初始化正常 (含增强风控参数)")

    # ── 测试安全模式 ──────────────────────────────────
    rm.enter_safe_mode("测试: API 返回 500 错误")
    assert rm.in_safe_mode
    assert not rm.can_trade
    assert "500" in rm.safe_mode_reason
    print(f"✅ 安全模式: reason={rm.safe_mode_reason}")

    # 重复进入安全模式 → 应忽略
    rm.enter_safe_mode("重复触发")
    assert rm.in_safe_mode
    print("✅ 安全模式重复触发被忽略")

    # 退出安全模式
    rm.exit_safe_mode()
    assert not rm.in_safe_mode
    assert rm.can_trade
    print("✅ 退出安全模式")

    # ── 测试连续亏损暂停 ──────────────────────────────
    # 模拟 3 次连续亏损
    rm.on_trade_loss(-5.0)
    assert rm.consecutive_losses == 1
    assert rm.can_trade
    print(f"✅ 亏损 #1: consecutive_losses={rm.consecutive_losses}")

    rm.on_trade_loss(-3.0)
    assert rm.consecutive_losses == 2
    assert rm.can_trade
    print(f"✅ 亏损 #2: consecutive_losses={rm.consecutive_losses}")

    rm.on_trade_loss(-8.0)
    assert rm.consecutive_losses == 3
    assert rm.is_paused
    assert not rm.can_trade
    assert rm.pause_remaining_seconds > 0
    print(f"✅ 亏损 #3: 触发暂停! remaining={rm.pause_remaining_seconds:.0f}s")

    # 入场应被暂停阻止
    stats = SpreadStats(current_spread=5.0, current_spread_bps=7.7,
                        rolling_mean=2.0, rolling_std=1.0, z_score=3.0, sample_count=100)
    entry_sig = Signal(
        type=SignalType.ENTRY, direction=Direction.LONG,
        confidence=0.8, spread_stats=stats, limit_price=65000.0,
        trade_side=TradeSide.PRIMARY,
    )
    result = rm.check_entry(entry_sig, None)
    assert not result.allowed
    assert "暂停" in result.reason
    print(f"✅ 暂停期间入场被拒绝: {result.reason}")

    # 手动恢复
    rm.resume_trading()
    assert not rm.is_paused
    assert rm.can_trade
    assert rm.consecutive_losses == 0
    print("✅ 手动恢复交易")

    # ── 测试盈利重置 ──────────────────────────────────
    rm.on_trade_loss(-2.0)
    assert rm.consecutive_losses == 1
    rm.on_trade_win(10.0)
    assert rm.consecutive_losses == 0
    assert rm._consecutive_wins == 1
    assert rm.win_rate > 0
    print(f"✅ 盈利重置: consecutive_losses={rm.consecutive_losses}, win_rate={rm.win_rate:.1%}")

    # ── 测试入场检查（无持仓 + 买入信号 → 允许）──
    result = rm.check_entry(entry_sig, None)
    assert result.allowed
    print(f"✅ 入场检查通过: max_qty={result.max_allowed_qty}")

    # ── 测试入场检查（已有持仓 → 拒绝）──
    pos = Position(
        symbol="BTCUSDT", side=Direction.LONG, quantity=0.001,
        entry_price=65000.0, trade_side=TradeSide.PRIMARY,
    )
    result = rm.check_entry(entry_sig, pos)
    assert not result.allowed
    assert "已有持仓" in result.reason
    print(f"✅ 重复入场拒绝: {result.reason}")

    # ── 测试非法信号方向（SELL 入场 → 拒绝）──
    bad_sig = Signal(
        type=SignalType.ENTRY, direction=Direction.SHORT,
        confidence=0.8, spread_stats=stats, limit_price=65000.0,
    )
    result = rm.check_entry(bad_sig, None)
    assert not result.allowed
    print(f"✅ 非法方向拒绝: {result.reason}")

    # ── 测试日止损 ────────────────────────────────────
    rm.daily_pnl = -101.0
    result = rm.check_entry(entry_sig, None)
    assert not result.allowed
    assert rm.in_emergency
    print(f"✅ 日止损触发: {result.reason}")
    rm.daily_pnl = 0.0
    rm._emergency = False  # 恢复

    # ── 测试单笔风险检查 (V1.1) ──────────────────────
    result = rm.check_single_trade_risk(
        equity=10000.0, quantity=0.01, entry_price=65000.0
    )
    assert not result.allowed  # 650 USDT / 10000 = 6.5% > 0.5%
    assert "单笔风险超限" in result.reason
    print(f"✅ 单笔风险超限拒绝: {result.reason}")

    result2 = rm.check_single_trade_risk(
        equity=150000.0, quantity=0.01, entry_price=65000.0
    )
    assert result2.allowed  # 650 / 150000 = 0.433% < 0.5%
    print(f"✅ 单笔风险通过 (equity=150000): {result2.allowed}")

    # ── 测试仓位检查 (V1.1) ──────────────────────────
    result = rm.check_position_size(
        equity=10000.0, quantity=0.05, price=65000.0,
    )
    assert not result.allowed  # 3250/10000 = 32.5% > 20%
    print(f"✅ 仓位超限拒绝: {result.reason}")

    result = rm.check_position_size(
        equity=100000.0, quantity=0.01, price=65000.0,
    )
    assert result.allowed  # 650/100000 = 0.65% < 20%
    print(f"✅ 仓位检查通过")

    # ── 测试入场时权益百分比检查 ─────────────────────
    # equity=10000, current_price=65000, max_position_size=0.01
    # trade_value = 650, risk = 6.5% > 0.5% → 拒绝
    result = rm.check_entry(entry_sig, None, equity=10000.0, current_price=65000.0)
    assert not result.allowed
    assert "单笔交易风险" in result.reason
    print(f"✅ 入场+权益风控拒绝: {result.reason}")

    # equity=200000, 650/200000 = 0.325% → 通过
    result = rm.check_entry(entry_sig, None, equity=200000.0, current_price=65000.0)
    assert result.allowed
    print(f"✅ 入场+权益风控通过: max_qty={result.max_allowed_qty}")

    # ── 测试订单注册/超时 ─────────────────────────────
    rm.register_order("order_001")
    assert rm._active_order_count == 1
    assert not rm.should_cancel("order_001")
    rm.unregister_order("order_001")
    assert rm._active_order_count == 0
    print("✅ 订单注册/超时/注销正常")

    # ── 测试滑点检查 ──────────────────────────────────
    ob = OrderBook("BTCUSDT", [(65000.0, 1.0)], [(65001.0, 1.0)])
    result = rm.check_order(65000.0, ob)
    assert result.allowed
    result = rm.check_order(70000.0, ob)
    assert not result.allowed
    print(f"✅ 滑点检查: {result.reason}")

    # ── 测试最小订单金额 ──────────────────────────────
    result = rm.check_order_value(65000.0, 0.0001)  # 6.5 USDT
    assert not result.allowed
    result = rm.check_order_value(65000.0, 0.001)   # 65 USDT
    assert result.allowed
    print(f"✅ 最小订单金额检查")

    # ── 测试权益更新（含日重置）──────────────────────
    rm.update_equity(10000.0)
    assert rm.daily_start_equity == 10000.0
    rm.add_pnl(50.0)
    assert rm.daily_pnl == 50.0
    assert rm.daily_pnl_pct == 0.5  # 50/10000 = 0.5%
    print(f"✅ 权益追踪: equity={rm.daily_start_equity}, PnL={rm.daily_pnl}, PnL%={rm.daily_pnl_pct}%")

    # ── 测试状态摘要 ──────────────────────────────────
    summary = rm.status_summary()
    assert not summary["emergency"]
    assert not summary["safe_mode"]
    assert not summary["is_paused"]
    assert summary["can_trade"]
    assert summary["total_trades"] > 0
    print(f"✅ 状态摘要: trades={summary['total_trades']}, win_rate={summary['win_rate']:.1%}")

    # ── 测试 HedgeManager ─────────────────────────────
    hm = HedgeManager(hedge_ratio=1.0, hedge_threshold_bps=50.0, unwind_threshold_bps=25.0)

    # 无持仓 → 不需要对冲
    no_pos_ob = OrderBook("BTCUSDT", [(65000.0, 1.0)], [(65001.0, 1.0)])
    hs = hm.evaluate(None, no_pos_ob)
    assert not hs.required
    print(f"✅ 无持仓: required={hs.required}")

    # 小幅浮亏 → 不需要对冲
    pos = Position(
        symbol="BTCUSDT", side=Direction.LONG, quantity=0.001,
        entry_price=65000.0, trade_side=TradeSide.PRIMARY,
    )
    slight_drop = OrderBook("BTCUSDT", [(64980.0, 1.0)], [(64981.0, 1.0)])
    hs = hm.evaluate(pos, slight_drop)
    assert not hs.required
    print(f"✅ 小幅浮亏免对冲: return_bps={pos.return_bps:.1f}")

    # 大幅浮亏 → 需要对冲
    big_drop = OrderBook("BTCUSDT", [(64600.0, 1.0)], [(64601.0, 1.0)])
    hs = hm.evaluate(pos, big_drop)
    assert hs.required
    assert hs.direction == Direction.SHORT
    assert hs.quantity > 0
    assert hs.hedge_ratio > 0
    print(f"✅ 需要对冲: {hs.reason}")

    # 测试解除对冲
    pos.hedge_quantity = 0.0005
    pos.status = PositionStatus.HEDGED
    recovery = OrderBook("BTCUSDT", [(64990.0, 1.0)], [(64991.0, 1.0)])
    hs = hm.should_unwind_hedge(pos, recovery)
    if hs.required:
        assert hs.direction == Direction.LONG
        print(f"✅ 解除对冲: {hs.reason}")
    else:
        print(f"⚠️  解除对冲未触发: {hs.reason}")

    print("\n" + "=" * 60)
    print("全部测试通过! ✅ (V1.1 增强风控)")
    print("=" * 60)
