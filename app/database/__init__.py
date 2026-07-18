"""
数据库模块 — SQLAlchemy 2.x ORM 持久化层

表结构:
  - trades: 交易记录（入场/出场/盈亏）
  - signal_logs: 信号日志（Z-score、价差、置信度）
  - risk_events: 风控事件日志

使用 SQLAlchemy 2.x 的 Mapped 声明式映射，
仅使用 Column/mapped_column 等非废弃 API。

模块可独立测试: python -m app.database
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import create_engine, Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
)

# SQLAlchemy 2.x 列类型（不使用废弃的 Column 直接导入）
from sqlalchemy import DateTime, Float, Integer, String, Text


class Base(DeclarativeBase):
    """ORM 基类"""
    pass


class TradeRecord(Base):
    """
    交易记录表

    记录每笔交易从入场到出场的完整信息，
    包含入场/出场价格、数量、盈亏、信号 Z-score 等。
    """
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True, comment="交易对")
    side: Mapped[str] = mapped_column(String(10), comment="买卖方向 (BUY/SELL)")
    direction: Mapped[str] = mapped_column(String(10), comment="策略方向 (LONG/SHORT)")
    quantity: Mapped[float] = mapped_column(Float, comment="交易数量 (BTC)")
    entry_price: Mapped[float] = mapped_column(Float, comment="入场价格")
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True, comment="出场价格")
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0, comment="已实现盈亏 (USDT)")
    spread_at_entry: Mapped[float] = mapped_column(Float, default=0.0, comment="入场时价差")
    zscore_at_entry: Mapped[float] = mapped_column(Float, default=0.0, comment="入场时 Z-score")
    status: Mapped[str] = mapped_column(String(20), default="OPEN", comment="状态 (OPEN/CLOSED)")
    opened_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), comment="开仓时间",
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True, comment="平仓时间",
    )
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="备注")

    def __repr__(self) -> str:
        return (
            f"<Trade #{self.id} {self.symbol} {self.direction} "
            f"qty={self.quantity} PnL={self.realized_pnl:.2f}>"
        )


class SignalLog(Base):
    """
    信号日志表

    记录每次策略生成的信号详情，
    用于事后分析和参数优化。
    """
    __tablename__ = "signal_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True, comment="交易对")
    signal_type: Mapped[str] = mapped_column(String(20), comment="信号类型")
    direction: Mapped[str] = mapped_column(String(10), comment="方向")
    confidence: Mapped[float] = mapped_column(Float, comment="置信度")
    spread: Mapped[float] = mapped_column(Float, comment="当前价差")
    spread_bps: Mapped[float] = mapped_column(Float, comment="当前价差 (bps)")
    z_score: Mapped[float] = mapped_column(Float, comment="Z-score")
    rolling_mean: Mapped[float] = mapped_column(Float, comment="滚动均值")
    rolling_std: Mapped[float] = mapped_column(Float, comment="滚动标准差")
    reason: Mapped[str] = mapped_column(Text, comment="信号原因")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), comment="创建时间",
    )


class RiskEvent(Base):
    """
    风控事件日志表

    记录所有风控触发事件，包括入场拒绝、滑点超限、
    日止损触发、超时撤单等。
    """
    __tablename__ = "risk_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(50), comment="事件类型")
    reason: Mapped[str] = mapped_column(Text, comment="触发原因")
    details: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="详细信息 (JSON)")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), comment="创建时间",
    )


class Database:
    """
    数据库管理器 — 封装 SQLAlchemy 操作

    使用方式:
        db = Database("data/trading.db")
        db.create_tables()
        db.save_trade(symbol="BTCUSDT", side="BUY", ...)
    """

    def __init__(self, db_path: str = "data/trading.db") -> None:
        """
        Args:
            db_path: SQLite 数据库文件路径
        """
        self._engine: Engine = create_engine(f"sqlite:///{db_path}", echo=False)

    def create_tables(self) -> None:
        """创建所有 ORM 表（如果不存在）"""
        Base.metadata.create_all(self._engine)

    def session(self) -> Session:
        """
        创建新的数据库会话

        Returns:
            SQLAlchemy Session 对象
        """
        return Session(self._engine)

    # ── 交易记录 CRUD ────────────────────────────────

    def save_trade(self, **kwargs: object) -> int:
        """
        保存交易记录

        Args:
            **kwargs: TradeRecord 的字段

        Returns:
            新记录的 ID
        """
        with self.session() as session:
            trade = TradeRecord(**kwargs)  # type: ignore[arg-type]
            session.add(trade)
            session.commit()
            return trade.id

    def update_trade(self, trade_id: int, **kwargs: object) -> None:
        """
        更新交易记录

        Args:
            trade_id: 记录ID
            **kwargs: 要更新的字段
        """
        with self.session() as session:
            session.query(TradeRecord).filter(
                TradeRecord.id == trade_id
            ).update(kwargs)
            session.commit()

    def get_trades(self, limit: int = 50) -> list[TradeRecord]:
        """
        获取最近的交易记录

        Args:
            limit: 返回数量

        Returns:
            TradeRecord 列表（按时间倒序）
        """
        with self.session() as session:
            return session.query(TradeRecord).order_by(
                TradeRecord.opened_at.desc()
            ).limit(limit).all()

    # ── 信号日志 ─────────────────────────────────────

    def save_signal(self, **kwargs: object) -> int:
        """
        保存信号日志

        Args:
            **kwargs: SignalLog 的字段

        Returns:
            新记录的 ID
        """
        with self.session() as session:
            sig = SignalLog(**kwargs)  # type: ignore[arg-type]
            session.add(sig)
            session.commit()
            return sig.id

    # ── 风控事件 ─────────────────────────────────────

    def save_risk_event(self, **kwargs: object) -> None:
        """
        保存风控事件

        Args:
            **kwargs: RiskEvent 的字段
        """
        with self.session() as session:
            session.add(RiskEvent(**kwargs))  # type: ignore[arg-type]
            session.commit()


# ═══════════════════════════════════════════════════════
# 独立测试入口
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    import tempfile
    import os

    print("=" * 50)
    print("数据库模块 — 独立测试")
    print("=" * 50)

    # 使用临时文件测试
    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    try:
        db = Database(tmp_path)
        db.create_tables()
        print("✅ 表创建成功")

        # 测试交易记录
        tid = db.save_trade(
            symbol="BTCUSDT", side="BUY", direction="LONG",
            quantity=0.001, entry_price=65000.0,
            spread_at_entry=2.0, zscore_at_entry=2.5,
        )
        assert tid > 0
        print(f"✅ 交易记录保存: id={tid}")

        # 测试更新
        db.update_trade(tid, exit_price=65100.0, realized_pnl=0.1, status="CLOSED")
        trades = db.get_trades(limit=10)
        assert len(trades) == 1
        assert trades[0].realized_pnl == 0.1
        print(f"✅ 交易记录更新: PnL={trades[0].realized_pnl}")

        # 测试信号日志
        sid = db.save_signal(
            symbol="BTCUSDT", signal_type="ENTRY_LONG", direction="LONG",
            confidence=0.8, spread=2.0, spread_bps=3.0,
            z_score=2.5, rolling_mean=1.0, rolling_std=0.5,
            reason="测试信号",
        )
        assert sid > 0
        print(f"✅ 信号日志保存: id={sid}")

        # 测试风控事件
        db.save_risk_event(
            event_type="ENTRY_REJECTED",
            reason="单日亏损超限",
            details='{"daily_pnl": -101.0}',
        )
        print("✅ 风控事件保存")

        print(f"\n全部测试通过! ✅ (临时数据库: {tmp_path})")
    finally:
        os.unlink(tmp_path)
