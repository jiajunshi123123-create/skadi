FROM python:3.11-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# 安装Python依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件
COPY . .

# 暴露端口
# 8080 - Web 前端服务
# 可选: 钉钉 Bot 不需要端口
EXPOSE 8080

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

# 默认启动 Web 服务（钉钉 Bot 需显式覆盖 CMD）
# 用法:
#   Web 服务:   docker run ... (默认)
#   钉钉 Bot:   docker run ... python dingtalk_bot.py
CMD ["python", "-m", "uvicorn", "web_server:app", "--host", "0.0.0.0", "--port", "8080"]
