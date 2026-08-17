# ADR 0002：密钥注入与导入零副作用

- 状态：Accepted
- 日期：2026-08-17

## 背景

模型在模块顶层创建或调用会导致测试、Web Worker 启动和静态检查意外产生费用或网络请求。本地 `.env` 也存在被误提交的风险。

## 决策

- Python 模块导入时不得创建模型、Agent、HTTP Session 或运行示例。
- `create_travel_agent()` 显式创建运行时对象，`main()` 是唯一默认 CLI 入口。
- 密钥只从进程环境读取；本地开发可由入口将未提交的 `.env` 加载到环境。
- 提交 `.env.example`，忽略 `.env`、私钥、数据库和本地缓存。
- 工具可恢复失败统一返回 `status/error_code/message/details`。

## 后果

服务可安全进行模块预加载和多 Worker 启动；部署平台可以改用 Vault/KMS/容器 Secret，而无需修改业务代码。
