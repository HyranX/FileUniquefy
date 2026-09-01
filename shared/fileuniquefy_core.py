"""FileUniquefy 的共享扫描与安全移动核心。

命令行版和图形界面版必须共同调用本模块，避免两套文件操作算法发生漂移。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable


MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
DUPLICATE_SUFFIX_RE = re.compile(r"\(\d+\)$")
SUPPORTED_HASHES = ("sha256", "blake2b")
KEEP_POLICIES = ("original_oldest", "oldest", "newest")
SCOPES = ("month", "all")
DEFAULT_OUTPUT_FOLDER = "重复"
WARN_FILE_COUNT = 500
HASH_CHUNK_SIZE = 1024 * 1024

ProgressCallback = Callable[[str, int, int, str], None]


class FileUniquefyError(Exception):
    """基础异常。"""


class SafetyError(FileUniquefyError):
    """目录或文件未通过安全检查。"""


class PlanStaleError(FileUniquefyError):
    """扫描后文件发生变化，计划已不再安全。"""


@dataclass(frozen=True)
class ScanOptions:
    scope: str = "month"
    recursive: bool = False
    min_size: int = 0
    keep_policy: str = "original_oldest"
    hash_algorithm: str = "sha256"
    output_folder: str = DEFAULT_OUTPUT_FOLDER
    output_directory: str | None = None

    def validate(self) -> None:
        if self.scope not in SCOPES:
            raise ValueError(f"不支持的扫描范围: {self.scope}")
        if self.keep_policy not in KEEP_POLICIES:
            raise ValueError(f"不支持的保留策略: {self.keep_policy}")
        if self.hash_algorithm not in SUPPORTED_HASHES:
            raise ValueError(f"不支持的哈希算法: {self.hash_algorithm}")
        if self.min_size < 0:
            raise ValueError("最小文件大小不能为负数")
        if self.output_directory:
            output_path = Path(self.output_directory).expanduser()
            if not output_path.is_absolute():
                raise SafetyError("移动目的地必须是绝对路径")
        else:
            validate_output_folder(self.output_folder)


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    month: str
    relative_path: Path
    size: int
    mtime_ns: int
    digest: str


@dataclass(frozen=True)
class MoveItem:
    keep: FileSnapshot
    duplicate: FileSnapshot
    destination: Path


@dataclass(frozen=True)
class DirectoryInfo:
    path: Path
    version: str
    user_id: str
    month_count: int
    total_files: int
    total_size: int


@dataclass(frozen=True)
class MonthSummary:
    month: str
    total_files: int
    eligible_files: int
    duplicate_files: int
    duplicate_bytes: int


@dataclass
class ScanResult:
    base_directory: Path
    options: ScanOptions
    month_directories: list[Path]
    move_items: list[MoveItem]
    month_summaries: list[MonthSummary]
    total_files: int
    eligible_files: int
    hashed_files: int
    duplicate_bytes: int
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass
class ExecutionResult:
    moved: list[tuple[Path, Path]]
    failures: list[str]
    journal_path: Path | None


def format_size(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _canonical(path: os.PathLike[str] | str, *, strict: bool = True) -> Path:
    return Path(path).expanduser().resolve(strict=strict)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _is_link_like(path: Path) -> bool:
    """同时识别符号链接和 Windows 目录联接点。"""
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction and is_junction())
    except OSError:
        return True


def validate_output_folder(name: str) -> None:
    if not name or name in {".", ".."}:
        raise SafetyError("输出文件夹名称不能为空，也不能是 . 或 ..")
    if Path(name).name != name or "/" in name or "\\" in name:
        raise SafetyError("输出文件夹必须是单一文件夹名称")
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.endswith((" ", ".")):
        raise SafetyError("输出文件夹名称包含 Windows 不允许的字符")
    stem = name.split(".", 1)[0].upper()
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if stem in reserved:
        raise SafetyError("输出文件夹名称是 Windows 保留设备名")


def validate_base_directory(path: os.PathLike[str] | str) -> tuple[Path, list[str]]:
    """校验操作根目录，返回规范路径及非致命警告。"""
    raw_path = Path(path).expanduser()
    if _is_link_like(raw_path):
        raise SafetyError("不允许把符号链接或目录联接点作为操作根目录")
    try:
        base = _canonical(raw_path)
    except (FileNotFoundError, OSError) as exc:
        raise SafetyError(f"目录不存在或无法访问: {path}") from exc
    if not base.is_dir():
        raise SafetyError(f"目标不是目录: {base}")

    home = Path.home().resolve()
    anchor = Path(base.anchor).resolve()
    if _same_path(base, anchor):
        raise SafetyError(f"禁止对磁盘或文件系统根目录操作: {base}")
    if _same_path(base, home):
        raise SafetyError(f"禁止对用户主目录本身操作: {base}")

    dangerous: list[Path] = []
    if os.name == "nt":
        for env_name in ("WINDIR", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA"):
            value = os.environ.get(env_name)
            if value:
                try:
                    dangerous.append(_canonical(value))
                except OSError:
                    pass
    else:
        dangerous.extend(Path(p) for p in ("/bin", "/sbin", "/usr", "/etc", "/var", "/tmp", "/opt", "/System", "/Library", "/Applications"))
    for danger in dangerous:
        if _same_path(base, danger) or _is_relative_to(base, danger):
            raise SafetyError(f"禁止对系统目录或其子目录操作: {base}")

    warnings: list[str] = []
    if len(base.parts) <= 2:
        warnings.append(f"目录层级很浅，请再次核对: {base}")
    lowered = str(base).casefold()
    keywords = ("wechat", "微信", "xwechat", "filestorage", "xwechat_files")
    if not any(keyword.casefold() in lowered for keyword in keywords):
        warnings.append("该路径不像已知的微信文件目录，请确认选择无误。")
    return base, warnings


def resolve_output_directory(base: Path, options: ScanOptions, month_dirs: list[Path] | None = None) -> Path:
    """解析并校验移动目的地；允许与源目录位于不同磁盘。"""
    options.validate()
    raw = Path(options.output_directory).expanduser() if options.output_directory else base / options.output_folder
    output = raw.resolve(strict=False)
    anchor = Path(output.anchor).resolve()
    if _same_path(output, anchor):
        raise SafetyError(f"禁止把磁盘根目录作为移动目的地: {output}")

    current = anchor
    for part in output.parts[1:]:
        current = current / part
        if current.exists() and _is_link_like(current):
            raise SafetyError(f"移动目的地不能经过符号链接或目录联接点: {current}")
    if output.exists() and not output.is_dir():
        raise SafetyError(f"移动目的地不是目录: {output}")

    if _same_path(output, base) or _is_relative_to(base, output):
        raise SafetyError("移动目的地不能等于源目录或包含源目录")
    for month_dir in month_dirs or find_month_directories(base):
        if _same_path(output, month_dir) or _is_relative_to(output, month_dir):
            raise SafetyError(f"移动目的地不能位于月份源目录内: {output}")

    if os.name == "nt":
        for env_name in ("WINDIR", "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA"):
            value = os.environ.get(env_name)
            if not value:
                continue
            danger = Path(value).resolve()
            if _same_path(output, danger) or _is_relative_to(output, danger):
                raise SafetyError(f"禁止把系统目录作为移动目的地: {output}")
    return output


def find_month_directories(base_directory: Path) -> list[Path]:
    result: list[Path] = []
    for entry in sorted(base_directory.iterdir(), key=lambda item: item.name.casefold()):
        if MONTH_RE.fullmatch(entry.name) and entry.is_dir() and not _is_link_like(entry):
            result.append(entry)
    return result


def _extract_wechat_meta(path: Path) -> tuple[str, str]:
    normalized = str(path).replace("\\", "/")
    match = re.search(r"xwechat_files/([^/]+)/msg/file", normalized, re.IGNORECASE)
    if match:
        return "新版微信", match.group(1)
    match = re.search(r"WeChat Files/([^/]+)/FileStorage/File", normalized, re.IGNORECASE)
    if match:
        return "旧版微信", match.group(1)
    return "未知", "未知"


def _count_directory(path: Path) -> tuple[int, int]:
    total_files = 0
    total_size = 0
    for root, dir_names, file_names in os.walk(path, followlinks=False):
        root_path = Path(root)
        dir_names[:] = [name for name in dir_names if not _is_link_like(root_path / name)]
        for name in file_names:
            file_path = root_path / name
            if _is_link_like(file_path):
                continue
            try:
                file_stat = file_path.stat()
                if stat.S_ISREG(file_stat.st_mode):
                    total_files += 1
                    total_size += file_stat.st_size
            except (OSError, PermissionError):
                continue
    return total_files, total_size


def discover_wechat_directory_info() -> list[DirectoryInfo]:
    """按旧版规则发现微信目录，并返回版本、账号和容量统计。"""
    roots: set[Path] = set()
    home = Path.home()
    document_names = ("document", "Document", "documents", "Documents", "文档")
    roots.add(home)
    roots.update(home / name for name in document_names)
    if os.name == "nt":
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = Path(f"{letter}:\\")
            if drive.exists():
                roots.add(drive)
                roots.update(drive / name for name in document_names)
                users_directory = drive / "Users"
                if users_directory.is_dir() and not _is_link_like(users_directory):
                    try:
                        for user_directory in users_directory.iterdir():
                            if user_directory.is_dir() and not _is_link_like(user_directory):
                                roots.add(user_directory)
                                roots.update(user_directory / name for name in document_names)
                    except (OSError, PermissionError):
                        pass

    patterns = (
        "xwechat_files/*/msg/file",
        "WeChat Files/*/FileStorage/File",
    )
    found: set[Path] = set()
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in patterns:
            try:
                for candidate in root.glob(pattern):
                    if candidate.is_dir() and find_month_directories(candidate):
                        found.add(candidate.resolve())
            except (OSError, PermissionError):
                continue
    result: list[DirectoryInfo] = []
    for path in sorted(found, key=lambda p: str(p).casefold()):
        months = find_month_directories(path)
        version, user_id = _extract_wechat_meta(path)
        total_files, total_size = _count_directory(path)
        result.append(DirectoryInfo(path, version, user_id, len(months), total_files, total_size))
    return result


def discover_wechat_directories() -> list[Path]:
    """兼容路径列表接口。"""
    return [info.path for info in discover_wechat_directory_info()]


def calculate_digest(path: Path, algorithm: str = "sha256") -> str:
    if algorithm not in SUPPORTED_HASHES:
        raise ValueError(f"不支持的哈希算法: {algorithm}")
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_files(month_dir: Path, recursive: bool) -> Iterable[Path]:
    if not recursive:
        for entry in sorted(month_dir.iterdir(), key=lambda item: item.name.casefold()):
            try:
                mode = entry.lstat().st_mode
            except OSError:
                continue
            if stat.S_ISREG(mode) and not _is_link_like(entry):
                yield entry
        return

    for root, dir_names, file_names in os.walk(month_dir, followlinks=False):
        root_path = Path(root)
        dir_names[:] = sorted(
            (name for name in dir_names if not _is_link_like(root_path / name)),
            key=str.casefold,
        )
        for name in sorted(file_names, key=str.casefold):
            path = root_path / name
            try:
                mode = path.lstat().st_mode
            except OSError:
                continue
            if stat.S_ISREG(mode) and not _is_link_like(path):
                yield path


def _keep_sort_key(snapshot: FileSnapshot, policy: str) -> tuple[object, ...]:
    normalized = str(snapshot.path).casefold()
    if policy == "oldest":
        return snapshot.mtime_ns, normalized
    if policy == "newest":
        return -snapshot.mtime_ns, normalized
    has_suffix = bool(DUPLICATE_SUFFIX_RE.search(snapshot.path.stem))
    return int(has_suffix), snapshot.mtime_ns, normalized


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _unique_destination(preferred: Path, reserved: set[str]) -> Path:
    candidate = preferred
    counter = 1
    while candidate.exists() or _path_key(candidate) in reserved:
        candidate = preferred.with_name(f"{preferred.stem}_{counter}{preferred.suffix}")
        counter += 1
    reserved.add(_path_key(candidate))
    return candidate


def scan_directory(
    base_directory: os.PathLike[str] | str,
    options: ScanOptions | None = None,
    progress: ProgressCallback | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> ScanResult:
    options = options or ScanOptions()
    options.validate()
    base, warnings = validate_base_directory(base_directory)
    month_dirs = find_month_directories(base)
    if not month_dirs:
        raise FileUniquefyError(f"未找到有效的 YYYY-MM 月份文件夹: {base}")

    output_root = resolve_output_directory(base, options, month_dirs)

    files_by_month: dict[str, list[Path]] = {}
    total_by_month: dict[str, int] = {}
    errors: list[str] = []
    total_files = 0
    eligible_files = 0
    for month_dir in month_dirs:
        files = list(_iter_files(month_dir, options.recursive))
        total_by_month[month_dir.name] = len(files)
        total_files += len(files)
        eligible: list[Path] = []
        for path in files:
            try:
                if path.stat().st_size >= options.min_size:
                    eligible.append(path)
                    eligible_files += 1
            except OSError as exc:
                errors.append(f"无法读取文件属性，已跳过: {path} ({exc})")
        files_by_month[month_dir.name] = eligible

    groups_to_scan: list[tuple[str, list[Path]]]
    if options.scope == "all":
        groups_to_scan = [("all", [path for files in files_by_month.values() for path in files])]
    else:
        groups_to_scan = list(files_by_month.items())

    hashed_files = 0
    move_items: list[MoveItem] = []
    reserved_destinations: set[str] = set()
    processed = 0
    total_to_consider = sum(len(files) for _, files in groups_to_scan)

    for _, files in groups_to_scan:
        size_groups: dict[int, list[Path]] = {}
        for path in files:
            try:
                size_groups.setdefault(path.stat().st_size, []).append(path)
            except OSError as exc:
                errors.append(f"无法读取文件属性，已跳过: {path} ({exc})")

        digest_groups: dict[tuple[int, str], list[FileSnapshot]] = {}
        for size, candidates in size_groups.items():
            if len(candidates) <= 1:
                processed += len(candidates)
                continue
            for path in candidates:
                if cancelled and cancelled():
                    raise FileUniquefyError("操作已取消")
                try:
                    before = path.stat()
                    digest = calculate_digest(path, options.hash_algorithm)
                    after = path.stat()
                    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                        errors.append(f"哈希期间文件发生变化，已跳过: {path}")
                        continue
                    month = path.relative_to(base).parts[0]
                    relative = path.relative_to(base / month)
                    snapshot = FileSnapshot(path, month, relative, after.st_size, after.st_mtime_ns, digest)
                    digest_groups.setdefault((size, digest), []).append(snapshot)
                    hashed_files += 1
                except (OSError, PermissionError) as exc:
                    errors.append(f"无法读取文件，已跳过: {path} ({exc})")
                finally:
                    processed += 1
                    if progress:
                        progress("scan", processed, total_to_consider, str(path))

        for snapshots in digest_groups.values():
            if len(snapshots) <= 1:
                continue
            snapshots.sort(key=lambda item: _keep_sort_key(item, options.keep_policy))
            keep = snapshots[0]
            for duplicate in snapshots[1:]:
                preferred = output_root / duplicate.month / duplicate.relative_path
                destination = _unique_destination(preferred, reserved_destinations)
                move_items.append(MoveItem(keep, duplicate, destination))

    move_items.sort(key=lambda item: str(item.duplicate.path).casefold())
    duplicate_count_by_month = {month.name: 0 for month in month_dirs}
    duplicate_bytes_by_month = {month.name: 0 for month in month_dirs}
    for item in move_items:
        duplicate_count_by_month[item.duplicate.month] += 1
        duplicate_bytes_by_month[item.duplicate.month] += item.duplicate.size
    month_summaries = [
        MonthSummary(
            month=month.name,
            total_files=total_by_month[month.name],
            eligible_files=len(files_by_month[month.name]),
            duplicate_files=duplicate_count_by_month[month.name],
            duplicate_bytes=duplicate_bytes_by_month[month.name],
        )
        for month in month_dirs
    ]
    return ScanResult(
        base_directory=base,
        options=options,
        month_directories=month_dirs,
        move_items=move_items,
        month_summaries=month_summaries,
        total_files=total_files,
        eligible_files=eligible_files,
        hashed_files=hashed_files,
        duplicate_bytes=sum(item.duplicate.size for item in move_items),
        warnings=warnings,
        errors=errors,
    )


def _revalidate_snapshot(snapshot: FileSnapshot, base: Path, algorithm: str) -> None:
    if _is_link_like(snapshot.path):
        raise PlanStaleError(f"文件已变成链接: {snapshot.path}")
    try:
        path = snapshot.path.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise PlanStaleError(f"文件已不存在: {snapshot.path}") from exc
    if not _is_relative_to(path, base):
        raise PlanStaleError(f"文件已移出安全范围或变成链接: {snapshot.path}")
    current = path.stat()
    if current.st_size != snapshot.size or current.st_mtime_ns != snapshot.mtime_ns:
        raise PlanStaleError(f"扫描后文件已变化，请重新扫描: {snapshot.path}")
    if calculate_digest(path, algorithm) != snapshot.digest:
        raise PlanStaleError(f"扫描后文件内容已变化，请重新扫描: {snapshot.path}")


def _open_journal(output_root: Path) -> tuple[Path, object]:
    journal_dir = output_root / ".fileuniquefy-journals"
    journal_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for counter in range(1000):
        suffix = "" if counter == 0 else f"-{counter}"
        path = journal_dir / f"move-{stamp}{suffix}.jsonl"
        try:
            return path, path.open("x", encoding="utf-8", newline="\n")
        except FileExistsError:
            continue
    raise FileUniquefyError("无法创建唯一的操作日志")


def _rollback_move(destination: Path, source: Path) -> bool:
    """在源路径仍空闲时，把移动后的文件安全放回原位。"""
    if source.exists() or not destination.exists():
        return False
    placeholder_created = False
    try:
        descriptor = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        placeholder_created = True
        os.replace(destination, source)
        return True
    except OSError:
        if placeholder_created:
            try:
                source.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def _copy_across_filesystems(
    item: MoveItem,
    destination: Path,
    base: Path,
    algorithm: str,
) -> None:
    """跨卷复制并校验；只有目标完整落位且源仍未变化时才删除源。"""
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with item.duplicate.path.open("rb") as source_stream, os.fdopen(descriptor, "wb") as target_stream:
            descriptor = None
            shutil.copyfileobj(source_stream, target_stream, HASH_CHUNK_SIZE)
            target_stream.flush()
            os.fsync(target_stream.fileno())
        shutil.copystat(item.duplicate.path, temporary, follow_symlinks=False)

        copied_stat = temporary.stat()
        copied_digest = calculate_digest(temporary, algorithm)
        if copied_stat.st_size != item.duplicate.size or copied_digest != item.duplicate.digest:
            raise PlanStaleError(f"跨卷复制内容校验失败: {temporary}")

        _revalidate_snapshot(item.keep, base, algorithm)
        _revalidate_snapshot(item.duplicate, base, algorithm)
        os.replace(temporary, destination)

        final_stat = destination.stat()
        final_digest = calculate_digest(destination, algorithm)
        if final_stat.st_size != item.duplicate.size or final_digest != item.duplicate.digest:
            raise PlanStaleError(f"跨卷目标落位后校验失败: {destination}")
        _revalidate_snapshot(item.keep, base, algorithm)
        _revalidate_snapshot(item.duplicate, base, algorithm)
        item.duplicate.path.unlink()
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _same_filesystem(source: Path, destination_parent: Path) -> bool:
    if os.name == "nt":
        return source.drive.casefold() == destination_parent.drive.casefold()
    return source.stat().st_dev == destination_parent.stat().st_dev


def execute_plan(
    result: ScanResult,
    progress: ProgressCallback | None = None,
    cancelled: Callable[[], bool] | None = None,
    stop_on_error: bool = True,
) -> ExecutionResult:
    """复核扫描快照后执行计划；永不覆盖已有目标文件。"""
    base, _ = validate_base_directory(result.base_directory)
    if not _same_path(base, result.base_directory):
        raise SafetyError("操作根目录已变化")
    result.options.validate()
    output_root = resolve_output_directory(base, result.options, result.month_directories)
    output_root.mkdir(parents=True, exist_ok=True)
    output_resolved = output_root.resolve(strict=True)
    if not _same_path(output_resolved, output_root):
        raise SafetyError("移动目的地解析后发生变化")

    journal_path, journal = _open_journal(output_root)
    moved: list[tuple[Path, Path]] = []
    failures: list[str] = []
    reserved: set[str] = set()
    try:
        header = {
            "event": "start",
            "base_directory": str(base),
            "output_directory": str(output_root),
            "hash_algorithm": result.options.hash_algorithm,
            "planned_moves": len(result.move_items),
            "time": time.time(),
        }
        journal.write(json.dumps(header, ensure_ascii=False) + "\n")
        journal.flush()
        os.fsync(journal.fileno())

        for index, item in enumerate(result.move_items, 1):
            if cancelled and cancelled():
                failures.append("用户取消了剩余操作")
                break
            placeholder_created = False
            destination: Path | None = None
            try:
                _revalidate_snapshot(item.keep, base, result.options.hash_algorithm)
                _revalidate_snapshot(item.duplicate, base, result.options.hash_algorithm)
                if item.keep.digest != item.duplicate.digest or item.keep.size != item.duplicate.size:
                    raise PlanStaleError("保留文件与重复文件已不再相同")

                destination = _unique_destination(item.destination, reserved)
                destination.parent.mkdir(parents=True, exist_ok=True)
                resolved_parent = destination.parent.resolve(strict=True)
                if not _is_relative_to(resolved_parent, output_resolved):
                    raise SafetyError(f"目标路径逃逸到输出目录之外: {destination}")

                descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(descriptor)
                placeholder_created = True
                same_filesystem = _same_filesystem(item.duplicate.path, destination.parent)
                if same_filesystem:
                    os.replace(item.duplicate.path, destination)
                    placeholder_created = False
                    final_stat = destination.stat()
                    final_digest = calculate_digest(destination, result.options.hash_algorithm)
                    try:
                        _revalidate_snapshot(item.keep, base, result.options.hash_algorithm)
                        post_move_valid = (
                            final_stat.st_size == item.duplicate.size
                            and final_digest == item.duplicate.digest
                        )
                        if not post_move_valid:
                            raise PlanStaleError(f"移动后内容校验失败: {destination}")
                    except Exception as verification_error:
                        rolled_back = _rollback_move(destination, item.duplicate.path)
                        outcome = "已自动回滚" if rolled_back else f"无法自动回滚，文件保留在 {destination}"
                        raise PlanStaleError(f"{verification_error}；{outcome}") from verification_error
                else:
                    _copy_across_filesystems(
                        item,
                        destination,
                        base,
                        result.options.hash_algorithm,
                    )
                    placeholder_created = False
                moved.append((item.duplicate.path, destination))
                event = {
                    "event": "moved",
                    "source": str(item.duplicate.path),
                    "destination": str(destination),
                    "kept": str(item.keep.path),
                    "size": item.duplicate.size,
                    "digest": item.duplicate.digest,
                    "time": time.time(),
                }
                journal.write(json.dumps(event, ensure_ascii=False) + "\n")
                journal.flush()
                os.fsync(journal.fileno())
                if progress:
                    progress("move", index, len(result.move_items), str(item.duplicate.path))
            except Exception as exc:  # 每项均写入日志，调用方可决定是否停止
                if placeholder_created and destination is not None:
                    try:
                        destination.unlink(missing_ok=True)
                    except OSError:
                        pass
                message = f"{item.duplicate.path}: {exc}"
                failures.append(message)
                journal.write(json.dumps({"event": "failed", "message": message, "time": time.time()}, ensure_ascii=False) + "\n")
                journal.flush()
                os.fsync(journal.fileno())
                if stop_on_error:
                    break
    finally:
        journal.close()

    return ExecutionResult(moved=moved, failures=failures, journal_path=journal_path)
