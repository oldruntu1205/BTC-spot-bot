"""
BTC Spot Bot — V2 Edge Score 多因子策略 主入口

策略: Edge Score (5因子) 现货买入 + USDⓈ-M 永续合约动态对冲

启动方式:
    python main.py                          # 使用默认配置
    BINANCE_API_KEY=xxx python main.py      # 环境变量注入 API Key

架构:
    main.py → app.services.TradingService → exchange / strategy / risk / database

模块可独立测试: python main.py --dry-run
"""
from __future__ import annotations

import sys
import asyncio
from pathlib import Path

# 将项目根目录加入 Python 路径
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger

from app.core.config import load_config
from app.services import TradingService


def setup_logging(config) -> None:
    """
    配置 loguru 日志系统

    双输出:
      - stderr: 彩色格式，实时查看
      - 文件: 结构化格式，持久保存

    Args:
        config: AppSettings 配置对象
    """
    # 移除默认处理器
    logger.remove()

    # 控制台输出（彩色）
    logger.add(
        sys.stderr,
        level=config.logging.level,
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<level>{message}</level>"
        ),
        colorize=True,
    )

    # 文件输出（持久化）
    log_dir = Path(config.logging.file).parent
    log_dir.mkdir(parents=True, exist_ok=True)

    logger.add(
        config.logging.file,
        level=config.logging.level,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | "
            "{name}:{function}:{line} | {message}"
        ),
        rotation=config.logging.rotation,
        retention=config.logging.retention,
        encoding="utf-8",
    )


async def main() -> None:
    """主入口函数"""
    try:
        # 1. 加载配置
        cfg = load_config()
        setup_logging(cfg)

        # 2. 验证配置
        if not cfg.is_configured:
            logger.error("=" * 50)
            logger.error("  API Key 未配置!")
            logger.error("  请通过以下方式之一配置:")
            logger.error("  1. 环境变量: export BINANCE_API_KEY=xxx")
            logger.error("  2. .env 文件: cp .env.example .env 后编辑")
            logger.error("=" * 50)
            sys.exit(1)

        # 3. 启动交易服务
        service = TradingService(cfg)

        try:
            await service.start()
        except KeyboardInterrupt:
            logger.info("收到中断信号 (Ctrl+C)")
        finally:
            await service.stop()

    except ValueError as e:
        logger.error(f"配置错误: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"启动失败: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
