import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "inventory_repositories.py"


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "projects"
        self.repo = self.root / "example"
        self.repo.mkdir(parents=True)
        self.git("init")
        (self.repo / "pyproject.toml").write_text("[project]\nname = 'example'\n")
        (self.repo / "app.py").write_text("print('fixture')\n")
        self.git("add", "pyproject.toml", "app.py")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                 "commit", "-m", "Add fixture")
        self.output = self.base / "inventory.json"

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args],
                              check=True, capture_output=True, text=True)

    def run_inventory(self, *extra):
        return subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root),
                               "--output", str(self.output), *extra],
                              capture_output=True, text=True)

    def read_report(self):
        return json.loads(self.output.read_text())

    def test_metadata_and_read_only_behavior(self):
        head = self.git("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(self.run_inventory().returncode, 0)
        record, = self.read_report()["repositories"]
        self.assertEqual(record["head"], head)
        self.assertFalse(record["dirty"])
        self.assertEqual(record["tracked_file_count"], 2)
        self.assertEqual(record["top_extensions"][".py"], 1)
        self.assertIn("pyproject.toml", record["inspection_candidates"])
        self.assertEqual(record["recent_commits"][0]["subject"], "Add fixture")
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_missing_root_keeps_available_results(self):
        result = self.run_inventory("--root", str(self.base / "missing"))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(len(self.read_report()["repositories"]), 1)
        self.assertTrue(self.read_report()["warnings"])

    def test_existing_output_is_preserved(self):
        self.output.write_text("existing report")
        self.assertEqual(self.run_inventory().returncode, 2)
        self.assertEqual(self.output.read_text(), "existing report")

    def test_duplicate_roots_and_symlinks_do_not_duplicate_repos(self):
        (self.root / "alias").symlink_to(self.repo, target_is_directory=True)
        self.assertEqual(self.run_inventory("--root", str(self.root), "--commits", "0").returncode, 0)
        record, = self.read_report()["repositories"]
        self.assertEqual(record["recent_commits"], [])

    def test_depth_limit_reports_empty_scan(self):
        self.assertEqual(self.run_inventory("--depth", "0").returncode, 2)
        self.assertEqual(self.read_report()["repositories"], [])

    def test_git_worktree_is_discovered(self):
        worktree = self.root / "worktree"
        self.git("-c", "core.hooksPath=/dev/null", "worktree", "add", "--detach", str(worktree), "HEAD")
        self.assertEqual(self.run_inventory().returncode, 0)
        records = self.read_report()["repositories"]
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["head"], records[1]["head"])

    def test_dirty_and_unborn_repositories(self):
        (self.repo / "app.py").write_text("print('changed')\n")
        empty = self.root / "empty"
        empty.mkdir()
        subprocess.run(["git", "-C", str(empty), "init"], check=True, capture_output=True)
        self.assertEqual(self.run_inventory().returncode, 2)
        records = {Path(r["path"]).name: r for r in self.read_report()["repositories"]}
        self.assertTrue(records["example"]["dirty"])
        self.assertIsNone(records["empty"]["head"])
        self.assertIn("head", records["empty"]["errors"])


if __name__ == "__main__":
    unittest.main()
