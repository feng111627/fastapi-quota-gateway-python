"""pytest 公共配置：确保单测日志打印请求输入与判定依据（INFO 级）。"""

import logging


def pytest_configure(config) -> None:  # noqa: ANN001
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
