import os
from typing import Generator

import pytest
try:
    # Linux expects the latest package version of 3.35.4 (as of pysqlite-binary 0.4.6)
    import pysqlite3 as sqlite3
except ModuleNotFoundError:
    # MacOS has latest brew version of 3.35.5 (as of 2021-06-20).
    # Windows builds use the official Python 3.10.0 builds and bundled version of 3.35.5.
    import sqlite3 # type: ignore[no-redef]

from electrumsv_database.sqlite import DatabaseContext, replace_db_context_with_connection

@pytest.fixture
def db_context() -> Generator[DatabaseContext, None, None]:
    unique_name = os.urandom(8).hex()
    db_filename = DatabaseContext.shared_memory_uri(unique_name)
    db_context = DatabaseContext(db_filename)
    yield db_context
    db_context.close()


# We are testing two things here. Firstly that this decorator works as advertised and secondly
# that the mypy type-checking approves of it.
def test_replace_db_context_with_connection(db_context: DatabaseContext) -> None:
    # TODO(typing) Mypy lacks PEP 612 support. The `id` parameter cannot be type checked.
    @replace_db_context_with_connection
    def reader(db: sqlite3.Connection, id: int) -> tuple[str, int]:
        assert isinstance(db, sqlite3.Connection)
        return "tuple", id
    result = reader(db_context, 111)
    assert result == ("tuple", 111)
