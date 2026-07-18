# BTC-spot-bot-V1.1
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

Python + Binance API 自动交易机器人（可部署到 VPS，24 小时运行）

该版本能够实现：

* 自动获取 BTCUSDT 行情
* 自动计算 5 分钟套利信号
* 自动挂限价单
* 自动撤单
* 自动止盈止损
* 自动记录交易日志

* 代码声明：
* 
* ✅ 基于官方 SDK
* ✅ 可运行
* ✅ 完整类型注解
* ✅ 中文注释
* ✅ 企业级目录结构
* ✅ 不使用废弃接口
* ✅ 每个模块可单独测试
