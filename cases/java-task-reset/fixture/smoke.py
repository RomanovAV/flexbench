import pathlib
import subprocess
import tempfile

SOURCE = '''public class Smoke {
 public static void main(String[] args) {
  var t = new TaskResetService.Task("t", "owner", TaskResetService.Status.FAILED, 3, "bad", 7, null, "data");
  var c = new TaskResetService.ResetCommand("owner", false, 7, "r");
  var n = TaskResetService.reset(t, c);
  if (!"data".equals(n.payload()) || n.version() != 8 || TaskResetService.reset(n, c) != n)
    throw new AssertionError("Preserve payload and support retry");
 }
}'''
root = pathlib.Path(__file__).resolve().parent
with tempfile.TemporaryDirectory() as directory:
    harness = pathlib.Path(directory) / "Smoke.java"
    harness.write_text(SOURCE)
    subprocess.run(["javac", "--release", "17", "-d", directory, *map(str, (root / "src").glob("*.java")), str(harness)], check=True)
    subprocess.run(["java", "-cp", directory, "Smoke"], check=True)
print("Smoke passed")
