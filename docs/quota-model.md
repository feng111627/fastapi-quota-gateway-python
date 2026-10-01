# 配额与多级限流模型

## 1. 令牌桶

每个限流维度对应一个令牌桶（`app/quota/bucket.py`）：

- `rate`：稳态补充速率（令牌/秒），决定长期平均吞吐；
- `burst`：桶容量，决定允许的最大瞬时突发；
- 每次请求到来先按经过时间补充令牌：
  `tokens = min(burst, tokens + Δt × rate)`，再尝试扣减；
- 桶内令牌不足即拒绝，并返回 `Retry-After`（= 尚需等待的秒数）。

令牌桶同时满足“限制稳态速率”与“容忍合理突发”两个目标。

## 2. 三个限流维度

`MultiLevelLimiter`（`app/quota/limiter.py`）对每个请求依次检查三级桶，
任一级不足即拒绝，且拒绝原因可区分：

| 顺序 | 维度 | key | 拒绝错误码 |
| --- | --- | --- | --- |
| 1 | 客户端（聚合该客户端所有接口） | `client:{client_id}` | `rate_limited.client` |
| 2 | 接口（聚合所有客户端调用） | `api:{api}` | `rate_limited.api` |
| 3 | 客户端 × 接口（最严格优先） | `client_api:{client}|{api}` | `rate_limited.client_api` |

未登记的客户端/接口分别回落到默认规则（见 `app/config.py`）。

## 3. 并发一致性：不超发、不重复扣减

- 判定全程持有单个 `asyncio.Lock`，把“补充 → 判定 → 扣减”串行化。
  因此即便 N 个协程同时到达，获得令牌的请求数也**恰好**不超过桶容量。
- 三级按顺序扣减；若后一级拒绝，则把前两级已扣的令牌**原样回补**，
  不会因一次失败请求造成配额被错误/重复扣减。

对应测试：`tests/test_quota_limiter.py`（50 并发 / 突发 5 → 恰好放行 5）。

## 4. 进程重启后的状态恢复

- 限流器周期性（`flush_interval`，默认 1s）把全部桶的
  `{tokens, last_refill}` 快照通过 `QuotaStore` 原子写入 JSON 文件
  （临时文件 + `os.replace`，写坏不留半成品）。
- 使用墙上时钟（`time.time()`）而非单调时钟，保证跨进程可比。
- 启动时读取快照，按“当前时间 − last_refill”补算停机期间应补充的令牌，
  上限仍为 `burst`。恢复约定因此是确定的：
  > 重启后令牌数 = min(burst, 落盘令牌数 + 停机秒数 × rate)
- 状态文件缺失或损坏时按冷启动处理（满桶），不阻断进程启动。

## 5. 错误响应示例

```json
{
  "error": {
    "code": "rate_limited.client_api",
    "message": "请求超出 client_api 维度配额",
    "details": { "level": "client_api", "retry_after": 0.5 }
  }
}
```

HTTP 状态码 `429`，并带 `Retry-After` 响应头。
