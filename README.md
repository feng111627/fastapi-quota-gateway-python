# fastapi-quota-gateway

面向客户端调用的接入层：分档配额、权重计费与失败退还、多级限流、接口版本协商、上游熔断与降级。基于 FastAPI，核心逻辑在 `gateway/` 包内，可脱离 HTTP 层独立使用与测试。

## 分档配额模型

- **套餐（`QuotaPlan`）**：每档定义 `rate_per_sec`（速率上限）、`burst`（突发量）、`quota_limit`（周期总额度）与重置周期（`QuotaPeriod.DAILY` 按 UTC 自然日 / `QuotaPeriod.ROLLING` 按 `period_seconds` 滚动）。
- **按客户分档**：`TieredQuotaManager.set_client_plan(client_id, plan)` 可给单个客户调档；未单独配置的客户落到默认档。限流器通过 `client_policy_resolver` 按客户当前套餐建立令牌桶（新桶生效，存量桶不追溯调整）。
- **总量窗口**：每个客户在每档下有独立账目（已用量 + 窗口起点）；窗口滚动时账目自动清零重计。
- **原子扣减**：`charge` 的检查与扣减在同一把锁内完成，并发下不超发。
- **账本三态机**：每个 `(client_id, request_id)` 的扣减记录在 `IN_FLIGHT`（已扣、结果未定）、`COMMITTED`（成功或业务错误，照常计费，占用永久确定）、`REFUNDED`（可退失败已退还）三态间单向迁移：
  - 只有 `IN_FLIGHT` 可以退还；`COMMITTED` 任何后续同编号重试都退不掉，`REFUNDED` 不重复退；
  - 成功或业务错误后由网关 `commit` 提交；超时/连接/降级时由网关 `refund` 退还，各自只迁移一次，并发下幂等。
- **状态恢复**：`snapshot()` 把客档映射、各客户账目、幂等账本（含三态）原子写入 JSON（临时文件 + `os.replace`）；重启后已用额度、已提交/已退还的重试结论都接得上，同一请求编号仍不重复扣。
- **拒绝原因可区分**："速度太快"（`client_rate_exceeded` / `endpoint_rate_exceeded`）与"额度用完"（`quota_exhausted`）是不同的 reason，均为 HTTP 429 + `Retry-After`。额度不足被拒时会归还已扣的速率令牌，因此同一客户反复拦截得到的结论稳定可复现。

## 权重与退还规则

- **接口权重**：`CostPolicy(endpoint_weights={...})` 给接口配权重，未配置的接口用 `default_weight`。
- **客户端声明**：请求可经 `X-Request-Cost` 声明本次要花的单位数；最终计费 = **接口权重 × 声明单位数**。
- **越界处理规矩（唯一）**：声明超出 `[min_declared, max_declared]` 时**按边界夹取**（不拒绝、不轻信），响应中 `cost.clamped=true` 标明发生了夹取；未声明时按 `min_declared` 计。权重同时作用于速率令牌扣减与总额度扣减。
- **失败退还边界（固定，不随重试变化）**：只有**上游超时、连接失败、撞上熔断降级**这类确实没拿到数据的失败才退还本次在途扣减（`detail.quota.refunded=true`），同一笔只退一次；**上游业务错误（5xx）照常计费、不退还**，并立即提交占用。
- **重试幂等**：客户端在 `X-Request-Id` 携带请求编号。
  - 同一编号重复请求：只扣一次（`duplicate`），不重复扣；
  - **已成功完成的请求占用永久确定**：首次成功（或业务错误）后记录进入 `COMMITTED`，之后同编号重试即使赶上超时或熔断降级，也只对重试自己做结论，**绝不会把前面成功那笔额度退回去**（退还返回 `refunded=false`，剩余额度不变）；
  - **并发同编号重试互不串扰**：网关对同一 `(client_id, request_id)` 的"扣减→调用→结算"整段串行化，配合三态账本，各笔重试不能互相提交或退还，最终账目要么恰好一笔占用、要么全额已退；
  - 同一编号的退还只生效一次；已退还（`REFUNDED`）的编号再次请求视为新一轮扣减；账本记录跨窗口自动失效；
  - 不带编号的请求：失败退还在同一调用路径内同步补偿，天然只发生一次。

## 降级与额度对外约定

- **判定顺序固定**：限流 → 版本协商 → 额度扣减 → 熔断 → 上游调用。任何并发时序下结论一致。
- **拒绝路径不留副作用**：版本协商失败（版本过旧/过新/格式非法，HTTP 400）在额度扣减之前结束，不触达上游，并**归还本步已扣的速率令牌**；因此同一个不受支持的版本反复调用，返回的原因、状态码、`Retry-After` 口径与剩余额度完全稳定，不会因为多调几次而退化成"请求太快"（429）。额度不足拒绝同样归还速率令牌（见上）。
- **降级不打上游**：熔断 OPEN 期间快速失败（503 `circuit_open` + `Retry-After` 即恢复等待时间），不触达上游；半开探测逻辑不受影响。降级退还的只是本次在途扣减；若该编号此前已 `COMMITTED`，则 `refunded=false`、成功笔额度不动。
- **统一响应口径**：正常响应与各类拒绝（限流 / 额度 / 版本 / 降级）都携带额度视图：
  - 成功：响应体 `quota={plan, limit, remaining, reset_at, cost}`、`cost={applied, weight, declared, clamped}`，响应头 `X-Quota-Limit` / `X-Quota-Remaining` / `X-Quota-Reset`；
  - 拒绝：错误体 `detail.quota={plan, limit, remaining, reset_at}`（失败退还时另带 `refunded`），同样的 `X-Quota-*` 响应头；`Retry-After` 在 429/503 上给出建议等待时间。
- 多跑几次结论可复现：同一状态下同一请求的 status_code、reason、剩余额度、建议等待时间稳定一致。

## 配额模型（限流）

- **两个维度的多级限流**：每个客户端（`client_id`）一个令牌桶，每个受管接口（`endpoint`）一个令牌桶；任一维度超限即拒绝。
- **令牌桶参数**：`rate_per_sec`（稳态速率）+ `burst`（突发容量）。新桶按满容量起步，允许合理突发。客户端令牌桶按套餐分档解析。
- **原子扣减**：检查与扣减在同一把锁内完成；任一维度失败时回滚已试扣的令牌，保证并发下**不超发、不重复扣减**。
- **状态恢复**：`snapshot()` 将各桶的令牌数与上次补充时间原子写入 JSON 文件；进程重启后按快照恢复，补充计时从快照时间继续。

## 版本兼容策略

- 版本格式：`v1`、`v1.2`、`1`、`1.2`；非法格式拒绝（`version_malformed`）。
- 每个版本可配置 `deprecated_since`（进入弃用期）与 `sunset_on`（下线日期）。
- **弃用期内**版本仍可用，响应带 `Deprecation: true` 与 `Warning` 头（中文按 RFC 5987 百分号编码）说明弃用与下线时间。
- **下线日当天起**拒绝，报 `version_too_old`；高于注册表的版本报 `version_too_new`（HTTP 400）。
- **未指定版本**时稳定解析到最新非弃用版本（全部弃用时取最新未下线版本），结果只依赖注册表与日期，可预期。

## 熔断与降级规则

- **CLOSED**：正常放行；连续上游失败达 `failure_threshold` 次进入 OPEN。
- **OPEN**：快速失败，抛 `circuit_open`（HTTP 503 + `Retry-After`），不触达上游；冷却 `recovery_timeout` 秒后进入 HALF_OPEN。
- **HALF_OPEN**：放行至多 `half_open_max_calls` 个并发探测；探测成功回到 CLOSED 并清零计数，探测失败回到 OPEN 并**重新计时冷却**。
- **失败归类**（`UpstreamError.kind`，reason 形如 `upstream_timeout`）：`timeout` / `connection` / `business`。其中 `timeout`、`connection` 触发额度退还，`business` 不退。
- 只统计上游失败；限流拒绝、版本拒绝与非上游异常不影响熔断计数。单次成功会清零连续失败计数。

## HTTP 接口

`GET /api/data`，请求头：

- `X-Client-Id`：客户端标识（缺省 `anonymous`）
- `X-Api-Version`：可选，如 `v1`
- `X-Request-Id`：可选，请求编号（幂等扣减/退还）
- `X-Request-Cost`：可选，声明本次请求的单位数（越界按边界夹取）

错误响应统一为 `{"error": <reason>, "message": ..., "detail": ...}`，`detail.quota` 携带额度视图。

环境变量：

- `GATEWAY_STATE_PATH`：限流状态快照路径（默认 `gateway_state.json`）
- `GATEWAY_QUOTA_STATE_PATH`：配额账目快照路径（默认 `gateway_quota.json`）
- `GATEWAY_CLIENT_PLANS`：客档覆盖，形如 `vip:premium,partner:standard`（内置 `default` / `standard` / `premium` 三档，未配置落 `default`）
- `UPSTREAM_MODE`：演示用上游故障注入，`ok` / `timeout` / `connection` / `flaky`

## 兼容范围

- `Gateway` 的 `quota` / `costs` 参数可缺省，缺省时行为与旧版完全一致（无总额度、按 1 次计费、无退还）。
- 原有拒绝原因、状态码、`Retry-After` 语义不变；新增 `quota_exhausted` 原因与 `X-Quota-*` 响应头为纯增量。
- 限流快照文件格式不变；配额账目使用独立的快照文件，互不影响。配额账本快照由旧的 `refunded` 布尔位升级为 `state` 三态字段，旧快照可自动读取（未退的在途记录按 `committed` 保守接续，避免重启后误退成功笔）。

## 本地验证

```bash
# 安装依赖
uv sync

# 运行全部测试（日志按用例打印所属业务模块 CASE START、
# 每条用例的输入与判定依据、以及 CASE END verdict=PASSED/FAILED）
uv run pytest -q
# 只看本轮修复相关的三类场景 + 熔断边界：
uv run pytest -q tests/test_quota.py::test_committed_charge_never_refunded_by_later_retry \
  tests/test_quota.py::test_committed_state_restored_after_restart \
  tests/test_gateway_quota.py::test_retry_after_success_then_timeout_keeps_quota \
  tests/test_gateway_quota.py::test_retry_after_success_then_circuit_open_keeps_quota \
  tests/test_gateway_quota.py::test_concurrent_same_id_retries_no_crosstalk \
  tests/test_gateway_quota.py::test_unsupported_version_repeated_conclusion_stable

# 启动服务
uv run python main.py

# 手动验证
curl -i -H 'X-Client-Id: demo' -H 'X-Api-Version: v1' -H 'X-Request-Id: r1' http://127.0.0.1:8000/api/data
curl -i -H 'X-Client-Id: demo' -H 'X-Request-Cost: 999' http://127.0.0.1:8000/api/data   # 观察 cost.clamped

# 场景一：同编号先成功，再用同编号重试赶上上游超时 —— 成功笔的剩余额度不再被退回去
curl -s -H 'X-Client-Id: demo' -H 'X-Request-Id: fixed-id' http://127.0.0.1:8000/api/data   # 剩余 999
UPSTREAM_MODE=timeout curl -s -H 'X-Client-Id: demo' -H 'X-Request-Id: fixed-id' \
  http://127.0.0.1:8000/api/data   # detail.quota.refunded=false，剩余仍为 999

# 场景三：不支持的版本反复调用 —— 始终是 400 version_too_new，剩余额度不变，不会变成 429
for i in 1 2 3 4 5; do
  curl -s -o /dev/null -w '%{http_code} ' -H 'X-Client-Id: demo' -H 'X-Api-Version: v9' \
    http://127.0.0.1:8000/api/data
done; echo   # 输出：400 400 400 400 400

UPSTREAM_MODE=timeout uv run python main.py   # 连续 3 次后观察 503 circuit_open 且在途额度被退还
GATEWAY_CLIENT_PLANS='demo:premium' uv run python main.py   # 给 demo 客户单独调档
```

对应自动化用例（覆盖输入、并发交错与最终账目判定）：

| 业务场景 | 用例 |
| --- | --- |
| 同编号先成功后超时重试，额度不变、退还被拒 | `test_retry_after_success_then_timeout_keeps_quota` / `test_committed_charge_never_refunded_by_later_retry` |
| 同编号成功后重试撞熔断降级，成功笔不退 | `test_retry_after_success_then_circuit_open_keeps_quota` |
| 同编号并发重试（成功/超时混杂）不串账 | `test_concurrent_same_id_retries_no_crosstalk` / `test_concurrent_retry_same_id_no_cross_accounting` |
| 反复调用不支持版本，原因/状态码/剩余额度稳定 | `test_unsupported_version_repeated_conclusion_stable` |
| 业务错误照常计费不退 | `test_business_error_not_refunded` |
| 重启后已提交/已退还结论接续 | `test_committed_state_restored_after_restart` / `test_state_restored_after_restart` |
