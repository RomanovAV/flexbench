import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "cases/sql-activity-indexes/fixture"
NEXT = "SELECT id, scheduled_at FROM activities WHERE owner_id=? AND status='PLANNED' AND scheduled_at>=? ORDER BY scheduled_at, id LIMIT ?"
WINDOW = "SELECT id FROM activities WHERE owner_id=? AND status=? AND scheduled_at>=? AND scheduled_at<? ORDER BY scheduled_at, id LIMIT ?"


def main():
    workspace = Path(sys.argv[1])
    checks = []

    def check(key, passed):
        checks.append({"id": key, "passed": bool(passed)})

    check("preserve_query_and_schema", all((workspace / name).is_file() and (workspace / name).read_bytes() == (FIXTURE / name).read_bytes()
                                          for name in ("schema.sql", "queries.py")))
    with sqlite3.connect(":memory:") as connection:
        connection.executescript((FIXTURE / "schema.sql").read_text())
        rows = [(i, "owner-" + str(i % 19), ("PLANNED", "DONE", "CANCELLED")[i % 3], i % 113,
                 "external-" + str(i % 7)) for i in range(1, 5001)]
        connection.executemany("INSERT INTO activities VALUES(?,?,?,?,?)", rows)
        connection.commit()
        before_info = connection.execute("PRAGMA table_info(activities)").fetchall()
        original_index = connection.execute("SELECT sql FROM sqlite_master WHERE name='idx_activities_external_id'").fetchone()
        samples = [(NEXT, (owner, lower, limit)) for owner in ("owner-0", "owner-7", "missing")
                   for lower in (-1, 50, 113) for limit in (0, 1, 8)]
        samples += [(WINDOW, (owner, status, lower, upper, 8)) for owner in ("owner-1", "owner-8", "missing")
                    for status in ("PLANNED", "DONE", "CANCELLED") for lower, upper in ((0, 113), (50, 51), (80, 20))]
        expected = [connection.execute(query, args).fetchall() for query, args in samples]
        try:
            sql = (workspace / "migration.sql").read_text()
            connection.executescript(sql)
        except (OSError, sqlite3.Error, UnicodeError) as error:
            check("migration_applies", False)
            print(json.dumps({"checks": checks, "detail": str(error)[-2000:]}))
            return
        check("migration_applies", True)
        first_schema = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name").fetchall()
        try:
            connection.executescript(sql)
            check("idempotent", connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY name").fetchall() == first_schema)
        except sqlite3.Error:
            check("idempotent", False)
        check("data_preserved", connection.execute("SELECT * FROM activities ORDER BY id").fetchall() == rows)
        check("columns_preserved", connection.execute("PRAGMA table_info(activities)").fetchall() == before_info)
        check("existing_index_preserved", connection.execute("SELECT sql FROM sqlite_master WHERE name='idx_activities_external_id'").fetchone() == original_index)
        objects = connection.execute("SELECT type,name,tbl_name FROM sqlite_master").fetchall()
        check("only_activity_indexes_added", all((kind == "table" and name == "activities") or
                                                (kind == "index" and table == "activities") for kind, name, table in objects)
              and all(row[2] == 0 for row in connection.execute("PRAGMA index_list(activities)")))
        check("query_results_preserved", [connection.execute(query, args).fetchall() for query, args in samples] == expected)
        connection.execute("PRAGMA automatic_index=OFF")
        plans = []
        for name, query, args in (("next_plan", NEXT, ("owner-0", 10, 5)), ("window_plan", WINDOW, ("owner-1", "DONE", 10, 90, 5))):
            details = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + query, args)]
            plans += details
            compact = " ".join(details).replace(" ", "")
            check(name, any("SEARCH activities" in d and "INDEX" in d for d in details)
                  and "owner_id=?" in compact and "status=?" in compact and "scheduled_at>?" in compact
                  and (name != "window_plan" or "scheduled_at<?" in compact)
                  and not any("SCAN activities" in d or "TEMP B-TREE" in d for d in details))
        try:
            connection.execute("INSERT INTO activities VALUES(6001,'new','PLANNED',50,'external-1')")
            connection.execute("INSERT INTO activities VALUES(6002,'new','PLANNED',50,'external-1')")
            check("writes_and_duplicates_preserved", connection.execute(NEXT, ("new", 50, 5)).fetchall() == [(6001, 50), (6002, 50)])
        except sqlite3.Error:
            check("writes_and_duplicates_preserved", False)
        print(json.dumps({"checks": checks, "detail": {"sqlite_version": sqlite3.sqlite_version, "plans": plans}}))


if __name__ == "__main__":
    try:
        main()
    except sqlite3.Error as error:
        print(json.dumps({"checks": [{"id": "schema_usable", "passed": False}], "detail": str(error)[-2000:]}))
