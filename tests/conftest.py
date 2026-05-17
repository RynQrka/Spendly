import contextlib
import os
import tempfile

import pytest

# Monkey-patch config before spendly.core.config is loaded
# This guarantees tests use an in-memory DB and dummy tokens
os.environ["TELEGRAM_TOKEN"] = "test-token"
os.environ["MY_USER_ID"] = "1"
os.environ["GEMINI_API_KEY"] = "test-api-key"
os.environ["MONTHLY_BUDGET"] = "5000.0"

from spendly.core.config import settings


@pytest.fixture(autouse=True)
def isolated_db_path(monkeypatch):
    """Ensure all tests run with an isolated temporary SQLite database."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    from pathlib import Path

    # Store original
    original_path = settings.db_path

    # Bypass frozen dataclass locally via object.__setattr__
    object.__setattr__(settings, "db_path", Path(path))

    yield path

    # Restore and cleanup
    object.__setattr__(settings, "db_path", original_path)
    if os.path.exists(path):
        with contextlib.suppress(OSError):
            os.remove(path)
