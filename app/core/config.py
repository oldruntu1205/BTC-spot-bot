"""
配置管理 — 基于 pydantic-settings 的类型安全配置系统

优先级: 环境变量 > YAML 配置文件 > 默认值
使用 pydantic-settings 的 BaseSettings 自动从 .env 文件加载 API Key。

模块可独立测试: python -m app.core.config
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ExchangeConfig(BaseModel):
    """交易所连接配置"""
    name: str = "binance"
    testnet: bool = True
    futures_testnet: bool = True

    @property
    def rest_url(self) -> str:
        """REST API 基础 URL — 根据 testnet 自动切换"""
        return "https://testnet.binance.vision" if self.testnet else "https://api.binance.com"

    @property
    def ws_url(self) -> str:
        """WebSocket 基础 URL — 根据 testnet 自动切换"""
        if self.testnet:
            return "wss://testnet.binance.vision/ws"
        return "wss://stream.binance.com:9443/ws"

    @property
    def futures_rest_url(self) -> str:
        """永续合约 REST API URL"""
        if self.futures_testnet:
            return "https://testnet.binancefuture.com"
        return "https://fapi.binance.com"

    @property
    def futures_ws_url(self) -> str:
        """永续合约 WebSocket URL"""
        if self.futures_testnet:
            return "wss://stream.binancefuture.com/ws"
        return "wss://fstream.binance.com/ws"


class EdgeConfig(BaseModel):
    """Edge Score 多因子参数配置"""
    ob_imbalance_weight: float = 0.30      # 买卖盘失衡权重
    vwap_deviation_weight: float = 0.20    # VWAP偏离权重
    trade_flow_weight: float = 0.20        # 成交流向权重
    momentum_weight: float = 0.15          # 5分钟动量权重
    volatility_filter_weight: float = 0.15 # 波动率过滤权重
    entry_threshold: float = 70.0          # 入场阈值 (Edge ≥ 70)
    exit_threshold: float = 40.0           # 出场阈值 (Edge ≤ 40)
    ob_depth_levels: int = 10              # OrderBook 深度档数
    vwap_window: int = 20                  # VWAP 滚动窗口
    trade_flow_window: int = 50            # 成交流滚动窗口
    atr_period: int = 14                   # ATR 周期


class StrategyConfig(BaseModel):
    """策略参数配置"""
    symbol: str = "BTCUSDT"          # 交易对
    timeframe: str = "5m"            # K线周期
    order_type: str = "LIMIT"        # 订单类型（仅限价单）
    trade_quantity: float = 0.001    # 固定交易量 (BTC)


class SpreadConfig(BaseModel):
    """价差统计参数配置"""
    lookback_window: int = 100       # 滚动统计窗口（5M K线数）
    entry_zscore: float = 2.0        # 入场 Z-score 阈值（仅买方优势侧）
    exit_zscore: float = 0.5         # 出场 Z-score 阈值（均值回归）
    min_spread_bps: float = 1.0      # 最小价差阈值（基点），低于此值忽略信号
    min_expected_return_bps: float = 1.0  # 最低预期收益率过滤（基点）


class HedgeConfig(BaseModel):
    """对冲参数配置"""
    enabled: bool = True                  # 是否启用对冲
    hedge_ratio: float = 1.0              # 全局对冲比例上限（1.0 = 完全对冲）
    hedge_threshold_bps: float = 50.0     # 触发对冲的最小浮亏（基点）
    unwind_threshold_bps: float = 25.0    # 解除对冲的浮亏回归阈值（基点）


class FuturesConfig(BaseModel):
    """永续合约配置"""
    enabled: bool = False                  # 默认关闭
    testnet: bool = True                   # 合约测试网
    leverage: int = 1                      # 杠杆倍数
    margin_type: str = "ISOLATED"          # ISOLATED / CROSSED
    base_hedge_ratio: float = 0.9          # 基础对冲比例 (0.8-1.0)


class RiskConfig(BaseModel):
    """风控参数配置"""
    max_position_size: float = 0.01       # 最大主策略持仓 (BTC)
    max_net_exposure: float = 0.005       # 最大净多头敞口 (BTC)
    max_drawdown_pct: float = 0.05        # 最大回撤百分比（5%）
    daily_loss_limit: float = 100.0       # 单日最大亏损 (USDT)
    order_timeout: int = 120              # 订单超时撤单时间（秒）
    max_slippage_bps: int = 5             # 最大允许滑点（基点）
    max_concurrent_orders: int = 3        # 最大并发挂单数
    min_order_value: float = 10.0         # 最小订单金额 (USDT)
    single_trade_risk_pct: float = 0.5    # 单笔风险 ≤ 0.5% 账户权益
    max_position_pct: float = 20.0         # 最大仓位 ≤ 20% 账户
    daily_loss_pct: float = 2.0           # 日亏损 ≤ 2%
    consecutive_loss_limit: int = 3       # 连续亏损暂停次数
    pause_minutes: int = 30               # 暂停时长
    max_hold_minutes: int = 45            # 最大持仓时间
    profit_target_pct: float = 0.4        # 止盈目标 (%)
    requote_timeout: int = 90             # 重报价超时(秒)


class LoggingConfig(BaseModel):
    """日志配置 — 使用 loguru"""
    level: str = "INFO"              # 日志级别
    file: str = "logs/bot.log"       # 日志文件路径
    rotation: str = "1 day"          # 日志轮转周期
    retention: str = "30 days"       # 日志保留时间


class DatabaseConfig(BaseModel):
    """数据库配置 — SQLAlchemy 2.x + SQLite"""
    path: str = "data/trading.db"    # 数据库文件路径

    @property
    def url(self) -> str:
        """生成 SQLAlchemy 连接 URL"""
        return f"sqlite+aiosqlite:///{self.path}"


class AppSettings(BaseSettings):
    """
    应用总配置 — 自动合并 YAML + 环境变量

    环境变量:
        BINANCE_API_KEY: 币安 API Key
        BINANCE_SECRET_KEY: 币安 Secret Key
    """
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 从环境变量加载 API Key
    binance_api_key: str = Field(default="", alias="BINANCE_API_KEY")
    binance_secret_key: str = Field(default="", alias="BINANCE_SECRET_KEY")

    # 子配置 — 由 YAML 文件填充
    exchange: ExchangeConfig = Field(default_factory=ExchangeConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    edge: EdgeConfig = Field(default_factory=EdgeConfig)
    spread: SpreadConfig = Field(default_factory=SpreadConfig)
    hedge: HedgeConfig = Field(default_factory=HedgeConfig)
    futures: FuturesConfig = Field(default_factory=FuturesConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)

    @property
    def is_configured(self) -> bool:
        """检查 API Key 是否已配置"""
        return bool(self.binance_api_key and self.binance_secret_key)


def load_config(config_path: Optional[str] = None) -> AppSettings:
    """
    加载配置，合并 YAML 文件和环境变量

    优先级: 环境变量 > YAML > 默认值

    Args:
        config_path: YAML 配置文件路径，默认 config/settings.yaml

    Returns:
        AppSettings: 完整的应用配置对象
    """
    if config_path is None:
        config_path = str(Path(__file__).parent.parent.parent / "config" / "settings.yaml")

    # 读取 YAML
    yaml_data: dict = {}
    config_file = Path(config_path)
    if config_file.exists():
        with open(config_file, "r", encoding="utf-8") as f:
            yaml_data = yaml.safe_load(f) or {}

    # 构建 pydantic 配置对象
    return AppSettings(
        exchange=ExchangeConfig(**yaml_data.get("exchange", {})),
        strategy=StrategyConfig(**yaml_data.get("strategy", {})),
        edge=EdgeConfig(**yaml_data.get("edge", {})),
        spread=SpreadConfig(**yaml_data.get("spread", {})),
        hedge=HedgeConfig(**yaml_data.get("hedge", {})),
        futures=FuturesConfig(**yaml_data.get("futures", {})),
        risk=RiskConfig(**yaml_data.get("risk", {})),
        logging=LoggingConfig(**yaml_data.get("logging", {})),
        database=DatabaseConfig(**yaml_data.get("database", {})),
    )


# 全局配置缓存
_config: Optional[AppSettings] = None


def get_config() -> AppSettings:
    """获取全局配置单例（懒加载）"""
    global _config
    if _config is None:
        _config = load_config()
    return _config


# ═══════════════════════════════════════════════════════
# 独立测试入口
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 50)
    print("配置管理模块 — 独立测试")
    print("=" * 50)

    # 测试默认配置
    cfg = load_config()
    assert cfg.exchange.name == "binance"
    assert cfg.strategy.symbol == "BTCUSDT"
    assert cfg.strategy.timeframe == "5m"
    assert cfg.spread.entry_zscore == 2.0
    assert cfg.risk.max_position_size == 0.01
    print("✅ 默认配置加载正常")

    # 测试 Edge Score 配置
    assert cfg.edge.entry_threshold == 70
    assert cfg.edge.exit_threshold == 40
    assert cfg.edge.ob_imbalance_weight == 0.30
    assert cfg.edge.atr_period == 14
    print("✅ Edge Score 配置正常")

    # 测试 Futures 配置
    assert cfg.futures.enabled is False
    assert cfg.futures.leverage == 1
    assert cfg.futures.margin_type == "ISOLATED"
    assert cfg.futures.base_hedge_ratio == 0.9
    print("✅ Futures 配置正常")

    # 测试新风险字段
    assert cfg.risk.single_trade_risk_pct == 0.5
    assert cfg.risk.max_position_pct == 20.0
    assert cfg.risk.daily_loss_pct == 2.0
    assert cfg.risk.consecutive_loss_limit == 3
    assert cfg.risk.pause_minutes == 30
    assert cfg.risk.max_hold_minutes == 45
    assert cfg.risk.profit_target_pct == 0.4
    assert cfg.risk.requote_timeout == 90
    print("✅ 增强风控配置正常")

    # 测试 URL 生成
    assert "testnet" in cfg.exchange.rest_url
    assert "testnet" in cfg.exchange.ws_url
    assert "testnet" in cfg.exchange.futures_rest_url
    assert "binancefuture.com" in cfg.exchange.futures_rest_url
    assert "binancefuture.com" in cfg.exchange.futures_ws_url
    print(f"   REST URL:          {cfg.exchange.rest_url}")
    print(f"   WS URL:            {cfg.exchange.ws_url}")
    print(f"   Futures REST URL:  {cfg.exchange.futures_rest_url}")
    print(f"   Futures WS URL:    {cfg.exchange.futures_ws_url}")

    # 测试数据库 URL
    assert cfg.database.url.startswith("sqlite")
    print(f"   数据库 URL: {cfg.database.url}")

    # 测试 is_configured（未设置 API Key 时应为 False）
    assert not cfg.is_configured
    print("✅ API Key 未配置检测正常")

    # 测试 get_config 单例
    cfg2 = get_config()
    assert cfg2 is _config
    print("✅ 配置单例正常")

    print("\n全部测试通过! ✅")
