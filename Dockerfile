# Bycall 应用镜像。
# Python 3.12-slim（zvec 需要 Python 3.10+，与 PoC 一致锁 3.12）。
FROM python:3.12-slim

# 运行时所需系统库（zvec 部分索引类型依赖）；精简镜像按需补。
# libgomp: OpenMP（numpy/zvec 向量运算）；g++ 仅构建期需要，这里不装。
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖（利用层缓存：代码变不影响依赖层）
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -e ".[dev]"

# 数据落容器卷（zvec 嵌入式数据、日志）
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# 镜像内默认零依赖可跑：状态库用 /app/data 下的 SQLite；生产用环境变量覆盖为 Postgres DSN
# （docker-compose 的 app 服务已覆盖），不依赖构建期 .env。
ENV BYCALL_POSTGRES_DSN=sqlite:////app/data/bycall.db \
    BYCALL_VECTOR_STORE_DATA_DIR=/app/data/zvec

EXPOSE 8000

# 生产入口：非 reload，多 worker（M0 先 1 worker，M5 调优）。
CMD ["python", "-m", "uvicorn", "bycall.api.app:create_app", \
     "--factory", "--host", "0.0.0.0", "--port", "8000"]
