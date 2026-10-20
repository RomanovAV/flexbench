import pathlib
import subprocess
import tempfile

SOURCE = '''import java.time.*; import java.util.*;
public class Smoke {
 public static void main(String[] args) {
  var now = Instant.parse("2026-01-01T00:00:00Z");
  var candidates = List.of(new RecommendationSelector.Candidate("a", "T", "S", now.minusSeconds(1), 10, 1));
  var rules = List.of(new RecommendationSelector.Rule("T", "S", 1, false));
  if (!RecommendationSelector.select(candidates, rules, now, 5).isEmpty())
    throw new AssertionError("Past candidate without carryOver");
 }
}'''
root = pathlib.Path(__file__).resolve().parent
with tempfile.TemporaryDirectory() as directory:
    harness = pathlib.Path(directory) / "Smoke.java"
    harness.write_text(SOURCE)
    subprocess.run(["javac", "--release", "17", "-d", directory, *map(str, (root / "src").glob("*.java")), str(harness)], check=True)
    subprocess.run(["java", "-cp", directory, "Smoke"], check=True)
print("Smoke passed")
