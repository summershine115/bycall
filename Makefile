# Bycall 常用开发入口。目标名保持简洁、跨平台一致（需 GNU make）。
# Python 解释器固定 3.12（本机 python3.12）。
PY := python3.12
PKG := src/bycall
VENV := .venv

.PHONY: help venv install dev test test-unit test-integration lint fmt check ci-local docker-up docker-down clean

help:  ## 显示所有可用目标
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

venv:  ## 创建虚拟环境 (.venv)
	$(PY) -m venv $(VENV)

install:  ## 安装运行时 + 开发依赖到当前环境
	$(PY) -m pip install -U pip
	$(PY) -m pip install -e ".[dev]"

dev:  ## 本地启动 API（热重载，端口 8000）
	PYTHONPATH=src $(PY) -m uvicorn bycall.api.app:create_app --factory --reload --port 8000

test: test-unit  ## 跑全部测试（默认仅 unit；integration 需外部服务）

test-unit:  ## 仅跑单元测试
	$(PY) -m pytest tests/unit -q

test-integration:  ## 跑集成测试（需先 docker-up）
	$(PY) -m pytest tests/integration -q -m integration

lint:  ## 静态检查
	$(PY) -m ruff check $(PKG) tests

fmt:  ## 格式化
	$(PY) -m ruff format $(PKG) tests
	$(PY) -m ruff check --fix $(PKG) tests

check: lint  ## CI 本地等价检查（lint only；test/build 在 CI 里跑）
	$(PY) -m ruff format --check $(PKG) tests

ci-local: lint test-unit  ## 本地模拟 CI：lint + unit test

docker-up:  ## 起 postgres + redis（内网无公网友好；zvec 嵌入式无独立服务）
	docker compose up -d postgres redis

docker-down:  ## 停 postgres + redis
	docker compose down

clean:  ## 清理缓存 / 构建产物（不删 data/）
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
