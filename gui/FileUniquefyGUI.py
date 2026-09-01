"""FileUniquefy Tkinter 图形界面。"""

from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

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
    find_month_directories,
    format_size,
    scan_directory,
    validate_base_directory,
)


SCOPE_VALUES = {"仅在各月份内比较（推荐）": "month", "跨所有月份比较": "all"}
KEEP_VALUES = {
    "优先原始文件名，再保留最旧文件（推荐）": "original_oldest",
    "始终保留最旧文件": "oldest",
    "始终保留最新文件": "newest",
}
PREFERRED_DIRECTORY = Path(r"D:\Document\xwechat_files\bachopin_bdc8\msg\file")
DEFAULT_OUTPUT_DIRECTORY = r"E:\Down\weixin"


class FileUniquefyApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("FileUniquefy 微信文件去重")
        self.geometry("1180x760")
        self.minsize(900, 620)
        self.option_add("*Font", ("Microsoft YaHei UI", 9))

        self.events: queue.Queue[tuple] = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.scan_result = None
        self.scan_signature: tuple | None = None

        self.directory_var = tk.StringVar(value=self._default_directory())
        self.scope_var = tk.StringVar(value=next(iter(SCOPE_VALUES)))
        self.keep_var = tk.StringVar(value=next(iter(KEEP_VALUES)))
        self.hash_var = tk.StringVar(value="sha256")
        self.recursive_var = tk.BooleanVar(value=False)
        self.min_size_var = tk.StringVar(value="0")
        self.output_var = tk.StringVar(value=DEFAULT_OUTPUT_DIRECTORY)
        self.status_var = tk.StringVar(value="请选择微信文件目录，然后先扫描预览。")
        self.summary_var = tk.StringVar(value="尚未扫描")

        self._build_ui()
        self.after(100, self._poll_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    @staticmethod
    def _default_directory() -> str:
        if PREFERRED_DIRECTORY.is_dir() and find_month_directories(PREFERRED_DIRECTORY):
            return str(PREFERRED_DIRECTORY)
        candidates = discover_wechat_directory_info()
        if not candidates:
            return ""
        selected = max(candidates, key=lambda info: (info.month_count, info.total_files, str(info.path).casefold()))
        return str(selected.path)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)

        path_frame = ttk.LabelFrame(outer, text="1. 操作目录", padding=10)
        path_frame.pack(fill=tk.X)
        path_entry = ttk.Entry(path_frame, textvariable=self.directory_var)
        path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(path_frame, text="浏览…", command=self._browse).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(path_frame, text="自动发现与统计…", command=self._show_discovered_directories).pack(side=tk.LEFT, padx=(8, 0))

        options = ttk.LabelFrame(outer, text="2. 扫描选项", padding=10)
        options.pack(fill=tk.X, pady=(10, 0))

        ttk.Label(options, text="比较范围").grid(row=0, column=0, sticky="w")
        ttk.Combobox(options, textvariable=self.scope_var, values=list(SCOPE_VALUES), state="readonly", width=30).grid(row=1, column=0, sticky="ew", padx=(0, 12))
        ttk.Label(options, text="保留策略").grid(row=0, column=1, sticky="w")
        ttk.Combobox(options, textvariable=self.keep_var, values=list(KEEP_VALUES), state="readonly", width=39).grid(row=1, column=1, sticky="ew", padx=(0, 12))
        ttk.Label(options, text="内容哈希").grid(row=0, column=2, sticky="w")
        ttk.Combobox(options, textvariable=self.hash_var, values=("sha256", "blake2b"), state="readonly", width=12).grid(row=1, column=2, sticky="ew", padx=(0, 12))
        ttk.Label(options, text="最小文件大小（MB）").grid(row=0, column=3, sticky="w")
        ttk.Entry(options, textvariable=self.min_size_var, width=12).grid(row=1, column=3, sticky="ew", padx=(0, 12))
        ttk.Label(options, text="移动目的地").grid(row=0, column=4, sticky="w")
        ttk.Entry(options, textvariable=self.output_var, width=24).grid(row=1, column=4, sticky="ew")
        ttk.Button(options, text="浏览…", command=self._browse_output).grid(row=1, column=5, sticky="w", padx=(6, 0))
        ttk.Checkbutton(options, text="递归扫描月份目录下的子文件夹（默认关闭）", variable=self.recursive_var).grid(row=2, column=0, columnspan=3, sticky="w", pady=(9, 0))
        ttk.Label(options, text="所有选项变化后都必须重新扫描；执行时还会再次校验大小、时间和哈希。", foreground="#555555").grid(row=2, column=2, columnspan=3, sticky="e", pady=(9, 0))
        options.columnconfigure(0, weight=2)
        options.columnconfigure(1, weight=2)

        actions = ttk.Frame(outer)
        actions.pack(fill=tk.X, pady=10)
        self.scan_button = ttk.Button(actions, text="扫描并生成计划", command=self._start_scan)
        self.scan_button.pack(side=tk.LEFT)
        self.execute_button = ttk.Button(actions, text="执行移动", command=self._start_execute, state=tk.DISABLED)
        self.execute_button.pack(side=tk.LEFT, padx=(8, 0))
        self.cancel_button = ttk.Button(actions, text="取消当前任务", command=self.cancel_event.set, state=tk.DISABLED)
        self.cancel_button.pack(side=tk.LEFT, padx=(8, 0))
        self.progress = ttk.Progressbar(actions, mode="determinate", maximum=100)
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(16, 0))

        ttk.Label(outer, textvariable=self.summary_var, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        ttk.Label(outer, textvariable=self.status_var, foreground="#444444").pack(anchor="w", pady=(2, 8))

        notebook = ttk.Notebook(outer)
        notebook.pack(fill=tk.BOTH, expand=True)

        stats_frame = ttk.Frame(notebook, padding=6)
        stats_columns = ("month", "total", "eligible", "duplicates", "duplicate_size")
        self.stats_tree = ttk.Treeview(stats_frame, columns=stats_columns, show="headings", selectmode="browse")
        self.stats_tree.heading("month", text="月份")
        self.stats_tree.heading("total", text="文件数")
        self.stats_tree.heading("eligible", text="参与比较")
        self.stats_tree.heading("duplicates", text="重复文件")
        self.stats_tree.heading("duplicate_size", text="重复大小")
        self.stats_tree.column("month", width=130, anchor="center")
        self.stats_tree.column("total", width=120, anchor="e")
        self.stats_tree.column("eligible", width=120, anchor="e")
        self.stats_tree.column("duplicates", width=120, anchor="e")
        self.stats_tree.column("duplicate_size", width=160, anchor="e")
        stats_scroll = ttk.Scrollbar(stats_frame, orient=tk.VERTICAL, command=self.stats_tree.yview)
        self.stats_tree.configure(yscrollcommand=stats_scroll.set)
        self.stats_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        stats_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        notebook.add(stats_frame, text="月份统计")

        plan_frame = ttk.Frame(notebook, padding=6)
        columns = ("size", "keep", "duplicate", "destination")
        self.tree = ttk.Treeview(plan_frame, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("size", text="大小")
        self.tree.heading("keep", text="保留文件")
        self.tree.heading("duplicate", text="待移动文件")
        self.tree.heading("destination", text="目标位置")
        self.tree.column("size", width=90, stretch=False, anchor="e")
        self.tree.column("keep", width=300)
        self.tree.column("duplicate", width=300)
        self.tree.column("destination", width=330)
        tree_scroll = ttk.Scrollbar(plan_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        notebook.add(plan_frame, text="详细移动计划")

        log_frame = ttk.Frame(notebook, padding=6)
        self.log = tk.Text(log_frame, height=8, wrap="word", state=tk.DISABLED)
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        self.log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        log_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        notebook.add(log_frame, text="日志与警告")

    def _browse(self) -> None:
        selected = filedialog.askdirectory(title="选择包含 YYYY-MM 子文件夹的微信文件目录")
        if selected:
            self.directory_var.set(selected)

    def _browse_output(self) -> None:
        selected = filedialog.askdirectory(
            title="选择重复文件的移动目的地",
            initialdir=self.output_var.get() if Path(self.output_var.get()).is_dir() else None,
        )
        if selected:
            self.output_var.set(selected)

    def _show_discovered_directories(self) -> None:
        self.status_var.set("正在按旧版规则发现微信目录并统计容量……")
        self.update_idletasks()
        try:
            candidates = discover_wechat_directory_info()
        except Exception as exc:
            messagebox.showerror("自动发现失败", str(exc), parent=self)
            return
        if not candidates:
            self.status_var.set("未自动发现微信文件目录，可使用“浏览”手动选择。")
            messagebox.showinfo("自动发现", "未发现包含合法月份文件夹的微信文件目录。", parent=self)
            return

        dialog = tk.Toplevel(self)
        dialog.title("自动发现的微信文件目录")
        dialog.geometry("1050x380")
        dialog.transient(self)
        dialog.grab_set()
        frame = ttk.Frame(dialog, padding=10)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text="以下统计与旧版命令行目录列表一致。选择一行后点击“使用此目录”。").pack(anchor="w", pady=(0, 8))
        columns = ("version", "user", "months", "files", "size", "path")
        tree = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "version": "版本", "user": "用户 ID", "months": "月份数",
            "files": "文件数", "size": "总大小", "path": "路径",
        }
        widths = {"version": 95, "user": 180, "months": 75, "files": 85, "size": 105, "path": 430}
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(column, width=widths[column], anchor="e" if column in {"months", "files", "size"} else "w")
        for index, info in enumerate(candidates):
            tree.insert("", tk.END, iid=str(index), values=(
                info.version, info.user_id, info.month_count, info.total_files,
                format_size(info.total_size), str(info.path),
            ))
        tree.pack(fill=tk.BOTH, expand=True)
        tree.selection_set("0")

        buttons = ttk.Frame(frame)
        buttons.pack(fill=tk.X, pady=(10, 0))

        def choose() -> None:
            selection = tree.selection()
            if not selection:
                return
            info = candidates[int(selection[0])]
            self.directory_var.set(str(info.path))
            self.status_var.set(
                f"已选择 {info.version}：{info.user_id}，{info.month_count} 个月份，"
                f"{info.total_files} 个文件，{format_size(info.total_size)}。"
            )
            dialog.destroy()

        ttk.Button(buttons, text="使用此目录", command=choose).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side=tk.RIGHT, padx=(0, 8))
        tree.bind("<Double-1>", lambda _event: choose())
        self.status_var.set(f"发现 {len(candidates)} 个微信文件目录。")

    def _signature(self) -> tuple:
        return (
            self.directory_var.get().strip(),
            self.scope_var.get(),
            self.keep_var.get(),
            self.hash_var.get(),
            self.recursive_var.get(),
            self.min_size_var.get().strip(),
            self.output_var.get().strip(),
        )

    def _build_options(self) -> ScanOptions:
        try:
            min_mb = float(self.min_size_var.get().strip() or "0")
        except ValueError as exc:
            raise ValueError("最小文件大小必须是数字") from exc
        if min_mb < 0:
            raise ValueError("最小文件大小不能为负数")
        return ScanOptions(
            scope=SCOPE_VALUES[self.scope_var.get()],
            recursive=self.recursive_var.get(),
            min_size=int(min_mb * 1024 * 1024),
            keep_policy=KEEP_VALUES[self.keep_var.get()],
            hash_algorithm=self.hash_var.get(),
            output_directory=self.output_var.get().strip(),
        )

    def _set_busy(self, busy: bool) -> None:
        self.scan_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self.cancel_button.configure(state=tk.NORMAL if busy else tk.DISABLED)
        if busy:
            self.execute_button.configure(state=tk.DISABLED)
        elif self.scan_result and self.scan_result.move_items:
            self.execute_button.configure(state=tk.NORMAL)

    def _clear_plan(self) -> None:
        self.scan_result = None
        self.scan_signature = None
        self.execute_button.configure(state=tk.DISABLED)
        for item in self.tree.get_children():
            self.tree.delete(item)
        for item in self.stats_tree.get_children():
            self.stats_tree.delete(item)

    def _append_log(self, message: str) -> None:
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, message.rstrip() + "\n")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def _start_scan(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        directory = self.directory_var.get().strip().strip('"')
        if not directory:
            messagebox.showerror("缺少目录", "请先选择操作目录。")
            return
        try:
            options = self._build_options()
            _, warnings = validate_base_directory(directory)
        except (FileUniquefyError, OSError, ValueError) as exc:
            messagebox.showerror("无法扫描", str(exc))
            return
        if warnings and not messagebox.askyesno("目录警告", "\n".join(warnings) + "\n\n仍要只读扫描吗？"):
            return

        self._clear_plan()
        self.cancel_event.clear()
        self.progress["value"] = 0
        self.summary_var.set("正在扫描……")
        self.status_var.set("正在按文件大小分组并计算内容哈希。扫描阶段不会移动文件。")
        self._set_busy(True)
        signature = self._signature()

        def progress(phase: str, current: int, total: int, message: str) -> None:
            self.events.put(("progress", phase, current, total, message))

        def work() -> None:
            try:
                result = scan_directory(directory, options, progress, self.cancel_event.is_set)
                self.events.put(("scan_done", result, signature))
            except Exception as exc:
                self.events.put(("error", "扫描失败", str(exc)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _start_execute(self) -> None:
        if not self.scan_result or not self.scan_result.move_items:
            return
        if self._signature() != self.scan_signature:
            self._clear_plan()
            messagebox.showwarning("需要重新扫描", "目录或选项已经改变，请重新扫描后再执行。")
            return
        count = len(self.scan_result.move_items)
        warning = ""
        if count > WARN_FILE_COUNT:
            warning = f"\n\n高风险提示：本次将移动 {count} 个文件。"
        confirmed = messagebox.askyesno(
            "确认执行移动",
            f"即将逐项复核并移动 {count} 个重复文件，约 {format_size(self.scan_result.duplicate_bytes)}。"
            f"\n不会覆盖已有文件，并会写入操作日志。{warning}\n\n是否继续？",
            icon="warning",
        )
        if not confirmed:
            return

        self.cancel_event.clear()
        self.progress["value"] = 0
        self.status_var.set("正在复核每个文件并执行移动……")
        self._set_busy(True)

        def progress(phase: str, current: int, total: int, message: str) -> None:
            self.events.put(("progress", phase, current, total, message))

        def work() -> None:
            try:
                execution = execute_plan(self.scan_result, progress, self.cancel_event.is_set)
                self.events.put(("execute_done", execution))
            except Exception as exc:
                self.events.put(("error", "执行失败", str(exc)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "progress":
                    _, phase, current, total, message = event
                    self.progress["value"] = (current / total * 100) if total else 0
                    action = "扫描" if phase == "scan" else "移动"
                    self.status_var.set(f"{action} {current}/{total}: {message}")
                elif kind == "scan_done":
                    _, result, signature = event
                    self.scan_result = result
                    self.scan_signature = signature
                    for summary in result.month_summaries:
                        self.stats_tree.insert("", tk.END, values=(
                            summary.month,
                            summary.total_files,
                            summary.eligible_files,
                            summary.duplicate_files if summary.duplicate_files else "-",
                            format_size(summary.duplicate_bytes) if summary.duplicate_bytes else "-",
                        ))
                    for item in result.move_items:
                        self.tree.insert("", tk.END, values=(
                            format_size(item.duplicate.size),
                            str(item.keep.path),
                            str(item.duplicate.path),
                            str(item.destination),
                        ))
                    self.summary_var.set(
                        f"共 {result.total_files} 个文件；发现 {len(result.move_items)} 个重复文件；"
                        f"预计释放 {format_size(result.duplicate_bytes)}"
                    )
                    for warning in result.warnings:
                        self._append_log(f"[警告] {warning}")
                    for error in result.errors:
                        self._append_log(f"[跳过] {error}")
                    self.progress["value"] = 100
                    self.status_var.set("扫描完成。请核对移动计划；只有点击“执行移动”并再次确认后才会改动文件。")
                    self._set_busy(False)
                elif kind == "execute_done":
                    execution = event[1]
                    self._append_log(f"[完成] 已移动 {len(execution.moved)} 个文件。")
                    if execution.journal_path:
                        self._append_log(f"[日志] {execution.journal_path}")
                    for failure in execution.failures:
                        self._append_log(f"[失败] {failure}")
                    self.summary_var.set(f"已移动 {len(execution.moved)} 个文件，失败 {len(execution.failures)} 个。")
                    self.status_var.set("执行结束。文件状态已改变，如需再次操作请重新扫描。")
                    self._clear_plan()
                    self._set_busy(False)
                    if execution.failures:
                        messagebox.showwarning("执行部分完成", "部分文件未能移动，请查看日志。")
                    else:
                        messagebox.showinfo("执行完成", f"已安全移动 {len(execution.moved)} 个重复文件。")
                elif kind == "error":
                    _, title, message = event
                    self.status_var.set(message)
                    self._append_log(f"[错误] {message}")
                    self._set_busy(False)
                    messagebox.showerror(title, message)
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("任务仍在运行", "要请求取消并关闭窗口吗？"):
                return
            self.cancel_event.set()
        self.destroy()


if __name__ == "__main__":
    app = FileUniquefyApp()
    if "--smoke-test" in sys.argv:
        app.withdraw()
        app.update_idletasks()
        app.destroy()
        print("GUI smoke test passed")
    else:
        app.mainloop()
