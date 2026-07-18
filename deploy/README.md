# BTC Spot Bot V1.1 — VPS 部署指南

## 30 秒快速启动 (Docker)

```bash
# 1. 克隆 + 配置
git clone <repo-url> /opt/btc-spot-bot && cd /opt/btc-spot-bot
cp .env.example .env
vim .env   # 填入真实的 Binance API Key

# 2. 一键启动
bash deploy/start.sh
# 选择: 1 (Docker)
```

## 裸机部署 (systemd)

```bash
bash deploy/start.sh
# 选择: 2 (systemd)
```

## 日常管理

```bash
# Docker
docker compose logs -f        # 查看日志
docker compose restart        # 重启
docker compose down           # 停止

# systemd
sudo journalctl -u btc-bot -f  # 查看日志
sudo systemctl restart btc-bot # 重启
sudo systemctl stop btc-bot    # 停止
```

## 测试网先行！

在 `config/settings.yaml` 中确保 `exchange.testnet: true`，用 [Binance Testnet](https://testnet.binance.vision/) 的 Key 先跑几天确认无误再切主网。

## 文件结构

```
/opt/btc-spot-bot/
├── app/           # 策略 + 风控 + 交易所
├── config/        # YAML 配置
├── deploy/        # 部署脚本
├── logs/          # 运行日志 (自动创建)
├── data/          # SQLite 数据库 (自动创建)
├── .env           # API Key (不提交 Git)
├── Dockerfile
├── docker-compose.yml
└── main.py        # 入口
```
