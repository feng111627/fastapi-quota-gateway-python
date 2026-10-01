"""FastAPI 配额网关接入层。

子模块职责：
- quota:       多级（客户端 × 接口）令牌桶限流，含状态持久化与重启恢复
- versioning:  接口多版本注册、协商与弃用提示
- resilience:  上游错误分类、熔断状态机与降级
- api:         FastAPI 路由与依赖装配
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
