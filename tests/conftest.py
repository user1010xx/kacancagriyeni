import os
import logging
import tempfile
from pathlib import Path

import pytest
import requests
from telegram.request import HTTPXRequest


_test_data = tempfile.TemporaryDirectory(prefix="kacancagri-tests-")
os.environ["DATA_DIR"] = str(Path(_test_data.name))


def pytest_sessionfinish(session, exitstatus):
    logging.shutdown()
    _test_data.cleanup()


@pytest.fixture(autouse=True)
def block_live_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Live network requests are forbidden in tests")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    monkeypatch.setattr(HTTPXRequest, "do_request", blocked)