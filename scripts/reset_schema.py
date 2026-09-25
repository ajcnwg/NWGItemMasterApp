"""
Wipes every table in the database (drops FKs first, then all tables), then
rebuilds the complete current schema by calling migrations/001_create_schema.py.

Destructive — this is for standing up a database from nothing (or nuking a
scratch/dev database to start over), never for a database with real data
you want to keep. Safe to re-run: DROP IF EXISTS throughout, and
001_create_schema.py's own statements are all idempotent.
"""

import importlib.util
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from itemmaster.db import get_engine

_spec = importlib.util.spec_from_file_location(
    "create_schema", Path(__file__).resolve().parent.parent / "migrations" / "001_create_schema.py"
)
_create_schema = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_create_schema)

DROP_ALL_FKS_SQL = """
DECLARE @sql NVARCHAR(MAX) = N'';
SELECT @sql += 'ALTER TABLE ' + QUOTENAME(s.name) + '.' + QUOTENAME(t.name) +
    ' DROP CONSTRAINT ' + QUOTENAME(fk.name) + ';' + CHAR(10)
FROM sys.foreign_keys fk
JOIN sys.tables t ON fk.parent_object_id = t.object_id
JOIN sys.schemas s ON t.schema_id = s.schema_id;
EXEC sp_executesql @sql;
"""

DROP_ALL_TABLES_SQL = """
DECLARE @sql NVARCHAR(MAX) = N'';
SELECT @sql += 'DROP TABLE ' + QUOTENAME(s.name) + '.' + QUOTENAME(t.name) + ';' + CHAR(10)
FROM sys.tables t
JOIN sys.schemas s ON t.schema_id = s.schema_id;
EXEC sp_executesql @sql;
"""

def main():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(text(DROP_ALL_FKS_SQL))
        conn.execute(text(DROP_ALL_TABLES_SQL))
        print("Dropped all existing tables and foreign keys.")

    _create_schema.main()


if __name__ == "__main__":
    main()
