# Security Policy

密钥只能由环境变量或部署平台的 Secret/Vault/KMS 注入。本地 `.env` 已被忽略，`.env.example` 只能保留空值。

不要在 Issue、日志、截图、测试夹具或 Agent 消息中记录 API Key、Cookie、Authorization Header、手机号、身份证件、支付信息或未经脱敏的用户偏好。日志只记录 `request_id`、工具名、状态和稳定错误码。

发现密钥泄露时：立即撤销并轮换密钥，停止相关部署，检查访问日志，并用合适的历史重写工具清理 Git 历史。不要把真实密钥提交进一次“修复提交”来验证。
