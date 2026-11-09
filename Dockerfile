FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove 2>/dev/null || true

# 非 root 用户（安全要求 §13.3）
RUN useradd --create-home --uid 10001 e5pulse \
    && mkdir -p /data/logs \
    && chown -R e5pulse:e5pulse /data

COPY src/ ./src/

USER e5pulse

# 简单健康检查（Phase 3 启用 /health HTTP 端点后可改 curl）
# 容器内无 curl，健康检查以状态文件存在性兜底
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD test -f /data/status.json || exit 1

ENTRYPOINT ["python", "-m", "src.main"]
