# fastapi-quota-gateway

面向客户端调用的接入层：**按客户分档的精细配额**、接口权重与自声明代价、失败退还与请求级幂等，加上原有的多级限流、版本协商、上游熔断降级。基于 FastAPI，核心逻辑在 `gateway/` 包内，可脱离 HTTP 直接使用与测试。

## 架构总览

| 模块 | 职责 |
| --- | --- |
| `gateway/plans.py` | 套餐档（速率/突发/周期总量），默认档 + 单客户覆盖 |
| `gateway/billing.py` | 接口权重、客户端自声明代价的解析与合规处理 |
| `gateway/quota.py` | 周期总量额度账本（滚动 / UTC 自然日重置） |
| `gateway/gate.py` | 计费闸门：一把锁串联速率桶、总量账本、幂等记录与快照 |
| `gateway/service.py` | 两代入口：原 `Gateway` 与新 `QuotaGateway` |
| `gateway/ratelimit.py` | 原 (client, endpoint) 多级令牌桶限流（保留，供旧入口使用） |
| `gateway/versioning.py` / `circuitbreaker.py` / `errors.py` | 版本协商、熔断、错误分类（新增额度/计费原因） |

判定顺序（`QuotaGateway.handle`）：

```
解析代价 → 幂等登记/重放 → 速率+总量预占 → 版本协商 → 熔断 → 上游调用
                ↑ 任一环节不通过即按固定规矩回滚/退还，账目与结论对得上
```

## 一、分档配额模型

每个套餐档 `Plan` 包含：

- `rate_per_sec` + `burst`：稳态速率与能扛的突发量（令牌桶，满桶起步）；
- `quota_limit` + `quota_period_sec`：一个周期的**总量上限**，`0` 表示不限总量；
- `reset_mode`：
  - `ROLLING` 滚动窗口——从首次计费起，满一个周期即开新窗口；
  - `DAILY` 按 **UTC 自然日** 00:00 重置。

`PlanRegistry` 负责按客户解析套餐：有 `set_override(client_id, plan)` 的用专属档，没有的一律落到默认档。运营可在运行期热调整，新参数在下一次判定时生效（已在账的速率桶按新突发收口，总量窗口不回拨）。

**两类拒绝明确区分**（都是 HTTP 429，但 `reason` 不同）：

| 现象 | reason | retry_after 含义 |
| --- | --- | --- |
| 速度太快（令牌桶不够） | `client_rate_exceeded` | 攒够本次代价所需秒数 |
| 周期总量用完 | `quota_exhausted` | 距离额度重置的秒数 |

速率与总量在同一把计费锁内原子预占：总量不足时速率预占一并回滚，两者互不串账；并发下**不超发**。

## 二、权重、自声明代价与合规规矩

`EndpointCostPolicy(weight, min_declared, max_declared)`：

- **未声明代价**：按接口权重 `weight` 计费；未配置的接口权重为 `1`。
- **接口开放自声明**（配了区间任一端）：声明值**夹取（clamp）** 到允许区间——
  低于下限取下限、高于上限取上限，并在响应 `cost` 中回显实际计费值与 `clamped` 标记。
  只配下限则上不封顶，反之亦然。
- **接口不开放自声明**（两端都是 `None`）：忽略声明，回落权重（声明恰好等于权重时不算夹取）。
- **非法声明**（无法解析为非负有限数值、负数、NaN/Inf、布尔）：
  返回 400 `declared_cost_malformed`，请求根本不进入计费，不扣任何额度。

> 选夹取而不是拒绝超范围声明，是为了让客户端"多报会被压回上限、少报不低于下限"，
> 行为对调用方明确且可复现；实际扣费值始终在响应里透明可见。

## 三、退还规则与请求级幂等

通过请求头 `X-Request-Id` 携带请求编号（不传则不做幂等）：

| 结局 | 速率+总量 | 幂等结论 | 同编号重试 |
| --- | --- | --- | --- |
| 成功 | 扣费落定 | 记录成功（含响应体） | 直接重放同一结果，**不再扣费、不再触达上游** |
| 上游 `timeout` / `connection` | **全额退还**（没拿到数据不收费） | 记录失败结论 | 重放同一失败，**不重复扣、不重复退** |
| 熔断 OPEN（降级） | **全额退还**（未触达上游，等同未受理） | 不固化终态 | 恢复后可真正执行；OPEN 期间稳定返回 503 |
| 上游业务失败 `business`（5xx 等） | **不退**（上游实际处理了） | 记录失败结论 | 重放失败，不重复扣费 |
| 版本拒绝 / 代价非法 / 非上游异常 | 回滚预占 | 清除登记 | 同一编号可重新使用 |

- 并发下同一 `X-Request-Id` 只有一个请求真正在途执行，其余得到
  **409 `request_in_flight`**；在途请求落定后再来即得到稳定重放，不会互相串账。
- 速率桶、总量账本、幂等结论由 `AccountingGate.snapshot()` 原子写入同一 JSON
  （先写 `.tmp` 再 `os.replace`），启动时恢复：
  - **已用额度接得上**，重启不会"当作没用过"；
  - 成功/失败的终态结论可继续重放；
  - 若进程在请求执行中途崩溃（记录停留在 `in_flight`），重启后**已扣额度保留**，
    但请求编号被解锁，允许客户端重新发起（结论未知，不替客户端猜结果）。

## 四、对外暴露约定

无论成功还是被拒，响应结构稳定一致、可复现。

成功 `200`：

```json
{
  "status": "ok",
  "body": { ... },
  "version": "v2.0",
  "cost": 5.0,
  "replayed": false,
  "quota": {
    "plan": "basic",
    "rate_remaining": 5.0,
    "rate_capacity": 10,
    "quota_limit": 1000,
    "quota_used": 5.0,
    "quota_remaining": 995.0,
    "quota_reset_at": 1790985600.0,
    "quota_reset_in_sec": 36400.1
  }
}
```

拒绝（429/400/409/502/503 同一套字段）：

```json
{
  "status": "rejected",
  "reason": "quota_exhausted",
  "message": "quota exhausted: quota_exhausted",
  "detail": { ... },
  "retry_after": 36400.1,
  "quota": { ... }
}
```

- `Retry-After` 响应头：限流给攒令牌时间，总量耗尽给重置等待，降级给熔断恢复时间。
- `GET /quota`：客户端随时自查套餐、速率剩余、周期总量剩余与重置时刻。
- 降级（`circuit_open`, 503）**不会反过来打上游**，半开探测逻辑沿用原熔断器，未改动。
- 弃用版本仍带 `Deprecation: true` 与 `Warning` 头；下线/越界/格式错误维持原 400 结论。

reason 一览：`client_rate_exceeded` / `quota_exhausted` / `declared_cost_malformed` /
`request_in_flight` / `version_too_old` / `version_too_new` / `version_malformed` /
`upstream_timeout` / `upstream_connection` / `upstream_business` / `circuit_open`。

## 五、HTTP 接口与请求头

| 路径 | 说明 |
| --- | --- |
| `GET /api/data` | 权重 1，不接受自声明 |
| `GET /api/heavy` | 权重 5，自声明允许区间 [1, 20] |
| `GET /quota` | 查询当前客户额度视图 |
| `GET /` | 健康检查 |

请求头：

- `X-Client-Id`：客户标识（缺省 `anonymous`）
- `X-Api-Version`：可选，如 `v1` / `v2`
- `X-Request-Id`：可选，幂等键（建议每次业务请求唯一编号）
- `X-Declared-Cost`：可选，本次自声明代价（数值）

环境变量：

- `GATEWAY_STATE_PATH`：账单/桶/幂等快照路径（默认 `gateway_state.json`）
- `UPSTREAM_MODE`：演示故障注入，`ok` / `timeout` / `connection` / `flaky`
- `PORT`：监听端口（默认 8000）

## 六、兼容范围

- 旧 `Gateway`、`MultiLevelRateLimiter`、`RateLimitPolicy` 等 API **原样保留**，
  旧测试一条未改；新功能全部走 `QuotaGateway` + `AccountingGate`。
- 旧快照（version 1，仅桶）可被新闸门忽略式加载，不报错；新快照多了 `ledger`/`idempotency`。
- 版本协商、熔断状态机语义不变；熔断统计仍只计上游失败，限流/版本/非上游异常不计入。
- `DAILY` 模式依赖 wall clock（Unix 纪元秒，生产用 `time.time()`）；
  `ROLLING` 模式与旧限流一样可在测试中注入任意单调时钟。

## 本地验证

```bash
uv sync

# 全部 86 个用例（日志会逐条打印每次请求的输入与判定依据）
uv run pytest -q

# 启动服务
uv run python main.py

# 正常调用（返回里带 cost / replayed / quota）
curl -s -H 'X-Client-Id: demo' -H 'X-Request-Id: req-1' \
  http://127.0.0.1:8000/api/data | python -m json.tool

# 自声明代价超上限被夹到 20
curl -s -H 'X-Client-Id: pro' -H 'X-Declared-Cost: 999' \
  http://127.0.0.1:8000/api/heavy | python -m json.tool

# 查询剩余额度
curl -s -H 'X-Client-Id: demo' http://127.0.0.1:8000/quota | python -m json.tool

# 超时退还 + 连续失败后观察 503 circuit_open（额度仍会退回）
UPSTREAM_MODE=timeout uv run python main.py
for i in 1 2 3 4; do
  curl -s -o /dev/null -w '%{http_code}\n' \
    -H "X-Client-Id: demo" -H "X-Request-Id: t-$i" http://127.0.0.1:8000/api/data
done
```

测试覆盖（新增）：分档与总量边界、权重声明上下限与非法声明、失败退还与重试幂等、
并发扣减/退还（多线程）、重启后账目一致、各类拒绝与降级原因区分；
`test_http_api.py` 经 TestClient 校验状态码、信封字段与并发同编号不重复扣。
