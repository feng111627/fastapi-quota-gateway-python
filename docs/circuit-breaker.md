# 熔断与降级规则

## 1. 上游异常分类（原因可区分）

`app/resilience/classifier.py` 把上游异常分类并映射到错误码：

| 异常 | 分类 | 错误码 | 未熔断时 HTTP |
| --- | --- | --- | --- |
| `httpx.TimeoutException`（含 ConnectTimeout/ReadTimeout） | `timeout` | `upstream.timeout` | 502 |
| `httpx.ConnectError`/`ReadError` 等传输层失败 | `connection` | `upstream.connection` | 502 |
| 上游可达但返回非 2xx（`UpstreamStatusError`） | `business` | `upstream.business` | 502 |
| 其它未预期异常 | `unknown` | `internal.error` | 500 语义 |

注意 `ConnectTimeout` 同时是超时与连接类，按“超时优先”归为 `timeout`。
熔断仍处于 CLOSED 时，这些失败以 `502` + 对应错误码返回，
调用方可区分是超时、连不上还是业务错误。

## 2. 熔断器状态机

熔断器按**接口（api）粒度**独立工作（`app/resilience/breaker.py`）：

```
            连续失败达阈值                冷却到期，首个探测请求
 CLOSED ───────────────────────► OPEN ───────────────────────► HALF-OPEN
   ▲                                │                             │
   │  探测成功累计达 success_threshold│ 探测失败                    │
   └────────────────────────────────┴──────────────► OPEN（重新计时）
```

- **CLOSED**：正常放行；连续失败计数达到 `failure_threshold`（默认 5）→ OPEN。
- **OPEN**：所有请求**快速失败**，不等待、不向上游发起调用，避免持续失败时
  请求继续堆积；持续 `open_duration`（默认 5s）后进入半开探测窗口。
- **HALF_OPEN**：只放行最多 `half_open_max_probes`（默认 1）个探测请求，
  其余仍快速失败；探测成功累计达到 `success_threshold`（默认 1）→ CLOSED
  并清零计数；探测期间任意一次失败 → 立即回到 OPEN 并**重新开始计时**。

所有状态判定与计数都在单个 `asyncio.Lock` 内完成，
因此“恢复瞬间有大量并发请求”时，也只有一个请求能拿到探测名额，
状态转换与结论一致、可复现（见 `test_concurrent_open_requests_only_one_probe`）。

## 3. 降级行为

请求经 `Gateway.handle`（`app/gateway.py`）处理：

1. 版本协商 → 2. 限流 → 3. 熔断判定 → 4. 调用上游。

- 熔断 OPEN（或 HALF_OPEN 探测名额已满）时，请求不打到上游，
  直接返回**降级成功**：HTTP `200`，响应体 `degraded=true`，并带头
  `X-Degraded: true`、`X-Degraded-Reason: breaker.open`。
- 调用上游失败时先分类、再 `record_failure`：
  - 若熔断因此变为（或本已处于）OPEN：返回降级结果（原因 `breaker.open`）；
  - 熔断仍 CLOSED：返回 `502` 与具体上游错误码。
- 调用成功则 `record_success`，半开探测成功后自动恢复正常回源。

降级响应示例：

```json
{
  "api": "echo",
  "degraded": true,
  "data": null,
  "message": "服务暂时不可用，已降级（原因：breaker.open）"
}
```

## 4. 观测

`GET /health` 返回各接口熔断器当前状态与连续失败/成功计数，
便于验证“熔断期间 OPEN、恢复后 CLOSED”的结论。
