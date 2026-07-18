"""
BTC Spot Bot — 核心类型定义

本模块定义所有模块共享的枚举、数据类和事件类型。
纯 Python 实现，不依赖任何第三方 SDK。

策略定位: BTC 现货 Edge Score 多因子买入 + USDⓈ-M 永续合约动态对冲
  - 5因子: 买卖盘失衡(30%) + VWAP偏离(20%) + 成交流向(20%) + 动量(15%) + 波动率过滤(15%)
  - Edge ≥ 70: 限价买入 BTC 现货
  - 0.8-1.0x 永续合约做空动态对冲
  - 资金费率驱动的对冲比例自适应
  - 增强风控: 安全模式 / 连续亏损暂停 / 动态重报价

模块可独立测试: python -m app.core.types
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Optional


# ═══════════════════════════════════════════════════════
# 枚举类型
# ═══════════════════════════════════════════════════════

class Direction(str, Enum):
    """交易方向"""
    LONG = "LONG"
    SHORT = "SHORT"
    NONE = "NONE"


class SignalType(str, Enum):
    """交易信号类型 (基于 Edge Score)"""
    ENTRY = "ENTRY"      # Edge ≥ 70 → 限价买入现货
    EXIT = "EXIT"        # Edge ≤ 40 / 止盈 / 超时 → 平仓
    HEDGE = "HEDGE"      # 永续合约对冲调整
    NONE = "NONE"


class TradeSide(str, Enum):
    """交易侧标记"""
    PRIMARY = "PRIMARY"  # 主策略现货单
    HEDGE = "HEDGE"      # 永续合约对冲单


class ExitReason(str, Enum):
    """出场原因"""
    EDGE_BELOW_THRESHOLD = "EDGE_BELOW_THRESHOLD"
    PROFIT_TARGET = "PROFIT_TARGET"
    SIGNAL_REVERSAL = "SIGNAL_REVERSAL"
    MAX_HOLD_TIME = "MAX_HOLD_TIME"
    RISK_STOP = "RISK_STOP"
    MANUAL = "MANUAL"


class OrderSide(str, Enum):
    """订单买卖方向"""
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    """订单类型"""
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class OrderStatus(str, Enum):
    """订单状态"""
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


class PositionStatus(str, Enum):
    """持仓状态"""
    OPEN = "OPEN"
    HEDGED = "HEDGED"
    CLOSED = "CLOSED"


class BotState(str, Enum):
    """机器人状态机"""
    IDLE = "IDLE"
    AWAITING_ENTRY = "AWAITING_ENTRY"
    IN_POSITION = "IN_POSITION"
    AWAITING_HEDGE = "AWAITING_HEDGE"
    HEDGED = "HEDGED"
    AWAITING_EXIT = "AWAITING_EXIT"
    SAFE_MODE = "SAFE_MODE"       # API异常/网络错误
    PAUSED = "PAUSED"             # 连续亏损暂停
    EMERGENCY = "EMERGENCY"


class EventType(Enum):
    """事件类型"""
    SPREAD_UPDATE = auto()
    AGGTRADE_UPDATE = auto()
    KLINE_UPDATE = auto()
    MARK_PRICE_UPDATE = auto()
    FUNDING_RATE_UPDATE = auto()
    EDGE_SCORE_COMPUTED = auto()
    SIGNAL_GENERATED = auto()
    ORDER_CREATED = auto()
    ORDER_FILLED = auto()
    ORDER_CANCELED = auto()
    ORDER_EXPIRED = auto()
    ORDER_REQUOTED = auto()
    POSITION_OPENED = auto()
    POSITION_CLOSED = auto()
    HEDGE_TRIGGERED = auto()
    HEDGE_UNWOUND = auto()
    HEDGE_ADJUSTED = auto()
    RISK_BREACH = auto()
    SAFE_MODE_ENTER = auto()
    SAFE_MODE_EXIT = auto()
    TRADING_PAUSED = auto()
    TRADING_RESUMED = auto()
    DAILY_RESET = auto()
    STATE_CHANGE = auto()
    ERROR = auto()


# ═══════════════════════════════════════════════════════
# 行情数据结构
# ═══════════════════════════════════════════════════════

@dataclass
class Ticker:
    """实时行情 — 对应币安 bookTicker"""
    symbol: str
    bid: float
    ask: float
    bid_qty: float = 0.0
    ask_qty: float = 0.0
    last_price: float = 0.0
    volume_24h: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class OrderBook:
    """订单簿深度 — 对应币安 depth 流"""
    symbol: str
    bids: list[tuple[float, float]] = field(default_factory=list)  # [(价格,数量)] 降序
    asks: list[tuple[float, float]] = field(default_factory=list)  # [(价格,数量)] 升序
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else 0.0

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    @property
    def spread_bps(self) -> float:
        mid = self.mid_price
        return (self.spread / mid) * 10000.0 if mid > 0.0 else 0.0

    @property
    def mid_price(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0

    def bid_volume(self, levels: int = 10) -> float:
        """前 N 档买单总成交量"""
        return sum(q for _, q in self.bids[:levels])

    def ask_volume(self, levels: int = 10) -> float:
        """前 N 档卖单总成交量"""
        return sum(q for _, q in self.asks[:levels])


@dataclass
class Kline:
    """K线数据 — 对应币安 kline 流"""
    symbol: str
    interval: str
    open_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: Optional[datetime] = None


@dataclass
class AggTrade:
    """逐笔成交 — 对应币安 @aggTrade 流"""
    symbol: str
    price: float
    quantity: float
    is_buyer_maker: bool     # True=主动卖出, False=主动买入
    trade_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class MarkPriceData:
    """永续合约标记价格 — 对应 @markPrice 流"""
    symbol: str
    mark_price: float
    index_price: float
    estimated_settle_price: float = 0.0
    funding_rate: float = 0.0         # 当前资金费率
    next_funding_time: Optional[datetime] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class FundingRateData:
    """资金费率数据"""
    symbol: str
    funding_rate: float               # 当前资金费率
    funding_countdown: float = 0.0    # 距下次结算剩余秒数
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class OpenInterestData:
    """未平仓量数据"""
    symbol: str
    open_interest: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ═══════════════════════════════════════════════════════
# 策略信号与统计
# ═══════════════════════════════════════════════════════

@dataclass
class SpreadStats:
    """价差统计 (保留兼容旧模块)"""
    current_spread: float
    current_spread_bps: float
    rolling_mean: float
    rolling_std: float
    z_score: float
    percentile: float = 50.0
    sample_count: int = 0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class EdgeScores:
    """
    Edge Score 综合评分 (0-100)

    5因子加权:
      ob_imbalance (30%): 买卖盘失衡 — 买方力量越强分数越高
      vwap_deviation (20%): VWAP偏离 — 价格低于VWAP时买方有利
      trade_flow (20%): 成交流向 — 主动买入占比越高越好
      momentum (15%): 5分钟动量 — 正动量 = 趋势有利
      volatility_filter (15%): 波动率过滤 — 低波动环境更可靠
    """
    edge: float                      # 综合 Edge Score
    ob_imbalance: float              # 买卖盘失衡 (0-100)
    vwap_deviation: float            # VWAP偏离 (0-100)
    trade_flow: float                # 成交流向 (0-100)
    momentum: float                  # 动量 (0-100)
    volatility_filter: float         # 波动率过滤 (0-100)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_bullish(self) -> bool:
        """是否偏多"""
        return self.edge >= 70.0

    @property
    def is_bearish(self) -> bool:
        """是否偏空 (应出场)"""
        return self.edge <= 40.0

    @property
    def is_neutral(self) -> bool:
        """是否中性 (不交易)"""
        return 40.0 < self.edge < 70.0

    def to_dict(self) -> dict[str, float]:
        """转为字典 (用于日志/存储)"""
        return {
            "edge": self.edge,
            "ob_imbalance": self.ob_imbalance,
            "vwap_deviation": self.vwap_deviation,
            "trade_flow": self.trade_flow,
            "momentum": self.momentum,
            "volatility_filter": self.volatility_filter,
        }


@dataclass
class EdgeConfig:
    """Edge Score 可调配置"""
    ob_imbalance_weight: float = 0.30
    vwap_deviation_weight: float = 0.20
    trade_flow_weight: float = 0.20
    momentum_weight: float = 0.15
    volatility_filter_weight: float = 0.15
    entry_threshold: float = 70.0
    exit_threshold: float = 40.0
    ob_depth_levels: int = 10
    vwap_window: int = 20
    trade_flow_window: int = 50
    atr_period: int = 14


@dataclass
class Signal:
    """
    交易信号 — Edge Score 版本

    语义:
      ENTRY: Edge ≥ 70 → 限价买入 BTC 现货
      EXIT:  Edge ≤ 40 / 止盈 / 超时 → 卖出平仓
      HEDGE: 永续合约对冲比例调整
    """
    type: SignalType
    direction: Direction
    confidence: float
    spread_stats: SpreadStats = field(default_factory=lambda: SpreadStats(0,0,0,0,0))
    limit_price: float = 0.0
    reason: str = ""
    trade_side: TradeSide = TradeSide.PRIMARY
    expected_return_bps: float = 0.0
    edge_scores: Optional[EdgeScores] = None    # Edge 综合评分
    exit_reason: Optional[ExitReason] = None     # 出场原因 (仅 EXIT 信号)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_entry(self) -> bool:
        return self.type == SignalType.ENTRY

    @property
    def is_exit(self) -> bool:
        return self.type == SignalType.EXIT

    @property
    def is_hedge(self) -> bool:
        return self.type == SignalType.HEDGE


# ═══════════════════════════════════════════════════════
# 订单与持仓
# ═══════════════════════════════════════════════════════

@dataclass
class Order:
    """订单对象"""
    order_id: str
    symbol: str
    side: OrderSide
    type: OrderType
    price: float
    quantity: float
    status: OrderStatus = OrderStatus.NEW
    filled_qty: float = 0.0
    filled_quote_qty: float = 0.0
    client_order_id: str = ""
    requote_count: int = 0               # V1.1: 重报价次数
    original_price: float = 0.0          # V1.1: 初始挂单价格（用于日志对比）
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_active(self) -> bool:
        return self.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)

    @property
    def is_filled(self) -> bool:
        return self.status == OrderStatus.FILLED

    @property
    def age_seconds(self) -> float:
        """订单存在时长 (秒)"""
        return (datetime.now(timezone.utc) - self.created_at).total_seconds()


@dataclass
class Position:
    """
    持仓对象

    spot + futures 联合追踪:
      quantity: 现货持仓量 (BTC)
      futures_hedge_qty: 永续合约做空数量 (BTC)
      net_exposure: 净敞口 = quantity - futures_hedge_qty
    """
    symbol: str
    side: Direction
    quantity: float
    entry_price: float
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    hedge_quantity: float = 0.0          # (保留兼容) 现货对冲量
    futures_hedge_qty: float = 0.0       # 永续合约做空数量
    hedge_entry_price: float = 0.0       # 永续合约入场均价
    hedge_positions: list[Position] = field(default_factory=list)
    status: PositionStatus = PositionStatus.OPEN
    trade_side: TradeSide = TradeSide.PRIMARY
    entry_edge_score: float = 0.0        # 入场时 Edge Score
    entry_spread_bps: float = 0.0
    entry_zscore: float = 0.0
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closed_at: Optional[datetime] = None

    @property
    def is_hedged(self) -> bool:
        return self.futures_hedge_qty > 0.0 or self.hedge_quantity > 0.0

    @property
    def net_exposure(self) -> float:
        """净敞口 = 现货多 - 合约空"""
        return self.quantity - self.futures_hedge_qty - self.hedge_quantity

    @property
    def hedge_ratio_pct(self) -> float:
        if self.quantity <= 0:
            return 0.0
        return (self.futures_hedge_qty + self.hedge_quantity) / self.quantity * 100.0

    @property
    def total_cost(self) -> float:
        return self.entry_price * self.quantity

    @property
    def return_bps(self) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (self.current_price - self.entry_price) / self.entry_price * 10000.0

    @property
    def return_pct(self) -> float:
        """收益率 (%)"""
        if self.entry_price <= 0:
            return 0.0
        return (self.current_price - self.entry_price) / self.entry_price * 100.0

    @property
    def holding_minutes(self) -> float:
        """持仓时长 (分钟)"""
        return (datetime.now(timezone.utc) - self.opened_at).total_seconds() / 60.0

    def update_unrealized_pnl(self, current_price: float) -> None:
        self.current_price = current_price
        if self.side == Direction.LONG:
            self.unrealized_pnl = (current_price - self.entry_price) * self.quantity
        else:
            self.unrealized_pnl = (self.entry_price - current_price) * self.quantity

    def add_hedge(self, hedge_pos: Position) -> None:
        self.hedge_positions.append(hedge_pos)
        self.hedge_quantity += hedge_pos.quantity
        if self.status == PositionStatus.OPEN:
            self.status = PositionStatus.HEDGED


@dataclass
class Portfolio:
    """账户组合"""
    balances: dict[str, float] = field(default_factory=dict)
    positions: list[Position] = field(default_factory=list)
    total_equity_usdt: float = 0.0
    daily_pnl: float = 0.0
    daily_start_equity: float = 0.0
    peak_equity: float = 0.0
    current_drawdown_pct: float = 0.0

    def get_balance(self, asset: str) -> float:
        return self.balances.get(asset, 0.0)


# ═══════════════════════════════════════════════════════
# 风控、对冲、配置
# ═══════════════════════════════════════════════════════

@dataclass
class RiskResult:
    """风控检查结果"""
    allowed: bool
    reason: str = ""
    max_allowed_qty: float = 0.0
    adjusted_price: float = 0.0


@dataclass
class HedgeSignal:
    """
    对冲信号

    direction:
      SHORT: 做空永续合约 → 增加对冲
      LONG:  平空永续合约 → 减少对冲
    """
    required: bool
    direction: Direction
    quantity: float
    limit_price: float = 0.0
    hedge_ratio: float = 0.0         # 目标对冲比例
    current_hedge_ratio: float = 0.0 # 当前对冲比例
    funding_rate: float = 0.0        # 当前资金费率
    reason: str = ""


@dataclass
class FuturesConfig:
    """永续合约配置"""
    enabled: bool = False
    testnet: bool = True
    leverage: int = 1
    margin_type: str = "ISOLATED"
    base_hedge_ratio: float = 0.9


@dataclass
class Event:
    """事件对象"""
    type: EventType
    data: dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# ═══════════════════════════════════════════════════════
# 独立测试入口
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 50)
    print("核心类型模块 — Edge Score 版本 独立测试")
    print("=" * 50)

    # ── 枚举 ────────────────────────────────────────
    assert Direction.LONG == "LONG"
    assert SignalType.ENTRY == "ENTRY"
    assert BotState.SAFE_MODE == "SAFE_MODE"
    assert BotState.PAUSED == "PAUSED"
    assert ExitReason.PROFIT_TARGET == "PROFIT_TARGET"
    print("✅ 枚举类型正常 (含 SAFE_MODE, PAUSED, ExitReason)")

    # ── EventType ───────────────────────────────────
    assert EventType.AGGTRADE_UPDATE is not None
    assert EventType.EDGE_SCORE_COMPUTED is not None
    assert EventType.MARK_PRICE_UPDATE is not None
    assert EventType.FUNDING_RATE_UPDATE is not None
    assert EventType.SAFE_MODE_ENTER is not None
    assert EventType.TRADING_PAUSED is not None
    assert EventType.ORDER_REQUOTED is not None
    print("✅ EventType 完整 (25个事件)")

    # ── OrderBook (含新方法) ────────────────────────
    ob = OrderBook(
        symbol="BTCUSDT",
        bids=[(65000.0, 1.0), (64999.5, 2.0), (64999.0, 3.0)],
        asks=[(65001.0, 0.5), (65002.0, 1.5), (65003.0, 2.0)],
    )
    assert ob.best_bid == 65000.0
    assert abs(ob.spread - 1.0) < 0.001
    assert ob.bid_volume(3) == 6.0
    assert ob.ask_volume(3) == 4.0
    print(f"✅ OrderBook: bid_vol={ob.bid_volume(2)}, ask_vol={ob.ask_volume(2)}")

    # ── AggTrade ────────────────────────────────────
    trade = AggTrade(
        symbol="btcusdt", price=65000.5, quantity=0.01,
        is_buyer_maker=False,  # 主动买入
    )
    assert not trade.is_buyer_maker  # taker buy
    print(f"✅ AggTrade: taker_buy={'是' if not trade.is_buyer_maker else '否'}")

    # ── MarkPriceData ───────────────────────────────
    mp = MarkPriceData(
        symbol="BTCUSDT", mark_price=65000.0, index_price=64999.5,
        funding_rate=0.0001,
    )
    assert mp.funding_rate == 0.0001
    print(f"✅ MarkPrice: 资金费率={mp.funding_rate*100:.2f}%")

    # ── EdgeScores ──────────────────────────────────
    es = EdgeScores(
        edge=75.0, ob_imbalance=80.0, vwap_deviation=70.0,
        trade_flow=75.0, momentum=65.0, volatility_filter=85.0,
    )
    assert es.is_bullish
    assert not es.is_bearish
    assert not es.is_neutral
    d = es.to_dict()
    assert len(d) == 6  # edge + 5 factor scores
    print(f"✅ EdgeScores: edge={es.edge} bullish={es.is_bullish}")

    # ── EdgeConfig ──────────────────────────────────
    ec = EdgeConfig()
    total_w = ec.ob_imbalance_weight + ec.vwap_deviation_weight + \
              ec.trade_flow_weight + ec.momentum_weight + ec.volatility_filter_weight
    assert abs(total_w - 1.0) < 0.001
    print(f"✅ EdgeConfig: 权重和={total_w}")

    # ── Signal (Edge Score 版本) ────────────────────
    sig = Signal(
        type=SignalType.ENTRY, direction=Direction.LONG,
        confidence=0.85, limit_price=65001.0,
        reason="Edge=75.0 ≥ 70, 买方优势入场",
        edge_scores=es, exit_reason=None,
    )
    assert sig.is_entry
    assert sig.edge_scores is not None
    assert sig.edge_scores.edge == 75.0
    print(f"✅ Signal: {sig.reason}")

    # ── Position (含新字段) ─────────────────────────
    pos = Position(
        symbol="BTCUSDT", side=Direction.LONG, quantity=0.001,
        entry_price=65000.0, entry_edge_score=75.0,
    )
    assert pos.net_exposure == 0.001
    assert pos.holding_minutes >= 0
    pos.futures_hedge_qty = 0.0008
    assert pos.is_hedged
    assert abs(pos.net_exposure - 0.0002) < 1e-9  # 浮点精度
    assert pos.hedge_ratio_pct == 80.0
    print(f"✅ Position: 净敞口={pos.net_exposure}, 对冲率={pos.hedge_ratio_pct:.0f}%")

    # ── FuturesConfig ───────────────────────────────
    fc = FuturesConfig(enabled=True, leverage=1, base_hedge_ratio=0.9)
    assert fc.leverage == 1
    assert fc.margin_type == "ISOLATED"
    print(f"✅ FuturesConfig: enabled={fc.enabled}")

    # ── HedgeSignal (新字段) ────────────────────────
    hs = HedgeSignal(
        required=True, direction=Direction.SHORT, quantity=0.0008,
        hedge_ratio=0.8, current_hedge_ratio=0.0,
        funding_rate=0.0001,
        reason="入场对冲: 做空 0.0008 BTC 永续合约",
    )
    assert hs.required
    assert hs.funding_rate > 0
    print(f"✅ HedgeSignal: {hs.reason}")

    print("\n全部测试通过! ✅")
