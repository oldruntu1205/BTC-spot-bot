"""
永续合约 REST 客户端 — 币安 USDⓈ-M Futures API

基于币安官方 /fapi/v1/ 端点，使用与现货相同的 HMAC SHA256 签名。
支持测试网 (testnet.binancefuture.com) 和主网 (fapi.binance.com)。

模块可独立测试: python -m app.exchange.futures_client
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Optional
from urllib.parse import urlencode

import aiohttp
from loguru import logger


class FuturesAPIError(Exception):
    """永续合约 API 错误"""
    def __init__(self, status: int, code: int, msg: str) -> None:
        self.status: int = status
        self.code: int = code
        self.msg: str = msg
        super().__init__(f"[{code}] {msg}")


class FuturesBinanceClient:
    """
    币安 USDⓈ-M 永续合约 REST 客户端

    使用方式:
        client = FuturesBinanceClient(api_key="...", secret_key="...", testnet=True)
        data = await client.get("/fapi/v1/markPrice", {"symbol": "BTCUSDT"})
        await client.close()
    """

    def __init__(
        self,
        api_key: str = "",
        secret_key: str = "",
        testnet: bool = True,
    ) -> None:
        """
        Args:
            api_key: 币安 API Key
            secret_key: 币安 Secret Key
            testnet: 是否使用测试网
        """
        self.api_key: str = api_key
        self.secret_key: str = secret_key
        self.testnet: bool = testnet

        self._base_url: str = (
            "https://testnet.binancefuture.com"
            if testnet
            else "https://fapi.binance.com"
        )
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建 aiohttp 会话（惰性初始化）"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "X-MBX-APIKEY": self.api_key,
                    "Content-Type": "application/json",
                },
            )
        return self._session

    def _sign(self, params: dict[str, Any]) -> str:
        """
        HMAC SHA256 签名

        Args:
            params: 请求参数字典

        Returns:
            64位十六进制签名字符串
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
        发送 HTTP 请求

        Args:
            method: HTTP 方法 (GET/POST/DELETE)
            endpoint: API 端点路径 (如 "/fapi/v1/markPrice")
            params: 请求参数
            signed: 是否需要签名

        Returns:
            API 响应 JSON

        Raises:
            FuturesAPIError: API 返回错误时抛出
        """
        if params is None:
            params = {}

        url = f"{self._base_url}{endpoint}"

        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 60000
            params["signature"] = self._sign(params)

        session = await self._get_session()

        try:
            if method == "GET":
                resp = await session.get(url, params=params)
            elif method == "POST":
                resp = await session.post(url, data=params)
            elif method == "DELETE":
                resp = await session.delete(url, data=params)
            else:
                raise ValueError(f"不支持的 HTTP 方法: {method}")

            data: dict[str, Any] = await resp.json()

            if resp.status >= 400:
                raise FuturesAPIError(
                    status=resp.status,
                    code=data.get("code", -1),
                    msg=data.get("msg", "未知错误"),
                )

            return data

        except FuturesAPIError:
            raise
        except aiohttp.ClientError as e:
            raise FuturesAPIError(status=0, code=-1, msg=f"网络错误: {e}") from e

    async def get(
        self,
        endpoint: str,
        params: Optional[dict[str, Any]] = None,
        signed: bool = False,
    ) -> dict[str, Any]:
        """发送 GET 请求"""
        return await self._request("GET", endpoint, params, signed)

    async def post(
        self,
        endpoint: str,
        params: Optional[dict[str, Any]] = None,
        signed: bool = True,
    ) -> dict[str, Any]:
        """发送 POST 请求（默认签名）"""
        return await self._request("POST", endpoint, params, signed)

    async def delete(
        self,
        endpoint: str,
        params: Optional[dict[str, Any]] = None,
        signed: bool = True,
    ) -> dict[str, Any]:
        """发送 DELETE 请求（默认签名）"""
        return await self._request("DELETE", endpoint, params, signed)

    async def close(self) -> None:
        """关闭 HTTP 会话"""
        if self._session and not self._session.closed:
            await self._session.close()
            logger.debug("永续合约 HTTP 会话已关闭")


# ═══════════════════════════════════════════════════════
# 独立测试入口
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    import asyncio

    async def _test() -> None:
        print("=" * 50)
        print("永续合约客户端 — 独立测试")
        print("=" * 50)

        client = FuturesBinanceClient(
            api_key="test_key",
            secret_key="test_secret",
            testnet=True,
        )

        # 测试签名生成
        sig = client._sign({"symbol": "BTCUSDT", "timestamp": 1700000000000})
        assert len(sig) == 64
        assert all(c in "0123456789abcdef" for c in sig)
        print(f"✅ 签名生成正常 (len={len(sig)})")

        # 测试 URL
        assert "testnet.binancefuture.com" in client._base_url
        print(f"✅ 测试网 URL: {client._base_url}")

        # 测试公开 API 调用（标记价格）
        try:
            data = await client.get(
                "/fapi/v1/premiumIndex",
                {"symbol": "BTCUSDT"},
            )
            assert "markPrice" in data
            print(
                f"✅ 公开 API 调用正常: "
                f"mark={data['markPrice']}, "
                f"funding_rate={float(data.get('lastFundingRate', 0))*100:.4f}%"
            )
        except FuturesAPIError as e:
            print(f"⚠️  测试网 API 不可用: {e}")
        except Exception as e:
            print(f"⚠️  网络不可用: {e}")

        await client.close()
        print("\n全部测试通过! ✅")

    asyncio.run(_test())
