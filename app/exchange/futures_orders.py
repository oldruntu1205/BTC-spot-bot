"""
永续合约订单模块 — 下单、撤单、仓位管理

基于币安 /fapi/v1/order 等签名端点。
支持 reduceOnly 标志用于对冲平仓。

模块可独立测试: python -m app.exchange.futures_orders
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.types import Order, OrderSide, OrderStatus, OrderType

if TYPE_CHECKING:
    from .futures_client import FuturesBinanceClient


class FuturesOrderAPI:
    """
    永续合约订单管理

    使用方式:
        client = FuturesBinanceClient(api_key, secret_key, testnet=True)
        api = FuturesOrderAPI(client)
        order = await api.create_order("BTCUSDT", OrderSide.SELL, OrderType.LIMIT, 0.001, 65000.0)
    """

    def __init__(self, client: FuturesBinanceClient) -> None:
        """
        Args:
            client: FuturesBinanceClient 实例
        """
        self._client: FuturesBinanceClient = client

    async def create_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: float,
        price: float = 0.0,
        reduce_only: bool = False,
        client_order_id: str = "",
        time_in_force: str = "GTC",
    ) -> Order:
        """
        创建永续合约订单

        Args:
            symbol: 交易对 (如 "BTCUSDT")
            side: BUY / SELL
            order_type: LIMIT / MARKET
            quantity: 委托数量
            price: 委托价格 (市价单可为 0)
            reduce_only: 仅减仓 (对冲平仓时设为 True)
            client_order_id: 客户端自定义 ID (幂等)
            time_in_force: GTC / IOC / FOK

        Returns:
            Order 对象
        """
        params: dict = {
            "symbol": symbol,
            "side": side.value,
            "type": order_type.value,
            "quantity": self._format_quantity(quantity),
        }

        if order_type == OrderType.LIMIT:
            params["price"] = self._format_price(price)
            params["timeInForce"] = time_in_force

        if reduce_only:
            params["reduceOnly"] = "true"

        if client_order_id:
            params["newClientOrderId"] = client_order_id

        data = await self._client.post("/fapi/v1/order", params, signed=True)
        return self._parse_order(data, symbol)

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """
        撤销订单

        Args:
            symbol: 交易对
            order_id: 订单ID

        Returns:
            True 表示撤销成功
        """
        data = await self._client.delete(
            "/fapi/v1/order",
            {"symbol": symbol, "orderId": order_id},
            signed=True,
        )
        return data.get("status") == "CANCELED"

    async def cancel_all_orders(self, symbol: str) -> list[dict]:
        """
        撤销所有挂单

        Args:
            symbol: 交易对

        Returns:
            已撤销的订单列表
        """
        data = await self._client.delete(
            "/fapi/v1/allOpenOrders",
            {"symbol": symbol},
            signed=True,
        )
        return data if isinstance(data, list) else []

    async def get_open_orders(self, symbol: str) -> list[Order]:
        """
        查询活跃订单

        Args:
            symbol: 交易对

        Returns:
            活跃订单列表
        """
        data = await self._client.get(
            "/fapi/v1/openOrders",
            {"symbol": symbol},
            signed=True,
        )
        orders = data if isinstance(data, list) else []
        return [self._parse_order(o, symbol) for o in orders]

    async def get_positions(self, symbol: str) -> list[dict]:
        """
        查询当前持仓

        Args:
            symbol: 交易对

        Returns:
            持仓信息列表 (positionAmt 正=多, 负=空)
        """
        data = await self._client.get(
            "/fapi/v2/positionRisk",
            {"symbol": symbol},
            signed=True,
        )
        return data if isinstance(data, list) else []

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        """
        设置杠杆倍数

        Args:
            symbol: 交易对
            leverage: 杠杆倍数 (1-125)

        Returns:
            API 响应
        """
        return await self._client.post(
            "/fapi/v1/leverage",
            {"symbol": symbol, "leverage": leverage},
            signed=True,
        )

    async def set_margin_type(self, symbol: str, margin_type: str) -> dict:
        """
        设置保证金模式

        Args:
            symbol: 交易对
            margin_type: ISOLATED (逐仓) / CROSSED (全仓)

        Returns:
            API 响应
        """
        return await self._client.post(
            "/fapi/v1/marginType",
            {"symbol": symbol, "marginType": margin_type},
            signed=True,
        )

    async def get_balance(self, asset: str = "USDT") -> float:
        """
        查询永续合约账户余额

        Args:
            asset: 资产 (默认 USDT)

        Returns:
            可用余额
        """
        data = await self._client.get("/fapi/v2/balance", signed=True)
        for item in data:
            if item.get("asset") == asset:
                return float(item.get("availableBalance", 0))
        return 0.0

    # ── 内部工具 ────────────────────────────────────

    def _parse_order(self, data: dict, symbol: str) -> Order:
        """
        将 API 响应转为 Order 对象

        Args:
            data: API 响应数据
            symbol: 交易对

        Returns:
            Order
        """
        return Order(
            order_id=str(data.get("orderId", "")),
            symbol=symbol,
            side=OrderSide(data["side"]),
            type=OrderType(data["type"]),
            price=float(data.get("price", 0)),
            quantity=float(data.get("origQty", 0)),
            status=OrderStatus(data.get("status", "NEW")),
            filled_qty=float(data.get("executedQty", 0)),
            filled_quote_qty=float(data.get("cumQuote", 0)),
            client_order_id=str(data.get("clientOrderId", "")),
        )

    @staticmethod
    def _format_quantity(quantity: float) -> str:
        """格式化数量 (BTC: 3位小数)"""
        return f"{quantity:.3f}"

    @staticmethod
    def _format_price(price: float) -> str:
        """格式化价格 (BTCUSDT: 1位小数)"""
        return f"{price:.1f}"


# ═══════════════════════════════════════════════════════
# 独立测试入口
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio
    from .futures_client import FuturesBinanceClient

    async def _test() -> None:
        print("=" * 50)
        print("永续合约订单 — 独立测试")
        print("=" * 50)

        client = FuturesBinanceClient(
            api_key="test_key",
            secret_key="test_secret",
            testnet=True,
        )
        api = FuturesOrderAPI(client)

        # 测试接口方法存在
        assert hasattr(api, "create_order")
        assert hasattr(api, "cancel_order")
        assert hasattr(api, "get_open_orders")
        assert hasattr(api, "get_positions")
        assert hasattr(api, "set_leverage")
        assert hasattr(api, "set_margin_type")
        print("✅ 接口方法完整")

        # 测试订单解析
        mock_data = {
            "orderId": 123456,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "type": "LIMIT",
            "price": "65000.0",
            "origQty": "0.001",
            "executedQty": "0.000",
            "cumQuote": "0.00",
            "status": "NEW",
            "clientOrderId": "sfn_test",
        }
        order = api._parse_order(mock_data, "BTCUSDT")
        assert order.symbol == "BTCUSDT"
        assert order.side == OrderSide.SELL
        assert order.price == 65000.0
        assert order.quantity == 0.001
        assert order.is_active
        print(f"✅ 订单解析正常: {order.side.value} {order.quantity} @ {order.price}")

        # 测试格式化
        assert "0.001" in api._format_quantity(0.001)
        assert "65000.0" in api._format_price(65000.0)
        print("✅ 格式化正常")

        # 测试类型注解
        import inspect
        sig = inspect.signature(api.create_order)
        assert "Order" in str(sig.return_annotation)
        print("✅ 返回类型注解正确")

        print("⚠️  真实下单测试需要有效 API Key，已跳过")

        await client.close()
        print("\n全部测试通过! ✅")

    asyncio.run(_test())
