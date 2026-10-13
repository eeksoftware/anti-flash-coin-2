from __future__ import annotations

import pytest

from audit_log import configure_logging, flush_logging, log_test_event, shutdown_logging


_test_reports: dict[str, list] = {}


def pytest_configure(config):
    configure_logging()


def pytest_runtest_logreport(report):
    _test_reports.setdefault(report.nodeid, []).append(report)


def pytest_runtest_logfinish(nodeid, location):
    reports = _test_reports.pop(nodeid, [])
    if not reports:
        return
    outcome = (
        "failed" if any(report.failed for report in reports)
        else "skipped" if any(report.skipped for report in reports)
        else "passed"
    )
    log_test_event({
        "type": "pytest",
        "test_nodeid": nodeid,
        "outcome": outcome,
        "duration_s": round(sum(float(report.duration) for report in reports), 6),
    })


@pytest.fixture(autouse=True)
def dashboard_login_disabled(monkeypatch):
    """Tests run the panel without a login unless they set credentials themselves.

    config loads .env on first import; import it first so a later lazy import
    cannot bring the operator's real dashboard password back into the process.
    """
    import config  # noqa: F401

    monkeypatch.delenv("DASHBOARD_USERNAME", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)


@pytest.fixture
def eval_logger():
    def record_evaluation(
        *,
        tx_hash: str,
        expected: str,
        actual: str,
        network: str = "ethereum",
        note: str | None = None,
    ) -> bool:
        return log_test_event({
            "type": "evaluation",
            "network": network,
            "tx_hash": tx_hash,
            "expected": expected,
            "actual": actual,
            "note": note,
        })

    return record_evaluation


def pytest_sessionfinish(session, exitstatus):
    flush_logging()


def pytest_unconfigure(config):
    flush_logging()
    shutdown_logging()
