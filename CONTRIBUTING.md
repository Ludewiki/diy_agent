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
python -m py_compile weather_tool.py weather_window.py travel_planner_tool.py
```

严禁提交 `.env`、真实密钥、用户原始隐私数据、本地 checkpoint 数据库和包含 Cookie 的抓取结果。若密钥曾进入 Git 历史，仅删除文件不够，必须立即轮换密钥并清理历史。
