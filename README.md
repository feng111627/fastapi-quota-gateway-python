# fastapi-quota-gateway

在基础 FastAPI 服务之上新增的一套**面向客户端调用的接入层**，为上游接口提供：

1. **多级配额限流**（客户端 / 接口 / 客户端×接口 三级令牌桶）
2. **多版本共存与版本协商**（缺省稳定解析、弃用提示、超范围拒绝）
3. **熔断与降级**（连续失败快速失败、半开探测恢复、错误原因可区分）

所有判定在并发下保持一致、可解释，并在日志中打印“请求输入 + 判定依据”。

## 目录结构

```
app/
  config.py                # 配额/熔断配置类型与默认值
  errors.py                # 统一错误码 ErrorCode 与 GatewayError
  gateway.py               # 编排：版本 -> 限流 -> 熔断 -> 上游/降级
  quota/
    bucket.py              # 令牌桶（稳态速率 + 突发 + 重启恢复）
    store.py               # JSON 快照原子落盘/读取
    limiter.py             # 三级限流器（加锁串行化 + 拒绝回补）
  versioning/
    registry.py            # 多版本注册表（supported/deprecated/removed）
    negotiator.py          # X-API-Version 协商
  resilience/
    classifier.py          # 超时/连接/业务错误分类
    breaker.py             # CLOSED/OPEN/HALF_OPEN 熔断状态机
    fallback.py            # 降级兜底响应
    upstream.py            # httpx 上游客户端 + 测试替身
  api/
    deps.py                # 组件装配与版本注册
    routes.py              # /invoke/{api}、/health
tests/                     # 并发限流 / 版本边界 / 熔断状态转换测试
docs/                      # 配额模型、版本策略、熔断降级规则文档
main.py                    # FastAPI 应用入口与统一错误处理
```

## 判定链路

```
请求 POST /invoke/{api}
  Header: X-Client-Id, X-API-Version
        │
        ▼
 版本协商 ──非法/淘汰──► 400 version.invalid / 426 version.unsupported（不扣配额）
        │通过（deprecated 带告警头与中文提示）
        ▼
 三级限流 ──超限──────► 429 rate_limited.{client|api|client_api} + Retry-After
        │通过
        ▼
 熔断判定 ──OPEN/探测满─► 200 degraded=true（X-Degraded-Reason: breaker.open）
        │放行
        ▼
 调用上游 ──失败──────► 分类 timeout/connection/business
        │                ├ 熔断打开 → 200 降级
        │                └ 仍闭合   → 502 对应错误码
        ▼成功
 200 回源结果（半开探测成功则自动恢复 CLOSED）
```

## 本地验证

需要 Python 3.14 与 [uv](https://docs.astral.sh/uv/)。

### 1) 安装依赖并运行测试

```bash
uv sync
uv run pytest                      # 全部 40 个用例，日志实时打印判定依据
uv run pytest tests/test_quota_limiter.py    # 只看并发限流
uv run pytest tests/test_versioning.py       # 只看版本边界
uv run pytest tests/test_resilience_breaker.py  # 只看熔断状态转换
```

### 2) 启动服务并手工验证

默认上游地址为 `http://127.0.0.1:9000`。即使不启动上游，也可以用
“上游连接失败”来观察限流与熔断降级：

```bash
uv run python main.py              # 启动在 127.0.0.1:8000

# 正常调用（上游不存在时返回 502 upstream.connection）
curl -s -X POST localhost:8000/invoke/echo \
  -H 'X-Client-Id: free' -H 'X-API-Version: 2' \
  -H 'Content-Type: application/json' -d '{"q":"hi"}'

# 连续调用 5 次以上后，熔断打开 → 200 降级 degraded=true
curl -si -X POST localhost:8000/invoke/echo -H 'X-Client-Id: free' -d '{}'

# 弃用版本：200 但带 Warning / X-Api-* 头与 deprecation_notice
curl -si -X POST localhost:8000/invoke/echo -H 'X-Client-Id: pro' \
  -H 'X-API-Version: 1' -d '{}'

# 非法版本 400 / 淘汰版本 426
curl -si -X POST localhost:8000/invoke/echo -H 'X-API-Version: abc' -d '{}'
curl -si -X POST localhost:8000/invoke/echo -H 'X-API-Version: 3'   -d '{}'

# 熔断状态观测
curl -s localhost:8000/health
```

> 提示：`free` 客户端配额较小（1/s，突发 2），快速连发即可观察到
> `429 rate_limited.*`；`pro` 客户端配额较大，适合观察熔断。

若要验证真实回源成功，可在本机 9000 端口起一个简单 HTTP 服务，
对 `POST /echo` 返回 2xx JSON，或修改 `app/api/deps.py` 中
`HTTPUpstreamClient` 的地址。

## 详细文档

- [配额模型](docs/quota-model.md)
- [版本兼容策略](docs/versioning.md)
- [熔断与降级规则](docs/circuit-breaker.md)
