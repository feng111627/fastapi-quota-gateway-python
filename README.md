# fastapi-quota-gateway

面向客户端调用的接入层：多级限流、接口版本协商、上游熔断与降级。基于 FastAPI，核心逻辑在 `gateway/` 包内，可脱离 HTTP 层独立使用与测试。

## 配额模型（限流）

- **两个维度的多级限流**：每个客户端（`client_id`）一个令牌桶，每个受管接口（`endpoint`）一个令牌桶；任一维度超限即拒绝。
- **令牌桶参数**：`rate_per_sec`（稳态速率）+ `burst`（突发容量）。新桶按满容量起步，允许合理突发。
- **原子扣减**：检查与扣减在同一把锁内完成；任一维度失败时回滚已试扣的令牌，保证并发下**不超发、不重复扣减**。
- **可区分的拒绝原因**：`client_rate_exceeded` / `endpoint_rate_exceeded`，HTTP 429 并携带 `Retry-After`。
- **状态恢复**：`snapshot()` 将各桶的令牌数与上次补充时间原子写入 JSON 文件（先写临时文件再 `os.replace`）；进程重启后按快照恢复，补充计时从快照时间继续。

## 版本兼容策略

- 版本格式：`v1`、`v1.2`、``、`1.2`；非法格式拒绝（`version_malformed`）。
- 每个版本可配置 `deprecated_since`（进入弃用期）与 `sunset_on`（下线日期）。
- **弃用期内**版本仍可用，响应带 `Deprecation: true` 与 `Warning` 头说明弃用与下线时间。
- **下线日当天起**拒绝，报 `version_too_old`；高于注册表的版本报 `version_too_new`（HTTP 400）。
- **未指定版本**时稳定解析到最新非弃用版本（全部弃用时取最新未下线版本），结果只依赖注册表与日期，可预期。

## 熔断与降级规则

- **CLOSED**：正常放行；连续上游失败达 `failure_threshold` 次进入 OPEN。
- **OPEN**：快速失败，抛 `circuit_open`（HTTP 503 + `Retry-After`），不触达上游；冷却 `recovery_timeout` 秒后进入 HALF_OPEN。
- **HALF_OPEN**：放行至多 `half_open_max_calls` 个并发探测；探测成功回到 CLOSED 并清零计数，探测失败回到 OPEN 并**重新计时冷却**。
- **失败归类**（`UpstreamError.kind`，reason 形如 `upstream_timeout`）：
  - `timeout`：上游超时
  - `connection`：连接失败
  - `business`：上游业务错误（5xx 等）
- 只统计上游失败；限流拒绝、版本拒绝与非上游异常不影响熔断计数。单次成功会清零连续失败计数。

## 判定顺序

`Gateway.handle` 固定按 **限流 → 版本协商 → 熔断 → 上游调用** 执行，保证同一请求在任何并发时序下得到一致、可解释的结论。

## HTTP 接口

`GET /api/data`，请求头：

- `X-Client-Id`：客户端标识（缺省 `anonymous`）
- `X-Api-Version`：可选，如 `v1`

错误响应统一为 `{"error": <reason>, "message": ..., "detail": ...}`。

环境变量：

- `GATEWAY_STATE_PATH`：限流状态快照路径（默认 `gateway_state.json`）
- `UPSTREAM_MODE`：演示用上游故障注入，`ok` / `timeout` / `connection` / `flaky`

## 本地验证

```bash
# 安装依赖
uv sync

# 运行全部测试（日志会打印每个请求的输入与判定依据）
uv run pytest -q

# 启动服务
uv run python main.py

# 手动验证
curl -i -H 'X-Client-Id: demo' -H 'X-Api-Version: v1' http://127.0.0.1:8000/api/data
UPSTREAM_MODE=timeout uv run python main.py   # 连续 3 次后观察 503 circuit_open
```
