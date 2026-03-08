"""
微信文件夹去重工具

扫描指定目录下所有形如 YYYY-MM 的子文件夹，分析每个文件夹内的重复文件，
将重复文件移动到上层目录的「重复/YYYY-MM/」文件夹中，保留最新修改的文件。
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


def find_wechat_dirs():
    """自动扫描系统中可能的微信文件目录。"""
    candidates = set()

    if platform.system() == "Windows":
        # 扫描所有盘符下的常见位置
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if not os.path.exists(drive):
                continue
            search_roots = [drive]
            # 扫描该盘符下所有用户的 Documents 目录
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
            # 也扫描盘符根目录（用户可能把 Documents 放在 D:\ 等）
            for root in search_roots:
                for pattern in [WECHAT_PATTERNS_NEW, WECHAT_PATTERNS_OLD]:
                    for path in glob.glob(os.path.join(root, pattern)):
                        if os.path.isdir(path):
                            candidates.add(os.path.normpath(path))
                    # 也搜索 root/document/ 等变体
                    for doc_name in ["document", "Document", "documents", "Documents"]:
                        doc_path = os.path.join(root, doc_name)
                        if os.path.isdir(doc_path):
                            for path in glob.glob(os.path.join(doc_path, pattern)):
                                if os.path.isdir(path):
                                    candidates.add(os.path.normpath(path))
    else:
        # macOS / Linux
        home = os.path.expanduser("~")
        search_roots = [home]
        # 常见 Documents 位置
        for doc_name in ["Documents", "documents", "文档"]:
            doc_path = os.path.join(home, doc_name)
            if os.path.isdir(doc_path):
                search_roots.append(doc_path)
        # macOS 微信还可能在 ~/Library/Containers/ 下
        containers = os.path.join(home, "Library", "Containers")
        if os.path.isdir(containers):
            search_roots.append(containers)

        for root in search_roots:
            for pattern in [WECHAT_PATTERNS_NEW, WECHAT_PATTERNS_OLD]:
                for path in glob.glob(os.path.join(root, pattern)):
                    if os.path.isdir(path):
                        candidates.add(os.path.normpath(path))

    # 过滤：只保留包含 YYYY-MM 子文件夹的目录
    valid = []
    month_pattern = re.compile(r"^\d{4}-\d{2}$")
    for d in sorted(candidates):
        try:
            has_month = any(
                month_pattern.match(e) and os.path.isdir(os.path.join(d, e))
                for e in os.listdir(d)
            )
            if has_month:
                valid.append(d)
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

        # 按修改时间降序排列，保留最新的
        files.sort(key=lambda x: os.path.getmtime(x), reverse=True)
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


def print_move_plan(month_name, move_plan):
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
        print(f"    [保留] {os.path.basename(keep)}")
        for dup, dest in dups:
            print(f"    [移动] {os.path.basename(dup)} -> {dest}")


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
        print(f"找到 {len(wechat_dirs)} 个微信文件目录:\n")
        for i, d in enumerate(wechat_dirs, 1):
            # 统计 YYYY-MM 子文件夹数量
            month_count = len(find_month_dirs(d))
            print(f"  [{i}] {d}")
            print(f"      ({month_count} 个月份文件夹)")
        print(f"\n  [0] 手动输入其他路径")
        print()

        while True:
            choice = input("请选择目录编号: ").strip()
            if choice == "0":
                break
            try:
                idx = int(choice)
                if 1 <= idx <= len(wechat_dirs):
                    return wechat_dirs[idx - 1]
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
    """统计每个月份文件夹的文件数、大小疑似重复数和 MD5 精确重复数。"""
    summary = []  # (month_dir, total_files, size_dups, md5_dups)
    for month_dir in month_dirs:
        files = list(find_files(month_dir))
        total = len(files)

        # 按文件大小分组
        size_map = {}
        for f in files:
            try:
                size = os.path.getsize(f)
                size_map.setdefault(size, []).append(f)
            except OSError:
                pass

        size_dups = sum(len(g) - 1 for g in size_map.values() if len(g) > 1)

        # 对大小相同的文件进行 MD5 精确比对
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

        summary.append((month_dir, total, size_dups, md5_dups))
    return summary


def main():
    base_directory = select_directory()

    # 安全检查
    validate_directory(base_directory)

    # 是否使用 MD5（推荐，更准确但较慢）
    use_md5 = True

    month_dirs = find_month_dirs(base_directory)
    if not month_dirs:
        print(f"未找到形如 YYYY-MM 的子文件夹: {base_directory}")
        sys.exit(0)

    # ===== 第一阶段：概览（含 MD5 精确重复数）=====
    print(f"\n找到 {len(month_dirs)} 个月份文件夹，正在分析重复情况...\n")
    summary = quick_scan_summary(month_dirs)

    has_dup = False
    print(f"  {'月份':<12} {'文件数':>6} {'大小疑似':>8} {'MD5重复':>8}")
    print(f"  {'-'*12} {'-'*6} {'-'*8} {'-'*8}")
    for month_dir, total, size_dups, md5_dups in summary:
        month_name = os.path.basename(month_dir)
        size_label = str(size_dups) if size_dups > 0 else "-"
        md5_label  = str(md5_dups)  if md5_dups  > 0 else "-"
        print(f"  {month_name:<12} {total:>6} {size_label:>8} {md5_label:>8}")
        if md5_dups > 0:
            has_dup = True

    total_files   = sum(s[1] for s in summary)
    total_size_dup = sum(s[2] for s in summary)
    total_md5_dup  = sum(s[3] for s in summary)
    print(f"  {'-'*12} {'-'*6} {'-'*8} {'-'*8}")
    print(f"  {'合计':<12} {total_files:>6} {total_size_dup:>8} {total_md5_dup:>8}")
    print()

    if not has_dup:
        print("所有文件夹中均无 MD5 重复文件，无需操作。")
        sys.exit(0)

    confirm = input("是否生成详细移动计划？(y/n): ").strip().lower()
    if confirm not in ("y", "yes"):
        print("已取消。")
        sys.exit(0)

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
        sys.exit(0)

    # ===== 汇报移动计划 =====
    print("=" * 60)
    print(f"扫描完成，共发现 {total_dup_count} 个重复文件待移动:")
    print(f"重复文件将移动到: {os.path.join(base_directory, '重复')}")
    print("=" * 60)
    print()

    for month_dir, plan in all_plans.items():
        month_name = os.path.basename(month_dir)
        print_move_plan(month_name, plan)
    print()

    # ===== 第三阶段：用户确认并执行 =====
    if total_dup_count > WARN_FILE_COUNT:
        print(f"⚠ 注意: 待移动文件数量较大 ({total_dup_count} 个)，请仔细核实目录是否正确！")
        print(f"  操作目录: {base_directory}\n")

    confirm = input("是否执行以上移动操作？(y/n): ").strip().lower()
    if confirm not in ("y", "yes"):
        print("已取消操作，未移动任何文件。")
        sys.exit(0)

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


if __name__ == "__main__":
    main()
