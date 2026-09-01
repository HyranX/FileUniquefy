# FileUniquefy 微信文件去重工具

FileUniquefy 扫描微信文件目录下合法的 `YYYY-MM` 月份文件夹，通过文件大小和强内容哈希识别重复文件。扫描阶段只生成计划；用户明确确认后，重复文件才会移动到配置的目的地。当前默认目的地为 `E:\Down\weixin`。

## 旧版功能完整保留

- 自动扫描常见微信目录，兼容新版 `xwechat_files` 和旧版 `WeChat Files`。
- 自动发现结果显示微信版本、用户 ID、月份数、文件数、总大小和完整路径。
- 选定目录后显示每个月份的文件数、参与比较数、重复文件数和重复容量，并显示合计。
- 展示每个重复组的保留文件、待移动文件和目标路径。
- 默认优先保留不带 `(N)` 后缀的原始文件名，再保留修改时间最早者。
- 默认只处理月份文件夹第一层，不递归处理子文件夹。
- 命令行版执行或取消后可返回主界面重新选择目录。
- GUI 可随时重新选择目录，并提供“自动发现与统计”窗口、“月份统计”页和“详细移动计划”页。

## 目录结构

```text
FileUniquefy/
├── 启动命令行版.cmd
├── cli/
│   ├── FileUniquefy.py
│   └── 启动命令行版.cmd
├── gui/
│   ├── FileUniquefyGUI.py
│   └── 启动图形界面.cmd
├── shared/
│   └── fileuniquefy_core.py
└── tests/
    └── test_core.py
```

命令行版和图形界面版位于两个独立子文件夹，但共同调用 `shared/fileuniquefy_core.py`。文件识别、计划生成、执行前复核和安全移动只有一套实现，避免两个界面的算法不一致。

## 快速启动

直接双击：

- 根目录 `启动命令行版.cmd`：交互式命令行版快捷入口。
- `cli/启动命令行版.cmd`：命令行版目录内入口。
- `gui/启动图形界面.cmd`：图形界面版。

两个 CMD 不只依赖当前终端的 PATH，会依次查找：

1. `%SCOOP%\apps\python\current\python.exe`
2. `D:\Scoop\apps\python\current\python.exe`
3. `%USERPROFILE%\scoop\apps\python\current\python.exe`
4. PATH 中的 `python`

因此，即使 VS Code 主进程尚未刷新 PATH，也可以正常启动。

## VS Code Terminal 与系统 CMD 为什么不同

Windows 进程启动时会复制一份环境变量。VS Code 的集成终端继承 VS Code 主进程启动时的环境快照；安装 Python或修改 PATH 后，新开的系统 CMD 会读取新环境，但未重启的 VS Code 及其新建终端仍可能使用旧快照。

解决方法：

1. 完全关闭所有 VS Code 窗口。
2. 重新启动 VS Code。
3. 在新终端运行 `where python` 和 `python --version`。

无需等待环境刷新时，可直接使用本项目提供的 CMD。

## 图形界面选项

- **比较范围**：默认只在同一月份内比较；可选择跨月份比较。
- **保留策略**：默认优先保留不带 `(N)` 后缀的原始文件名，再按修改时间保留最旧文件；也可只按最旧或最新时间保留。
- **内容哈希**：默认 SHA-256，也可选择 BLAKE2b。
- **最小文件大小**：忽略小于指定大小的文件。
- **递归扫描**：默认关闭，只处理月份目录第一层；启用后才扫描子文件夹。
- **移动目的地**：默认 `E:\Down\weixin`，可通过 GUI 浏览按钮或命令行 `--output-dir` 修改。

GUI 必须先扫描并展示完整移动计划。修改任意选项后，旧计划不能执行，必须重新扫描。

点击“自动发现与统计…”可查看所有检测到的微信目录，以及各目录的版本、账号、月份数、文件数和总大小。扫描选定目录后，“月份统计”页显示逐月结果，“详细移动计划”页显示具体保留与移动路径。

当前电脑的 GUI 默认操作目录为 `D:\Document\xwechat_files\bachopin_bdc8\msg\file`。如果该目录以后不存在，GUI 会按旧版脚本的发现规则重新查找，并选择有效月份最多的微信目录。

## 命令行用法

```powershell
# 交互式选择目录
& '.\cli\启动命令行版.cmd'

# 指定目录，只生成计划
& '.\cli\启动命令行版.cmd' 'D:\document\WeChat Files\账号\FileStorage\File' --dry-run

# 跨月份比较并递归扫描；确认后才执行
& '.\cli\启动命令行版.cmd' 'D:\document\WeChat Files\账号\FileStorage\File' --scope all --recursive

# 指定其他移动目的地
& '.\cli\启动命令行版.cmd' 'D:\document\WeChat Files\账号\FileStorage\File' --output-dir 'E:\Down\weixin'
```

其他参数可运行：

```powershell
& '.\cli\启动命令行版.cmd' --help
```

## 文件操作安全设计

本项目对原算法做了以下加固：

1. **强哈希**：先按大小分组，只对可能重复的文件计算 SHA-256 或 BLAKE2b，不再使用 MD5。
2. **严格月份**：只接受月份为 `01` 至 `12` 的 `YYYY-MM` 文件夹。
3. **确定性保留**：相同内容组使用明确排序和路径兜底，结果不依赖文件系统枚举顺序。
4. **不跟随链接**：跳过符号链接和 Windows 目录联接点，防止读写逃逸到操作目录外。
5. **危险路径阻断**：禁止磁盘根目录、用户主目录本身、Windows、Program Files、ProgramData 等系统目录及其子目录。
6. **执行前重新验证**：每次移动前复核保留文件和待移动文件的大小、纳秒修改时间及完整哈希；扫描后有任何变化就停止。
7. **绝不覆盖**：执行时使用独占占位文件保留目标名；若目标名已存在则自动生成新名称。
8. **路径边界复核**：目标目录创建后再次解析真实路径，必须仍位于输出文件夹内。
9. **跨卷安全复制**：D→E 等跨磁盘操作先复制临时文件并验证完整哈希，目标安全落位且源文件再次复核后才删除源文件。
10. **逐项日志**：每次执行在 `E:\Down\weixin\.fileuniquefy-journals\` 写入 JSON Lines 日志，记录保留、来源、目标、大小和哈希。
11. **默认保守**：默认不递归、不跨月份；扫描与执行分离，移动前再次确认。

目的地中的文件不是永久删除，可在确认无误后由用户自行清理。

## 测试

```powershell
& 'D:\Scoop\apps\python\current\python.exe' -m unittest discover -s tests -v
```

自动化测试覆盖：同大小不同内容、原始文件名优先、非法月份、递归开关、跨月份比较、目标同名冲突、扫描后源文件变化、执行日志和符号链接跳过。

## 环境需求

- Python 3.10 或更高版本
- 仅使用 Python 标准库
- GUI 使用 Python 自带的 Tkinter
