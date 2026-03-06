#!/usr/bin/env python3
"""
微信文件去重工具

自动扫描微信 FileStorage/File 目录下各月份文件夹中的重复文件，
生成报告供用户确认后，将重复文件按月份目录移动到下载文件夹中。
"""

import hashlib
import os
import sys
import platform
from collections import defaultdict
from datetime import datetime
from shutil import move


def get_default_wechat_dir():
    """根据操作系统返回微信文件存储的默认父目录。"""
    system = platform.system()
    if system == "Windows":
        return os.path.join(os.environ.get("USERPROFILE", ""), "Documents", "WeChat Files")
    elif system == "Darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Containers",
                            "com.tencent.xinWeChat", "Data", "Library",
                            "Application Support", "com.tencent.xinWeChat")
    else:
        return os.path.join(os.path.expanduser("~"), "WeChat Files")


def get_default_download_dir():
    """返回系统默认下载目录。"""
    system = platform.system()
    if system == "Windows":
        return os.path.join(os.environ.get("USERPROFILE", ""), "Downloads")
    else:
        return os.path.join(os.path.expanduser("~"), "Downloads")


def calculate_md5(filepath):
    """计算文件的 MD5 哈希值。"""
    hash_md5 = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


def find_month_dirs(base_dir):
    """
    在 base_dir 下查找所有月份目录（格式: YYYY-MM）。
    支持在 FileStorage/File 下直接查找，也支持递归查找微信账号目录。
    """
    month_dirs = []

    # 尝试直接作为 FileStorage/File 目录
    file_storage = os.path.join(base_dir, "FileStorage", "File")
    if os.path.isdir(file_storage):
        _collect_month_dirs(file_storage, month_dirs)
    else:
        # 可能 base_dir 是 WeChat Files 目录，遍历各账号
        if os.path.isdir(base_dir):
            for entry in os.listdir(base_dir):
                account_file_storage = os.path.join(base_dir, entry, "FileStorage", "File")
                if os.path.isdir(account_file_storage):
                    _collect_month_dirs(account_file_storage, month_dirs)

    # 如果以上都没找到，把 base_dir 本身也检查一下是否包含月份目录
    if not month_dirs:
        _collect_month_dirs(base_dir, month_dirs)

    return sorted(month_dirs)


def _collect_month_dirs(parent_dir, result_list):
    """收集 parent_dir 下所有形如 YYYY-MM 的子目录。"""
    for entry in os.listdir(parent_dir):
        full_path = os.path.join(parent_dir, entry)
        if os.path.isdir(full_path) and _is_month_dir(entry):
            result_list.append(full_path)


def _is_month_dir(name):
    """检查目录名是否符合 YYYY-MM 格式。"""
    try:
        datetime.strptime(name, "%Y-%m")
        return True
    except ValueError:
        return False


def scan_duplicates(month_dirs):
    """
    扫描所有月份目录，查找重复文件。

    返回:
        duplicates: dict, key=md5, value=文件路径列表（长度 >= 2）
        stats: dict, 统计信息
    """
    md5_map = defaultdict(list)
    total_files = 0
    total_size = 0
    errors = []

    for month_dir in month_dirs:
        month_name = os.path.basename(month_dir)
        for filename in os.listdir(month_dir):
            filepath = os.path.join(month_dir, filename)
            if not os.path.isfile(filepath):
                continue
            total_files += 1
            try:
                file_size = os.path.getsize(filepath)
                total_size += file_size
                md5 = calculate_md5(filepath)
                md5_map[md5].append({
                    "path": filepath,
                    "month": month_name,
                    "size": file_size,
                    "mtime": os.path.getmtime(filepath),
                    "filename": filename,
                })
            except (OSError, PermissionError) as e:
                errors.append(f"  无法读取: {filepath} ({e})")

    # 只保留有重复的组
    duplicates = {k: v for k, v in md5_map.items() if len(v) > 1}

    stats = {
        "total_files": total_files,
        "total_size": total_size,
        "duplicate_groups": len(duplicates),
        "duplicate_files": sum(len(v) - 1 for v in duplicates.values()),
        "duplicate_size": sum(
            sum(f["size"] for f in files[1:])
            for files in (sorted(v, key=lambda x: x["mtime"], reverse=True) for v in duplicates.values())
        ),
        "errors": errors,
    }
    return duplicates, stats


def format_size(size_bytes):
    """将字节数格式化为人类可读的大小。"""
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def generate_report(duplicates, stats, download_dir):
    """
    生成重复文件报告。

    返回:
        report: str, 报告文本
        move_plan: list of (src, dst), 计划移动的文件
    """
    lines = []
    lines.append("=" * 60)
    lines.append("        微信文件去重报告")
    lines.append("=" * 60)
    lines.append(f"  扫描时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"  扫描文件总数: {stats['total_files']}")
    lines.append(f"  文件总大小: {format_size(stats['total_size'])}")
    lines.append(f"  重复文件组数: {stats['duplicate_groups']}")
    lines.append(f"  重复文件数量: {stats['duplicate_files']}")
    lines.append(f"  重复文件占用空间: {format_size(stats['duplicate_size'])}")
    lines.append(f"  目标移动目录: {download_dir}")
    lines.append("=" * 60)

    if stats["errors"]:
        lines.append(f"\n[警告] 有 {len(stats['errors'])} 个文件无法读取:")
        for err in stats["errors"]:
            lines.append(err)

    if not duplicates:
        lines.append("\n未发现重复文件，无需操作。")
        return "\n".join(lines), []

    move_plan = []
    group_num = 0

    for md5, files in duplicates.items():
        group_num += 1
        # 按修改时间降序排列，保留最新的
        files_sorted = sorted(files, key=lambda x: x["mtime"], reverse=True)
        keep = files_sorted[0]
        to_move = files_sorted[1:]

        lines.append(f"\n--- 重复组 #{group_num} (MD5: {md5[:12]}...) ---")
        lines.append(f"  [保留] {keep['path']}")
        lines.append(f"         大小: {format_size(keep['size'])}  "
                      f"修改时间: {datetime.fromtimestamp(keep['mtime']).strftime('%Y-%m-%d %H:%M:%S')}")

        for f in to_move:
            dst_dir = os.path.join(download_dir, "微信重复文件", f["month"])
            dst_path = os.path.join(dst_dir, f["filename"])
            # 处理目标文件名冲突
            if os.path.exists(dst_path) or any(d == dst_path for _, d in move_plan):
                base, ext = os.path.splitext(f["filename"])
                dst_path = os.path.join(dst_dir, f"{base}_{md5[:8]}{ext}")

            move_plan.append((f["path"], dst_path))
            lines.append(f"  [移动] {f['path']}")
            lines.append(f"     --> {dst_path}")
            lines.append(f"         大小: {format_size(f['size'])}  "
                          f"修改时间: {datetime.fromtimestamp(f['mtime']).strftime('%Y-%m-%d %H:%M:%S')}")

    lines.append("\n" + "=" * 60)
    lines.append(f"  合计将移动 {len(move_plan)} 个重复文件")
    lines.append(f"  可释放空间: {format_size(stats['duplicate_size'])}")
    lines.append("=" * 60)

    return "\n".join(lines), move_plan


def execute_move(move_plan):
    """执行文件移动操作。"""
    success = 0
    failed = 0
    for src, dst in move_plan:
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            move(src, dst)
            print(f"  已移动: {os.path.basename(src)}")
            success += 1
        except (OSError, PermissionError) as e:
            print(f"  失败: {src} -> {e}")
            failed += 1

    print(f"\n移动完成: 成功 {success} 个, 失败 {failed} 个")


def save_report(report, download_dir):
    """将报告保存到文件。"""
    report_dir = os.path.join(download_dir, "微信重复文件")
    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(report_dir, f"去重报告_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    return report_path


def main():
    print("=" * 60)
    print("        微信文件去重工具")
    print("=" * 60)

    # 获取微信目录
    default_wechat = get_default_wechat_dir()
    wechat_dir = input(f"\n请输入微信文件目录\n(默认: {default_wechat})\n> ").strip()
    if not wechat_dir:
        wechat_dir = default_wechat

    if not os.path.isdir(wechat_dir):
        print(f"\n错误: 目录不存在 - {wechat_dir}")
        sys.exit(1)

    # 获取下载目录
    default_download = get_default_download_dir()
    download_dir = input(f"\n请输入重复文件移动目标目录\n(默认: {default_download})\n> ").strip()
    if not download_dir:
        download_dir = default_download

    # 查找月份目录
    print(f"\n正在扫描目录: {wechat_dir}")
    month_dirs = find_month_dirs(wechat_dir)

    if not month_dirs:
        print("未找到任何月份目录（格式: YYYY-MM）。请检查路径是否正确。")
        sys.exit(1)

    print(f"找到 {len(month_dirs)} 个月份目录:")
    for d in month_dirs:
        print(f"  {os.path.basename(d)}")

    # 扫描重复文件
    print("\n正在计算文件 MD5 并查找重复文件，请稍候...")
    duplicates, stats = scan_duplicates(month_dirs)

    # 生成报告
    report, move_plan = generate_report(duplicates, stats, download_dir)
    print("\n" + report)

    if not move_plan:
        print("\n没有需要移动的文件，程序结束。")
        return

    # 保存报告
    report_path = save_report(report, download_dir)
    print(f"\n报告已保存到: {report_path}")

    # 用户确认
    confirm = input("\n是否执行移动操作？(y/N) > ").strip().lower()
    if confirm in ("y", "yes", "是"):
        print("\n正在移动文件...")
        execute_move(move_plan)
        print("\n操作完成！重复文件已移动到:", os.path.join(download_dir, "微信重复文件"))
    else:
        print("\n已取消操作。您可以查看报告后再决定。")


if __name__ == "__main__":
    main()
