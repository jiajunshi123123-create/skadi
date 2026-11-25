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
# 钉钉 / 飞书 Bot 不监听端口，健康检查看心跳文件。
EXPOSE 8080

# 镜像默认跑 Web。容器里必须监听 0.0.0.0，端口映射才进得来。
# 本机直接 `python web_server.py` 不走这个 ENV，仍默认 127.0.0.1。
ENV WEB_BIND_HOST=0.0.0.0

# 打到 /health，不带密钥，响应里也没有配置。
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=5)"

# 用法:
#   Web 服务:   docker run ... (默认)
#   钉钉 Bot:   docker run ... python dingtalk_bot.py
#   飞书 Bot:   docker run ... python -m channels.feishu
CMD ["python", "-m", "config.entrypoint", "web"]
