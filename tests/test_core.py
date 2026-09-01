from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SHARED_DIR = PROJECT_ROOT / "shared"
sys.path.insert(0, str(SHARED_DIR))

from fileuniquefy_core import (  # noqa: E402
    PlanStaleError,
    SafetyError,
    ScanOptions,
    _same_filesystem,
    execute_plan,
    find_month_directories,
    scan_directory,
    validate_base_directory,
    validate_output_folder,
)


class CoreAlgorithmTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="FileUniquefy-WeChat-")
        self.base = Path(self.temp.name) / "WeChat Files" / "account" / "FileStorage" / "File"
        self.base.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def month(self, name: str = "2026-01") -> Path:
        path = self.base / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def write(path: Path, content: bytes, mtime_ns: int | None = None) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        if mtime_ns is not None:
            os.utime(path, ns=(mtime_ns, mtime_ns))
        return path

    def test_original_filename_is_kept_even_when_newer(self) -> None:
        month = self.month()
        old = time.time_ns() - 10_000_000_000
        original = self.write(month / "报告.pdf", b"same", time.time_ns())
        duplicate = self.write(month / "报告(1).pdf", b"same", old)
        result = scan_directory(self.base)
        self.assertEqual(1, len(result.move_items))
        self.assertEqual(original, result.move_items[0].keep.path)
        self.assertEqual(duplicate, result.move_items[0].duplicate.path)

    def test_same_size_different_content_is_not_duplicate(self) -> None:
        month = self.month()
        self.write(month / "a.bin", b"abc")
        self.write(month / "b.bin", b"xyz")
        result = scan_directory(self.base)
        self.assertEqual([], result.move_items)

    def test_invalid_month_name_is_ignored(self) -> None:
        self.month("2026-01")
        self.month("2026-13")
        self.month("2026-00")
        self.assertEqual(["2026-01"], [path.name for path in find_month_directories(self.base)])

    def test_subdirectories_are_opt_in(self) -> None:
        month = self.month()
        self.write(month / "nested" / "a.txt", b"same")
        self.write(month / "nested" / "a(1).txt", b"same")
        self.assertEqual(0, len(scan_directory(self.base).move_items))
        recursive = scan_directory(self.base, ScanOptions(recursive=True))
        self.assertEqual(1, len(recursive.move_items))

    def test_cross_month_mode_finds_cross_month_duplicate(self) -> None:
        self.write(self.month("2026-01") / "a.txt", b"same")
        self.write(self.month("2026-02") / "b.txt", b"same")
        self.assertEqual(0, len(scan_directory(self.base).move_items))
        global_result = scan_directory(self.base, ScanOptions(scope="all"))
        self.assertEqual(1, len(global_result.move_items))

    def test_month_statistics_are_complete(self) -> None:
        january = self.month("2026-01")
        february = self.month("2026-02")
        self.write(january / "a.txt", b"same")
        self.write(january / "a(1).txt", b"same")
        self.write(january / "unique.txt", b"unique")
        self.write(february / "only.txt", b"only")
        result = scan_directory(self.base)
        summaries = {summary.month: summary for summary in result.month_summaries}
        self.assertEqual(3, summaries["2026-01"].total_files)
        self.assertEqual(1, summaries["2026-01"].duplicate_files)
        self.assertEqual(4, summaries["2026-01"].duplicate_bytes)
        self.assertEqual(1, summaries["2026-02"].total_files)
        self.assertEqual(0, summaries["2026-02"].duplicate_files)

    def test_destination_collision_never_overwrites(self) -> None:
        month = self.month()
        self.write(month / "a.txt", b"same")
        duplicate = self.write(month / "a(1).txt", b"same")
        existing = self.write(self.base / "重复" / month.name / "a(1).txt", b"do-not-overwrite")
        result = scan_directory(self.base)
        execution = execute_plan(result)
        self.assertEqual([], execution.failures)
        self.assertFalse(duplicate.exists())
        self.assertEqual(b"do-not-overwrite", existing.read_bytes())
        destination = execution.moved[0][1]
        self.assertNotEqual(existing, destination)
        self.assertEqual(b"same", destination.read_bytes())

    def test_destination_created_after_scan_is_not_overwritten(self) -> None:
        month = self.month()
        self.write(month / "a.txt", b"same")
        self.write(month / "a(1).txt", b"same")
        result = scan_directory(self.base)
        planned = result.move_items[0].destination
        self.write(planned, b"appeared-after-scan")
        execution = execute_plan(result)
        self.assertEqual(b"appeared-after-scan", planned.read_bytes())
        self.assertEqual(1, len(execution.moved))
        self.assertNotEqual(planned, execution.moved[0][1])

    def test_scan_is_read_only(self) -> None:
        month = self.month()
        self.write(month / "a.txt", b"same")
        self.write(month / "a(1).txt", b"same")
        result = scan_directory(self.base)
        self.assertEqual(1, len(result.move_items))
        self.assertFalse((self.base / "重复").exists())

    def test_newest_policy_is_respected(self) -> None:
        month = self.month()
        old = time.time_ns() - 10_000_000_000
        newest = self.write(month / "new.txt", b"same", time.time_ns())
        self.write(month / "old.txt", b"same", old)
        result = scan_directory(self.base, ScanOptions(keep_policy="newest"))
        self.assertEqual(newest, result.move_items[0].keep.path)

    def test_minimum_size_filter_is_respected(self) -> None:
        month = self.month()
        self.write(month / "small.txt", b"x")
        self.write(month / "small(1).txt", b"x")
        self.write(month / "large.txt", b"0123456789")
        self.write(month / "large(1).txt", b"0123456789")
        result = scan_directory(self.base, ScanOptions(min_size=10))
        self.assertEqual(1, len(result.move_items))
        self.assertEqual("large(1).txt", result.move_items[0].duplicate.path.name)

    def test_stale_plan_is_rejected_before_move(self) -> None:
        month = self.month()
        self.write(month / "a.txt", b"same")
        duplicate = self.write(month / "a(1).txt", b"same")
        result = scan_directory(self.base)
        duplicate.write_bytes(b"changed")
        execution = execute_plan(result)
        self.assertEqual(0, len(execution.moved))
        self.assertEqual(1, len(execution.failures))
        self.assertIn("重新扫描", execution.failures[0])
        self.assertTrue(duplicate.exists())

    def test_successful_move_writes_journal(self) -> None:
        month = self.month()
        keep = self.write(month / "a.txt", b"same")
        duplicate = self.write(month / "a(1).txt", b"same")
        result = scan_directory(self.base)
        execution = execute_plan(result)
        self.assertTrue(keep.exists())
        self.assertFalse(duplicate.exists())
        self.assertEqual(1, len(execution.moved))
        self.assertIsNotNone(execution.journal_path)
        self.assertTrue(execution.journal_path.exists())
        journal = execution.journal_path.read_text(encoding="utf-8")
        self.assertIn('"event": "moved"', journal)

    def test_external_destination_and_cross_volume_copy_path(self) -> None:
        month = self.month()
        keep = self.write(month / "a.txt", b"same")
        duplicate = self.write(month / "a(1).txt", b"same")
        destination_root = Path(self.temp.name) / "external-destination"
        options = ScanOptions(output_directory=str(destination_root))
        result = scan_directory(self.base, options)
        self.assertTrue(str(result.move_items[0].destination).startswith(str(destination_root)))
        with mock.patch("fileuniquefy_core._same_filesystem", return_value=False):
            execution = execute_plan(result)
        self.assertEqual([], execution.failures)
        self.assertTrue(keep.exists())
        self.assertFalse(duplicate.exists())
        self.assertEqual(b"same", execution.moved[0][1].read_bytes())

    def test_external_destination_must_be_absolute(self) -> None:
        self.month()
        with self.assertRaises(SafetyError):
            scan_directory(self.base, ScanOptions(output_directory="relative-output"))

    def test_real_d_to_e_cross_volume_move(self) -> None:
        if os.name != "nt" or not Path("E:/Down").is_dir():
            self.skipTest("没有可用于跨卷测试的 E:\\Down")
        with tempfile.TemporaryDirectory(prefix="cross-volume-source-", dir=PROJECT_ROOT) as source_temp:
            with tempfile.TemporaryDirectory(prefix="cross-volume-target-", dir=Path("E:/Down")) as target_temp:
                base = Path(source_temp) / "WeChat Files" / "account" / "FileStorage" / "File"
                month = base / "2026-01"
                month.mkdir(parents=True)
                keep = self.write(month / "a.txt", b"cross-volume-content")
                duplicate = self.write(month / "a(1).txt", b"cross-volume-content")
                destination = Path(target_temp)
                self.assertFalse(_same_filesystem(duplicate, destination))
                result = scan_directory(base, ScanOptions(output_directory=str(destination)))
                execution = execute_plan(result)
                self.assertEqual([], execution.failures)
                self.assertTrue(keep.exists())
                self.assertFalse(duplicate.exists())
                self.assertEqual(b"cross-volume-content", execution.moved[0][1].read_bytes())

    def test_post_move_content_change_is_detected_and_rolled_back(self) -> None:
        month = self.month()
        self.write(month / "a.txt", b"same")
        duplicate = self.write(month / "a(1).txt", b"same")
        result = scan_directory(self.base)
        real_replace = os.replace

        def replace_then_corrupt(source, destination):
            real_replace(source, destination)
            destination_path = Path(destination)
            if destination_path.name.startswith("a(1)"):
                destination_path.write_bytes(b"changed-during-move")

        with mock.patch("fileuniquefy_core.os.replace", side_effect=replace_then_corrupt):
            execution = execute_plan(result)
        self.assertEqual([], execution.moved)
        self.assertEqual(1, len(execution.failures))
        self.assertIn("自动回滚", execution.failures[0])
        self.assertTrue(duplicate.exists())

    def test_symlink_file_is_not_followed(self) -> None:
        month = self.month()
        target = self.write(month / "target.txt", b"same")
        link = month / "target(1).txt"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("当前系统不允许创建符号链接")
        result = scan_directory(self.base)
        self.assertEqual([], result.move_items)

    def test_output_folder_validation(self) -> None:
        for invalid in ("", "..", "a/b", "a\\b", "CON", "name."):
            with self.subTest(invalid=invalid), self.assertRaises(SafetyError):
                validate_output_folder(invalid)

    def test_windows_system_directory_is_rejected(self) -> None:
        windows_directory = os.environ.get("WINDIR")
        if not windows_directory:
            self.skipTest("非 Windows 环境")
        with self.assertRaises(SafetyError):
            validate_base_directory(windows_directory)


if __name__ == "__main__":
    unittest.main()
