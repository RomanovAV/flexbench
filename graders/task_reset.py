import json
from pathlib import Path
import subprocess
import sys
import tempfile

HARNESS = r'''
import java.util.*;
public class CheckReset {
 static TaskResetService.Task task(TaskResetService.Status s) {
  return new TaskResetService.Task("t", "owner", s, 3, "error", 7, null, "payload");
 }
 static TaskResetService.ResetCommand cmd(String actor, boolean admin, long v, String id) {
  return new TaskResetService.ResetCommand(actor, admin, v, id);
 }
 static boolean raises(Class<? extends Throwable> type, Runnable action) {
  try { action.run(); return false; } catch (Throwable e) { return type.isInstance(e); }
 }
 static void check(String id, java.util.function.BooleanSupplier test) {
  boolean ok; try { ok = test.getAsBoolean(); } catch (Throwable e) { ok = false; }
  System.out.println(id + "=" + ok);
 }
 public static void main(String[] args) {
  check("reset_fields", () -> {
   for (var s : List.of(TaskResetService.Status.FAILED, TaskResetService.Status.CANCELLED)) {
    var t = task(s); var n = TaskResetService.reset(t, cmd("owner", false, 7, "r"));
    if (n == t || n.status() != TaskResetService.Status.NEW || n.attempt() != 0 || n.error() != null
      || n.version() != 8 || !"r".equals(n.lastResetId())) return false;
   } return true;
  });
  check("preserve_payload", () -> {
   var t = task(TaskResetService.Status.FAILED); var n = TaskResetService.reset(t, cmd("owner", false, 7, "r"));
   return n.id().equals(t.id()) && n.owner().equals(t.owner()) && n.payload().equals(t.payload())
    && t.version() == 7 && t.attempt() == 3 && t.error().equals("error");
  });
  check("admin_allowed", () -> TaskResetService.reset(task(TaskResetService.Status.FAILED), cmd("admin", true, 7, "r")).version() == 8);
  check("unauthorized", () -> raises(SecurityException.class, () -> TaskResetService.reset(task(TaskResetService.Status.FAILED), cmd("other", false, 7, "r"))));
  check("optimistic_version", () -> List.of(-1L, 6L, 8L).stream().allMatch(v -> raises(IllegalStateException.class,
    () -> TaskResetService.reset(task(TaskResetService.Status.FAILED), cmd("owner", false, v, "r")))));
  check("terminal_states", () -> List.of(TaskResetService.Status.NEW, TaskResetService.Status.RUNNING, TaskResetService.Status.DONE).stream()
    .allMatch(s -> raises(IllegalStateException.class, () -> TaskResetService.reset(task(s), cmd("owner", false, 7, "r")))));
  check("idempotent_retry", () -> {
   var n = TaskResetService.reset(task(TaskResetService.Status.FAILED), cmd("owner", false, 7, "r"));
   return TaskResetService.reset(n, cmd("owner", false, -99, "r")) == n;
  });
  check("permission_before_retry", () -> {
   var n = TaskResetService.reset(task(TaskResetService.Status.FAILED), cmd("owner", false, 7, "r"));
   return raises(SecurityException.class, () -> TaskResetService.reset(n, cmd("other", false, -99, "r")));
  });
  check("new_request_checks_state", () -> {
   var n = TaskResetService.reset(task(TaskResetService.Status.FAILED), cmd("owner", false, 7, "r"));
   return raises(IllegalStateException.class, () -> TaskResetService.reset(n, cmd("owner", false, 8, "new")));
  });
  check("null_arguments", () -> raises(IllegalArgumentException.class, () -> TaskResetService.reset(null, cmd("owner", false, 7, "r")))
    && raises(IllegalArgumentException.class, () -> TaskResetService.reset(task(TaskResetService.Status.FAILED), null)));
  check("invalid_command", () -> Arrays.asList(null, "", "  ").stream().allMatch(s ->
    raises(IllegalArgumentException.class, () -> TaskResetService.reset(task(TaskResetService.Status.FAILED), cmd(s, true, 7, "r")))
    && raises(IllegalArgumentException.class, () -> TaskResetService.reset(task(TaskResetService.Status.FAILED), cmd("owner", false, 7, s)))));
  check("invalid_task", () -> {
   var tasks = Arrays.asList(
    new TaskResetService.Task(null,"owner",TaskResetService.Status.FAILED,0,null,7,null,null),
    new TaskResetService.Task(" ","owner",TaskResetService.Status.FAILED,0,null,7,null,null),
    new TaskResetService.Task("t",null,TaskResetService.Status.FAILED,0,null,7,null,null),
    new TaskResetService.Task("t"," ",TaskResetService.Status.FAILED,0,null,7,null,null),
    new TaskResetService.Task("t","owner",null,0,null,7,null,null),
    new TaskResetService.Task("t","owner",TaskResetService.Status.FAILED,0,null,-1,null,null));
   return tasks.stream().allMatch(t -> raises(IllegalArgumentException.class, () -> TaskResetService.reset(t, cmd("owner",true,7,"r"))));
  });
  check("validation_before_retry", () -> {
   var t = new TaskResetService.Task("t","owner",TaskResetService.Status.NEW,0,null,-1,"r",null);
   return raises(IllegalArgumentException.class, () -> TaskResetService.reset(t, cmd("owner",false,-1,"r")));
  });
  check("version_overflow", () -> {
   var t = new TaskResetService.Task("t","owner",TaskResetService.Status.FAILED,0,null,Long.MAX_VALUE,null,null);
   return raises(IllegalStateException.class, () -> TaskResetService.reset(t, cmd("owner",false,Long.MAX_VALUE,"r")));
  });
  check("literal_ids_and_no_global_state", () -> {
   var t = new TaskResetService.Task("t","owner ",TaskResetService.Status.FAILED,0,null,7,null,null);
   if (!raises(SecurityException.class, () -> TaskResetService.reset(t, cmd("owner",false,7,"r")))) return false;
   var n = TaskResetService.reset(t, cmd("owner ",false,7,"r"));
   var again = TaskResetService.reset(t, cmd("owner ",false,7,"r"));
   return n.payload() == null && again.version() == 8;
  });
 }
}
'''
IDS = ["reset_fields", "preserve_payload", "admin_allowed", "unauthorized", "optimistic_version",
       "terminal_states", "idempotent_retry", "permission_before_retry", "new_request_checks_state",
       "null_arguments", "invalid_command", "invalid_task", "validation_before_retry", "version_overflow",
       "literal_ids_and_no_global_state"]


def main():
    workspace = Path(sys.argv[1])
    with tempfile.TemporaryDirectory() as directory:
        harness = Path(directory) / "CheckReset.java"
        harness.write_text(HARNESS, encoding="utf-8")
        compiled = subprocess.run(["javac", "--release", "17", "-encoding", "UTF-8", "-d", directory,
                                   *map(str, (workspace / "src").glob("*.java")), str(harness)], capture_output=True, text=True)
        checks = [{"id": "compiles", "passed": compiled.returncode == 0}]
        if compiled.returncode:
            print(json.dumps({"checks": checks, "detail": compiled.stderr[-4000:]}))
            return
        result = subprocess.run(["java", "-cp", directory, "CheckReset"], capture_output=True, text=True)
        lines = set(result.stdout.splitlines())
        checks += [{"id": key, "passed": result.returncode == 0 and key + "=true" in lines} for key in IDS]
        print(json.dumps({"checks": checks, "detail": result.stderr[-4000:]}))


if __name__ == "__main__":
    main()
