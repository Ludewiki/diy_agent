# Weather-aware Travel Planner Agent

面向全球城市的多工具旅行规划 Agent。系统先从近期预报中选择连续天气窗口，再从 Wikivoyage 提取景点，结合用户兴趣评分、OpenRouteService（ORS）交通矩阵、容量约束聚类和路径算法生成多日路线。

当前工程提供可直接操作的 Web 产品页、同步演示接口和可恢复的异步执行链路：FastAPI 接收请求，PostgreSQL 持久化 User、AuthSession、Agent Session、Message、Run 与 SSE Event，独立 Worker 使用数据库租约领取任务、续租、失败重试，并阻止失去租约的旧 Worker 写回结果。

## 架构

```mermaid
flowchart LR
    U[Web / 飞书] -->|HttpOnly Cookie + CSRF| API[FastAPI]
    API --> AUTH[User / AuthSession]
    API -->|Session / Message / Run| PG[(PostgreSQL)]
    API -->|同步演示| AGENT[LangChain Agent]
    WORKER[独立 Worker] -->|FOR UPDATE SKIP LOCKED| PG
    WORKER -->|心跳续租 / 重试| PG
    WORKER --> CONTEXT[Token 预算 / 历史窗口 / 滚动摘要]
    WORKER --> MEMORY[MemoryService]
    MEMORY -->|事实与生命周期| PG
    MEMORY -->|跨 Session 召回| LGSTORE[LangGraph PostgresStore]
    CONTEXT -->|同一 Session 上下文| AGENT
    LGSTORE -->|已确认记忆| CONTEXT
    WORKER --> AGENT
    AGENT --> WEATHER[天气 Tool]
    WEATHER --> GUIDE[攻略与景点 Tool]
    GUIDE --> ROUTE[评分 / 聚类 / 路由]
    ROUTE --> AGENT
    AGENT -->|结果与事件| PG
    PG -->|SSE 回放 / 断线续传| API
    API --> U
    API --> OTEL[OpenTelemetry Collector]
    WORKER --> OTEL
    OTEL --> JAEGER[Jaeger Trace]
    OTEL --> PROM[Prometheus Metrics]
```

请求流程：创建 Session → 提交 Message → 创建带版本化快照的 `PENDING` Run → Worker 领取并执行 → 原子持久化 Tool 成果和进度事件 → 前端通过 SSE 接收状态并查询正式 Run 结果。

## Web 产品入口

启动 Compose 后访问 `http://127.0.0.1:8000/`。页面不需要 Node 或前端构建工具，由 FastAPI 同源托管原生 HTML/CSS/JavaScript，提供：

- 城市、旅行天数、兴趣、预算和补充偏好输入；
- 首次规划后切换为三栏工作台：左侧近期 Session，中间多轮消息、Tool 进度与上下文预算，右侧或移动端下方展示天气、地图、路线和来源；
- 支持在当前 Session 内发送最长 20,000 字的自由文本追问，例如局部调整某一天、降低预算或解释日期选择；追问继续调用 `POST /v1/sessions/{session_id}/messages`，不会创建新 Session；
- 天气 Tool 返回后实时展示前三个连续候选日期；
- 使用持久化 SSE Event 展示 Run、Agent 和 Tool 进度，断线后由服务端回放；
- 实时展示本次 Run 使用的历史消息数、滚动摘要状态和估算 Token 占用；
- “长期记忆”面板允许用户查看候选记忆，确认后跨 Session 生效，并支持手动添加、修改和删除；运行时会显示本次实际召回数量；
- Leaflet + OpenStreetMap 展示每日景点顺序，无外网或 Leaflet 不可用时降级到原生 SVG 坐标路线；
- 展示 Wikivoyage 来源、Open-Meteo/ORS 使用状态、风险提示与最终 Agent 行程。
- 左侧展示当前用户的近期 Session；管理弹窗提供分页历史、最近消息和最后 Run 状态；
- 支持重命名、归档、恢复归档与永久删除，选择会话时加载消息时间线；
- 页面刷新后自动恢复最近会话；SSE 仅恢复实时状态，天气、路线、来源、警告和最终回答统一从版本化 Run 结果恢复。

Tool 输出不会原样推送到浏览器。后端只对白名单字段生成展示快照，未知 Tool 默认不暴露任何结果；动态上游文本使用 DOM `textContent` 渲染，不作为 HTML 注入。

`GET /v1/runs/{run_id}` 的 `output` 使用 `RunResultV1`：包含请求快照、规划版本、Assistant 回答、天气、每日路线、结构化来源/警告、组件状态与 ContextUsage。Tool 完成时结果快照和 SSE 状态事件在同一事务写入；SSE 不携带 Tool 正文。失败 Run 保留已经完成的组件，旧版 `answer/reference` 在读取时自动转换为 V1。

## 鉴权与数据隔离

Web 页面提供注册、登录和退出入口。密码使用 Argon2id 哈希；登录后浏览器只持有随机、不透明的 Session Cookie，服务端仅保存它的 SHA-256 摘要。认证 Cookie 设置为 `HttpOnly`、`SameSite=Lax`，不会写入 `localStorage`；生产 HTTPS 环境必须设置 `AUTH_COOKIE_SECURE=true`。

所有 Session、Message、Run、取消与 SSE 查询都同时校验当前 `user_id`。访问其他用户或不存在的资源统一返回 404，避免泄露资源是否存在。所有会改变状态的请求同时校验：

- 可读 CSRF Cookie 与 `X-CSRF-Token` 请求头必须恒定时间匹配；
- 浏览器发送 `Origin` 时必须是当前同源地址或 `CSRF_TRUSTED_ORIGINS` 中的明确来源；
- SSE 使用同源 `EventSource`，HttpOnly Cookie 自动随请求发送，不需要把令牌暴露给 JavaScript。

认证 API 为 `GET /v1/auth/csrf`、`POST /v1/auth/register`、`POST /v1/auth/login`、`GET /v1/auth/me` 和 `POST /v1/auth/logout`。`GET /v1/sessions` 返回当前用户的分页历史 Session，并包含消息预览、消息数、最后 Run ID 和状态；`PATCH /v1/sessions/{id}` 用于重命名或归档，`DELETE /v1/sessions/{id}` 永久删除整个会话。

长期记忆 API 同样按当前用户隔离：`GET/POST /v1/memories` 用于查看和手动添加，`PATCH /v1/memories/{id}` 用于确认候选或修改内容，`DELETE /v1/memories/{id}` 写入删除墓碑。其他用户访问同一 UUID 也只会得到 404。

![Web 产品入口](docs/images/web-product.png)

## 主要目录

| 路径 | 职责 |
| --- | --- |
| `weather_tool.py` | Open-Meteo 查询、天气归一化评分和连续日期窗口 |
| `travel_planner/` | 数据源、评分、聚类、天气分配、路由和 Schema |
| `weather_window.py` | 显式 Agent/CLI 入口，导入时不会调用模型或网络 |
| `app/main.py` | FastAPI 路由、同步接口、Run API 和 SSE |
| `app/auth.py` | Argon2 密码校验、不透明 AuthSession、Cookie Token 摘要 |
| `app/demo_seed.py` | 显式启用的本地演示用户与 legacy Session 绑定 |
| `app/models.py` | SQLAlchemy User、AuthSession、Agent Session、Message、Run、Event 模型 |
| `app/store.py` | 事务、任务领取、租约、重试、取消和事件持久化 |
| `app/context.py` | Token 估算、最近历史窗口与幂等滚动摘要 |
| `app/memory.py` | 长期记忆提取、治理、业务表与 LangGraph Store 适配 |
| `app/memory_setup.py` | 显式初始化 LangGraph PostgresStore 所需表 |
| `app/worker.py` | 独立 Worker、租约心跳和 Agent 执行 |
| `app/telemetry.py` | OpenTelemetry SDK、Trace 传播和业务 Metrics |
| `app/web/` | 无构建 Web 页面、响应式视觉、SSE 和地图交互 |
| `migrations/` | Alembic PostgreSQL Schema 迁移 |
| `observability/` | Collector、Prometheus 抓取与 recording rules |
| `benchmarks/` | PostgreSQL 并发、Exactly-once 和租约回收基准 |
| `tests/` | 离线单元测试和 PostgreSQL 集成测试 |

架构决策见 [ADR 0001](docs/adr/0001-modular-travel-planner.md)、[ADR 0002](docs/adr/0002-secrets-and-runtime-side-effects.md)、[ADR 0003](docs/adr/0003-durable-worker-and-sse.md)、[ADR 0004](docs/adr/0004-postgresql-worker-leases.md)、[ADR 0005](docs/adr/0005-container-runtime-and-ci.md)、[ADR 0006](docs/adr/0006-opentelemetry-and-runtime-benchmark.md)、[ADR 0007](docs/adr/0007-web-product-entry.md)、[ADR 0008](docs/adr/0008-multi-turn-context-and-short-term-memory.md)、[ADR 0009](docs/adr/0009-browser-auth-and-tenant-isolation.md)、[ADR 0010](docs/adr/0010-session-history-and-local-demo-data.md)、[ADR 0011](docs/adr/0011-versioned-run-results.md) 和 [ADR 0012](docs/adr/0012-governed-long-term-memory.md)。

## Docker Compose 一键启动

复制环境变量模板，在本机 `.env` 中填写密钥，然后构建并启动完整运行栈：

```powershell
Copy-Item .env.example .env
docker compose up -d --build --wait
docker compose ps -a
```

Compose 使用同一个非 root 应用镜像承担 API、Worker、迁移和本地数据初始化角色，并按依赖顺序启动：

| 服务 | 生命周期与职责 |
| --- | --- |
| `postgres` | 长期运行；持久化会话、任务和 SSE 事件 |
| `migration` | 一次性运行；数据库健康后执行 `alembic upgrade head`，成功退出后才放行应用 |
| `memory-setup` | 一次性运行；显式创建/升级 LangGraph Store 表，成功后才放行应用 |
| `demo-seed` | 一次性运行；仅在本地开关启用时选择演示用户并绑定 legacy Session |
| `api` | 长期运行；提供 FastAPI、OpenAPI 和健康检查 |
| `worker` | 长期运行；领取任务、续租、重试并写入进度事件 |
| `otel-collector` | 接收 API/Worker 的 OTLP Trace 与 Metrics，并分别转发 |
| `jaeger` | 存储和检索跨 API、Worker、LLM、Tool 与 PostgreSQL 的 Trace |
| `prometheus` | 抓取 Metrics 并计算 P50/P95 与成功率 recording rules |

访问 `http://127.0.0.1:8000/` 使用产品页面，访问 `http://127.0.0.1:8000/docs` 查看接口文档。排查启动过程时使用：

```powershell
docker compose logs migration
docker compose logs memory-setup
docker compose logs -f api worker
```

`DATABASE_URL` 在宿主机仍使用 `localhost`；Compose 会为容器内的 API、Worker 和 migration 自动改用服务名 `postgres`。生产部署必须通过 Secret 管理注入密码和 API Key，不能沿用模板中的开发密码。

本地 Compose 默认设置 `DEMO_USER_ENABLED=true`。初始化服务优先选择创建时间最早的、处于启用状态的 `@163.com` 用户，并将 0004 迁移保留在禁用 legacy 用户下的历史 Session 绑定给该用户。只有找不到 163 用户和已有 `admin@admin.com` 时，才创建 `admin@admin.com / 123456`。这是弱口令本地演示账号，生产环境必须设置 `DEMO_USER_ENABLED=false`，并且不能部署该初始化服务。

如果页面通过反向代理部署在 HTTPS 域名下，还必须将该完整 Origin 写入 `CSRF_TRUSTED_ORIGINS`，并设置 `AUTH_COOKIE_SECURE=true`。不要把认证 Cookie 改成可由 JavaScript 读取，也不要将 Session Token 或 JWT 放入 `localStorage`。

## 可观测性

OpenTelemetry Trace 使用 W3C Trace Context 穿过 HTTP，并把 carrier 持久化到 Run，因此 API 与 Worker 即使位于不同进程、不同时间执行，仍属于同一条 Trace：

`HTTP request → Session → Run → PostgreSQL queue → Worker → LLM → Tool → PostgreSQL`

API 响应包含 `X-Trace-ID`、`X-Request-ID` 和 `Server-Timing`。系统只记录模型名、Tool 名、状态、耗时和 Token 数，不把 prompt、Tool 参数、模型输出或密钥写入 Span/Metric。

| 目标 | OpenTelemetry Metric / Prometheus recording rule |
| --- | --- |
| API P50/P95 | `agent.http.server.duration` / `diy_agent:api_latency_seconds:p50_5m`、`p95_5m` |
| Run 排队/执行时间 | `agent.run.queue.duration`、`agent.run.execution.duration` |
| Tool 成功率/耗时 | `agent.tool.calls`、`agent.tool.duration` / `diy_agent:tool_success_ratio:5m` |
| 重试/租约回收 | `agent.run.retries`、`agent.run.lease_reclaims` |
| LLM Token | `agent.llm.tokens`，按 input/output/total 区分 |
| Worker/积压 | `agent.workers.active`、`agent.runs.pending` |
| 上下文 Token/历史条数 | `agent.context.input.tokens`、`agent.context.history.messages` |
| 摘要/裁剪/调用次数 | `agent.context.messages.summarized`、`agent.context.messages.truncated`、`agent.context.summaries`、`agent.run.llm.invocations`、`agent.run.tool.invocations` |
| 长期记忆召回 | `agent.context.memory.tokens`、`agent.context.memories.recalled` |

本地入口：

- FastAPI：`http://127.0.0.1:8000/docs`
- Jaeger：`http://127.0.0.1:16686`
- Prometheus：`http://127.0.0.1:9090`
- Collector Prometheus exporter：`http://127.0.0.1:8889/metrics`

开发环境默认全采样；生产环境应通过 `OTEL_TRACE_SAMPLE_RATIO` 降低采样率，并用 `OTEL_DEPLOYMENT_ENVIRONMENT` 标记环境。2～4 个 Worker 可用 `docker compose up -d --scale worker=4` 横向扩展。实现遵循 [OpenTelemetry Python instrumentation](https://opentelemetry.io/docs/languages/python/instrumentation/) 与 [exporter](https://opentelemetry.io/docs/languages/python/exporters/) 文档。

## 量化并发基准

基准脚本通过真实 HTTP、真实 PostgreSQL 事务、`FOR UPDATE SKIP LOCKED`、Worker 租约/心跳/fencing 执行；只把外部 LLM/Tool 替换为固定 20 ms 的本地 stub，避免网络波动和费用污染调度结果。脚本强制数据库名以 `_benchmark` 结尾，并会重建该数据库的 `public` Schema，绝不能指向开发库或生产库。

```powershell
docker compose exec postgres createdb -U diy_agent diy_agent_benchmark
$env:BENCHMARK_DATABASE_URL="postgresql+psycopg://diy_agent:change-me-for-local-development@localhost:5432/diy_agent_benchmark"
uv run python -m benchmarks.runtime_benchmark --worker-counts 2,4 --runs-per-scenario 100 --http-concurrency 16 --synthetic-delay-ms 20 --output docs/benchmarks/2026-08-27-runtime-benchmark.json
```

2026-08-27 本机实测环境为 Windows 11、Python 3.13.14、16 logical CPU、PostgreSQL 专用基准库。下面的数据来自该命令的实际结果，不是估算：

| 场景 | 数量 | 吞吐量 | P50 | P95 |
| --- | ---: | ---: | ---: | ---: |
| `POST /v1/sessions` | 200 requests | 总体 API 178.042 req/s | 74.659 ms | 92.596 ms |
| `POST /v1/sessions/{id}/messages` | 200 requests | 总体 API 178.042 req/s | 101.577 ms | 120.447 ms |
| 2 Workers | 100 Runs | 31.179 Runs/s | 执行 54.942 ms | 执行 59.079 ms |
| 4 Workers | 100 Runs | 52.521 Runs/s | 执行 63.989 ms | 执行 73.399 ms |

2 Worker 的队列 P95 为 3050.368 ms，4 Worker 为 1812.681 ms；这里先一次性压入 100 个 Run，因此测到的是 burst backlog 的尾部等待时间。两组共 200 个正常 Run 均成功且每个 prompt 只执行一次，重复数为 0。中断场景中，任务租约过期后由另一 Worker 回收，`attempt_count=2`，真实 runner 只执行 1 次，旧 Worker 写回被 fencing 拒绝。完整机器可读结果见 [基准报告](docs/benchmarks/2026-08-27-runtime-benchmark.json)。

## 数据源

| 数据源 | 用途 | 限制 |
| --- | --- | --- |
| Open-Meteo | 地理编码和近期逐日天气 | 可用预报窗口有限，远期结果不确定 |
| Wikivoyage MediaWiki API | 全球城市攻略、See/Do Listing | 城市与语言版本覆盖不均；遵守 CC BY-SA 归属要求 |
| OpenRouteService | 地理编码、距离和时间矩阵 | 需要 `ORS_API_KEY`；Geocoder 与 Matrix 权限可能分别开通，Geocoder 不可用时优先使用 Wikivoyage 自带坐标 |
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

## 只启动 PostgreSQL 与迁移

使用项目自带的 Compose 服务：

```powershell
docker compose up -d --wait postgres
docker compose ps
uv run alembic upgrade head
uv run python -m app.memory_setup
uv run alembic current
```

默认开发连接为：

```dotenv
DATABASE_URL=postgresql+psycopg://diy_agent:change-me-for-local-development@localhost:5432/diy_agent
```

如果修改 `POSTGRES_USER`、`POSTGRES_PASSWORD`、`POSTGRES_DB` 或端口，必须同步修改 `DATABASE_URL`。生产环境应由 Secret 管理服务注入连接串，并启用 TLS、独立账号和最小权限。

应用启动时只检查数据库连接，不会自动建表。每次部署应先升级业务 Schema，再显式初始化 LangGraph Store：

```powershell
uv run alembic upgrade head
uv run python -m app.memory_setup
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
| `GET /v1/memories` | 查询当前用户的已确认/待确认长期记忆 |
| `POST /v1/memories` | 手动添加一条已确认记忆 |
| `PATCH /v1/memories/{memory_id}` | 确认候选或修改记忆 |
| `DELETE /v1/memories/{memory_id}` | 删除并阻止自动重新创建记忆 |

Worker 通过 `WORKER_LEASE_SECONDS`、`WORKER_HEARTBEAT_SECONDS`、`WORKER_RETRY_DELAY_SECONDS` 和 `WORKER_MAX_ATTEMPTS` 控制租约与重试。心跳周期必须小于租约时长。PostgreSQL 的 `FOR UPDATE SKIP LOCKED` 允许多个 Worker 并发领取不同任务；租约过期后任务可被回收，旧 Worker 的结果会被 fencing 检查拒绝。

## 多轮上下文与短期记忆

异步 Worker 执行 Run 前会读取同一 Session、当前用户消息之前的对话。当前问题始终完整保留；最近最多 `CONTEXT_RECENT_MESSAGE_LIMIT` 条消息按 Token 预算保留原文，更早内容写入 Session 级滚动摘要。摘要游标记录已经覆盖到的 Message ID，后续 Run 与失败重试不会重复总结同一批消息。

滚动摘要使用确定性的抽取式实现，不额外调用模型，因此不会增加隐藏的 LLM 成本、重试点或摘要幻觉；`Summarizer` 接口可在未来替换成经过评测的模型摘要器。`tiktoken` 用于稳定估算上下文占用，模型 API 返回的实际 Token 仍由 `agent.llm.tokens` 单独记录。

| 环境变量 | 默认值 | 作用 |
| --- | ---: | --- |
| `CONTEXT_MAX_INPUT_TOKENS` | 12000 | 输入上下文总预算 |
| `CONTEXT_SYSTEM_RESERVED_TOKENS` | 1400 | 系统提示预留 |
| `CONTEXT_TOOL_RESERVED_TOKENS` | 3200 | Tool Schema/结果预留 |
| `CONTEXT_OUTPUT_RESERVED_TOKENS` | 1800 | 模型最大输出 |
| `CONTEXT_RECENT_MESSAGE_LIMIT` | 8 | 保留完整原文的最近消息上限 |
| `CONTEXT_SUMMARY_MAX_TOKENS` | 1200 | 滚动摘要上限 |
| `AGENT_MAX_LLM_CALLS` | 6 | 单次 Run 的 LLM 调用硬上限 |
| `AGENT_MAX_TOOL_CALLS` | 4 | 单次 Run 的 Tool 调用硬上限 |

Worker 会通过 `CONTEXT_PREPARED` SSE 事件发送历史条数、摘要状态和 Token 数；不会发送历史或摘要正文。达到调用上限时写入 `CONTEXT_LIMIT_REACHED`，Run 以不可重试的 `AGENT_INVOCATION_LIMIT_EXCEEDED` 结束。同一 Session 的历史消息与摘要会随当前请求发送给配置的 DeepSeek 服务，部署方应在隐私政策中披露，并为敏感场景增加脱敏、保留期和删除能力。

## 跨 Session 长期记忆

长期记忆采用混合架构。PostgreSQL 的 `travel_memories` 业务表是内容、来源、状态和删除墓碑的事实来源；LangGraph `PostgresStore` 只承担按 `user_id` namespace 的跨 Session 召回。User、AuthSession、Message、Run 等业务数据仍由原有模型管理，LangGraph 不接管鉴权和任务状态。

Worker 只把 `CONFIRMED` 且未过期的记忆放入上下文。首次规划表单产生的兴趣、预算和补充偏好默认是 `CANDIDATE`，不会直接影响未来 Session；用户明确说“请记住”“以后”“我喜欢/不喜欢”时可生成已确认记忆。自动提取使用保守、确定性的规则，不额外调用 LLM。用户删除会保留业务墓碑，因此相同内容不会被下一次自动提取静默恢复。

召回内容被放入独立的 SystemMessage，并明确标记为背景偏好而非系统指令；当前请求始终优先。每次 Run 的 `ContextUsage` 和 `CONTEXT_PREPARED` 事件记录召回条数与 Token，但不向 SSE 暴露记忆正文。

| 环境变量 | 默认值 | 作用 |
| --- | ---: | --- |
| `MEMORY_ENABLED` | true | 是否在 Worker 执行前召回长期记忆 |
| `MEMORY_RECALL_LIMIT` | 8 | 每次 Run 最多召回条数 |
| `MEMORY_CONTEXT_MAX_TOKENS` | 800 | 长期记忆独立上下文预算 |
| `MEMORY_AUTO_EXTRACT` | true | 成功 Run 后是否提取候选/明确记忆 |

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

未设置 `TEST_DATABASE_URL` 时 PostgreSQL 集成测试会跳过；CI 会提供专用测试库并真实执行，包括 LangGraph PostgresStore 的 namespace 隔离、召回与删除。

![离线测试演示](docs/images/test-demo.svg)

## 持续集成

`.github/workflows/ci.yml` 在推送到 `main`、Pull Request 和手动触发时运行。GitHub Actions 会启动独立的 PostgreSQL 18 service container，并依次执行：

1. 按 `uv.lock` 安装 Python 3.13 依赖；
2. 执行 Alembic 升级、LangGraph Store 初始化，并检查是否存在未生成的模型变更；
3. 执行普通单元测试；
4. 显式注入 `TEST_DATABASE_URL` 执行 PostgreSQL 集成测试；
5. 构建生产应用镜像。

因此本地未配置测试库时出现的一个 `skipped`，在 CI 中会真实执行。工作流只读取仓库内容；真实密钥应配置为 GitHub Actions Secret，不能写入 workflow 或 `.env.example`。

## 日志、编码与安全

- `.editorconfig`、`.gitattributes` 与 `PYTHONUTF8=1` 统一 UTF-8。
- `logging_config.py` 输出 UTF-8 JSON Lines，并使用稳定的 `event`、`request_id` 和 `error_code` 字段。
- 不记录 API Key、数据库密码、Cookie 或完整个人信息。
- JSON 日志会对 URL 查询参数、Bearer Token 以及已注入的 DeepSeek/ORS Key 二次脱敏；密钥若曾出现在旧日志中仍必须立即轮换。
- API Key 和数据库连接串只能来自环境变量或部署平台 Secret；提交前运行 `git check-ignore .env`。
- API 已有身份认证、用户隔离与 CSRF；公网部署前仍需加入限流、配额、审计保留期和账号恢复流程。

## 当前限制与路线图

- 天气只在数据源近期预报范围内可靠，不能声称准确预测数月后的最佳日期。
- Wikivoyage 的全球覆盖和结构化程度不均，开放时间、票价和停业状态需出发前复核。
- 路线暂不包含实时拥堵、公交班次、预约时段、无障碍和跨日行李约束。
- PostgreSQL 目前同时承担持久化与任务分派；更大吞吐量下应评估 Redis、RabbitMQ 或托管任务队列。
- LangGraph Store 当前使用结构化/关键词召回，尚未接入 embedding 语义索引；还未实现记忆过期管理 UI、暂停/恢复 checkpoint、告警通知、酒店/机票授权供应商适配和支付确认。

建议后续顺序：记忆召回评测与冲突合并 → 告警与 SLO → 酒店只读推荐 → 授权票务沙箱与人工确认 → 飞书 Bot。

## 提交前检查

```powershell
git status --short
git check-ignore .env
git diff --check
uv run pytest
```

如果密钥曾进入提交，仅增加 `.gitignore` 不能消除泄露：必须立即轮换密钥，并在推送前清理 Git 历史。
