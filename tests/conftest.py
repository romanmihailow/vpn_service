"""Pytest fixtures и env для тестов (без доступа к /app/logs и БД)."""
import os
import sys
import tempfile
from unittest.mock import MagicMock

import pytest

# Подменяем app.db и app.wg до первого импорта app, чтобы тесты не требовали БД и WireGuard
if "app.db" not in sys.modules:
    sys.modules["app.db"] = MagicMock()
if "app.wg" not in sys.modules:
    sys.modules["app.wg"] = MagicMock()


@pytest.fixture(scope="session", autouse=True)
def _log_dir_for_tests():
    """Перенаправляем логи в временную директорию."""
    d = tempfile.mkdtemp(prefix="vpn_service_test_logs_")
    os.environ["LOG_DIR"] = d
    yield d
