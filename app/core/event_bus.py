"""
事件总线 — 异步发布/订阅模式的事件分发系统

基于 asyncio.Queue 实现，解耦各模块间的通信。
所有模块通过 EventBus 发布和订阅事件，无需直接引用。

模块可独立测试: python -m app.core.event_bus
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any, Callable, Coroutine

from loguru import logger

from .types import Event, EventType

# 事件处理器类型别名
EventHandler = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    """
    异步事件总线

    使用方式:
        # 订阅
        async def handle_order_filled(event: Event) -> None:
            ...

        event_bus.subscribe(EventType.ORDER_FILLED, handle_order_filled)

        # 发布
        await event_bus.publish(Event(EventType.ORDER_FILLED, {"order": order}))
    """

    def __init__(self) -> None:
        # 事件类型 → 处理器列表
        self._handlers: dict[EventType, list[EventHandler]] = defaultdict(list)
        # 异步事件队列
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=10000)
        self._running: bool = False
        self._dispatch_task: asyncio.Task[None] | None = None

    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """
        订阅指定类型的事件

        Args:
            event_type: 要订阅的事件类型
            handler: 异步回调函数，接收 Event 对象
        """
        self._handlers[event_type].append(handler)
        logger.debug(f"订阅事件 [{event_type.name}] -> {handler.__name__}")

    def unsubscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """取消订阅"""
        try:
            self._handlers[event_type].remove(handler)
            logger.debug(f"取消订阅 [{event_type.name}] -> {handler.__name__}")
        except ValueError:
            pass

    async def publish(self, event: Event) -> None:
        """
        异步发布事件（非阻塞）

        Args:
            event: 要发布的事件对象
        """
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(f"事件队列已满，丢弃事件: {event.type.name}")

    def publish_sync(self, event: Event) -> None:
        """
        同步发布事件（用于非异步上下文）

        Args:
            event: 要发布的事件对象
        """
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    async def start(self) -> None:
        """启动事件分发循环"""
        self._running = True
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        logger.info("事件总线已启动")

    async def stop(self) -> None:
        """停止事件分发"""
        self._running = False
        if self._dispatch_task is not None:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
        logger.info("事件总线已停止")

    async def _dispatch_loop(self) -> None:
        """事件分发主循环 — 从队列取事件并分发给所有订阅者"""
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                await self._dispatch(event)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"事件分发异常: {e}")

    async def _dispatch(self, event: Event) -> None:
        """
        将事件分发给所有注册的处理器

        Args:
            event: 要分发的事件
        """
        handlers = self._handlers.get(event.type, [])
        if not handlers:
            return

        # 并发执行所有处理器，不阻塞
        tasks = [asyncio.create_task(self._safe_invoke(h, event)) for h in handlers]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _safe_invoke(self, handler: EventHandler, event: Event) -> None:
        """
        安全调用处理器，捕获异常避免影响其他处理器

        Args:
            handler: 事件处理器
            event: 事件对象
        """
        try:
            await handler(event)
        except Exception as e:
            logger.error(f"事件处理器 [{handler.__name__}] 异常: {e}")


# 全局事件总线单例
event_bus = EventBus()


# ═══════════════════════════════════════════════════════
# 独立测试入口
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    async def _test() -> None:
        """测试事件总线核心功能"""
        print("=" * 50)
        print("事件总线模块 — 独立测试")
        print("=" * 50)

        bus = EventBus()
        received_events: list[Event] = []

        async def handler(event: Event) -> None:
            received_events.append(event)

        # 测试订阅
        bus.subscribe(EventType.ORDER_FILLED, handler)
        assert len(bus._handlers[EventType.ORDER_FILLED]) == 1
        print("✅ 订阅正常")

        # 测试发布
        await bus.start()
        await bus.publish(Event(EventType.ORDER_FILLED, {"test": True}))
        await asyncio.sleep(0.2)
        await bus.stop()

        assert len(received_events) == 1
        assert received_events[0].type == EventType.ORDER_FILLED
        assert received_events[0].data["test"] is True
        print("✅ 发布/分发正常")

        # 测试取消订阅
        bus.unsubscribe(EventType.ORDER_FILLED, handler)
        assert len(bus._handlers[EventType.ORDER_FILLED]) == 0
        print("✅ 取消订阅正常")

        print("\n全部测试通过! ✅")

    asyncio.run(_test())
