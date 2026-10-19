import json
from pathlib import Path
import subprocess
import sys
import tempfile

HARNESS = r'''
import java.time.Instant;
import java.util.*;
public class CheckActivity {
  static Instant now = Instant.parse("2026-01-01T12:00:00Z");
  static ActivityService.Activity a(String id, String owner, String status, Instant time) {
    return new ActivityService.Activity(id, owner, status, time);
  }
  static void check(String id, java.util.function.BooleanSupplier test) {
    try { System.out.println(id + "=" + test.getAsBoolean()); }
    catch (Throwable e) { System.out.println(id + "=false"); }
  }
  static String selected(List<ActivityService.Activity> xs) {
    return ActivityService.nearestPlanned(xs, "owner", now).map(ActivityService.Activity::id).orElse("EMPTY");
  }
  public static void main(String[] args) {
    check("owner_status_future", () -> selected(List.of(a("other", "x", "PLANNED", now),
      a("done", "owner", "DONE", now), a("past", "owner", "PLANNED", now.minusSeconds(1)),
      a("valid", "owner", "PLANNED", now.plusSeconds(5)))).equals("valid"));
    check("nearest", () -> selected(List.of(a("late", "owner", "PLANNED", now.plusSeconds(9)),
      a("early", "owner", "PLANNED", now.plusSeconds(1)))).equals("early"));
    check("inclusive_now", () -> selected(List.of(a("future", "owner", "PLANNED", now.plusSeconds(1)),
      a("exact", "owner", "PLANNED", now))).equals("exact"));
    check("stable_tie", () -> selected(List.of(a("z", "owner", "PLANNED", now),
      a("a", "owner", "PLANNED", now))).equals("a"));
    check("null_entries", () -> selected(Arrays.asList(null, a("missing", "owner", "PLANNED", null),
      a("ok", "owner", "PLANNED", now))).equals("ok"));
    check("null_list", () -> selected(null).equals("EMPTY"));
    check("empty_no_matches", () -> selected(List.of()).equals("EMPTY") && selected(List.of(
      a("x", "owner", "CANCELLED", now))).equals("EMPTY"));
    check("no_mutation", () -> {
      var items = new ArrayList<>(List.of(a("z", "owner", "PLANNED", now.plusSeconds(4)),
        a("a", "owner", "PLANNED", now)));
      var before = new ArrayList<>(items); selected(items); return items.equals(before);
    });
  }
}
'''
IDS = ["owner_status_future", "nearest", "inclusive_now", "stable_tie", "null_entries",
       "null_list", "empty_no_matches", "no_mutation"]


def main():
    workspace = Path(sys.argv[1])
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        harness = root / "CheckActivity.java"
        harness.write_text(HARNESS)
        compiled = subprocess.run(["javac", "-d", directory, *map(str, (workspace / "src").glob("*.java")),
                                   str(harness)], capture_output=True, text=True)
        checks = [{"id": "compiles", "passed": compiled.returncode == 0}]
        if compiled.returncode:
            print(json.dumps({"checks": checks, "detail": compiled.stderr[-4000:]}))
            return
        result = subprocess.run(["java", "-cp", directory, "CheckActivity"], capture_output=True, text=True)
        lines = set(result.stdout.splitlines())
        checks += [{"id": key, "passed": result.returncode == 0 and key + "=true" in lines} for key in IDS]
        print(json.dumps({"checks": checks, "detail": result.stderr[-4000:]}))


if __name__ == "__main__":
    main()
