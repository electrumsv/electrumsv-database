from __future__ import annotations
import asyncio
import concurrent.futures
from enum import Enum
import functools
import logging
import queue
try:
    # Linux expects the latest package version of 3.35.4 (as of pysqlite-binary 0.4.6)
    import pysqlite3 as sqlite3
except ModuleNotFoundError:
    # MacOS has latest brew version of 3.35.5 (as of 2021-06-20).
    # Windows builds use the official Python 3.10.0 builds and bundled version of 3.35.5.
    import sqlite3 # type: ignore[no-redef]
import threading
import time
from typing import Any, cast, Callable, Collection, Optional, ParamSpec, Protocol, Sequence, \
    Type, TypeVar


DATABASE_EXT = ".sqlite"

class DatabaseFunction(Protocol):
    def __call__(*args: Any, **kwargs: Any) -> Any:
        ...


P1 = ParamSpec("P1")
T1 = TypeVar("T1")
T2 = TypeVar('T2')


class LeakedSQLiteConnectionError(Exception):
    pass


# TODO(rt12): Remove the special case exception for WAL journal mode and see if the in-memory
#     databases work now that there's locking preventing concurrent enabling of the WAL mode,
#     in addition to the backing off of retries at enabling it. I vaguely recall that it perhaps
#     was exacerbated on the Azure Pipelines CI, and had errors there it didn't when running
#     the unit tests locally (the unit tests exercise the in-memory storage).

def max_sql_variables() -> int:
    """Get the maximum number of arguments allowed in a query by the current
    sqlite3 implementation.

    ESV amendment: Report that on CentOS the following error occurs:
       "sqlite3.OperationalError: too many terms in compound SELECT"
    This is another limit, likely lower: SQLITE_LIMIT_COMPOUND_SELECT

    Returns
    -------
    int
        inferred SQLITE_MAX_VARIABLE_NUMBER
    """
    db = sqlite3.connect(':memory:')
    cur = db.cursor()
    cur.execute('CREATE TABLE t (test)')
    low, high = 0, 100000
    while (high - 1) > low:
        guess = (high + low) // 2
        query = 'INSERT INTO t VALUES ' + ','.join(['(?)' for _ in
                                                    range(guess)])
        args = [str(i) for i in range(guess)]
        try:
            cur.execute(query, args)
        except sqlite3.OperationalError as e:
            es = str(e)
            if "too many SQL variables" in es or "too many terms in compound SELECT" in es:
                high = guess
            else:
                raise
        else:
            low = guess
    cur.close()
    db.close()
    return low

# If the query deals with a list of values, then just batching using `SQLITE_MAX_VARS` should
# be enough. If it deals with expressions, then batch using the least of that and
# `SQLITE_EXPR_TREE_DEPTH`.
# - This shows how to estimate the maximum variables.
#   https://stackoverflow.com/a/36788489
# - This shows that even if you have higher maximum variables you get:
#   "Expression tree is too large (maximum depth 1000)"
#   https://github.com/electrumsv/electrumsv/issues/539
SQLITE_MAX_VARS = max_sql_variables()
SQLITE_EXPR_TREE_DEPTH = 1000


class WriteDisabledError(Exception):
    pass


class SqliteWriteDispatcher:
    """
    This is a relatively simple write dispatcher for Sqlite that keeps all the writes on one thread,
    in order to avoid conflicts. Any higher level context that invokes a write, can choose to
    get notified on completion. If an exception happens in the course of a writer, the exception
    is passed back to the invoker in the completion notification.

    Completion notifications are done in a thread so as to not block the write dispatcher.
    """

    def __init__(self, db_context: DatabaseContext, write_warn_ms: int=0) -> None:
        self._db_context = db_context
        self._logger = logging.getLogger("sqlite-writer")

        self._writer_queue: queue.Queue["ExecutorItem"] = queue.Queue()
        self._writer_thread = threading.Thread(target=self._writer_thread_main, daemon=True)
        self._writer_loop_event = threading.Event()

        self._allow_puts = True
        self._is_alive = True
        self._exit_when_empty = False
        self._write_warn_ms = write_warn_ms

        self._writer_thread.start()

    def _writer_thread_main(self) -> None:
        self._db: sqlite3.Connection = self._db_context.acquire_connection()

        while self._is_alive:
            self._writer_loop_event.set()

            # Block until we have at least one write action. If we already have write
            # actions at this point, it is because we need to retry after a transaction
            # was rolled back.
            try:
                write_entry: ExecutorItem = self._writer_queue.get(timeout=0.1)
            except queue.Empty:
                if self._exit_when_empty:
                    return
                continue

            time_start = time.time()
            write_entry(self._db)
            time_ms = int((time.time() - time_start) * 1000)
            if time_ms > self._write_warn_ms:
                self._logger.debug("Invoked write callback in %d ms", time_ms)

    def put(self, write_entry: 'ExecutorItem') -> None:
        # If the writer is closed, then it is expected the caller should have made sure that
        # no more puts will be made, and the error will only be raised if something puts to
        # flag that it is wrong.
        if not self._allow_puts:
            raise WriteDisabledError()

        self._writer_queue.put_nowait(write_entry)

    def stop(self) -> None:
        if self._exit_when_empty:
            return

        self._allow_puts = False
        self._exit_when_empty = True

        # Wait for both threads to exit.
        self._writer_loop_event.wait()
        self._writer_thread.join()
        self._db_context.release_connection(self._db)
        self._is_alive = False

    def is_stopped(self) -> bool:
        return not self._is_alive


class JournalModes(Enum):
    DELETE = "DELETE"
    TRUNCATE = "TRUNCATE"
    PERSIST = "PERSIST"
    MEMORY = "MEMORY"
    WAL = "WAL"
    OFF = "OFF"


class DatabaseContext:
    MEMORY_PATH = ":memory:"
    JOURNAL_MODE = JournalModes.WAL

    def __init__(self, wallet_path: str, *, write_warn_ms: int=0) -> None:
        if not self.is_special_path(wallet_path) and not wallet_path.endswith(DATABASE_EXT):
            wallet_path += DATABASE_EXT
        self._db_path = wallet_path
        self._connection_pool: queue.Queue[sqlite3.Connection] = queue.Queue()
        self._active_connections: set[sqlite3.Connection] = set()

        self._logger = logging.getLogger("sqlite-context")
        self._lock = threading.Lock()

        # There is a thing where some people run ElecturmSV in environments that it is not
        # compatible with, because SQLite is not compatible with them. The reason given by the
        # SQLite community, is that people are hosting files on network shares and WAL mode
        # cannot handle it. For this reason, we precreate a database connection on the thread
        # that constructs the database context, so hopefully this will raise any error up to
        # the calling logic (see issue #432). We do this before starting the writer thread
        # rather than letting it happen in the decoupled writer thread.
        self.increase_connection_pool()
        self._write_dispatcher = SqliteWriteDispatcher(self, write_warn_ms)
        self._executor = SqliteExecutor(self._write_dispatcher)

    def acquire_connection(self) -> sqlite3.Connection:
        try:
            conn = self._connection_pool.get_nowait()
        except queue.Empty:
            self.increase_connection_pool()
            conn = self._connection_pool.get_nowait()
        self._active_connections.add(conn)
        return conn

    def release_connection(self, connection: sqlite3.Connection) -> None:
        self._active_connections.remove(connection)
        self._connection_pool.put(connection)

    def increase_connection_pool(self) -> None:
        """adds 1 more connection to the pool"""
        # pylint: disable=line-too-long
        # `isolation_level` is set to `None` in order to disable the automatic transaction
        # management in :mod:`sqlite3`. See the `Python documentation <https://docs.python.org/3/library/sqlite3.html#controlling-transactions>`_
        is_special_path = self.is_special_path(self._db_path)
        connection = sqlite3.connect(self._db_path, check_same_thread=False,
            isolation_level=None, uri=is_special_path)
        connection.execute("PRAGMA busy_timeout=5000;")
        connection.execute("PRAGMA foreign_keys=ON;")
        # We do not enable journaling for in-memory databases. It resulted in 'database is locked'
        # errors. Perhaps it works now with the locking and backoff retries.
        if not self.is_special_path(self._db_path):
            self._ensure_journal_mode(connection)

        self._connection_pool.put(connection)

    def decrease_connection_pool(self) -> None:
        """release 1 more connection from the pool - raises empty queue error"""
        connection = self._connection_pool.get_nowait()
        connection.close()

    def _ensure_journal_mode(self, connection: sqlite3.Connection) -> None:
        with self._lock:
            cursor = connection.execute(f"PRAGMA journal_mode;")
            journal_mode = cursor.fetchone()[0]
            if journal_mode.upper() == self.JOURNAL_MODE.value:
                return

            self._logger.debug("Switching database from journal mode %s to journal mode %s",
                journal_mode.upper(), self.JOURNAL_MODE.value)

            time_start = time.time()
            attempt = 1
            delay = 0.05
            while True:
                try:
                    cursor = connection.execute(f"PRAGMA journal_mode={self.JOURNAL_MODE.value};")
                except sqlite3.OperationalError:
                    time_delta = time.time() - time_start
                    if time_delta < 10.0:
                        delay = min(delay, max(0.05, 10.0 - time_delta))
                        time.sleep(delay)
                        self._logger.warning("Database %s pragma attempt %d at %ds",
                            self.JOURNAL_MODE.value, attempt, time_delta)
                        delay *= 2
                        attempt += 1
                        continue
                    raise
                else:
                    journal_mode = cursor.fetchone()[0]
                    if journal_mode.upper() != self.JOURNAL_MODE.value:
                        self._logger.error(
                            "Database unable to switch from journal mode %s to journal mode %s",
                            self.JOURNAL_MODE.value, journal_mode.upper())
                        return
                    break

            self._logger.debug("Database now in journal mode %s", self.JOURNAL_MODE.value)

    def get_path(self) -> str:
        return self._db_path

    def close(self) -> None:
        self._write_dispatcher.stop()

        # Force close all outstanding connections
        outstanding_connections = list(self._active_connections)
        for conn in outstanding_connections:
            self.release_connection(conn)

        while self._connection_pool.qsize() > 0:
            self.decrease_connection_pool()

        leak_count = len(outstanding_connections)
        if leak_count:
            raise LeakedSQLiteConnectionError(f"Leaked {leak_count} SQLite connections "
                "when closing DatabaseContext.")
        assert self.is_closed(), f"{self._write_dispatcher.is_stopped()}"

    def is_closed(self) -> bool:
        return self._connection_pool.qsize() == 0 and self._write_dispatcher.is_stopped()

    def is_special_path(self, path: str) -> bool:
        # Each connection has a private database.
        if path == self.MEMORY_PATH:
            return True
        # Shared temporary in-memory database.
        # file:memdb1?mode=memory&cache=shared"
        if path.startswith("file:") and "mode=memory" in path:
            return True
        return False

    def post_to_thread(self, func: Callable[P1, T1], *args: P1.args, **kwargs: P1.kwargs) \
            -> concurrent.futures.Future[T1]:
        return self._executor.submit(func, *args, **kwargs)

    def run_in_thread(self, func: Callable[P1, T1], *args: P1.args, **kwargs: P1.kwargs) -> T1:
        future = self._executor.submit(func, *args, **kwargs)
        return cast(T1, future.result())

    async def run_in_thread_async(self, func: Callable[P1, T1], *args: P1.args, \
            **kwargs: P1.kwargs) -> T1:
        """
        Yield the current task until the function has executed in the SQLite write thread.

        This should never be called from outside :mod:`electrumsv.wallet_database`. It is limited
        to positional arguments because that is all :meth:`asyncio.BaseEventLoop.run_in_executor`
        takes.

        The first argument will be the `sqlite3.Connection` instance of the database connection
        that should be used for writing (and reads related to the write). If the caller needs
        positional arguments to precede the database connection, they should use
        :meth:`functools.partial` to achieve that.

        .. code-block:: python

           def _writer(db: sqlite3.Connection) -> Any:
               cursor = db.execute("DELETE FROM Transactions WHERE tx_data IS NULL")
               return cursor.rowcount

           db_context.run_in_thread_async(_writer)
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._executor, functools.partial(func, *args, **kwargs))

    @classmethod
    def shared_memory_uri(cls, unique_name: str) -> str:
        return f"file:{unique_name}?mode=memory&cache=shared"


# Based on `concurrent.futures.thread._ExecutorItem`.
# Relabels `run` to `__call__` and
class ExecutorItem(object):
    def __init__(self, future: concurrent.futures.Future[Any], fn: DatabaseFunction,
            args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        self._future = future
        self._fn: DatabaseFunction = fn
        self._args = args
        self._kwargs = kwargs

    def __call__(self, db: sqlite3.Connection) -> None:
        if not self._future.set_running_or_notify_cancel():
            return

        db.execute("BEGIN")
        try:
            result = self._fn(*self._args, **self._kwargs, db=db)
        except BaseException as exc:
            db.execute("ROLLBACK")
            self._future.set_exception(exc)
            # Break a reference cycle with the exception 'exc'
            self = None # type: ignore
        else:
            db.execute("COMMIT")
            self._future.set_result(result)


class SqliteExecutor(concurrent.futures.Executor):
    """
    Allow queueing SQLite database writes on the writer thread.

    At this time is is intended that this should only be used through
    :meth:`DatabaseContext.run_in_thread`, and not used directly.
    """
    def __init__(self, dispatcher: SqliteWriteDispatcher) -> None:
        self._dispatcher = dispatcher
        self._shutdown = False
        self._shutdown_lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self._active_items = 0

    # NOTE(typing) mypy wants a perfect function signature match with Executor parent class
    def submit(self, fn, *args, **kwargs) -> concurrent.futures.Future:  # type: ignore
        # The `db` argument is injected by the write dispatcher. Typing requires that the
        # client application see this parameter in their write callback but they should have it
        # last and optional so they never need to provide it and typing ignores it.
        assert "db" not in kwargs
        with self._shutdown_lock:
            if self._shutdown:
                raise RuntimeError('cannot schedule new futures after shutdown')
            self._active_items += 1
            future: concurrent.futures.Future[Any] = concurrent.futures.Future()
            # Used to implement the wait on shutdown.
            future.add_done_callback(self._on_future_done)
            self._dispatcher.put(ExecutorItem(future, fn, args, kwargs))
            return future

    # NOTE(typing) mypy wants a perfect function signature match with Executor parent class
    def shutdown(self, wait: bool=True) -> None:  # type: ignore
        with self._shutdown_lock:
            self._shutdown = True
        if wait:
            self._shutdown_event.wait()

    def _on_future_done(self, _future: concurrent.futures.Future[Any]) -> None:
        with self._shutdown_lock:
            self._active_items -= 1
            if self._active_items == 0:
                self._shutdown_event.set()


# It is currently not possible to implement the disabled typed version below because mypy does
# not have support for PEP 612 yet. This results in the following errors:
# 'error: The first argument to Callable must be a list of types or "..."  [misc]'
# - https://github.com/python/mypy/issues/11833
# - https://github.com/python/typeshed/issues/4827
# TODO(typing) Mypy lacks PEP 612 support. When it supports it, delete this.
def replace_db_context_with_connection(func: Callable[..., T1]) -> Callable[..., T1]:
    def wrapped_call(db_context: DatabaseContext, *args: Any, **kwargs: Any) -> T1:
        db = db_context.acquire_connection()
        try:
            return func(db, *args, **kwargs)
        finally:
            db_context.release_connection(db)
    return wrapped_call

# TODO(typing) Mypy lacks PEP 612 support. When it supports it, uncomment this.
# # This takes a function and a set of parameters prepended with a database context and replaces
# # the database context with a database connection. It should propagate typing through the
# # decorator.
# def replace_db_context_with_connection(func: Callable[Concatenate[sqlite3.Connection, P1], T1]) \
#         -> Callable[Concatenate[DatabaseContext, P1], T1]:
#     def wrapped_call(db_context: DatabaseContext, *args: P1.args, **kwargs: P1.kwargs) -> T1:
#         db = db_context.acquire_connection()
#         try:
#             return func(db, *args, **kwargs)
#         finally:
#             db_context.release_connection(db)
#     return wrapped_call


def read_rows_by_id(return_type: Type[T1], db: sqlite3.Connection, sql: str, params: Sequence[Any],
        ids: Sequence[T2]) -> list[T1]:
    """
    Batch read rows as constrained by database limitations.
    """
    results: list[T1] = []
    batch_size = SQLITE_MAX_VARS - len(params)
    remaining_ids = ids
    params = tuple(params)
    while len(remaining_ids):
        batch_ids = tuple(remaining_ids[:batch_size])
        sql = sql.format(",".join("?" for k in batch_ids))
        cursor = db.execute(sql, params + tuple(batch_ids))
        rows = cursor.fetchall()
        cursor.close()
        # Skip copying/conversion for standard types.
        if len(rows):
            if return_type is bytes:
                assert len(rows[0]) == 1 and type(rows[0][0]) is return_type
                results.extend(row[0] for row in rows)
            else:
                results.extend(return_type(*row) for row in rows)
        remaining_ids = remaining_ids[batch_size:]
    return results


def read_rows_by_ids(return_type: Type[T1], db: sqlite3.Connection, sql: str, sql_condition: str,
        sql_values: list[Any], ids: Sequence[Collection[T2]], where_condition: str|None=None) \
            -> list[T1]:
    """
    Read rows in batches as constrained by database limitations.
    """
    batch_size = min(SQLITE_MAX_VARS, SQLITE_EXPR_TREE_DEPTH) // 2 - len(sql_values)
    results: list[T1] = []
    remaining_ids = ids
    while len(remaining_ids):
        batch = remaining_ids[:batch_size]
        batch_values: list[Any] = list(sql_values)
        for batch_entry in batch:
            batch_values.extend(batch_entry)
        conditions = [ sql_condition ] * len(batch)
        if where_condition is not None:
            batch_query = f"{sql} WHERE {where_condition} AND ({' OR '.join(conditions)})"
        else:
            batch_query = f"{sql} WHERE {' OR '.join(conditions)}"
        cursor = db.execute(batch_query, batch_values)
        results.extend(return_type(*row) for row in cursor.fetchall())
        cursor.close()
        remaining_ids = remaining_ids[batch_size:]
    return results


# TODO(ideal) In an ideal world we would pass in a row factory and a return type, or use the typing
#   stuff to use them. For instance, if fields in the return type are `IntFlag` structures they are
#   not treated the same in their integer form as they would be if they were cast to the structure.
def execute_sql_by_id(db: sqlite3.Connection, sql: str,
        sql_values: list[Any], ids: Sequence[T2], return_type: Optional[Type[T1]]=None) \
            -> tuple[int, list[T1]]:
    """
    Update, delete or whatever rows in batches as constrained by database limitations.

    If the update statement returns values via `RETURNING`, specify a return type. However, it has
    been observed that sometimes rows will be updated and returned, where `rowcount` is set to
    zero. So it is better to check the count of the updates returned values, than rely on the
    `rowcount` field.
    """
    batch_size = SQLITE_MAX_VARS - len(sql_values)
    rows_updated = 0
    remaining_ids = ids
    rows: list[T1] = []
    while len(remaining_ids):
        batch_ids = remaining_ids[:batch_size]
        sql = sql.format(",".join("?" for k in batch_ids))
        # NOTE(typing) Cannot add a sequence to a list.
        cursor = db.execute(sql, sql_values + batch_ids) # type: ignore
        if return_type is not None:
            rows.extend(return_type(*row) for row in cursor.fetchall())
        rows_updated += cursor.rowcount
        cursor.close()
        remaining_ids = remaining_ids[batch_size:]
    return rows_updated, rows


def bulk_insert_returning(return_type: Type[T1], db: sqlite3.Connection, sql_prefix: str,
        sql_suffix: str, insert_rows: Sequence[Collection[T2]]) -> list[T1]:
    """
    Do a bulk insert in a way where we get the primary key value back.

    sql_prefix expected value `INSERT INTO MyTable (column1, column2) VALUES `
    sql_suffix expected value `RETURNING key_column, column1, column2`

    SQLite does not guarantee the order of the returned values, so it cannot be used to just
    return the assigned primary key value for each given row. Instead the simplest approach is
    to just return the whole row and give it back to the calling logic to use in place of the
    source row data that went in.

    It is the reponsibility of the caller to ensure the RETURNING column order provided in
    `sql_suffix` matches the number of elements, and order, of the type to be returned
    `return_type`.
    """
    row_size = len(insert_rows[0])
    rows_per_batch = int(SQLITE_MAX_VARS // row_size)

    value_parameter_text = "("+ ",".join(["?"] * row_size) +")"
    next_row_index = 0
    final_rows: list[T1] = []
    while next_row_index < len(insert_rows):
        batch_row_count = min(rows_per_batch, len(insert_rows)-next_row_index)
        batch_values: list[Any] = []
        values_text = ""
        for i in range(next_row_index, next_row_index + batch_row_count):
            if len(values_text):
                values_text += ","
            values_text += value_parameter_text
            batch_values.extend(insert_rows[i])

        sql_completed = f"{sql_prefix} {values_text} {sql_suffix}"
        for inserted_row in db.execute(sql_completed, batch_values):
            final_rows.append(return_type(*inserted_row))

        next_row_index += batch_row_count
    return final_rows


def update_rows_by_ids(db: sqlite3.Connection, sql: str, sql_id_expression: str,
        sql_values: list[Any], ids: Sequence[Collection[T1]],
        sql_where_expression: Optional[str]=None) -> int:
    """
    Update rows in batches as constrained by database limitations.

    This does not yet support `RETURNING`.
    """
    batch_size = min(SQLITE_MAX_VARS, SQLITE_EXPR_TREE_DEPTH) // 2 - len(sql_values)
    rows_updated = 0
    remaining_ids = ids
    while len(remaining_ids):
        batch_ids = remaining_ids[:batch_size]
        batch_values: list[Any] = sql_values[:]
        for batch_entry in batch_ids:
            batch_values.extend(batch_entry)
        id_sql_expressions = [ sql_id_expression ] * len(batch_ids)
        sql_completed = sql +" WHERE "
        sql_completed_id_expression = " OR ".join(id_sql_expressions)
        if sql_where_expression:
            sql_completed += f"{sql_where_expression} AND ({sql_completed_id_expression})"
        else:
            sql_completed += sql_completed_id_expression
        cursor = db.execute(sql_completed, batch_values)
        rows_updated += cursor.rowcount
        cursor.close()
        remaining_ids = remaining_ids[batch_size:]
    return rows_updated
