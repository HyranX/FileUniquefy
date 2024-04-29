# FileUniquefy 文件去重工具

这个工具用于识别和移动指定目录下重复的文件。它提供了两种方式来判断文件是否重复：通过文件的MD5值或文件大小。

## 功能描述

- **MD5检测**：通过计算文件的MD5哈希值来检查文件是否重复。
- **文件大小检测**：通过比较文件的大小来检查文件是否重复。
- **自动处理**：将识别出的较旧的重复文件自动移动到指定目录下的`pack`文件夹。

## 使用方法

1. 确保你的Python环境已安装。
3. 在代码中设置你的目标目录。
4. 运行脚本。默认情况下，脚本使用MD5哈希来识别重复文件，你可以修改 `use_md5` 变量为 `False` 来改用文件大小进行判断。

## 环境需求
Python 3.x
hashlib (通常随Python安装)

## 贡献
欢迎通过GitHub Issues和Pull Requests提供反馈和贡献。