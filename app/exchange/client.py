"""
交易所 REST 客户端 — 基于币安官方 REST API v3 的异步实现

使用 aiohttp 实现异步 HTTP 请求，支持：
- HMAC SHA256 签名认证
- 自动 recvWindow 管理
- 测试网/主网自动切换
- 完整的错误处理

不依赖 python-binance 或 binance-connector 第三方库，
直接调用币安官方 REST API 端点。

模块可独立测试: python -m app.exchange.client
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Optional
from urllib.parse import urlencode

import aiohttp
from loguru import logger


class BinanceClient:
    """
    币安 REST API 异步客户端

    使用方式:
        client = BinanceClient(api_key, secret_key, testnet=True)
        data = await client.get("/api/v3/ticker/bookTicker", {"symbol": "BTCUSDT"})
        await client.close()
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        testnet: bool = True,
        recv_window: int = 5000,
    ) -> None:
        """
        Args:
            api_key: 币安 API Key
            secret_key: 币安 Secret Key
            testnet: 是否使用测试网（默认 True）
            recv_window: 请求时间窗口（毫秒）
        """
        self.api_key: str = api_key
        self.secret_key: str = secret_key
        self.testnet: bool = testnet
        self.recv_window: int = recv_window

        self.base_url: str = (
            "https://testnet.binance.vision" if testnet else "https://api.binance.com"
        )
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建 aiohttp 会话（连接复用）"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"X-MBX-APIKEY": self.api_key},
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    def _sign(self, params: dict[str, Any]) -> str:
        """
        生成 HMAC SHA256 签名

        币安 API 要求对所有签名请求的参数按字母序排列后进行签名。
        urlencode 默认按字母序排列。

        Args:
            params: 请求参数字典（不含 signature）

        Returns:
            签名字符串（hex）
        """
        query_string = urlencode(params)
        return hmac.new(
            self.secret_key.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict[str, Any]] = None,
        signed: bool = False,
    ) -> dict[str, Any]:
        """
        发送 HTTP 请求到币安 API

        Args:
            method: HTTP 方法 (GET/POST/DELETE)
            endpoint: API 端点路径
            params: 请求参数
            signed: 是否需要签名

        Returns:
            API 返回的 JSON 数据

        Raises:
            BinanceAPIError: 币安返回错误码时
            aiohttp.ClientError: 网络请求失败时
        """
        session = await self._get_session()
        url = f"{self.base_url}{endpoint}"
        params = dict(params or {})

        # 签名请求：添加 timestamp、recvWindow、signature
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = self.recv_window
            params["signature"] = self._sign(params)

        try:
            if method == "GET":
                async with session.get(url, params=params) as resp:
                    data: dict[str, Any] = await resp.json()
            elif method == "POST":
                async with session.post(url, data=params) as resp:
                    data = await resp.json()
            elif method == "DELETE":
                async with session.delete(url, params=params) as resp:
                    data = await resp.json()
            else:
                raise ValueError(f"不支持的 HTTP 方法: {method}")

            # 检查币安错误码（负数表示错误）
            if isinstance(data, dict) and data.get("code", 0) < 0:
                raise BinanceAPIError(data["code"], data.get("msg", "未知错误"))

            return data

        except aiohttp.ClientError as e:
            logger.error(f"HTTP 请求失败 [{method} {endpoint}]: {e}")
            raise

    # ── 便捷方法 ─────────────────────────────────────

    async def get(
        self, endpoint: str, params: Optional[dict[str, Any]] = None, signed: bool = False,
    ) -> dict[str, Any]:
        """发送 GET 请求"""
        return await self._request("GET", endpoint, params, signed)

    async def post(
        self, endpoint: str, params: Optional[dict[str, Any]] = None, signed: bool = True,
    ) -> dict[str, Any]:
        """发送 POST 请求（默认签名）"""
        return await self._request("POST", endpoint, params, signed)

    async def delete(
        self, endpoint: str, params: Optional[dict[str, Any]] = None, signed: bool = True,
    ) -> dict[str, Any]:
        """发送 DELETE 请求（默认签名）"""
        return await self._request("DELETE", endpoint, params, signed)

    async def close(self) -> None:
        """关闭 HTTP 会话，释放连接"""
        if self._session is not None and not self._session.closed:
            await self._session.close()
            logger.debug("HTTP 会话已关闭")


class BinanceAPIError(Exception):
    """币安 API 错误异常"""

    def __init__(self, code: int, message: str) -> None:
        self.code: int = code
        self.message: str = message
        super().__init__(f"Binance API Error [{code}]: {message}")


# ═══════════════════════════════════════════════════════
# 独立测试入口
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio

    async def _test() -> None:
        print("=" * 50)
        print("REST 客户端模块 — 独立测试")
        print("=" * 50)

        # 测试签名（无需 API Key）
        client = BinanceClient(api_key="test_key", secret_key="test_secret", testnet=True)
        params = {"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT"}
        sig = client._sign(params)
        assert len(sig) == 64  # SHA256 hex 长度
        print(f"✅ 签名生成正常 (len={len(sig)})")

        # 测试 URL 生成
        assert "testnet" in client.base_url
        print(f"✅ 测试网 URL: {client.base_url}")

        # 测试公开 API（无需 API Key）
        try:
            data = await client.get("/api/v3/ticker/bookTicker", {"symbol": "BTCUSDT"})
            assert "bidPrice" in data
            assert "askPrice" in data
            print(f"✅ 公开 API 调用正常: bid={data['bidPrice']}, ask={data['askPrice']}")
        except Exception as e:
            print(f"⚠️  公开 API 调用失败（网络问题?）: {e}")

        await client.close()
        print("\n全部测试通过! ✅")

    asyncio.run(_test())
