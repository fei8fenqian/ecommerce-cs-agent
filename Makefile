# =============================================================================
# Makefile — 项目命令入口
# 用法：make <命令名>
# 新同事 clone 下来先敲 make install，然后看 Makefile 就知道所有操作
# =============================================================================  

# -------------------------------------------------------------------
# 安装依赖
# -------------------------------------------------------------------
install:
		pip install -e ".[dev]" -i https://pypi.tuna.tsinghua.edu.cn/simple     


# -------------------------------------------------------------------
# 代码检查（提交前必跑）
# ruff check：检查代码风格 + 逻辑错误
# mypy：类型检查
# -------------------------------------------------------------------
lint:
	ruff check scripts/
	ruff check src/ 2>/dev/null || true
	mypy src/ 2>/dev/null || true

# -------------------------------------------------------------------
# 自动格式化
# ruff format：统一双引号、import 排序、换行风格
# -------------------------------------------------------------------
format:
	ruff format scripts/
	ruff format src/ 2>/dev/null || true

# -------------------------------------------------------------------
# 运行测试
# -------------------------------------------------------------------
TEST_DB ?= ecommerce_agent_full_test
REFUND_TEST_DB ?= ecommerce_agent_refund_test
S3_TEST_DB ?= ecommerce_agent_s3_02_test
V7_TEST_DB ?= ecommerce_agent_v7_test

test:
		PG_DBNAME=$(TEST_DB) pytest -v

# 独立 Alembic 分支的验证必须使用独立数据库，不能和客服 Agent 共享库混跑。
test-refund-integration:
		SKIP_SHARED_DB_SETUP=1 PG_DBNAME=$(REFUND_TEST_DB) pytest -v tests/test_checkout_refund_integration.py

test-s3-integration:
		SKIP_SHARED_DB_SETUP=1 PG_DBNAME=$(S3_TEST_DB) pytest -v tests/test_s3_01_schema.py tests/test_s3_02_store_integration.py

test-v7-schema:
		SKIP_SHARED_DB_SETUP=1 PG_DBNAME=$(V7_TEST_DB) pytest -v tests/test_v7_01a_schema.py

# -------------------------------------------------------------------
# 跑评估（Phase 2 后面用）
# -------------------------------------------------------------------
eval:
	python scripts/eval.py

# -------------------------------------------------------------------
# 数据注入（知识库 + 产品一起灌）
# -------------------------------------------------------------------
ingest:
	python -m scripts.ingest.knowledge
	python -m scripts.ingest.laptops
	python -m scripts.ingest.phones

# -------------------------------------------------------------------
# 清理缓存
# -------------------------------------------------------------------
clean:
		rm -rf __pycache__ .pytest_cache .mypy_cache
		find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true     
		find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true 
