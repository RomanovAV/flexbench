from pathlib import Path
import sqlite3
from queries import NEXT

root = Path(__file__).resolve().parent
connection = sqlite3.connect(":memory:")
connection.executescript((root / "schema.sql").read_text())
connection.executescript((root / "migration.sql").read_text())
connection.execute("PRAGMA automatic_index=OFF")
plan = ' '.join(row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + NEXT, ('owner', 100, 10)))
print(plan)
assert 'SEARCH' in plan and 'owner_id=?' in plan and 'TEMP B-TREE' not in plan
print("Smoke passed")
