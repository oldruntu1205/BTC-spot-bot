#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
# BTC Spot Bot V1.1 — VPS 一键部署脚本
# ═══════════════════════════════════════════════════════════════
# 用法:
#   curl -fsSL https://your-repo/start.sh | bash
#   或本地:
#   chmod +x deploy/start.sh && ./deploy/start.sh
# ═══════════════════════════════════════════════════════════════
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PROJECT_DIR="/opt/btc-spot-bot"
PYTHON_BIN="python3.11"

echo -e "${GREEN}"
echo "╔══════════════════════════════════════════════╗"
echo "║   BTC Spot Bot V1.1 — 一键部署              ║"
echo "║   Edge Score 多因子策略 + 动态对冲          ║"
echo "╚══════════════════════════════════════════════╝"
echo -e "${NC}"

# ── 1. 环境检查 ──────────────────────────────
echo -e "${YELLOW}[1/6] 检查环境...${NC}"

if ! command -v $PYTHON_BIN &>/dev/null; then
    echo -e "${RED}错误: 需要 Python 3.11+，正在安装...${NC}"
    sudo apt-get update && sudo apt-get install -y python3.11 python3.11-venv python3-pip
fi

if ! command -v git &>/dev/null; then
    echo -e "${YELLOW}安装 git...${NC}"
    sudo apt-get install -y git
fi

echo -e "${GREEN}  ✓ Python: $($PYTHON_BIN --version)${NC}"
echo -e "${GREEN}  ✓ Git: $(git --version | head -1)${NC}"

# ── 2. 克隆/更新代码 ─────────────────────────
echo -e "${YELLOW}[2/6] 部署代码...${NC}"

if [ -d "$PROJECT_DIR/.git" ]; then
    echo "  检测到已有代码，执行 git pull..."
    cd "$PROJECT_DIR"
    git pull origin main
else
    # 如果通过 curl | bash 运行，需要你手动克隆
    if [ ! -d "$PROJECT_DIR" ]; then
        echo -e "${RED}  请先克隆项目: git clone <repo-url> $PROJECT_DIR${NC}"
        echo -e "${RED}  然后重新运行: cd $PROJECT_DIR && bash deploy/start.sh${NC}"
        exit 1
    fi
fi

cd "$PROJECT_DIR"

# ── 3. 配置 API Key ──────────────────────────
echo -e "${YELLOW}[3/6] 检查 API Key 配置...${NC}"

if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo -e "${RED}  ⚠ 请编辑 .env 文件填入真实的 Binance API Key:${NC}"
        echo -e "${RED}    vim $PROJECT_DIR/.env${NC}"
        echo -e "${RED}    测试网 Key: https://testnet.binance.vision/${NC}"
        echo ""
        read -p "  是否现在编辑? (y/n) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            ${EDITOR:-vim} .env
        else
            echo -e "${RED}  请稍后手动编辑 .env 文件，然后重新运行此脚本${NC}"
            exit 1
        fi
    fi
fi

# 检查是否已填入真实 Key
if grep -q "your_api_key_here" .env 2>/dev/null; then
    echo -e "${RED}  ⚠ .env 中仍是示例 Key，请先填入真实 API Key${NC}"
    exit 1
fi
echo -e "${GREEN}  ✓ API Key 已配置${NC}"

# ── 4. 安装依赖 ──────────────────────────────
echo -e "${YELLOW}[4/6] 安装 Python 依赖...${NC}"

# 创建虚拟环境
if [ ! -d "venv" ]; then
    $PYTHON_BIN -m venv venv
fi
source venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo -e "${GREEN}  ✓ 依赖安装完成${NC}"

# ── 5. 创建必要目录 ──────────────────────────
echo -e "${YELLOW}[5/6] 初始化目录...${NC}"
mkdir -p logs data
echo -e "${GREEN}  ✓ 目录就绪${NC}"

# ── 6. 选择启动方式 ──────────────────────────
echo -e "${YELLOW}[6/6] 启动服务...${NC}"
echo ""
echo "  选择启动方式:"
echo "  1) Docker (推荐 — 隔离环境，自动重启)"
echo "  2) systemd (裸机 — 需要 root)"
echo "  3) 前台运行 (调试用)"
echo ""

read -p "  请输入选项 [1-3]: " choice

case $choice in
    1)
        echo -e "${GREEN}Docker 部署...${NC}"
        if ! command -v docker &>/dev/null; then
            echo -e "${RED}Docker 未安装。安装命令: curl -fsSL https://get.docker.com | bash${NC}"
            exit 1
        fi
        docker compose up -d --build
        echo -e "${GREEN}✅ 已启动! 查看日志: docker compose logs -f${NC}"
        ;;
    2)
        echo -e "${GREEN}systemd 部署...${NC}"
        sudo cp deploy/btc-bot.service /etc/systemd/system/
        sudo sed -i "s|/opt/btc-spot-bot|$PROJECT_DIR|g" /etc/systemd/system/btc-bot.service
        sudo sed -i "s|User=botuser|User=$USER|g" /etc/systemd/system/btc-bot.service
        sudo sed -i "s|Group=botuser|Group=$USER|g" /etc/systemd/system/btc-bot.service
        sudo systemctl daemon-reload
        sudo systemctl enable --now btc-bot
        echo -e "${GREEN}✅ 已启动! 查看日志: sudo journalctl -u btc-bot -f${NC}"
        ;;
    3)
        echo -e "${GREEN}前台运行 (Ctrl+C 停止)...${NC}"
        echo ""
        $PYTHON_BIN main.py
        ;;
    *)
        echo -e "${RED}无效选项${NC}"
        exit 1
        ;;
esac

echo ""
echo -e "${GREEN}══════════════════════════════════════════════${NC}"
echo -e "${GREEN}  部署完成!${NC}"
echo -e "${GREEN}  日志目录: $PROJECT_DIR/logs/${NC}"
echo -e "${GREEN}  数据库:   $PROJECT_DIR/data/trading.db${NC}"
echo -e "${GREEN}══════════════════════════════════════════════${NC}"
