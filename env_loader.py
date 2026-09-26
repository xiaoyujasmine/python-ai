"""
Minimal, zero-dependency .env loader.

Why not python-dotenv: the repo deliberately keeps `aiohttp` as its only
dependency so the tutorial code stays readable. This is ~30 lines and
covers what we need.

Rules:
    - reads `.env` next to this file (falls back to the CWD)
    - ignores blank lines and `#` comments
    - strips one layer of matching quotes, supports `export KEY=...`
    - never overrides a variable that is already set in the real
      environment, so `export LLM_API_KEY=...` still wins
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
