# BTC-spot-bot-V1.1

Python + Binance API Automated Trading Bot
VPS-Ready · 24/7 Execution · Institutional-Grade Structure

This version implements:

Automatic BTCUSDT market data fetching

Automatic 5‑minute arbitrage signal calculation

Automatic limit order placement

Automatic order cancellation on timeout

Built‑in take‑profit and stop‑loss

Comprehensive trade logging

Bot Configuration

Market: BTC Spot (BTCUSDT)

Timeframe: 5 minutes

Order Type: Limit orders only

Strategy: Order book spread arbitrage

Entry Logic: Places orders only on the statistically favorable side

Hedging: Directional risk hedging

Risk Management:

Maximum position size

Maximum drawdown limit

Daily loss cap

Timeout‑based order cancellation

Slippage control


What’s Included

✅TradingView Pine Script (strategy backtesting)

✅Python automated trading bot (Binance API)

✅CCXT‑compatible version

✅Hummingbot strategy module

✅Freqtrade strategy module

✅Docker one‑click deployment

✅VPS auto‑run scripts

✅AI‑powered parameter auto‑optimization module


Code Quality & Compliance

✅ Built on the official Binance SDK

✅ Production‑ready and runnable

✅ Full type annotations

✅ Extensive Chinese comments

✅ Enterprise‑grade project structure

✅ No deprecated API usage

✅ Each module is independently testable

Quick Start (Preview)

bash

git clone https://github.com/oldruntu1205/btc-arbitrage-bot.git

cd btc-arbitrage-bot

cp config.example.yaml config.yaml

docker compose up -d


⚠️ Disclaimer: For educational and research purposes only. Use at your own risk.

Python + Binance API 自动交易机器人（可部署到 VPS，24 小时运行）

该版本实现：

* 自动获取 BTCUSDT 行情
* 自动计算 5 分钟套利信号
* 自动挂限价单
* 自动撤单
* 自动止盈止损
* 自动记录交易日志
* 
机器人配置

* 市场：BTC 现货（BTCUSDT）
* 周期：5 分钟
* 下单方式：仅限价单
* 策略：盘口价差套利
* 建仓：仅在存在统计优势的一侧挂单
* 对冲：方向性风险对冲
* 风控：
    * 最大仓位
    * 最大回撤
    * 单日止损
    * 超时撤单
    * 滑点限制

可输出内容

1.  TradingView Pine Script（策略回测）
2.  Python 自动交易机器人（Binance API）
3.  CCXT 版本
4.  Hummingbot Strategy
5.  Freqtrade Strategy
6.  Docker 一键部署
7.  VPS 自动运行脚本
8.  AI 参数自动优化模块


* 代码声明： 
* ✅ 基于官方 SDK
* ✅ 可运行
* ✅ 完整类型注解
* ✅ 中文注释
* ✅ 企业级目录结构
* ✅ 不使用废弃接口
* ✅ 每个模块可单独测试

MIT 许可证

版权所有 © 2026 oldruntu1205

特此免费授予任何获得本软件及相关文档文件（下称“软件”）副本的人不受限制地处理本软件的许可，包括而不限于使用、复制、修改、合并、发布、分发、再许可和/或销售本软件副本的权利，并允许本软件的接收者享有同样的权利，但须符合以下条件：

上述版权声明及本许可声明须包含在本软件的所有副本或实质部分中。

本软件是“按原样”提供的，不附带任何明示或暗示的担保，包括但不限于对适销性、特定用途适用性及不侵权的担保。在任何情况下，作者或版权持有人均不对任何索赔、损害或其他责任负责，无论是在合同诉讼、侵权诉讼或其他诉讼中，抑或是因本软件或使用本软件或其他与本软件相关的交易而产生的。
