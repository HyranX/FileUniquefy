"""
微信文件夹去重工具

扫描指定目录下所有形如 YYYY-MM 的子文件夹，分析每个文件夹内的重复文件，
将重复文件移动到上层目录的「重复/YYYY-MM/」文件夹中，保留最早修改的文件（原始文件）。
"""

import glob
import hashlib
import os
import platform
import re
import string
import sys
from shutil import move

# 安全阈值：待移动文件数超过此值时额外警告
WARN_FILE_COUNT = 500

# 禁止操作的危险目录（规范化后比较）
DANGEROUS_DIRS_UNIX = {"/", "/bin", "/sbin", "/usr", "/etc", "/var", "/tmp",
                       "/System", "/Library", "/Applications", "/opt"}

# 微信文件目录的特征模式
# 新版微信: .../xwechat_files/<账号>/msg/file/
# 旧版微信: .../WeChat Files/<账号>/FileStorage/File/
WECHAT_PATTERNS_NEW = os.path.join("xwechat_files", "*", "msg", "file")
WECHAT_PATTERNS_OLD = os.path.join("WeChat Files", "*", "FileStorage", "File")


def _display_width(s):
    """计算字符串在终端中的显示宽度（中文字符占2列）。"""
    width = 0
    for c in s:
        cp = ord(c)
        if (0x1100 <= cp <= 0x115F or 0x2E80 <= cp <= 0x303E or
                0x3040 <= cp <= 0xA4CF or 0xAC00 <= cp <= 0xD7AF or
                0xF900 <= cp <= 0xFAFF or 0xFE10 <= cp <= 0xFE1F or
                0xFE30 <= cp <= 0xFE6F or 0xFF01 <= cp <= 0xFF60 or
                0xFFE0 <= cp <= 0xFFE6):
            width += 2
        else:
            width += 1
    return width


def _ljust(s, width):
    """按显示宽度左对齐填充空格。"""
    return s + " " * max(0, width - _display_width(s))


def _rjust(s, width):
    """按显示宽度右对齐填充空格。"""
    return " " * max(0, width - _display_width(s)) + s


def _fmt_size(size_bytes):
    """将字节数格式化为可读字符串。"""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def _count_dir(path):
    """递归统计目录下的文件总数和总大小（字节）。"""
    total_files = 0
    total_size = 0
    try:
        for root, _, files in os.walk(path):
            for f in files:
                total_files += 1
                try:
                    total_size += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except PermissionError:
        pass
    return total_files, total_size


def _extract_wechat_meta(path):
    """从路径中提取微信版本和用户ID。返回 (version, user_id)。"""
    norm = path.replace("\\", "/")
    # 新版: .../xwechat_files/<user_id>/msg/file
    m = re.search(r"xwechat_files/([^/]+)/msg/file", norm, re.IGNORECASE)
    if m:
        return "新版微信", m.group(1)
    # 旧版: .../WeChat Files/<user_id>/FileStorage/File
    m = re.search(r"WeChat Files/([^/]+)/FileStorage/File", norm, re.IGNORECASE)
    if m:
        return "旧版微信", m.group(1)
    return "未知", "未知"


def find_wechat_dirs():
    """自动扫描系统中可能的微信文件目录。
    返回列表，每个元素为 dict: {path, version, user_id, month_count}
    """
    candidates = set()

    if platform.system() == "Windows":
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if not os.path.exists(drive):
                continue
            search_roots = [drive]
            users_dir = os.path.join(drive, "Users")
            if os.path.isdir(users_dir):
                try:
                    for user in os.listdir(users_dir):
                        user_path = os.path.join(users_dir, user)
                        if os.path.isdir(user_path):
                            search_roots.append(user_path)
                            docs = os.path.join(user_path, "Documents")
                            if os.path.isdir(docs):
                                search_roots.append(docs)
                except PermissionError:
                    pass
            for root in search_roots:
                for pattern in [WECHAT_PATTERNS_NEW, WECHAT_PATTERNS_OLD]:
                    for path in glob.glob(os.path.join(root, pattern)):
                        if os.path.isdir(path):
                            candidates.add(os.path.realpath(path))
                    for doc_name in ["document", "Document", "documents", "Documents"]:
                        doc_path = os.path.join(root, doc_name)
                        if os.path.isdir(doc_path):
                            for path in glob.glob(os.path.join(doc_path, pattern)):
                                if os.path.isdir(path):
                                    candidates.add(os.path.realpath(path))
    else:
        home = os.path.expanduser("~")
        search_roots = [home]
        for doc_name in ["Documents", "documents", "文档"]:
            doc_path = os.path.join(home, doc_name)
            if os.path.isdir(doc_path):
                search_roots.append(doc_path)
        containers = os.path.join(home, "Library", "Containers")
        if os.path.isdir(containers):
            search_roots.append(containers)
        for root in search_roots:
            for pattern in [WECHAT_PATTERNS_NEW, WECHAT_PATTERNS_OLD]:
                for path in glob.glob(os.path.join(root, pattern)):
                    if os.path.isdir(path):
                        candidates.add(os.path.realpath(path))

    # 过滤：只保留含 YYYY-MM 子文件夹的目录，并附加元数据
    valid = []
    month_pattern = re.compile(r"^\d{4}-\d{2}$")
    for d in sorted(candidates):
        try:
            month_dirs = [
                e for e in os.listdir(d)
                if month_pattern.match(e) and os.path.isdir(os.path.join(d, e))
            ]
            if not month_dirs:
                continue
            version, user_id = _extract_wechat_meta(d)
            total_files, total_size = _count_dir(d)
            valid.append({
                "path": d,
                "version": version,
                "user_id": user_id,
                "month_count": len(month_dirs),
                "total_files": total_files,
                "total_size": total_size,
            })
        except PermissionError:
            pass

    return valid


def validate_directory(path):
    """检查目录是否安全，防止误操作系统关键目录。"""
    real_path = os.path.realpath(path)

    # 1. 禁止根目录
    if real_path == os.path.abspath(os.sep):
        print(f"错误: 禁止对根目录操作 - {real_path}")
        sys.exit(1)

    # 2. 禁止 home 目录本身
    home = os.path.expanduser("~")
    if os.path.realpath(home) == real_path:
        print(f"错误: 禁止对用户主目录操作 - {real_path}")
        sys.exit(1)

    # 3. 禁止系统关键目录
    if platform.system() == "Windows":
        if re.match(r"^[A-Za-z]:\\?$", real_path):
            print(f"错误: 禁止对磁盘根目录操作 - {real_path}")
            sys.exit(1)
        win_dir = os.environ.get("WINDIR", r"C:\Windows")
        prog_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        for danger in [win_dir, prog_files, os.environ.get("PROGRAMFILES(X86)", "")]:
            if danger and os.path.realpath(danger) == real_path:
                print(f"错误: 禁止对系统目录操作 - {real_path}")
                sys.exit(1)
    else:
        if real_path in DANGEROUS_DIRS_UNIX:
            print(f"错误: 禁止对系统目录操作 - {real_path}")
            sys.exit(1)

    # 4. 目录层级过浅警告
    parts = [p for p in real_path.replace("\\", "/").split("/") if p]
    if len(parts) <= 2:
        print(f"警告: 目录层级很浅，可能不是预期的目标目录: {real_path}")
        confirm = input("确定要继续吗？(y/n): ").strip().lower()
        if confirm not in ("y", "yes"):
            print("已取消。")
            sys.exit(0)

    # 5. 非微信路径提醒
    wechat_keywords = ["wechat", "WeChat", "微信", "xwechat",
                       "FileStorage", "filestorage", "xwechat_files"]
    if not any(kw in real_path for kw in wechat_keywords):
        print(f"警告: 该路径不是已知的微信文件目录！")
        print(f"  当前路径: {real_path}")
        confirm = input("确认要对此目录操作？(y/n): ").strip().lower()
        if confirm not in ("y", "yes"):
            print("已取消。")
            sys.exit(0)


def calculate_md5(filepath):
    """计算文件的 MD5 哈希值"""
    hash_md5 = hashlib.md5()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash_md5.update(chunk)
    return hash_md5.hexdigest()


def find_files(directory):
    """查找目录下第一层的所有文件（不递归子文件夹）"""
    for entry in os.listdir(directory):
        full_path = os.path.join(directory, entry)
        if os.path.isfile(full_path):
            yield full_path


def find_month_dirs(base_directory):
    """查找所有形如 YYYY-MM 的子文件夹"""
    pattern = re.compile(r"^\d{4}-\d{2}$")
    month_dirs = []
    for entry in sorted(os.listdir(base_directory)):
        full_path = os.path.join(base_directory, entry)
        if os.path.isdir(full_path) and pattern.match(entry):
            month_dirs.append(full_path)
    return month_dirs


def scan_month_dir(month_dir, use_md5=True):
    """
    扫描单个月份文件夹，找出重复文件，返回移动计划。

    返回列表，每个元素为 (keep_file, dup_file, dest_path) 的元组。
    """
    base_dir = os.path.dirname(month_dir)
    month_name = os.path.basename(month_dir)
    dup_base = os.path.join(base_dir, "重复", month_name)

    # 按 MD5 或文件大小分组
    files_map = {}
    for file_path in find_files(month_dir):
        if use_md5:
            try:
                file_key = calculate_md5(file_path)
            except (PermissionError, OSError) as e:
                print(f"  [跳过] 无法读取: {file_path} ({e})")
                continue
        else:
            file_key = os.path.getsize(file_path)

        files_map.setdefault(file_key, []).append(file_path)

    move_plan = []
    dest_paths_used = set()

    for key, files in files_map.items():
        if len(files) <= 1:
            continue

        # 排序优先级：1) 无 (N) 后缀的原始文件优先保留；2) 修改时间升序作为次级排序
        def _sort_key(fp):
            stem = os.path.splitext(os.path.basename(fp))[0]
            has_dup_suffix = 1 if re.search(r'\(\d+\)$', stem) else 0
            return (has_dup_suffix, os.path.getmtime(fp))

        files.sort(key=_sort_key)
        latest_file = files[0]

        for dup_file in files[1:]:
            # 保持相对路径结构
            rel_path = os.path.relpath(dup_file, month_dir)
            dest_path = os.path.join(dup_base, rel_path)
            dest_dir = os.path.dirname(dest_path)

            # 目标文件名冲突处理（考虑已有文件和本批次内冲突）
            if os.path.exists(dest_path) or dest_path in dest_paths_used:
                name, ext = os.path.splitext(os.path.basename(dest_path))
                counter = 1
                candidate = dest_path
                while os.path.exists(candidate) or candidate in dest_paths_used:
                    candidate = os.path.join(dest_dir, f"{name}_{counter}{ext}")
                    counter += 1
                dest_path = candidate

            dest_paths_used.add(dest_path)
            move_plan.append((latest_file, dup_file, dest_path))

    return move_plan


def execute_move_plan(move_plan):
    """执行移动计划，实际移动文件。"""
    for _, dup_file, dest_path in move_plan:
        dest_dir = os.path.dirname(dest_path)
        os.makedirs(dest_dir, exist_ok=True)
        move(dup_file, dest_path)


def print_move_plan(month_name, move_plan, base_directory):
    """打印单个月份的移动计划。"""
    if not move_plan:
        print(f"  {month_name}: 无重复文件")
        return

    print(f"  {month_name}: 发现 {len(move_plan)} 个重复文件")
    # 按保留文件分组显示
    groups = {}
    for keep, dup, dest in move_plan:
        groups.setdefault(keep, []).append((dup, dest))

    for keep, dups in groups.items():
        print(f"    [保留] {month_name}/{os.path.basename(keep)}")
        for dup, dest in dups:
            rel_dest = os.path.relpath(dest, base_directory)
            print(f"    [移动] {month_name}/{os.path.basename(dup)} -> {rel_dest}")


def select_directory():
    """选择要处理的微信文件目录。支持命令行参数、自动扫描或手动输入。"""
    # 1. 命令行参数直接指定
    if len(sys.argv) > 1:
        path = os.path.abspath(sys.argv[1])
        if not os.path.isdir(path):
            print(f"错误: 目录不存在 - {path}")
            sys.exit(1)
        return path

    # 2. 自动扫描微信目录
    print("正在扫描系统中的微信文件目录...\n")
    wechat_dirs = find_wechat_dirs()

    if wechat_dirs:
        # 计算各列宽度
        uid_width = max(len(d["user_id"]) for d in wechat_dirs)
        uid_width = max(uid_width, 6)  # 最小宽度"用户ID"

        print(f"  找到 {len(wechat_dirs)} 个微信文件目录:\n")
        ver_w = max(_display_width(d["version"]) for d in wechat_dirs)
        size_w = max(len(_fmt_size(d["total_size"])) for d in wechat_dirs)
        size_w = max(size_w, 8)
        print(f"  {'编号':>4}  {_ljust('版本', ver_w)}  {_ljust('用户ID', uid_width)}  {'月份数':>6}  {'文件数':>6}  {'总大小':>{size_w}}  路径")
        print(f"  {'----':>4}  {'-'*ver_w}  {'-'*uid_width}  {'------':>6}  {'------':>6}  {'-'*size_w}  ----")
        for i, d in enumerate(wechat_dirs, 1):
            size_str = _fmt_size(d["total_size"])
            print(f"  [{i:>2}]  {_ljust(d['version'], ver_w)}  {_ljust(d['user_id'], uid_width)}  {d['month_count']:>6}  {d['total_files']:>6}  {size_str:>{size_w}}  {d['path']}")

        print(f"\n  [ 0]  手动输入其他路径")
        print()

        while True:
            choice = input("请选择目录编号: ").strip()
            if choice == "0":
                break
            try:
                idx = int(choice)
                if 1 <= idx <= len(wechat_dirs):
                    return wechat_dirs[idx - 1]["path"]
            except ValueError:
                pass
            print(f"无效输入，请输入 0-{len(wechat_dirs)} 之间的数字。")
    else:
        print("未自动检测到微信文件目录。\n")

    # 3. 手动输入
    path = input("请输入微信文件夹路径（包含 YYYY-MM 子文件夹的目录）: ").strip()
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        print(f"错误: 目录不存在 - {path}")
        sys.exit(1)
    return path


def quick_scan_summary(month_dirs):
    """统计每个月份文件夹的文件数和 MD5 精确重复数。"""
    summary = []  # (month_dir, total_files, md5_dups)
    for month_dir in month_dirs:
        files = list(find_files(month_dir))
        total = len(files)

        # 先按文件大小分组，只对同大小文件计算 MD5（减少 IO）
        size_map = {}
        for f in files:
            try:
                size = os.path.getsize(f)
                size_map.setdefault(size, []).append(f)
            except OSError:
                pass

        md5_map = {}
        for group in size_map.values():
            if len(group) <= 1:
                continue
            for f in group:
                try:
                    md5 = calculate_md5(f)
                    md5_map.setdefault(md5, []).append(f)
                except (PermissionError, OSError):
                    pass

        md5_dups = sum(len(g) - 1 for g in md5_map.values() if len(g) > 1)

        summary.append((month_dir, total, md5_dups))
    return summary


def process_directory(base_directory):
    """处理一个微信文件目录的完整流程，返回 True 表示希望返回主界面。"""
    use_md5 = True

    month_dirs = find_month_dirs(base_directory)
    if not month_dirs:
        print(f"未找到形如 YYYY-MM 的子文件夹: {base_directory}")
        return True

    # ===== 第一阶段：概览（含 MD5 精确重复数）=====
    print(f"\n找到 {len(month_dirs)} 个月份文件夹，正在分析重复情况...\n")
    summary = quick_scan_summary(month_dirs)

    has_dup = False
    c0, c1, c2 = 12, 6, 8
    print(f"  {_ljust('月份', c0)} {_rjust('文件数', c1)} {_rjust('MD5重复', c2)}")
    print(f"  {'-'*c0} {'-'*c1} {'-'*c2}")
    for month_dir, total, md5_dups in summary:
        month_name = os.path.basename(month_dir)
        md5_label  = str(md5_dups) if md5_dups > 0 else "-"
        print(f"  {_ljust(month_name, c0)} {total:>{c1}} {md5_label:>{c2}}")
        if md5_dups > 0:
            has_dup = True

    total_files   = sum(s[1] for s in summary)
    total_md5_dup = sum(s[2] for s in summary)
    print(f"  {'-'*c0} {'-'*c1} {'-'*c2}")
    print(f"  {_ljust('合计', c0)} {total_files:>{c1}} {total_md5_dup:>{c2}}")
    print()

    if not has_dup:
        print("所有文件夹中均无 MD5 重复文件，无需操作。")
        return True

    confirm = input("是否生成详细移动计划？(y/n): ").strip().lower()
    if confirm not in ("y", "yes"):
        print("已取消。")
        return True

    # ===== 第二阶段：生成移动计划 =====
    print("\n正在生成移动计划...\n")
    all_plans = {}
    total_dup_count = 0

    for month_dir in month_dirs:
        month_name = os.path.basename(month_dir)
        print(f"  扫描: {month_name} ...", end=" ", flush=True)
        plan = scan_month_dir(month_dir, use_md5=use_md5)
        all_plans[month_dir] = plan
        total_dup_count += len(plan)
        print(f"发现 {len(plan)} 个重复文件")

    print()

    if total_dup_count == 0:
        print("经 MD5 精确比对，无重复文件，无需操作。")
        return True

    # ===== 汇报移动计划 =====
    print("=" * 60)
    print(f"扫描完成，共发现 {total_dup_count} 个重复文件待移动:")
    print(f"重复文件将移动到: {os.path.join(base_directory, '重复')}")
    print("=" * 60)
    print()

    for month_dir, plan in all_plans.items():
        month_name = os.path.basename(month_dir)
        print_move_plan(month_name, plan, base_directory)
    print()

    # ===== 第三阶段：用户确认 =====
    if total_dup_count > WARN_FILE_COUNT:
        print(f"⚠ 注意: 待移动文件数量较大 ({total_dup_count} 个)，请仔细核实目录是否正确！")
        print(f"  操作目录: {base_directory}\n")

    confirm = input("是否执行以上移动操作？(y/n): ").strip().lower()
    if confirm not in ("y", "yes"):
        print("已取消操作，未移动任何文件。")
        return True

    # ===== 第四阶段：执行移动 =====
    print("\n正在移动文件...")
    total_moved = 0
    for month_dir, plan in all_plans.items():
        if not plan:
            continue
        month_name = os.path.basename(month_dir)
        execute_move_plan(plan)
        print(f"  {month_name}: 已移动 {len(plan)} 个文件")
        total_moved += len(plan)

    print(f"\n完成! 共移动 {total_moved} 个重复文件到「重复」文件夹。")
    return True


def main():
    # 命令行参数模式：直接处理，不循环
    if len(sys.argv) > 1:
        path = os.path.abspath(sys.argv[1])
        if not os.path.isdir(path):
            print(f"错误: 目录不存在 - {path}")
            sys.exit(1)
        validate_directory(path)
        process_directory(path)
        return

    # 交互模式：支持返回主界面
    while True:
        print("\n" + "=" * 60)
        base_directory = select_directory()
        validate_directory(base_directory)
        process_directory(base_directory)

        print()
        again = input("返回主界面选择其他目录？(y/n): ").strip().lower()
        if again not in ("y", "yes"):
            print("退出程序。")
            break


if __name__ == "__main__":
    main()
