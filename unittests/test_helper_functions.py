import os
from typing import Generator, NamedTuple

import pytest
try:
    # Linux expects the latest package version of 3.35.4 (as of pysqlite-binary 0.4.6)
    import pysqlite3 as sqlite3
except ModuleNotFoundError:
    # MacOS has latest brew version of 3.35.5 (as of 2021-06-20).
    # Windows builds use the official Python 3.10.0 builds and bundled version of 3.35.5.
    import sqlite3 # type: ignore[no-redef]

from electrumsv_database.sqlite import DatabaseContext, read_rows_by_ids, \
    replace_db_context_with_connection

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


def test_read_rows_by_ids(db_context: DatabaseContext) -> None:
    """
    `read_rows_by_ids` allows filtering on a composite id for each row. This test checks that
    it works correctly.
    """
    connection = db_context.acquire_connection()
    try:
        connection.execute("CREATE TABLE A (myid INTEGER PRIMARY KEY, field1 INTEGER, "
            "field2 INTEGER, name TEXT)")
        connection.execute("INSERT INTO A (field1, field2, name) VALUES (20, 11, 'first')")
        connection.execute("INSERT INTO A (field1, field2, name) VALUES (30, 11, 'second')")
        connection.execute("INSERT INTO A (field1, field2, name) VALUES (40, 11, 'third')")

        class Row(NamedTuple):
            myid: int
            field1: int
            field2: int
            name: str

        filter_ids = [ (20, 11), (30, 11), (40, 11) ]

        # No static filter, filter on all the known ids.
        rows = read_rows_by_ids(Row, connection, "SELECT myid, field1, field2, name FROM A",
            "field1=? AND field2=?", [], filter_ids)
        assert rows == [ Row(1, 20, 11, "first"), Row(2, 30, 11, "second"),
            Row(3, 40, 11, "third") ]

        # Use a static filter (field2 < 40) to exclude the third, filter on all known ids.
        rows = read_rows_by_ids(Row, connection, "SELECT myid, field1, field2, name FROM A",
            "field1=? AND field2=?", [40], filter_ids, "field1<?")
        assert rows == [ Row(1, 20, 11, "first"), Row(2, 30, 11, "second") ]

        # Use a static filter (field2 < 40) to exclude the third, filter on the last two ids.
        rows = read_rows_by_ids(Row, connection, "SELECT myid, field1, field2, name FROM A",
            "field1=? AND field2=?", [40], filter_ids[1:], "field1<?")
        assert rows == [ Row(2, 30, 11, "second") ]
    finally:
        db_context.release_connection(connection)
