"""
migration_framework.py – Verified table-rebuild migration helpers for SmartHR.

SQLite cannot ALTER a CHECK constraint, change a column type, or add a
NOT NULL column without rebuilding the table. Rebuilds are only safe when
every existing row is preserved and dependent schema objects (indexes,
triggers) are recreated.

This module provides the reusable, assertion-guarded building blocks used
by the migrations in init_db.py:

  - backup_database()  – consistent online snapshot before a migration
  - get_table_info()   – capture source columns, indexes and triggers
  - rebuild_table()    – transactional copy -> assert -> drop -> rename,
                         recreating indexes/triggers and verifying integrity

Contract for callers
--------------------
* Migrations MUST detect "already applied" state and skip before calling
  rebuild_table() (rebuilds are single-shot by design).
* Migrations MUST take a backup (backup_before_migration) before running
  against any database that holds real data.
* rebuild_table() runs its own transaction and raises RuntimeError on any
  assertion failure, rolling the transaction back so the source table is
  left untouched.
"""

import os
import sqlite3
from datetime import datetime


def backup_database(db_path, backup_path):
    """Create a consistent snapshot of `db_path` at `backup_path`.

    Uses the sqlite3 online-backup API so the snapshot is consistent even
    if the source database is being written concurrently (e.g. WAL mode).
    Returns `backup_path`.
    """
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(backup_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return backup_path


def backup_before_migration(db_path, backup_dir=None):
    """Create a timestamped, never-overwriting backup before a rebuild.

    The backup is written to `backup_dir` (default `<db parent>/backups`),
    which is always outside the active database file path. If the
    timestamped name already exists, a numeric suffix is appended rather
    than overwriting an existing backup.

    A backup failure aborts the caller (an exception propagates) so a
    rebuild never runs without a pre-migration snapshot. Returns the path
    of the created backup.
    """
    if backup_dir is None:
        backup_dir = os.path.join(os.path.dirname(os.path.abspath(db_path)), 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    base = 'smarthr_backup_%s' % stamp
    path = os.path.join(backup_dir, base + '.db')
    n = 1
    while os.path.exists(path):
        path = os.path.join(backup_dir, '%s_%d.db' % (base, n))
        n += 1
    return backup_database(db_path, path)


def get_table_info(con, table):
    """Return (columns, index_sqls, trigger_sqls, index_names) for `table`.

    `index_sqls`/`trigger_sqls` are the original CREATE statements captured
    from sqlite_master so they can be replayed verbatim after a rebuild.
    """
    columns = [r[1] for r in con.execute("PRAGMA table_info(%s)" % table)]
    index_rows = con.execute(
        """SELECT name, sql FROM sqlite_master
           WHERE type='index' AND tbl_name=? AND sql IS NOT NULL""", (table,)).fetchall()
    trigger_rows = con.execute(
        """SELECT name, sql FROM sqlite_master
           WHERE type='trigger' AND tbl_name=? AND sql IS NOT NULL""", (table,)).fetchall()
    return (columns,
            [r[1] for r in index_rows],
            [r[1] for r in trigger_rows],
            [r[0] for r in index_rows])


def rebuild_table(con, table, new_ddl, common_columns=None):
    """Transactionally rebuild `table` using `new_ddl`, preserving all rows.

    `new_ddl` must create a table literally named `<table>_new` (the copy
    target). Columns copied are the intersection of source and new columns
    (pass `common_columns` to override).

    Guards (each raises RuntimeError and rolls back on failure):
      - source and destination row counts match
      - every source row value-combination exists in the destination
        (set comparison on the common columns, combined with the row-count
        parity check so duplicate rows cannot be silently lost)
      - PRAGMA foreign_key_check reports no violations
      - every index and trigger that existed on the source is recreated
    """
    src_columns, src_indexes, src_triggers, src_index_names = get_table_info(con, table)
    if not src_columns:
        raise RuntimeError("rebuild_table: unknown source table %r" % table)

    new_table = "%s_new" % table
    if con.in_transaction:
        raise RuntimeError("rebuild_table must not run inside an open transaction")

    src_count = con.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]

    con.execute("PRAGMA foreign_keys = OFF")
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute(new_ddl)
        new_columns = [r[1] for r in con.execute("PRAGMA table_info(%s)" % new_table)]
        missing = [c for c in src_columns if c not in new_columns]
        if missing:
            raise RuntimeError(
                "rebuild_table: new table missing source columns: %s" % missing)

        cols = common_columns or [c for c in src_columns if c in new_columns]
        col_list = ", ".join(cols)

        con.execute(
            "INSERT INTO %s (%s) SELECT %s FROM %s" % (new_table, col_list, col_list, table))

        dst_count = con.execute("SELECT COUNT(*) FROM %s" % new_table).fetchone()[0]
        if dst_count != src_count:
            raise RuntimeError(
                "rebuild_table: row-count mismatch for %s: %s source vs %s copied"
                % (table, src_count, dst_count))

        missing_rows = con.execute(
            "SELECT COUNT(*) FROM (SELECT %s FROM %s EXCEPT SELECT %s FROM %s)"
            % (col_list, table, col_list, new_table)).fetchone()[0]
        if missing_rows:
            raise RuntimeError(
                "rebuild_table: %s source rows not preserved in %s" % (missing_rows, new_table))

        fk_issues = [dict(r) for r in con.execute("PRAGMA foreign_key_check")]
        if fk_issues:
            raise RuntimeError(
                "rebuild_table: foreign-key violations after copy: %s" % fk_issues[:5])

        con.execute("DROP TABLE %s" % table)
        con.execute("ALTER TABLE %s RENAME TO %s" % (new_table, table))

        for sql in src_indexes:
            con.execute(sql)
        for sql in src_triggers:
            con.execute(sql)

        after_indexes = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,))}
        missing_indexes = [n for n in src_index_names if n not in after_indexes]
        if missing_indexes:
            raise RuntimeError(
                "rebuild_table: indexes missing after rebuild: %s" % missing_indexes)

        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.execute("PRAGMA foreign_keys = ON")
