"""
订单模块 — 下单、撤单、订单查询

基于币安官方 REST API v3 的 /api/v3/order 端点。
仅使用限价单 (LIMIT)，支持 GTC (Good-Til-Canceled) 模式。

模块可独立测试: python -m app.exchange.orders
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional, TYPE_CHECKING

from loguru import logger

from app.core.types import Order, OrderSide, OrderStatus, OrderType

if TYPE_CHECKING:
    from .client import BinanceClient


class OrderAPI:
    """
    币安订单 API 封装

    使用方式:
        client = BinanceClient(api_key, secret_key, testnet=True)
        orders = OrderAPI(client)
        order = await orders.create_order("BTCUSDT", OrderSide.BUY, OrderType.LIMIT, 0.001, 65000.0)
    """

    # ── 状态映射 — 与币安 API 返回值一致 ─────────────

    _STATUS_MAP: dict[str, OrderStatus] = {
        "NEW": OrderStatus.NEW,
        "PARTIALLY_FILLED": OrderStatus.PARTIALLY_FILLED,
        "FILLED": OrderStatus.FILLED,
        "CANCELED": OrderStatus.CANCELED,
        "EXPIRED": OrderStatus.EXPIRED,
        "REJECTED": OrderStatus.REJECTED,
    }

    def __init__(self, client: BinanceClient) -> None:
        """
        Args:
            client: BinanceClient 实例
        """
        self._client: BinanceClient = client

    # ── 下单 ────────────────────────────────────────

    async def create_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: float,
        price: Optional[float] = None,
        client_order_id: str = "",
    ) -> Order:
        """
        创建新订单（真实成交）

        使用 /api/v3/order (POST) 端点。
        限价单会自动添加 timeInForce=GTC。

        Args:
            symbol: 交易对
            side: 买卖方向
            order_type: 订单类型（LIMIT 或 MARKET）
            quantity: 委托数量
            price: 委托价格（限价单必填）
            client_order_id: 客户端自定义ID（用于幂等性）

        Returns:
            Order 对象（含交易所返回的 orderId）
        """
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side.value,
            "type": order_type.value,
            "quantity": self._format_quantity(symbol, quantity),
        }

        # 限价单：添加价格和 GTC 模式
        if order_type == OrderType.LIMIT and price is not None:
            params["price"] = self._format_price(symbol, price)
            params["timeInForce"] = "GTC"

        # 自定义客户端订单ID（幂等性保证）
        if client_order_id:
            params["newClientOrderId"] = client_order_id

        data = await self._client.post("/api/v3/order", params, signed=True)
        logger.info(
            f"下单成功 | {side.value} {quantity} @ {price} | "
            f"orderId={data.get('orderId')}"
        )
        return self._parse_order(data, symbol)

    async def create_test_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: float,
        price: Optional[float] = None,
    ) -> dict[str, Any]:
        """
        测试下单（验证参数但不实际成交）

        使用 /api/v3/order/test (POST) 端点。
        用于验证订单参数是否正确，不会产生实际交易。

        Args:
            symbol: 交易对
            side: 买卖方向
            order_type: 订单类型
            quantity: 委托数量
            price: 委托价格

        Returns:
            API 原始响应（空对象 {} 表示参数有效）
        """
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side.value,
            "type": order_type.value,
            "quantity": self._format_quantity(symbol, quantity),
        }
        if order_type == OrderType.LIMIT and price is not None:
            params["price"] = self._format_price(symbol, price)

        return await self._client.post("/api/v3/order/test", params, signed=True)

    # ── 撤单 ────────────────────────────────────────

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """
        撤销指定订单

        使用 /api/v3/order (DELETE) 端点。

        Args:
            symbol: 交易对
            order_id: 交易所订单ID

        Returns:
            True 表示撤单成功，False 表示订单不存在
        """
        try:
            await self._client.delete("/api/v3/order", {
                "symbol": symbol,
                "orderId": order_id,
            }, signed=True)
            logger.info(f"撤单成功 | orderId={order_id}")
            return True
        except Exception as e:
            logger.warning(f"撤单失败 [orderId={order_id}]: {e}")
            return False

    async def cancel_all_orders(self, symbol: str) -> list[dict[str, Any]]:
        """
        撤销指定交易对的所有挂单

        使用 /api/v3/openOrders (DELETE) 端点。

        Args:
            symbol: 交易对

        Returns:
            已撤销的订单列表
        """
        result = await self._client.delete(
            "/api/v3/openOrders", {"symbol": symbol}, signed=True,
        )
        logger.info(f"批量撤单 | {symbol} | 撤销 {len(result) if isinstance(result, list) else '?'} 单")
        return result if isinstance(result, list) else []

    # ── 查询 ────────────────────────────────────────

    async def get_order(self, symbol: str, order_id: str) -> Order:
        """
        查询指定订单状态

        Args:
            symbol: 交易对
            order_id: 交易所订单ID

        Returns:
            Order 对象
        """
        data = await self._client.get("/api/v3/order", {
            "symbol": symbol,
            "orderId": order_id,
        }, signed=True)
        return self._parse_order(data, symbol)

    async def get_open_orders(self, symbol: str) -> list[Order]:
        """
        查询所有未成交的挂单

        Args:
            symbol: 交易对

        Returns:
            活跃订单列表
        """
        data = await self._client.get(
            "/api/v3/openOrders", {"symbol": symbol}, signed=True,
        )
        return [self._parse_order(o, symbol) for o in data]

    async def get_order_history(
        self, symbol: str, limit: int = 50,
    ) -> list[Order]:
        """
        查询历史订单（含已成交和已撤销）

        Args:
            symbol: 交易对
            limit: 返回数量（默认 50，最大 1000）

        Returns:
            历史订单列表
        """
        data = await self._client.get("/api/v3/allOrders", {
            "symbol": symbol,
            "limit": limit,
        }, signed=True)
        return [self._parse_order(o, symbol) for o in data]

    # ── 内部工具方法 ─────────────────────────────────

    @classmethod
    def _parse_order(cls, data: dict[str, Any], symbol: str) -> Order:
        """
        将币安 API 原始响应转换为 Order 对象

        Args:
            data: 币安 API 返回的订单数据
            symbol: 交易对

        Returns:
            Order 对象
        """
        return Order(
            order_id=str(data.get("orderId", "")),
            symbol=symbol,
            side=OrderSide(data["side"]),
            type=OrderType(data["type"]),
            price=float(data.get("price", 0) or 0),
            quantity=float(data.get("origQty", 0)),
            status=cls._STATUS_MAP.get(data.get("status", "NEW"), OrderStatus.NEW),
            filled_qty=float(data.get("executedQty", 0)),
            filled_quote_qty=float(data.get("cummulativeQuoteQty", 0)),
            client_order_id=str(data.get("clientOrderId", "")),
        )

    @staticmethod
    def _format_quantity(symbol: str, qty: float) -> str:
        """
        格式化数量精度

        BTC 交易对: 6 位小数
        其他: 8 位小数

        Args:
            symbol: 交易对
            qty: 数量

        Returns:
            格式化后的字符串
        """
        return f"{qty:.6f}" if "BTC" in symbol else f"{qty:.8f}"

    @staticmethod
    def _format_price(symbol: str, price: float) -> str:
        """
        格式化价格精度

        BTCUSDT: 2 位小数
        其他: 8 位小数

        Args:
            symbol: 交易对
            price: 价格

        Returns:
            格式化后的字符串
        """
        return f"{price:.2f}" if "BTC" in symbol else f"{price:.8f}"


# ═══════════════════════════════════════════════════════
# 独立测试入口
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio
    from .client import BinanceClient

    async def _test() -> None:
        print("=" * 50)
        print("订单模块 — 独立测试")
        print("=" * 50)

        client = BinanceClient(api_key="test", secret_key="test", testnet=True)
        orders = OrderAPI(client)

        # 测试解析（用模拟数据）
        mock_data = {
            "orderId": 123456,
            "symbol": "BTCUSDT",
            "side": "BUY",
            "type": "LIMIT",
            "price": "65000.00",
            "origQty": "0.00100000",
            "executedQty": "0.00000000",
            "cummulativeQuoteQty": "0.00000000",
            "status": "NEW",
            "clientOrderId": "test_001",
        }
        order = orders._parse_order(mock_data, "BTCUSDT")
        assert order.order_id == "123456"
        assert order.side == OrderSide.BUY
        assert order.price == 65000.0
        assert order.is_active
        assert not order.is_filled
        print(f"✅ 订单解析正常: {order.side.value} {order.quantity} @ {order.price}")

        # 测试格式化
        assert orders._format_quantity("BTCUSDT", 0.001) == "0.001000"
        assert orders._format_price("BTCUSDT", 65000.0) == "65000.00"
        print("✅ 格式化正常")

        # 测试类型注解
        # 注: from __future__ import annotations 使注解转为字符串
        import inspect
        sig = inspect.signature(orders.create_order)
        assert sig.return_annotation in (Order, "Order")
        print("✅ 返回类型注解正确")

        # 测试测试下单（需要有效 API Key）
        print("⚠️  真实下单测试需要有效 API Key，已跳过")

        await client.close()
        print("\n全部测试通过! ✅")

    asyncio.run(_test())
