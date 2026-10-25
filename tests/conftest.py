import os
import tempfile
from pathlib import Path
import pytest
import pytest_asyncio

from db import init_db, set_db_path


@pytest_asyncio.fixture
async def temp_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        test_db_path = Path(tmpdir) / "test_state.db"
        set_db_path(test_db_path)
        await init_db(test_db_path)
        yield test_db_path

