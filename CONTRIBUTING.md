# 贡献指南

## 开发流程

1. 每个可验证变更对应一个 Issue；外部数据源变更须记录许可、配额和降级策略。
2. 架构或不可逆技术选择先在 `docs/adr/` 新增 ADR。
3. 分支使用 `feat/`、`fix/`、`refactor/` 或 `docs/` 前缀。
4. 提交应小而完整，推荐 `feat:`、`fix:`、`refactor:`、`test:`、`docs:`、`chore:`；不要把全部项目压成一个 `initial commit`。
5. PR 必须包含测试证据、风险、回滚方式和关联 Issue。

## 提交前检查

```powershell
uv sync --dev
uv run pytest
uv run alembic heads
uv run alembic upgrade head --sql
uv run python -m compileall -q app migrations tests
git diff --check
```

数据库模型变更必须同时提交经过人工审查的 Alembic migration。需要运行 PostgreSQL 集成测试时，只能把 `TEST_DATABASE_URL` 指向名称以 `_test` 结尾的专用数据库；测试会清空该数据库的 `public` Schema。

严禁提交 `.env`、真实密钥、用户原始隐私数据、本地 checkpoint 数据库和包含 Cookie 的抓取结果。若密钥曾进入 Git 历史，仅删除文件不够，必须立即轮换密钥并清理历史。
