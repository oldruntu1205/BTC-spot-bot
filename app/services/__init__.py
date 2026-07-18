"""
服务层 — 交易服务编排与订单生命周期管理

TradingService 是核心编排器，协调 exchange / strategy / risk / database 模块。
OrderManager 管理限价单从创建到成交/撤销的完整生命周期。

策略定位: BTC 现货单向买入套利 + 方向性对冲风控
  - 仅限价单买入入场（ENTRY + LONG）
  - 价差均值回归卖出止盈（EXIT + SHORT）
  - 方向性风险对冲卖出削减敞口（HEDGE + SHORT）

模块可独立测试: python -m app.services (需要 mock exchange)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from app.core.types import (
    BotState, Direction, Event, EventType,
    Order, OrderBook, OrderSide, OrderStatus, OrderType, SignalType, TradeSide,
)
from app.core.event_bus import event_bus
from app.exchange import (
    AccountAPI, BinanceClient, MarketAPI, MarketStream, OrderAPI,
)
from app.strategy import EdgeStrategy
from app.risk import HedgeManager, RiskManager
from app.database import Database


class OrderManager:
    """
    订单生命周期管理器

    职责:
      - 创建限价单对象（含 clientOrderId 幂等性）
      - 追踪订单状态（NEW → FILLED / CANCELED）
      - 超时订单检测
      - 基于订单簿深度的限价计算（VWAP 前 N 档）

    使用方式:
        mgr = OrderManager("BTCUSDT")
        order = mgr.create_limit(OrderSide.BUY, 65000.0, 0.001)
        mgr.register(order, exchange_order_id)
    """

    def __init__(self, symbol: str = "BTCUSDT") -> None:
        """
        Args:
            symbol: 交易对
        """
        self.symbol: str = symbol
        self._orders: dict[str, Order] = {}  # order_id → Order
        self._counter: int = 0
        self._requote_counts: dict[str, int] = {}  # V1.1: client_order_id → 重报价次数
        self.MAX_REQUOTES: int = 5  # V1.1: 最大重报价次数

    # ── 订单创建 ────────────────────────────────────

    def create_limit(
        self, side: OrderSide, price: float, quantity: float,
    ) -> Order:
        """
        创建限价单对象（尚未发送到交易所）

        Args:
            side: 买卖方向
            price: 委托价格
            quantity: 委托数量

        Returns:
            Order 对象（含唯一 clientOrderId）
        """
        self._counter += 1
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        client_id = f"sfn_{timestamp}_{self._counter:04d}"

        return Order(
            order_id="",  # 交易所返回后填充
            symbol=self.symbol,
            side=side,
            type=OrderType.LIMIT,
            price=price,
            quantity=quantity,
            client_order_id=client_id,
        )

    # ── 订单追踪 ────────────────────────────────────

    def register(self, order: Order, exchange_order_id: str) -> None:
        """
        注册已发送到交易所的订单

        Args:
            order: 订单对象
            exchange_order_id: 交易所返回的订单ID
        """
        order.order_id = exchange_order_id
        self._orders[exchange_order_id] = order
        logger.debug(f"订单已注册: {exchange_order_id} | {order.side.value} {order.quantity} @ {order.price}")

    def get(self, order_id: str) -> Optional[Order]:
        """根据订单ID获取订单"""
        return self._orders.get(order_id)

    def get_active(self) -> list[Order]:
        """获取所有活跃订单（NEW 或 PARTIALLY_FILLED）"""
        return [o for o in self._orders.values() if o.is_active]

    # ── V1.1 动态重报价 ────────────────────────────

    def can_requote(self, order: Order) -> bool:
        """
        检查订单是否可以重报价

        条件:
          - 订单仍处于活跃状态
          - 重报价次数未超过上限
          - 挂单时间超过 requote_timeout

        Args:
            order: 待检查的订单

        Returns:
            True 表示可以重报价
        """
        if not order.is_active:
            return False
        if order.requote_count >= self.MAX_REQUOTES:
            return False
        return True

    def get_requote_candidates(self, requote_timeout: int) -> list[Order]:
        """
        获取需要重报价的订单列表

        Args:
            requote_timeout: 重报价超时时间（秒），超过此时间未成交的订单需要重报价

        Returns:
            需要重报价的订单列表（已排除超过最大重报价次数的订单）
        """
        now = datetime.now(timezone.utc)
        candidates: list[Order] = []
        for order in self._orders.values():
            if not self.can_requote(order):
                continue
            elapsed = (now - order.created_at).total_seconds()
            if elapsed > requote_timeout:
                candidates.append(order)
        return candidates

    def increment_requote(self, order: Order) -> None:
        """
        增加订单的重报价计数并重置创建时间

        Args:
            order: 被重报价的订单
        """
        order.requote_count += 1
        order.created_at = datetime.now(timezone.utc)  # 重置计时器
        logger.debug(
            f"重报价 #{order.requote_count}/{self.MAX_REQUOTES} | "
            f"order_id={order.order_id} | side={order.side.value}"
        )

    def mark_canceled(self, order_id: str) -> None:
        """标记订单为已撤销"""
        if order_id in self._orders:
            self._orders[order_id].status = OrderStatus.CANCELED

    def get_expired(self, timeout_seconds: int) -> list[Order]:
        """
        获取超时未成交的订单（基于原始 order_timeout，非 requote_timeout）

        Args:
            timeout_seconds: 超时阈值（秒）

        Returns:
            超时订单列表
        """
        now = datetime.now(timezone.utc)
        expired: list[Order] = []
        for order in self._orders.values():
            if not order.is_active:
                continue
            elapsed = (now - order.created_at).total_seconds()
            if elapsed > timeout_seconds:
                expired.append(order)
        return expired

    # ── 限价计算 ────────────────────────────────────

    @staticmethod
    def calc_limit_price(
        orderbook: OrderBook,
        side: OrderSide,
        slippage_bps: int = 5,
        depth_levels: int = 3,
    ) -> float:
        """
        基于订单簿前 N 档 VWAP 计算限价

        买单: bids 侧加权均价 × (1 + 滑点) → 略高于最优买价
        卖单: asks 侧加权均价 × (1 - 滑点) → 略低于最优卖价

        Args:
            orderbook: 当前订单簿
            side: 买卖方向
            slippage_bps: 滑点保护（基点）
            depth_levels: 使用的深度档数

        Returns:
            建议限价
        """
        mid = orderbook.mid_price

        if side == OrderSide.BUY:
            bids = orderbook.bids[:depth_levels]
            if not bids:
                return mid * (1.0 + slippage_bps / 10000.0)
            total_qty = sum(q for _, q in bids)
            vwap = sum(p * q for p, q in bids) / total_qty if total_qty > 0 else bids[0][0]
            return vwap * (1.0 + slippage_bps / 10000.0)
        else:
            asks = orderbook.asks[:depth_levels]
            if not asks:
                return mid * (1.0 - slippage_bps / 10000.0)
            total_qty = sum(q for _, q in asks)
            vwap = sum(p * q for p, q in asks) / total_qty if total_qty > 0 else asks[0][0]
            return vwap * (1.0 - slippage_bps / 10000.0)


class TradingService:
    """
    核心交易服务 — 编排所有模块的主循环

    主循环流程:
      1. WebSocket 接收实时订单簿
      2. 策略引擎评估 → 生成信号
      3. 风控检查
      4. 执行入场/出场订单
      5. 检查对冲需求
      6. 定期清理超时订单

    使用方式:
        cfg = load_config()
        svc = TradingService(cfg)
        await svc.start()
    """

    def __init__(self, settings) -> None:
        """
        Args:
            settings: AppSettings 配置对象
        """
        self.cfg = settings
        self.symbol: str = settings.strategy.symbol

        # ── 交易所层 ──────────────────────────────────
        self._client: BinanceClient = BinanceClient(
            api_key=settings.binance_api_key,
            secret_key=settings.binance_secret_key,
            testnet=settings.exchange.testnet,
        )
        self.account: AccountAPI = AccountAPI(self._client)
        self.market: MarketAPI = MarketAPI(self._client)
        self.orders: OrderAPI = OrderAPI(self._client)
        self.stream: MarketStream = MarketStream(
            symbol=self.symbol.lower(),
            ws_url=settings.exchange.ws_url,
        )

        # ── 策略 + 风控 ───────────────────────────────
        self.strategy: EdgeStrategy = EdgeStrategy(
            trade_quantity=settings.strategy.trade_quantity,
            entry_threshold=settings.edge.entry_threshold,
            exit_threshold=settings.edge.exit_threshold,
            profit_target_pct=settings.risk.profit_target_pct,
            max_hold_minutes=settings.risk.max_hold_minutes,
            base_hedge_ratio=settings.futures.base_hedge_ratio,
        )
        self.risk: RiskManager = RiskManager(
            max_position_size=settings.risk.max_position_size,
            max_net_exposure=settings.risk.max_net_exposure,
            max_drawdown_pct=settings.risk.max_drawdown_pct,
            daily_loss_limit=settings.risk.daily_loss_limit,
            order_timeout=settings.risk.order_timeout,
            max_slippage_bps=settings.risk.max_slippage_bps,
            max_concurrent_orders=settings.risk.max_concurrent_orders,
            min_order_value=settings.risk.min_order_value,
            # V1.1 增强风控参数
            single_trade_risk_pct=settings.risk.single_trade_risk_pct,
            max_position_pct=settings.risk.max_position_pct,
            daily_loss_pct=settings.risk.daily_loss_pct,
            consecutive_loss_limit=settings.risk.consecutive_loss_limit,
            pause_minutes=settings.risk.pause_minutes,
        )
        self.hedge: HedgeManager = HedgeManager(
            hedge_ratio=settings.hedge.hedge_ratio,
            hedge_threshold_bps=settings.hedge.hedge_threshold_bps,
            unwind_threshold_bps=settings.hedge.unwind_threshold_bps,
        )
        self.order_mgr: OrderManager = OrderManager(self.symbol)
        self.db: Database = Database(settings.database.path)

        # ── 运行时状态 ────────────────────────────────
        self._running: bool = False
        self._last_ob: Optional[OrderBook] = None
        self._ob_updated: asyncio.Event = asyncio.Event()
        self._requote_timeout: int = settings.risk.requote_timeout  # V1.1: 重报价超时
        self._agg_trades: list = []  # V1.1: 收集最近 AggTrade 供 EdgeCalculator 使用
        self._last_kline: Optional[dict] = None  # V1.1: 最近 K 线

    # ═══════════════════════════════════════════════════
    # 生命周期
    # ═══════════════════════════════════════════════════

    async def start(self) -> None:
        """启动交易服务"""
        self._print_banner()

        # 初始化数据库
        self.db.create_tables()

        # 注册 WebSocket 回调
        self.stream.on_orderbook(self._on_orderbook)
        self.stream.on_kline(self._on_kline)
        self.stream.on_agg_trade(self._on_agg_trade)

        # 注册事件处理器
        event_bus.subscribe(EventType.ORDER_FILLED, self._on_order_filled)
        event_bus.subscribe(EventType.RISK_BREACH, self._on_risk_breach)

        # 启动基础设施
        await event_bus.start()
        await self._init_account()

        # 启动行情流（含 depth + kline + aggTrade 三个流）
        await self.stream.start()

        # 进入主循环
        self._running = True
        await self._main_loop()

    async def stop(self) -> None:
        """停止交易服务（安全退出）"""
        logger.info("正在安全退出...")
        self._running = False

        # 撤销所有挂单
        try:
            open_orders = await self.orders.get_open_orders(self.symbol)
            for o in open_orders:
                await self.orders.cancel_order(self.symbol, o.order_id)
            logger.info(f"已撤销 {len(open_orders)} 个挂单")
        except Exception as e:
            logger.error(f"撤销挂单异常: {e}")

        # 停止基础设施
        await self.stream.stop()
        await event_bus.stop()
        await self._client.close()

        logger.info("交易服务已安全停止")

    def _print_banner(self) -> None:
        """打印启动横幅"""
        logger.info("=" * 60)
        logger.info("  BTC Spot Bot — V1.1 Edge Score")
        logger.info(f"  交易对: {self.symbol}")
        logger.info(f"  环境:   {'测试网' if self.cfg.exchange.testnet else '⚠️ 主网'}")
        logger.info(f"  入场 Edge: ≥ {self.cfg.edge.entry_threshold}")
        logger.info(f"  出场 Edge: ≤ {self.cfg.edge.exit_threshold}")
        logger.info(f"  交易量: {self.cfg.strategy.trade_quantity} BTC")
        logger.info(f"  止盈:   {self.cfg.risk.profit_target_pct}%")
        logger.info(f"  对冲:   {'启用' if self.cfg.hedge.enabled else '禁用'}")
        logger.info(f"  风控:   单笔≤{self.cfg.risk.single_trade_risk_pct}% | 仓位≤{self.cfg.risk.max_position_pct}% | 日损≤{self.cfg.risk.daily_loss_pct}%")
        logger.info("=" * 60)

    async def _init_account(self) -> None:
        """初始化账户状态"""
        try:
            btc = await self.account.get_balance("BTC")
            usdt = await self.account.get_balance("USDT")
            logger.info(f"账户余额 | BTC={btc:.6f} | USDT={usdt:.2f}")

            # 估算总权益
            ticker = await self.market.get_ticker(self.symbol)
            equity = usdt + btc * ticker.last_price
            self.risk.update_equity(equity)
            logger.info(f"估算权益 | {equity:.2f} USDT")
        except Exception as e:
            logger.warning(f"账户初始化失败（可能需要有效 API Key）: {e}")

    # ═══════════════════════════════════════════════════
    # WebSocket 回调
    # ═══════════════════════════════════════════════════

    def _on_orderbook(self, orderbook: OrderBook) -> None:
        """订单簿更新回调 — 唤醒主循环"""
        self._last_ob = orderbook
        self._ob_updated.set()

    def _on_kline(self, kline_data: dict) -> None:
        """K线更新回调"""
        self._last_kline = kline_data
        if kline_data.get("is_closed"):
            logger.debug(
                f"K线闭合 | O={kline_data['open']:.2f} C={kline_data['close']:.2f} "
                f"V={kline_data['volume']:.4f}"
            )

    def _on_agg_trade(self, trade) -> None:
        """AggTrade 逐笔成交回调 — 收集数据供 EdgeCalculator 使用"""
        self._agg_trades.append(trade)
        # 保持最近 200 条
        if len(self._agg_trades) > 200:
            self._agg_trades = self._agg_trades[-200:]

    # ═══════════════════════════════════════════════════
    # 事件回调
    # ═══════════════════════════════════════════════════

    async def _on_order_filled(self, event: Event) -> None:
        """订单成交事件 — 更新策略状态"""
        order = event.data.get("order")
        if order is None:
            return

        if not self.strategy.in_position:
            # 入场成交
            signal = event.data.get("signal")
            self.strategy.on_entry_filled(order, signal)
            self.db.save_trade(
                symbol=order.symbol,
                side=order.side.value,
                direction=self.strategy.position.side.value if self.strategy.position else "NONE",
                quantity=order.filled_qty,
                entry_price=order.price,
                spread_at_entry=event.data.get("spread", 0.0),
                zscore_at_entry=event.data.get("zscore", 0.0),
            )
        else:
            # 出场成交
            self.strategy.on_exit_filled(order)
            if self.strategy.position:
                pnl = self.strategy.position.realized_pnl
                self.risk.add_pnl(pnl)
                # V1.1: 通知风控模块盈亏结果
                if pnl > 0:
                    self.risk.on_trade_win(pnl)
                else:
                    self.risk.on_trade_loss(pnl)
                logger.info(
                    f"💰 交易盈亏: {pnl:+.2f} USDT | 日累计: {self.risk.daily_pnl:+.2f} | "
                    f"连续亏损={self.risk.consecutive_losses}"
                )

    async def _on_risk_breach(self, event: Event) -> None:
        """风控违规事件 — 紧急撤销所有订单"""
        reason = event.data.get("reason", "未知原因")
        logger.warning(f"⚠️ 风控触发: {reason}")

        try:
            open_orders = await self.orders.get_open_orders(self.symbol)
            for o in open_orders:
                await self.orders.cancel_order(self.symbol, o.order_id)
                self.order_mgr.mark_canceled(o.order_id)
                self.risk.unregister_order(o.order_id)
        except Exception as e:
            logger.error(f"紧急撤单异常: {e}")

        self.db.save_risk_event(
            event_type="RISK_BREACH",
            reason=reason,
        )

    # ═══════════════════════════════════════════════════
    # 主循环
    # ═══════════════════════════════════════════════════

    async def _main_loop(self) -> None:
        """主交易循环"""
        # ── 预热阶段 ──────────────────────────────────
        logger.info("预热中 — 收集订单簿数据初始化 EdgeCalculator...")
        for _ in range(30):
            await asyncio.sleep(0.5)
            if self._last_ob is not None:
                # 用订单簿预热 EdgeCalculator 的 VWAP 窗口
                self.strategy.calculator.compute(
                    orderbook=self._last_ob,
                    agg_trades=list(self._agg_trades),
                    klines=[],
                )
        logger.info(f"预热完成 | EdgeCalculator 已初始化")

        # ── 主循环 ────────────────────────────────────
        tick_counter = 0
        while self._running:
            try:
                # 等待订单簿更新（30s 超时）
                await asyncio.wait_for(self._ob_updated.wait(), timeout=30.0)
                self._ob_updated.clear()

                ob = self._last_ob
                if ob is None:
                    continue

                tick_counter += 1

                # 1. 计算 Edge Score
                agg_trades_snapshot = list(self._agg_trades)
                klines = self._build_klines_from_cache()
                edge_scores = self.strategy.calculator.compute(
                    orderbook=ob,
                    agg_trades=agg_trades_snapshot,
                    klines=klines,
                )

                # 2. 生成交易信号
                signal = self.strategy.evaluate(edge_scores, ob)

                # 3. 处理入场/出场
                if signal.is_entry:
                    await self._handle_entry(signal, ob)
                elif signal.is_exit:
                    await self._handle_exit(signal, ob)

                # 4. 对冲检查（每 5 个 tick）
                if self.cfg.hedge.enabled and tick_counter % 5 == 0:
                    await self._check_hedge(ob)

                # 5. V1.1: 动态重报价检查（每 tick）
                await self._check_dynamic_requote(ob)

                # 6. 定期清理（每 10 个 tick）
                if tick_counter >= 10:
                    await self._periodic_cleanup()
                    tick_counter = 0

            except asyncio.TimeoutError:
                logger.warning("OrderBook 更新超时 (30s) — 检查 WebSocket 连接")
            except Exception as e:
                error_msg = str(e)
                logger.error(f"主循环异常: {error_msg}", exc_info=True)
                # V1.1: API/网络异常 → 进入安全模式
                if any(kw in error_msg.lower() for kw in (
                    "connection", "timeout", "dns", "refused", "reset",
                    "4", "5",  # HTTP 4xx/5xx
                    "api", "server", "internal",
                )):
                    self.risk.enter_safe_mode(f"主循环异常: {error_msg[:100]}")
                    await self._cancel_all_orders()
                await asyncio.sleep(1)

    def _build_klines_from_cache(self) -> list:
        """
        从缓存的 K 线数据构造 Kline 对象列表

        供 EdgeCalculator 的动量指标和波动率过滤使用。
        回测中数据完整，实盘中用 WebSocket 接收的 K 线数据近似。
        """
        klines = []
        if self._last_kline is not None:
            from app.core.types import Kline
            from datetime import datetime, timezone
            kline = Kline(
                symbol=self.symbol,
                interval="5m",
                open_time=datetime.fromtimestamp(
                    self._last_kline.get("open_time", 0) / 1000.0, tz=timezone.utc
                ) if self._last_kline.get("open_time", 0) > 0 else datetime.now(timezone.utc),
                open=float(self._last_kline.get("open", 0)),
                high=float(self._last_kline.get("high", 0)),
                low=float(self._last_kline.get("low", 0)),
                close=float(self._last_kline.get("close", 0)),
                volume=float(self._last_kline.get("volume", 0)),
            )
            klines.append(kline)
        return klines

    # ═══════════════════════════════════════════════════
    # 入场/出场/对冲/清理
    # ═══════════════════════════════════════════════════

    async def _handle_entry(self, signal, ob: OrderBook) -> None:
        """
        处理入场信号

        流程: 策略判断 → 风控检查 → 计算限价 → 下单 → 注册追踪
        """
        should_enter, side, limit_price = self.strategy.should_enter(signal)
        if not should_enter or side is None:
            return

        # 风控检查 (V1.1: 传入权益和当前价格以支持百分比风控)
        current_equity = self.risk.daily_start_equity + self.risk.daily_pnl
        risk_result = self.risk.check_entry(
            signal, self.strategy.position,
            equity=current_equity,
            current_price=ob.mid_price,
        )
        if not risk_result.allowed:
            logger.info(f"入场被风控拒绝: {risk_result.reason}")
            self.db.save_risk_event(
                event_type="ENTRY_REJECTED",
                reason=risk_result.reason,
            )
            return

        # 计算限价（含订单簿深度 + 滑点保护）
        final_price = self.order_mgr.calc_limit_price(
            ob, side, self.cfg.risk.max_slippage_bps,
        )

        # 创建订单
        order = self.order_mgr.create_limit(
            side, final_price, self.cfg.strategy.trade_quantity,
        )

        try:
            exchange_order = await self.orders.create_order(
                symbol=self.symbol,
                side=side,
                order_type=order.type,
                quantity=order.quantity,
                price=final_price,
                client_order_id=order.client_order_id,
            )
            self.order_mgr.register(order, exchange_order.order_id)
            self.risk.register_order(exchange_order.order_id)

            logger.info(
                f"📊 入场信号 | {side.value} {order.quantity} BTC @ {final_price:.2f} | "
                f"Z={signal.spread_stats.z_score:.2f} | {signal.reason}"
            )

            # 发布事件
            await event_bus.publish(Event(
                EventType.ORDER_CREATED,
                {"order": exchange_order, "signal": signal,
                 "spread": signal.spread_stats.current_spread,
                 "zscore": signal.spread_stats.z_score},
            ))

        except Exception as e:
            logger.error(f"入场下单失败: {e}")
            # V1.1: API异常 → 进入安全模式
            if any(kw in str(e).lower() for kw in ("connection", "timeout", "4", "5", "api", "server")):
                self.risk.enter_safe_mode(f"入场下单异常: {str(e)[:100]}")
                await self._cancel_all_orders()

    async def _handle_exit(self, signal, ob: OrderBook) -> None:
        """
        处理出场信号

        流程: 撤销所有挂单 → 下出场限价单 → 等待成交
        """
        if not self.strategy.should_exit(signal):
            return

        try:
            # 出场始终卖出，以 best_bid 为限价（确保成交）
            side = OrderSide.SELL
            price = ob.best_bid

            # 先撤销所有挂单
            open_orders = await self.orders.get_open_orders(self.symbol)
            for o in open_orders:
                await self.orders.cancel_order(self.symbol, o.order_id)
                self.order_mgr.mark_canceled(o.order_id)
                self.risk.unregister_order(o.order_id)

            # 下出场单
            order = self.order_mgr.create_limit(
                side, price, self.cfg.strategy.trade_quantity,
            )

            exchange_order = await self.orders.create_order(
                symbol=self.symbol,
                side=side,
                order_type=order.type,
                quantity=order.quantity,
                price=price,
                client_order_id=order.client_order_id,
            )

            self.order_mgr.register(order, exchange_order.order_id)

            exit_reason_str = signal.exit_reason.value if signal.exit_reason else "UNKNOWN"
            logger.info(f"📉 出场信号 | Edge={signal.edge_scores.edge if signal.edge_scores else '?'} | {signal.reason}")

            # 注意：生产环境应监听 WebSocket 用户数据流等待成交确认
            # 此处为简化处理，模拟成交后触发回调
            order.status = OrderStatus.FILLED
            order.filled_qty = order.quantity
            await self._on_order_filled(Event(
                EventType.ORDER_FILLED,
                {"order": order, "exit_reason": exit_reason_str},
            ))

        except Exception as e:
            logger.error(f"出场处理失败: {e}")

    async def _check_hedge(self, ob: OrderBook) -> None:
        """检查并执行对冲/解除对冲"""
        # 先检查是否需要解除对冲
        unwind_signal = self.hedge.should_unwind_hedge(self.strategy.position, ob)
        if unwind_signal.required:
            try:
                await self.orders.create_order(
                    symbol=self.symbol,
                    side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    quantity=unwind_signal.quantity,
                    price=unwind_signal.limit_price,
                )
                logger.info(f"🔓 {unwind_signal.reason}")
            except Exception as e:
                logger.error(f"解除对冲失败: {e}")
            return

        # 检查是否需要新增对冲
        hedge_signal = self.hedge.evaluate(self.strategy.position, ob)
        if not hedge_signal.required:
            return

        side = OrderSide.SELL  # 始终卖出对冲
        price = hedge_signal.limit_price if hedge_signal.limit_price > 0 else ob.best_bid

        try:
            await self.orders.create_order(
                symbol=self.symbol,
                side=side,
                order_type=OrderType.LIMIT,
                quantity=hedge_signal.quantity,
                price=price,
            )
            logger.info(f"🛡️ {hedge_signal.reason}")
        except Exception as e:
            logger.error(f"对冲下单失败: {e}")

    async def _check_dynamic_requote(self, ob: OrderBook) -> None:
        """
        V1.1 动态重报价 — 挂单超时未成交时取消旧单并按最新行情重报

        逻辑:
          1. 扫描活跃订单，找出超过 requote_timeout 未成交的
          2. 检查重报价次数是否达到上限 (MAX_REQUOTES=5)
          3. 取消原订单 → 按最新订单簿重新计算限价 → 重新下单
          4. 新订单继承原订单的 trade_side 和 client_order_id 前缀

        安全约束:
          - 最多重报价 5 次，超过后不再重报（避免无限循环）
          - 安全模式下不重报价
          - 暂停状态下不重报价
        """
        # 安全模式/暂停状态 → 不重报价
        if self.risk.in_safe_mode or self.risk.is_paused:
            return

        candidates = self.order_mgr.get_requote_candidates(self._requote_timeout)
        if not candidates:
            return

        for old_order in candidates:
            try:
                # 记录旧价格用于日志
                old_price = old_order.price
                old_id = old_order.order_id

                # 1. 取消原订单
                await self.orders.cancel_order(self.symbol, old_id)
                self.order_mgr.mark_canceled(old_id)
                self.risk.unregister_order(old_id)

                # 2. 按最新订单簿重新计算限价
                new_price = self.order_mgr.calc_limit_price(
                    ob, old_order.side, self.cfg.risk.max_slippage_bps,
                )

                # 3. 检查滑点 — 新价格与原价格差异不能太大
                if old_price > 0:
                    price_change_bps = abs(new_price - old_price) / old_price * 10000.0
                    if price_change_bps > self.cfg.risk.max_slippage_bps * 3:
                        logger.warning(
                            f"重报价价格偏离过大 ({price_change_bps:.1f} bps)，跳过 | "
                            f"old={old_price:.2f} new={new_price:.2f}"
                        )
                        continue

                # 4. 创建新订单（复用旧订单的 trade_side 等属性）
                new_order = self.order_mgr.create_limit(
                    old_order.side, new_price, old_order.quantity,
                )
                new_order.requote_count = old_order.requote_count + 1
                if old_order.original_price <= 0:
                    new_order.original_price = old_price
                else:
                    new_order.original_price = old_order.original_price

                # 5. 下单
                exchange_order = await self.orders.create_order(
                    symbol=self.symbol,
                    side=new_order.side,
                    order_type=new_order.type,
                    quantity=new_order.quantity,
                    price=new_price,
                    client_order_id=new_order.client_order_id,
                )
                self.order_mgr.register(new_order, exchange_order.order_id)
                self.risk.register_order(exchange_order.order_id)

                # 6. 日志
                orig_info = f" 原始价={new_order.original_price:.2f}" if new_order.original_price > 0 else ""
                logger.info(
                    f"🔄 动态重报价 #{new_order.requote_count}/{self.order_mgr.MAX_REQUOTES} | "
                    f"旧单={old_id} | {old_order.side.value} | "
                    f"旧价={old_price:.2f} → 新价={new_price:.2f}{orig_info} | "
                    f"挂单 {old_order.age_seconds:.0f}s 未成交"
                )

                # 发布重报价事件
                await event_bus.publish(Event(
                    EventType.ORDER_REQUOTED,
                    {
                        "old_order_id": old_id,
                        "new_order_id": exchange_order.order_id,
                        "old_price": old_price,
                        "new_price": new_price,
                        "requote_count": new_order.requote_count,
                        "side": new_order.side.value,
                    },
                ))

            except Exception as e:
                logger.error(f"重报价失败 (order={old_order.order_id}): {e}")
                # 单次重报价失败不进入安全模式，继续处理下一个

    async def _periodic_cleanup(self) -> None:
        """定期清理：超时撤单 + 状态日志"""
        try:
            # 超时撤单
            expired = self.order_mgr.get_expired(self.cfg.risk.order_timeout)
            for order in expired:
                await self.orders.cancel_order(self.symbol, order.order_id)
                self.order_mgr.mark_canceled(order.order_id)
                self.risk.unregister_order(order.order_id)
                logger.info(f"⏰ 超时撤单 | id={order.order_id} | 已挂单超 {self.cfg.risk.order_timeout}s")

            # 状态日志
            active_orders = len(self.order_mgr.get_active())
            logger.debug(
                f"状态 | state={self.strategy.state.value} | "
                f"active_orders={active_orders} | "
                f"daily_pnl={self.risk.daily_pnl:+.2f} | "
                f"drawdown={self.risk.drawdown_pct*100:.2f}%"
            )

        except Exception as e:
            logger.error(f"定期清理异常: {e}")

    async def _cancel_all_orders(self) -> None:
        """安全模式：撤销所有活跃订单"""
        try:
            open_orders = await self.orders.get_open_orders(self.symbol)
            for o in open_orders:
                await self.orders.cancel_order(self.symbol, o.order_id)
                self.order_mgr.mark_canceled(o.order_id)
                self.risk.unregister_order(o.order_id)
            if open_orders:
                logger.info(f"🚨 安全模式: 已撤销 {len(open_orders)} 个挂单")
        except Exception as e:
            logger.error(f"安全模式撤单异常: {e}")


# ═══════════════════════════════════════════════════════
# 独立测试入口
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    from datetime import datetime, timezone

    print("=" * 50)
    print("服务层 — 独立测试")
    print("=" * 50)

    # ── 测试 OrderManager ─────────────────────────────
    mgr = OrderManager("BTCUSDT")

    # 创建限价单
    order = mgr.create_limit(OrderSide.BUY, 65000.0, 0.001)
    assert order.symbol == "BTCUSDT"
    assert order.side == OrderSide.BUY
    assert order.price == 65000.0
    assert order.quantity == 0.001
    assert order.client_order_id.startswith("sfn_")
    print(f"✅ 限价单创建: client_id={order.client_order_id}")

    # 注册订单
    mgr.register(order, "exchange_12345")
    assert mgr.get("exchange_12345") is order
    assert len(mgr.get_active()) == 1
    print("✅ 订单注册/查询正常")

    # 超时检测
    assert len(mgr.get_expired(999999)) == 0  # 不会超时
    print("✅ 超时检测正常")

    # 取消
    mgr.mark_canceled("exchange_12345")
    assert order.status == OrderStatus.CANCELED
    assert len(mgr.get_active()) == 0
    print("✅ 撤单标记正常")

    # ── 测试限价计算 ──────────────────────────────────
    ob = OrderBook(
        symbol="BTCUSDT",
        bids=[(65000.0, 1.0), (64999.5, 2.0), (64999.0, 3.0)],
        asks=[(65001.0, 0.5), (65002.0, 1.5), (65003.0, 2.0)],
    )

    buy_price = OrderManager.calc_limit_price(ob, OrderSide.BUY, slippage_bps=5)
    sell_price = OrderManager.calc_limit_price(ob, OrderSide.SELL, slippage_bps=5)

    assert buy_price > ob.mid_price  # 买单略高于中间价
    assert sell_price < ob.mid_price  # 卖单略低于中间价
    print(f"✅ 限价计算: buy={buy_price:.2f} | mid={ob.mid_price:.2f} | sell={sell_price:.2f}")

    # ── 测试动态重报价 (V1.1) ────────────────────────
    print("\n── 动态重报价测试 ──")

    # 创建订单并验证 requote 追踪
    order2 = mgr.create_limit(OrderSide.BUY, 65000.0, 0.001)
    assert order2.requote_count == 0
    assert order2.original_price == 0.0
    assert mgr.can_requote(order2)  # 新订单应可重报价
    print(f"✅ 新订单: requote_count={order2.requote_count}, can_requote={mgr.can_requote(order2)}")

    # 注册后模拟重报价递增
    mgr.register(order2, "exchange_99999")
    mgr.increment_requote(order2)
    assert order2.requote_count == 1
    assert mgr.can_requote(order2)
    print(f"✅ 重报价 #1: requote_count={order2.requote_count}")

    # 模拟达到上限
    order2.requote_count = 5
    assert not mgr.can_requote(order2)  # 达到上限
    print(f"✅ 重报价上限: can_requote={mgr.can_requote(order2)} (上限={mgr.MAX_REQUOTES})")

    # 已取消的订单不可重报价
    mgr.mark_canceled("exchange_99999")
    assert not mgr.can_requote(order2)
    print(f"✅ 已取消订单: can_requote={mgr.can_requote(order2)}")

    # get_requote_candidates: 新创建的活跃订单 + 短超时 → 应为候选
    order3 = mgr.create_limit(OrderSide.BUY, 65000.0, 0.001)
    mgr.register(order3, "exchange_candidate")
    import time
    time.sleep(0.1)  # 等待一小段时间
    candidates = mgr.get_requote_candidates(requote_timeout=0)  # timeout=0 立即超时
    assert len(candidates) >= 1
    print(f"✅ 重报价候选: {len(candidates)} 个订单")

    print("\n全部测试通过! ✅ (含 V1.1 动态重报价)")
    print("(TradingService 完整测试需要 mock exchange，建议用集成测试)")
