# 接口版本兼容与协商策略

## 1. 版本模型

- 版本号为**正整数**（`1, 2, 3 ...`），同一接口的多个版本可在注册表中共存。
- 每个版本处于三种状态之一（`app/versioning/registry.py`）：

| 状态 | 行为 |
| --- | --- |
| `supported` | 当前版本，正常可用 |
| `deprecated` | 弃用期内**仍可正常使用**，但每次响应都带明确提示 |
| `removed` | 已超出支持范围，必须拒绝（426），并指明替代版本 |

内置注册（`app/api/deps.py`）：

| 接口 | v1 | v2 | v3 |
| --- | --- | --- | --- |
| echo | deprecated（2026-12-31 停用，替代 v2） | supported | removed（替代 v2） |
| data | — | deprecated（替代 v3） | supported |

## 2. 协商规则（结果稳定可预期）

客户端通过请求头 `X-API-Version: <正整数>` 指定版本。
协商逻辑见 `app/versioning/negotiator.py`：

1. **头缺失 / 空白**：不报错，稳定解析为该接口“最新可用版本”
   （即状态非 `removed` 的最大版本号），`explicit=false`，
   无论谁调用、调用多少次，缺省解析结果都相同。
2. **非法值**（非数字、0、负数、小数）：`400 version.invalid`，回显非法输入。
3. **合法整数但未注册**：`426 version.unsupported`，附当前支持版本清单。
4. **已 removed**：`426 version.unsupported`，附支持清单与 `successor`。
5. **deprecated**：放行，同时给出提示（见下）。
6. **supported**：直接放行。

协商先于限流执行：非法/淘汰版本直接拒绝，**不消耗配额**。

## 3. 弃用提示（仍可用，但要明确告知）

弃用版本的响应同时携带：

- `Warning: 299 gateway "Deprecated API version 1"; sunset="2026-12-31"; successor="echo-v2"`
- `X-Api-Deprecated: true`
- `X-Api-Sunset: 2026-12-31`
- `X-Api-Successor-Version: 2`
- 响应体内的中文 `deprecation_notice`

> 注：HTTP 头只能使用 latin-1，所以头里是标准英文告警，中文说明放在响应体。

## 4. 错误响应示例

```json
// 400 version.invalid
{ "error": { "code": "version.invalid",
  "message": "非法的 X-API-Version: 'v2'，应为正整数",
  "details": { "header": "X-API-Version", "input": "v2" } } }

// 426 version.unsupported
{ "error": { "code": "version.unsupported",
  "message": "接口 echo 的版本 3 已停止支持",
  "details": { "supported": [1, 2], "successor": 2, "header": "X-API-Version" } } }
```

## 5. 如何新增/下线版本

在 `build_registry()` 中：新版本注册为 `supported`，把旧版本改为
`deprecated` 并填 `sunset_date/successor`；弃用期满后改为 `removed`。
缺省版本会自动跟随“最新可用版本”，无需额外改动。
