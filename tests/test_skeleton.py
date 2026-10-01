"""骨架冒烟测试：仅验证所有模块可被导入。"""


def test_imports() -> None:
    import app.api  # noqa: F401
    import app.config  # noqa: F401
    import app.errors  # noqa: F401
    import app.gateway  # noqa: F401
    import app.quota  # noqa: F401
    import app.resilience  # noqa: F401
    import app.versioning  # noqa: F401
