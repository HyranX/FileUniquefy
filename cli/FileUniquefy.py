"""FileUniquefy 命令行版。"""

from __future__ import annotations

import argparse
import sys
import unicodedata
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SHARED_DIR = PROJECT_ROOT / "shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))

from fileuniquefy_core import (  # noqa: E402
    FileUniquefyError,
    ScanOptions,
    WARN_FILE_COUNT,
    discover_wechat_directory_info,
    execute_plan,
    format_size,
    scan_directory,
    validate_base_directory,
)


POLICY_LABELS = {
    "original_oldest": "优先保留无 (N) 后缀的文件，其次保留最旧文件",
    "oldest": "保留修改时间最早的文件",
    "newest": "保留修改时间最新的文件",
}
DEFAULT_OUTPUT_DIRECTORY = r"E:\Down\weixin"


def display_width(value: object) -> int:
    return sum(2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1 for character in str(value))


def pad_cell(value: object, width: int, align: str = "left") -> str:
    text = str(value)
    padding = " " * max(0, width - display_width(text))
    return padding + text if align == "right" else text + padding


def format_table(headers: list[str], rows: list[list[object]], alignments: list[str]) -> list[str]:
    widths = [display_width(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], display_width(value))
    lines = ["  " + "  ".join(pad_cell(value, widths[index], alignments[index]) for index, value in enumerate(headers))]
    lines.append("  " + "  ".join("-" * width for width in widths))
    for row in rows:
        lines.append("  " + "  ".join(pad_cell(value, widths[index], alignments[index]) for index, value in enumerate(row)))
    return lines


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    answer = input(f"{prompt} {suffix}: ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def select_directory() -> Path:
    print("正在查找常见微信文件目录……")
    candidates = discover_wechat_directory_info()
    if candidates:
        print(f"\n找到 {len(candidates)} 个微信文件目录:\n")
        rows: list[list[object]] = []
        for index, info in enumerate(candidates, 1):
            rows.append([f"[{index:>2}]", info.version, info.user_id, info.month_count, info.total_files, format_size(info.total_size), info.path])
        for line in format_table(
            ["编号", "版本", "用户 ID", "月份", "文件", "总大小", "路径"],
            rows,
            ["left", "left", "left", "right", "right", "right", "left"],
        ):
            print(line)
        print("  [0] 手动输入目录")
        while True:
            answer = input("请选择目录编号: ").strip()
            if answer == "0":
                break
            try:
                selected = int(answer)
            except ValueError:
                selected = -1
            if 1 <= selected <= len(candidates):
                return candidates[selected - 1].path
            print("输入无效，请重新选择。")
    else:
        print("未自动发现微信文件目录。")
    return Path(input("请输入包含 YYYY-MM 文件夹的目录: ").strip().strip('"'))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="安全扫描并移动微信月份目录内的重复文件")
    parser.add_argument("directory", nargs="?", help="包含 YYYY-MM 子目录的微信文件目录")
    parser.add_argument("--scope", choices=("month", "all"), default="month", help="month=仅月内去重，all=跨月份去重")
    parser.add_argument("--recursive", action="store_true", help="递归扫描月份目录下的子文件夹")
    parser.add_argument("--min-size", type=int, default=0, metavar="BYTES", help="忽略小于该字节数的文件")
    parser.add_argument("--keep", choices=("original_oldest", "oldest", "newest"), default="original_oldest", help="重复组保留策略")
    parser.add_argument("--hash", choices=("sha256", "blake2b"), default="sha256", dest="hash_algorithm", help="内容哈希算法")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIRECTORY, help=f"移动目的地（默认: {DEFAULT_OUTPUT_DIRECTORY}）")
    parser.add_argument("--dry-run", action="store_true", help="只生成计划，绝不移动文件")
    parser.add_argument("--yes", action="store_true", help="确认警告并执行计划，适合自动化调用")
    return parser


def print_summary(result) -> None:
    print("\n扫描结果")
    print(f"  操作目录: {result.base_directory}")
    print(f"  移动目的地: {result.options.output_directory or result.options.output_folder}")
    print()
    rows: list[list[object]] = []
    for summary in result.month_summaries:
        duplicate_label = str(summary.duplicate_files) if summary.duplicate_files else "-"
        duplicate_size = format_size(summary.duplicate_bytes) if summary.duplicate_bytes else "-"
        rows.append([summary.month, summary.total_files, summary.eligible_files, duplicate_label, duplicate_size])
    rows.append(["合计", result.total_files, result.eligible_files, len(result.move_items), format_size(result.duplicate_bytes)])
    for line in format_table(
        ["月份", "文件数", "参与比较", "重复文件", "重复大小"],
        rows,
        ["left", "right", "right", "right", "right"],
    ):
        print(line)
    print(f"\n  实际哈希文件数: {result.hashed_files}")
    print(f"  保留策略: {POLICY_LABELS[result.options.keep_policy]}")
    for warning in result.warnings:
        print(f"  [警告] {warning}")
    for error in result.errors:
        print(f"  [跳过] {error}")



def print_detailed_plan(result) -> None:
    print("\n移动计划")
    for item in result.move_items:
        print(f"  [保留] {item.keep.path}")
        print(f"  [移动] {item.duplicate.path}")
        print(f"      -> {item.destination}")


def process_one_directory(args, directory: Path) -> int:
    options = ScanOptions(
        scope=args.scope,
        recursive=args.recursive,
        min_size=args.min_size,
        keep_policy=args.keep,
        hash_algorithm=args.hash_algorithm,
        output_directory=args.output_dir,
    )
    try:
        _, warnings = validate_base_directory(directory)
        if warnings and not args.yes:
            for warning in warnings:
                print(f"[警告] {warning}")
            if not ask_yes_no("仍要扫描此目录吗？"):
                print("已取消，未读取或移动文件。")
                return 0

        print("正在进行大小分组和内容哈希，请稍候……")
        result = scan_directory(directory, options)
        print_summary(result)
        if not result.move_items:
            print("\n没有发现需要移动的重复文件。")
            return 0
        if not args.yes and not args.dry_run and not ask_yes_no("是否显示详细移动计划？"):
            print("已取消，未移动任何文件。")
            return 0
        print_detailed_plan(result)
        if args.dry_run:
            print("\n仅预览模式：未移动任何文件。")
            return 0
        if len(result.move_items) > WARN_FILE_COUNT:
            print(f"\n[高风险警告] 一次将移动 {len(result.move_items)} 个文件。")
        if not args.yes and not ask_yes_no("执行上述移动计划吗？"):
            print("已取消，未移动任何文件。")
            return 0

        print("\n正在逐项复核并移动……")
        execution = execute_plan(result)
        print(f"已移动 {len(execution.moved)} 个文件。")
        if execution.journal_path:
            print(f"操作日志: {execution.journal_path}")
        if execution.failures:
            print("以下项目未完成：")
            for failure in execution.failures:
                print(f"  {failure}")
            return 2
        return 0
    except (FileUniquefyError, OSError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.directory:
        return process_one_directory(args, Path(args.directory))

    while True:
        exit_code = process_one_directory(args, select_directory())
        if exit_code != 0:
            return exit_code
        print()
        if not ask_yes_no("返回主界面选择其他目录？"):
            print("退出程序。")
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
