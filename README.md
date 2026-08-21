# Weather-aware Travel Planner Agent

面向全球城市的多工具旅行规划 Agent。系统先从近期预报中选择连续天气窗口，再从 Wikivoyage 提取景点，结合用户兴趣评分、OpenRouteService（ORS）交通矩阵、容量约束聚类和路径算法生成多日路线。

当前工程同时提供同步演示接口和可恢复的异步执行链路：FastAPI 接收请求，PostgreSQL 持久化 Session、Message、Run 与 SSE Event，独立 Worker 使用数据库租约领取任务、续租、失败重试，并阻止失去租约的旧 Worker 写回结果。

## 架构

```mermaid
flowchart LR
    U[Web / 飞书] --> API[FastAPI]
    API -->|Session / Message / Run| PG[(PostgreSQL)]
    API -->|同步演示| AGENT[LangChain Agent]
    WORKER[独立 Worker] -->|FOR UPDATE SKIP LOCKED| PG
    WORKER -->|心跳续租 / 重试| PG
    WORKER --> AGENT
    AGENT --> WEATHER[天气 Tool]
    WEATHER --> GUIDE[攻略与景点 Tool]
    GUIDE --> ROUTE[评分 / 聚类 / 路由]
    ROUTE --> AGENT
    AGENT -->|结果与事件| PG
    PG -->|SSE 回放 / 断线续传| API
    API --> U
```

请求流程：创建 Session → 提交 Message → 创建 `PENDING` Run → Worker 领取并执行 → 持久化进度事件 → 前端通过 SSE 接收进度 → 查询最终 Run。

## 主要目录

| 路径 | 职责 |
| --- | --- |
| `weather_tool.py` | Open-Meteo 查询、天气归一化评分和连续日期窗口 |
| `travel_planner/` | 数据源、评分、聚类、天气分配、路由和 Schema |
| `weather_window.py` | 显式 Agent/CLI 入口，导入时不会调用模型或网络 |
| `app/main.py` | FastAPI 路由、同步接口、Run API 和 SSE |
| `app/models.py` | SQLAlchemy Session、Message、Run、Event 模型 |
| `app/store.py` | 事务、任务领取、租约、重试、取消和事件持久化 |
| `app/worker.py` | 独立 Worker、租约心跳和 Agent 执行 |
| `migrations/` | Alembic PostgreSQL Schema 迁移 |
| `tests/` | 离线单元测试和 PostgreSQL 集成测试 |

架构决策见 [ADR 0001](docs/adr/0001-modular-travel-planner.md)、[ADR 0002](docs/adr/0002-secrets-and-runtime-side-effects.md)、[ADR 0003](docs/adr/0003-durable-worker-and-sse.md) 和 [ADR 0004](docs/adr/0004-postgresql-worker-leases.md)。

## 数据源

| 数据源 | 用途 | 限制 |
| --- | --- | --- |
| Open-Meteo | 地理编码和近期逐日天气 | 可用预报窗口有限，远期结果不确定 |
| Wikivoyage MediaWiki API | 全球城市攻略、See/Do Listing | 城市与语言版本覆盖不均；遵守 CC BY-SA 归属要求 |
| OpenRouteService | 地理编码、距离和时间矩阵 | 需要 `ORS_API_KEY`；配额有限，当前不含公交班次 |
| DeepSeek | 意图识别、Tool 调用和回答组织 | 需要 `DEEPSEEK_API_KEY`；确定性评分和路线仍由代码完成 |

项目不依赖绕过登录、验证码或反爬机制的小红书/携程抓取。酒店和票务应接入获得授权的开放 API、联盟 API 或沙箱，并在真实预订或支付前要求用户确认。

## 本地安装

要求：Python 3.13、[uv](https://docs.astral.sh/uv/)、Docker Desktop（或可访问的 PostgreSQL）。

```powershell
cd D:\project_of_python
uv sync --dev
Copy-Item .env.example .env
```

只在本机 `.env` 中填写密钥并修改数据库密码；`.env` 已被 Git 忽略，不得提交。`.env.example` 只能保留无效占位值。

## 启动 PostgreSQL 与迁移

使用项目自带的 Compose 服务：

```powershell
docker compose up -d --wait postgres
docker compose ps
uv run alembic upgrade head
uv run alembic current
```

默认开发连接为：

```dotenv
DATABASE_URL=postgresql+psycopg://diy_agent:change-me-for-local-development@localhost:5432/diy_agent
```

如果修改 `POSTGRES_USER`、`POSTGRES_PASSWORD`、`POSTGRES_DB` 或端口，必须同步修改 `DATABASE_URL`。生产环境应由 Secret 管理服务注入连接串，并启用 TLS、独立账号和最小权限。

应用启动时只检查数据库连接，不会自动建表。每次部署应先执行：

```powershell
uv run alembic upgrade head
```

新增 Schema 变更时先修改模型，再生成并人工审查迁移：

```powershell
uv run alembic revision --autogenerate -m "describe schema change"
uv run alembic upgrade head
```

旧版 `resources/agent_api.db` 是本地 Demo 数据，不会自动导入 PostgreSQL。当前表里没有必须保留的生产数据时，建议保留为备份或删除；若要保留，需要单独编写一次性、可校验且可重复执行的数据迁移脚本。

## 启动 API 与 Worker

打开两个 PowerShell。第一个启动 API：

```powershell
uv run uvicorn app.main:app --reload --env-file .env
```

第二个启动 Worker：

```powershell
uv run python -m app.worker --env-file .env
```

访问 `http://127.0.0.1:8000/docs` 查看 OpenAPI 文档。

| 接口 | 用途 |
| --- | --- |
| `GET /health` | 检查 API 与数据库连接 |
| `POST /v1/agent/invoke` | 同步调用 Agent，仅适合开发演示 |
| `POST /v1/sessions` | 创建会话 |
| `POST /v1/sessions/{id}/messages` | 保存消息并返回异步 Run |
| `GET /v1/runs/{run_id}` | 查询状态、尝试次数和最终结果 |
| `GET /v1/runs/{run_id}/events` | SSE 进度流，支持 `Last-Event-ID` 续传 |
| `POST /v1/runs/{run_id}/cancel` | 请求取消任务 |

Worker 通过 `WORKER_LEASE_SECONDS`、`WORKER_HEARTBEAT_SECONDS`、`WORKER_RETRY_DELAY_SECONDS` 和 `WORKER_MAX_ATTEMPTS` 控制租约与重试。心跳周期必须小于租约时长。PostgreSQL 的 `FOR UPDATE SKIP LOCKED` 允许多个 Worker 并发领取不同任务；租约过期后任务可被回收，旧 Worker 的结果会被 fencing 检查拒绝。

## 测试

默认测试使用临时 SQLite，仅作为快速、隔离的持久化替身，不是正式运行时：

```powershell
uv run pytest
git diff --check
```

PostgreSQL 集成测试会清空测试库的 `public` Schema，因此数据库名被强制要求以 `_test` 结尾，绝不能指向开发库或生产库：

```powershell
$env:TEST_DATABASE_URL="postgresql+psycopg://diy_agent:password@localhost:5432/diy_agent_test"
uv run pytest -m postgres
```

当前默认回归结果为 `21 passed, 1 skipped`；跳过项是未设置 `TEST_DATABASE_URL` 时的 PostgreSQL 集成测试。

![离线测试演示](docs/images/test-demo.svg)

## 日志、编码与安全

- `.editorconfig`、`.gitattributes` 与 `PYTHONUTF8=1` 统一 UTF-8。
- `logging_config.py` 输出 UTF-8 JSON Lines，并使用稳定的 `event`、`request_id` 和 `error_code` 字段。
- 不记录 API Key、数据库密码、Cookie 或完整个人信息。
- API Key 和数据库连接串只能来自环境变量或部署平台 Secret；提交前运行 `git check-ignore .env`。
- API 对外部署前仍需加入认证、授权、限流和配额控制。

## 当前限制与路线图

- 天气只在数据源近期预报范围内可靠，不能声称准确预测数月后的最佳日期。
- Wikivoyage 的全球覆盖和结构化程度不均，开放时间、票价和停业状态需出发前复核。
- 路线暂不包含实时拥堵、公交班次、预约时段、无障碍和跨日行李约束。
- PostgreSQL 目前同时承担持久化与任务分派；更大吞吐量下应评估 Redis、RabbitMQ 或托管任务队列。
- 尚未实现身份认证、用户偏好表、暂停/恢复 checkpoint、OpenTelemetry、酒店/机票授权供应商适配和支付确认。

建议后续顺序：身份与用户偏好 → OpenTelemetry → 酒店只读推荐 → 授权票务沙箱与人工确认 → 飞书 Bot。

## 提交前检查

```powershell
git status --short
git check-ignore .env
git diff --check
uv run pytest
```

如果密钥曾进入提交，仅增加 `.gitignore` 不能消除泄露：必须立即轮换密钥，并在推送前清理 Git 历史。
