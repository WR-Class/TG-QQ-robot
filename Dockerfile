# TGJQR Bot Docker 镜像
# 多阶段构建：分离构建环境和运行环境

# === 第一阶段：构建（安装编译依赖） ===
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# === 第二阶段：运行（最小镜像） ===
FROM python:3.11-slim

# 创建非 root 用户
RUN groupadd -r app && useradd -r -g app -d /app -s /sbin/nologin app

WORKDIR /app

# 只复制 pip 安装的包，不含编译工具链
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages

# 复制项目代码（排除运行时数据和敏感文件）
COPY --chown=app:app main.py config.py group_member_store.py ./
COPY --chown=app:app napcat_bridge.py napcat_ws.py web_server.py ./
COPY --chown=app:app handlers/ ./handlers/
COPY --chown=app:app models/ ./models/
COPY --chown=app:app web/ ./web/

# 创建数据目录
RUN mkdir -p /app/data && chown app:app /app/data

# 环境变量
ENV PYTHONUNBUFFERED=1

# 切换到非 root 用户
USER app

# 健康检查：先检查进程是否存活，再检查 HTTP 端口是否就绪
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "
import urllib.request, sys
try:
    r = urllib.request.urlopen('http://localhost:58080/health', timeout=5)
    sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
"

CMD ["python", "main.py"]
