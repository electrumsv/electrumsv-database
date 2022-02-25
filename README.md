# ElectrumSV Database

    Licence: MIT License
    Maintainers: Roger Taylor, AustEcon
    Project Lead: Roger Taylor
    Homepage: https://electrumsv.io/


# Overview

This is the database support library for ElectrumSV. This functionality has been extracted into
an independent package so that it can be used by other projects.

## Usage

### Reads

It is envisioned that most reads will be done with the aid of the
`replace_db_context_with_connection` decorator. The calling logic will have a reference to a
database context, and the decorator will inject a database connection as the first argument to
the wrapped function. These can happen inline in the calling context.

If a read query is one that will take more than a nominal amount of time, the developer should
use worker threads to farm out the query. There is a good argument that we should add that to
this library in order to deal with the typing complications.

### Writes

SQLite has a [well known limitation](https://www.sqlite.org/faq.html#q5) in that only one
connection can be making changes, or writes, at a time. What this package does is use one
writer thread to apply write queries sequentially through it's connection. This is all managed
as part of the `DatabaseContext` class, which creates the `SqliteWriteDispatcher` and
`SqliteExecutor` for you.

Creating a database context::

```
from electrumsv_database import DatabaseContext
database_context = DatabaseContext(database_path)
```

Block executing a writer callback as a transaction::

```
def write(a: int, s: str, db: Optional[sqlite3.Connection]=None) -> str:
    assert db is not None and isinstance(db, sqlite3.Connection)
    db.execute("INSERT INTO SomeTable(a, s) VALUES (?, ?)", (a, s))
    return "whatever return value"

s = database_context.run_in_thread(write, 5, "test")
assert s == "whatever return value"
```

Post a writer callback to be executed as a transaction::

```
def write(a: int, s: str, db: Optional[sqlite3.Connection]=None) -> str:
    assert db is not None and isinstance(db, sqlite3.Connection)
    db.execute("INSERT INTO SomeTable(a, s) VALUES (?, ?)", (a, s))
    return "whatever return value"

future = database_context.post_to_thread(write, 5, "test")
# Perform whatever other logic.
s = future.result()
assert s == "whatever return value"
```

Asynchronously block executing a writer callback as a transaction::

```
def write(a: int, s: str, db: Optional[sqlite3.Connection]=None) -> str:
    assert db is not None and isinstance(db, sqlite3.Connection)
    db.execute("INSERT INTO SomeTable(a, s) VALUES (?, ?)", (a, s))
    return "whatever return value"

s = await database_context.run_in_thread_async(write, 5, "test")
assert s == "whatever return value"
```


## Typing

Python has flawed problematic typing. It is very easy to have code that is wrong and not being
checked, but be unaware of it. This package makes various choices to try and ensure that all
of it's operations are typed.

### Write functions

Queries that do write operations are executed using callbacks, and this means that we want to
check the types of the arguments in the application logic. We use `ParamSpec` for this, but it has
a limitation in that the typing of its `args` and `kwargs` attributes are atomic.

```
P1 = ParamSpec("P1")
T1 = TypeVar("T1")

    async def run_in_thread_async(self, func: Callable[P1, T1], *args: P1.args, \
            **kwargs: P1.kwargs) -> T1:
        ...
```

It is not possible to remove or add arguments to take into account perhaps extra ones added in
the writer thread - like a reference to the database connection which the write callback should
use to execute it's query. For this reason we use the following pattern, the write callback
adds an optional `db` keyword argument to the end of it's argument list, the write dispatcher
provides that adding it as an extra argument over the one the application provided.

The following pattern should be used::

```
def set_payment_channel_closed(channel_id: int, channel_state: ChannelState,
        db: Optional[sqlite3.Connection]=None) -> None:
    assert db is not None and isinstance(db, sqlite3.Connection)
    ...
```
