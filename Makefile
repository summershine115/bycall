# Bycall 常用开发入口。目标名保持简洁、跨平台一致（需 GNU make）。
# Python 解释器固定 3.12（本机 python3.12）。
PY := $(if $(wildcard .venv/bin/python),.venv/bin/python,python3.12)
PKG := src/bycall
VENV := .venv

.PHONY: help venv install dev test test-unit test-integration lint fmt check ci-local eval eval-security seed-reference docker-up docker-down clean

help:  ## 显示所有可用目标
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

venv:  ## 创建虚拟环境 (.venv)
	$(PY) -m venv $(VENV)

install:  ## 安装运行时 + 开发依赖到当前环境
	$(PY) -m pip install -U pip
	$(PY) -m pip install -e ".[dev]"

dev:  ## 本地启动 API（热重载，端口 8000）
	PYTHONPATH=src $(PY) -m uvicorn bycall.api.app:create_app --factory --reload --port 8000

test:  ## 跑全部本地测试（unit + 使用临时依赖的 integration；-n auto 并行）
	$(PY) -m pytest tests -q -n auto

test-unit:  ## 仅跑单元测试（并行）
	$(PY) -m pytest tests/unit -q -n auto

test-integration:  ## 跑 integration 目录（当前无需 docker，均使用临时依赖/mock）
	$(PY) -m pytest tests/integration -q -n auto

lint:  ## 静态检查
	$(PY) -m ruff check $(PKG) tests evals examples scripts

fmt:  ## 格式化
	$(PY) -m ruff format $(PKG) tests evals examples scripts
	$(PY) -m ruff check --fix $(PKG) tests evals examples scripts

check: lint  ## CI 本地等价检查（lint only；test/build 在 CI 里跑）
	$(PY) -m ruff format --check $(PKG) tests evals examples scripts

eval:  ## 跑四类评测集（知识质量/权限安全/执行质量/运营效果）
	PYTHONPATH=src $(PY) -m evals.run

eval-security:  ## 只跑权限安全红线用例（任一失败即红灯）
	PYTHONPATH=src $(PY) -m evals.run --security

seed-reference:  ## 灌入参考场景包（示例角色/账号/MCP 连接器，幂等；见 ADR-0009）
	PYTHONPATH=src $(PY) scripts/seed_reference.py

ci-local: lint test eval eval-security  ## 本地模拟 CI：lint + 全部测试 + 评测门

docker-up:  ## 起 postgres + redis（内网无公网友好；zvec 嵌入式无独立服务）
	docker compose up -d postgres redis

docker-down:  ## 停 postgres + redis
	docker compose down

clean:  ## 清理缓存 / 构建产物（不删 data/）
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
