"""测试日志约定：每个用例开始时打印所属业务模块与用例名，
结束时打印判定结果（PASSED/FAILED）；各用例内部的 logger.info
负责打印输入与关键判定依据，pytest 的 log_cli 会原样输出到控制台。
"""

from __future__ import annotations

import logging

import pytest

logger = logging.getLogger("test.case")


@pytest.fixture(autouse=True)
def _log_case_boundaries(request: pytest.FixtureRequest) -> None:
    logger.info(
        "CASE START business=%s case=%s",
        request.module.__name__,
        request.node.name,
    )
    yield


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when == "call":
        logger.info(
            "CASE END   case=%s verdict=%s",
            report.nodeid,
            report.outcome.upper(),
        )
