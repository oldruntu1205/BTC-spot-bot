# BTC Spot Bot V1.1 — Docker 镜像
# 构建: docker build -t btc-spot-bot:v1.1 .
# 运行: docker run -d --env-file .env --name btc-bot btc-spot-bot:v1.1
FROM python:3.11-slim

LABEL maintainer="btc-spot-bot"
LABEL description="BTC Spot Bot V1.1 — Edge Score 多因子策略"

# 设置工作目录
WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY app/ ./app/
COPY config/ ./config/
COPY main.py .

# 创建必要目录
RUN mkdir -p logs data

# 非 root 用户运行
RUN useradd -m -s /bin/bash botuser && chown -R botuser:botuser /app
USER botuser

# 健康检查 — 每 60s 检查进程是否存活
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
    CMD pgrep -f "python main.py" || exit 1

# 启动
CMD ["python", "main.py"]
