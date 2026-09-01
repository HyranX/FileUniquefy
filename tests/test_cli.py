from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI_DIR = PROJECT_ROOT / "cli"
sys.path.insert(0, str(CLI_DIR))

import FileUniquefy  # noqa: E402


class CliFeatureTests(unittest.TestCase):
    def test_chinese_table_uses_terminal_display_width(self) -> None:
        lines = FileUniquefy.format_table(
            ["编号", "版本", "文件"],
            [["[ 1]", "旧版微信", 2], ["[ 2]", "新版微信", 1820]],
            ["left", "left", "right"],
        )
        widths = [FileUniquefy.display_width(line) for line in lines]
        self.assertEqual(1, len(set(widths)))

    def test_dry_run_prints_month_statistics_and_detailed_plan(self) -> None:
        with tempfile.TemporaryDirectory(prefix="FileUniquefy-WeChat-") as temporary:
            base = Path(temporary) / "WeChat Files" / "account" / "FileStorage" / "File"
            january = base / "2026-01"
            february = base / "2026-02"
            january.mkdir(parents=True)
            february.mkdir()
            (january / "a.txt").write_bytes(b"same")
            (january / "a(1).txt").write_bytes(b"same")
            (february / "unique.txt").write_bytes(b"unique")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = FileUniquefy.main([str(base), "--dry-run", "--output-dir", str(Path(temporary) / "destination")])
            rendered = output.getvalue()
            self.assertEqual(0, exit_code)
            self.assertIn("月份", rendered)
            self.assertIn("2026-01", rendered)
            self.assertIn("2026-02", rendered)
            self.assertIn("移动计划", rendered)
            self.assertFalse((base / "重复").exists())


if __name__ == "__main__":
    unittest.main()
