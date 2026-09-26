"""
.env 加载器，零依赖（不引 python-dotenv）。

为什么自己写：本仓库刻意只依赖 aiohttp，让教程代码保持"一眼看得完"。
这里 30 行就够用了，不值得为此多一个依赖。

优先级（重要）：
    命令行 export / 系统环境变量  >  .env 文件  >  代码里的默认值
即：.env 只补不覆盖。临时换 key 或换模型时，export 一下就能压过 .env，
不用去改文件、也不用担心把配置提交上去。

解析规则：
    - 读本文件同目录下的 .env，找不到再看当前工作目录
    - 忽略空行和 # 开头的注释行
    - 剥掉一层成对引号，兼容 `export KEY=...` 写法
    - 只剥未加引号时的行内注释（`KEY=value # 说明`），引号内的 # 不当注释

使用姿势（顺序不能反）：
    from env_loader import load_env
    load_env()                      # 必须先于读配置的 import
    import agentic_workflows        # 它在模块级就读 os.getenv
因为 agentic_workflows / check_provider / *_prod 都在 import 时就把配置求值成
模块级常量了，.env 晚加载一步就完全不生效。
"""

import os
from pathlib import Path


def _parse(text):
    entries = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # trailing inline comment, only when unquoted
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        if key:
            entries.append((key, value))
    return entries


def load_env(path=None):
    """Populate os.environ from .env without clobbering existing values."""
    candidates = [Path(path)] if path else [Path(__file__).with_name(".env"), Path.cwd() / ".env"]
    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
        except OSError:
            continue
        loaded = 0
        for key, value in _parse(candidate.read_text(encoding="utf-8", errors="replace")):
            if key not in os.environ:
                os.environ[key] = value
                loaded += 1
        return str(candidate), loaded
    return None, 0


def load_env_verbose(path=None):
    """Same as load_env(), plus a one-line summary for CLI scripts."""
    source, count = load_env(path)
    if source:
        print(f"[env] 已加载 {source}（{count} 项）；命令行 export 的值优先")
    return source, count
