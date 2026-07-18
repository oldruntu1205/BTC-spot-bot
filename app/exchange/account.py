"""
账户模块 — 币安账户余额查询和账户信息

基于币安官方 REST API v3 的 /api/v3/account 端点。
所有方法均为异步，返回类型带完整注解。

模块可独立测试: python -m app.exchange.account
"""
from __future__ import annotations

from typing import Any, TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from .client import BinanceClient


class AccountAPI:
    """
    币安账户 API 封装

    使用方式:
        client = BinanceClient(api_key, secret_key, testnet=True)
        account = AccountAPI(client)
        btc_balance = await account.get_balance("BTC")
    """

    def __init__(self, client: BinanceClient) -> None:
        """
        Args:
            client: BinanceClient 实例
        """
        self._client: BinanceClient = client

    async def get_balance(self, asset: str) -> float:
        """
        查询指定资产的可用余额

        Args:
            asset: 资产代码（如 "BTC", "USDT"）

        Returns:
            可用余额（float），未找到返回 0.0
        """
        data: dict[str, Any] = await self._client.get("/api/v3/account", signed=True)
        for balance in data.get("balances", []):
            if balance["asset"] == asset:
                return float(balance["free"])
        return 0.0

    async def get_all_balances(self) -> dict[str, float]:
        """
        查询所有资产的可用余额（过滤零余额）

        Returns:
            {"BTC": 0.01, "USDT": 1000.0, ...}
        """
        data: dict[str, Any] = await self._client.get("/api/v3/account", signed=True)
        result: dict[str, float] = {}
        for balance in data.get("balances", []):
            free = float(balance["free"])
            if free > 0.0:
                result[balance["asset"]] = free
        return result

    async def get_account_info(self) -> dict[str, Any]:
        """
        查询完整账户信息

        Returns:
            币安 /api/v3/account 的原始响应
            包含 balances, permissions, canTrade 等字段
        """
        return await self._client.get("/api/v3/account", signed=True)

    async def get_trade_fee(self, symbol: str = "BTCUSDT") -> dict[str, Any]:
        """
        查询指定交易对的手续费率

        Args:
            symbol: 交易对（默认 BTCUSDT）

        Returns:
            包含 makerCommission, takerCommission 等字段
        """
        return await self._client.get(
            "/api/v3/account/commission",
            {"symbol": symbol},
            signed=True,
        )


# ═══════════════════════════════════════════════════════
# 独立测试入口
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio
    from .client import BinanceClient

    async def _test() -> None:
        print("=" * 50)
        print("账户模块 — 独立测试")
        print("=" * 50)

        # 注意：需要有效的 API Key 才能测试签名接口
        client = BinanceClient(
            api_key="test_key",
            secret_key="test_secret",
            testnet=True,
        )
        account = AccountAPI(client)

        # 测试模块结构
        assert hasattr(account, "get_balance")
        assert hasattr(account, "get_all_balances")
        assert hasattr(account, "get_account_info")
        assert hasattr(account, "get_trade_fee")
        print("✅ 接口方法完整")

        # 测试类型注解
        # 注: from __future__ import annotations 使注解转为字符串
        import inspect
        sig = inspect.signature(account.get_balance)
        assert sig.return_annotation in (float, "float")
        print("✅ 返回类型注解正确")

        await client.close()
        print("\n模块结构验证通过! ✅")
        print("(需要 API Key 才能进行完整集成测试)")

    asyncio.run(_test())
