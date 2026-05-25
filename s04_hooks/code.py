#!/usr/bin/env python3
"""
s04: Hooks — move extension logic out of the loop, onto hooks.

  User types query
       │
       ▼
  ┌──────────────────┐
  │ UserPromptSubmit │ ── trigger_hooks() before LLM
  └────────┬─────────┘
           ▼
  ┌────────────┐     ┌─────────────────────────────┐
  │  messages  │────▶│  LLM (stop_reason=tool_use?)│
  └────────────┘     │   No ──▶ Stop hooks ──▶ exit │
                     │   Yes ──▶ tool_use block ──┐ │
                     └────────────────────────────┘ │
                                                    ▼
                                          ┌──────────────────┐
                                          │ trigger_hooks()   │
                                          │  PreToolUse:      │
                                          │   permission_hook │
                                          │   log_hook        │
                                          └───────┬──────────┘
                                                  │ (not blocked)
                                          ┌───────▼──────────┐
                                          │ TOOL_HANDLERS[x]  │
                                          └───────┬──────────┘
                                                  │
                                          ┌───────▼──────────┐
                                          │ trigger_hooks()   │
                                          │  PostToolUse:     │
                                          │   large_output    │
                                          └───────┬──────────┘
                                                  │
                                          results ──▶ back to messages

Changes from s03:
  + HOOKS registry (event -> list of callbacks)
  + register_hook() / trigger_hooks()
  + context_inject_hook (UserPromptSubmit)
  + permission_hook, log_hook (PreToolUse)
  + large_output_hook (PostToolUse)
  + summary_hook (Stop)
  - check_permission() removed from loop body
    (logic moved into permission_hook, triggered via PreToolUse)

Run: python s04_hooks/code.py
Needs: pip install anthropic python-dotenv + ANTHROPIC_API_KEY in .env
"""

import os, subprocess, sys
from pathlib import Path
from datetime import datetime
import requests
import glob
import json
import re
import time

try:
    import readline

    readline.parse_and_bind("set bind-tty-special-chars off")
    readline.parse_and_bind("set input-meta on")
    readline.parse_and_bind("set output-meta on")
    readline.parse_and_bind("set convert-meta off")
except ImportError:
    pass

from anthropic import Anthropic
from dotenv import load_dotenv
from collections import Counter

load_dotenv(override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

WORKDIR = Path.cwd()
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. Act, don't explain."

DEBUG = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")
DEBUG_DIR = WORKDIR / ".debug"

# ═══════════════════════════════════════════════════════════
#  FROM s03: Permission (check_permission used by permission_hook)
# ═══════════════════════════════════════════════════════════

DENY_LIST = [
    "rm -rf",
    "sudo",
    "shutdown",
    "reboot",
    "mkfs",
    "dd if=",
    "> /dev/sda",
    "rd /s /q",
    "rmdir /s /q",
    "del /f /s /q C:\*.*",
    "format-volume",
    "shutdown /s",
    "reg delete HKCR\\...",
    "del \%systemroot\%\system32\*.* /f /s /q",
]

# 硬拒：永远不允许改/删
HARD_SENSITIVE = {
    ".env",
    ".env.example",
    ".git/",  # 目录前缀
    "credentials",  # 文件名前缀，如 credentials.json
}

# 软敏感：弹窗询问
SOFT_SENSITIVE = {
    ".gitignore",
    "config.json",
    "config.yaml",
    "config.yml",
    "config.ini",
    "config.toml",
    "config.xml",
}

PATH_MUTATING_TOOLS = {
    "write_file",
    "edit_file",
    "append_file",
    "apply_patch",
    "safe_delete_file",
}

# 工具使用时间
TOOL_USE_TIMES = {}


# 直接拒绝权限判断
def check_deny_list(command: str) -> str | None:
    for pattern in DENY_LIST:
        if pattern in command:
            return f"Blocked: '{pattern}' is on the deny list"
    return None


# 权限判断规则
PERMISSION_RULES = [
    {
        "tools": ["write_file", "edit_file", "append_file", "apply_patch"],
        "check": lambda args: not (WORKDIR / args.get("path", ""))
        .resolve()
        .is_relative_to(WORKDIR),
        "message": "Writing outside workspace",
    },
    {
        "tools": ["bash"],
        "check": lambda args: any(
            kw in args.get("command", "")
            for kw in [
                "rm ",
                "> /etc/",
                "chmod 777",
                "Remove-Item",
                "Remove-ItemProperty",
                "Remove-ItemType",
                "Remove-ItemProperty",
                "Remove-ItemType",
                "Remove-ItemProperty",
                "Remove-ItemType",
            ]
        ),
        "message": "Potentially destructive command",
    },
]


def check_rules(tool_name: str, args: dict) -> str | None:
    for rule in PERMISSION_RULES:
        if tool_name in rule["tools"] and rule["check"](args):
            return rule["message"]
    return None


# 用户审批
def ask_user(tool_name: str, args: dict, reason: str) -> str:
    print(f"\n⚠  {reason}")
    print(f"   Tool: {tool_name}({args})")
    choice = input("   Allow? [y/N] ").strip().lower()
    return "allow" if choice in ("y", "yes") else "deny"


def _resolve_rel_path(path: str) -> str | None:
    """Return workspace-relative posix path, or None if outside workspace."""
    try:
        return (WORKDIR / path).resolve().relative_to(WORKDIR).as_posix()
    except ValueError:
        return None


def _matches_sensitive_pattern(rel: str, patterns: set[str]) -> bool:
    name = Path(rel).name
    for raw in patterns:
        if raw.endswith("/"):
            base = raw.rstrip("/")
            if rel == base or rel.startswith(base + "/"):
                return True
            continue

        if raw.startswith("."):
            if rel == raw or name == raw or name.startswith(raw + "."):
                return True
        elif name == raw or name.startswith(raw + ".") or name.startswith(raw + "_"):
            return True
    return False


def is_hard_sensitive(path: str) -> bool:
    rel = _resolve_rel_path(path)
    if rel is None:
        return False  # 工作区外不在这里硬拒，交给 check_rules
    return _matches_sensitive_pattern(rel, HARD_SENSITIVE)


def is_soft_sensitive(path: str) -> bool:
    rel = _resolve_rel_path(path)
    if rel is None:
        return False
    return _matches_sensitive_pattern(rel, SOFT_SENSITIVE)
    


# ═══════════════════════════════════════════════════════════
#  FROM s01 (unchanged)
# ═══════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════
#  FROM s03: Tool Implementations (16 tools)
# ═══════════════════════════════════════════════════════════


def run_bash(command: str, command_timeout: int | None = None) -> str:
    """Run a shell command in the workspace directory.

    Args:
        command: The shell command to execute.
        command_timeout: Maximum execution time in seconds (capped at 120).

    Returns:
        Command output (stdout + stderr), or an error message.
    """
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    timeout = 120 if command_timeout is None else max(1, min(command_timeout, 120))
    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: Timeout ({timeout}s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  NEW in s02: 4 个新工具
# ═══════════════════════════════════════════════════════════


def safe_path(p: str) -> Path:
    """Resolve a relative path and ensure it stays within WORKDIR.

    Args:
        p: Relative path string.

    Returns:
        Resolved Path object.

    Raises:
        ValueError: If the resolved path escapes the workspace.
    """
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_read(path: str, limit: int | None = None, offset: int = 1) -> str:
    """Read file contents with optional line limit and offset.

    Args:
        path: Path to the file (relative to WORKDIR).
        limit: Maximum number of lines to read.
        offset: Starting line number (1-based).

    Returns:
        File content as a string, or an error message.
    """
    try:
        lines = (
            safe_path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        )
        start = max(offset - 1, 0)
        if limit is not None:
            lines = lines[start : start + int(limit)]
        else:
            lines = lines[start:]
        return "\n".join(lines) if lines else "(no lines)"
    except Exception as e:
        return f"Error: {e}"


def run_write(path: str, content: str) -> str:
    """Write content to a file, creating parent directories if needed.

    Args:
        path: Path to the file (relative to WORKDIR).
        content: Text content to write.

    Returns:
        Success message with byte count, or an error message.
    """
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8", errors="replace")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


def run_append_file(path: str, content: str) -> str:
    """Append content to a file, creating parent directories if needed."""
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(
            file_path.read_text(encoding="utf-8", errors="replace") + content,
            encoding="utf-8",
            errors="replace",
        )
        return f"Appended {len(content)} bytes to {path}, new file size: {file_path.stat().st_size} bytes"
    except Exception as e:
        return f"Error: {e}"


def run_edit(path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
    """Replace exact text in a file once or all occurrences.

    Args:
        path: Path to the file (relative to WORKDIR).
        old_text: Text to search for.
        new_text: Replacement text.
        replace_all: If True, replace all occurrences; otherwise replace first.

    Returns:
        Success message with replacement count, or an error message.
    """
    try:
        file_path = safe_path(path)
        text = file_path.read_text(encoding="utf-8", errors="replace")
        if old_text not in text:
            return f"Error: text not found in {path}"
        if replace_all:
            file_path.write_text(
                text.replace(old_text, new_text), encoding="utf-8", errors="replace"
            )
            return f"Edited {path} (replaced all occurrences)"
        else:
            file_path.write_text(
                text.replace(old_text, new_text, 1), encoding="utf-8", errors="replace"
            )
            return f"Edited {path} (replaced first occurrence)"
    except Exception as e:
        return f"Error: {e}"


def run_glob(pattern: str) -> str:
    """Find files matching a glob pattern within the workspace.

    Args:
        pattern: Glob pattern string.

    Returns:
        Newline-separated list of matching paths, or an error message.
    """
    import glob as g

    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


def run_list_dir(path: str = ".", recursive: bool = False) -> str:
    """List directory contents, optionally recursively.

    Args:
        path: Directory path (relative to WORKDIR).
        recursive: If True, list subdirectories recursively.

    Returns:
        Newline-separated list of paths, or an error message.
    """
    try:
        curr_path = safe_path(path)
        if not curr_path.is_dir():
            return f"Error: {path} is not a directory"
        results = []
        for item in curr_path.iterdir():
            if recursive and item.is_dir():
                sub = run_list_dir(item.as_posix(), recursive)
                if sub and sub != "(no items)":
                    results.extend(sub.splitlines())
            else:
                results.append(item.relative_to(WORKDIR).as_posix())
        return "\n".join(results) if results else "(no items)"
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  NEW in s02: 新增URL抓取工具
# ═══════════════════════════════════════════════════════════
def run_web_fetch(url: str) -> str:
    """Fetch web contents from a URL.

    Args:
        url: HTTP or HTTPS URL to fetch.

    Returns:
        Response text (truncated to 50000 chars), or an error message.
    """
    # only allow http or https urls
    if not (url.startswith("http://") or url.startswith("https://")):
        return "Error: Invalid URL"
    if url.startswith("file://"):
        return "Error: file:// URLs are not allowed"
    if url.startswith("data://"):
        return "Error: data:// URLs are not allowed"
    if url.startswith("mailto://"):
        return "Error: mailto:// URLs are not allowed"
    if url.startswith("tel://"):
        return "Error: tel:// URLs are not allowed"
    if url.startswith("sms://"):
        return "Error: sms:// URLs are not allowed"
    if url.startswith("ftp://"):
        return "Error: ftp:// URLs are not allowed"
    if url.startswith("ftps://"):
        return "Error: ftps:// URLs are not allowed"
    if url.startswith("sftp://"):
        return "Error: sftp:// URLs are not allowed"
    if url.startswith("scp://"):
        return "Error: scp:// URLs are not allowed"
    if url.startswith("rsync://"):
        return "Error: rsync:// URLs are not allowed"
    if url.startswith("smb://"):
        return "Error: smb:// URLs are not allowed"
    if url.startswith("nfs://"):
        return "Error: nfs:// URLs are not allowed"
    if url.startswith("iscsi://"):
        return "Error: iscsi:// URLs are not allowed"
    if url.startswith("nvme://"):
        return "Error: nvme:// URLs are not allowed"
    try:
        response = requests.get(url, timeout=120)
        if response.status_code != 200:
            return f"Error: {response.status_code} {response.text[:50000]}"
        return response.text[:50000] if response.text else "(no content)"
    except requests.Timeout:
        return "Error: Timeout (120s)"
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  NEW in s02: 新增安全删除工具
# ═══════════════════════════════════════════════════════════
def run_safe_delete_file(path: str) -> str:
    """Delete a file safely within the workspace.

    Args:
        path: Path to the file (relative to WORKDIR).

    Returns:
        Success message, or an error message.
    """
    try:
        file_path = safe_path(path)
        if not file_path.exists():
            return f"Error: {path} does not exist"
        if file_path.is_dir():
            return f"Error: {path} is a directory"
        file_path.unlink()
        return f"Deleted file: {path}"
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  NEW in s02: 新增代码文件行数统计工具
# ═══════════════════════════════════════════════════════════
def run_count_lines(glob_pattern: str = "**/*.py") -> str:
    """Count lines in files matching a glob pattern.

    Args:
        glob_pattern: Glob pattern to match files (default: "**/*.py").

    Returns:
        JSON string mapping file paths to line counts, or an error message.
    """
    try:
        lines_count = {}
        for match in glob.glob(glob_pattern, root_dir=WORKDIR):
            file_path = (WORKDIR / match).resolve()
            if not file_path.is_relative_to(WORKDIR) or not file_path.is_file():
                continue
            content = file_path.read_text(encoding="utf-8", errors="replace")
            lines_count[match] = len(content.splitlines())
        return json.dumps(lines_count, indent=2, ensure_ascii=False)
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  NEW in s02: 新增Python代码文件运行工具
# ═══════════════════════════════════════════════════════════
def run_python(code: str) -> str:
    """Run Python code in a restricted subprocess.

    Args:
        code: Python code string to execute.

    Returns:
        Output from stdout/stderr, or an error message.
    """
    forbidden = [
        "import os",
        "import subprocess",
        "import sys",
        "os.system",
        "subprocess.run",
        "sys.exit",
        "__import__",
        "eval(",
        "exec(",
    ]
    for pattern in forbidden:
        if pattern in code:
            return f"Error: '{pattern}' is forbidden"
    if re.search(r"\bopen\s*\(", code):
        return "Error: Using open() is forbidden"
    try:
        r = subprocess.run(
            [sys.executable, "-c", code],
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=120,
            encoding="utf-8",
            errors="replace",
        )
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        if r.returncode != 0:
            return (
                f"Error (exit {r.returncode}): {out[:50000]}"
                if out
                else f"Error: exit code {r.returncode}"
            )
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  NEW in s02: 新增git状态查看工具
# ═══════════════════════════════════════════════════════════
def run_git_status() -> str:
    """Get the short status of the git repository.

    Returns:
        Git status output, or an error message.
    """
    try:
        r = subprocess.run(
            ["git", "status", "--short"],
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=120,
            encoding="utf-8",
            errors="replace",
        )
        if r.returncode != 0:
            return f"Error (exit {r.returncode}): {r.stderr.strip()[:50000]}"
        return r.stdout.strip()[:50000] if r.stdout else "(no output)"
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
# NEW in s02: 新增应用补丁工具
# ═══════════════════════════════════════════════════════════


def parse_search_replace_blocks(patch: str) -> list[tuple[str, str]]:
    """Parse SEARCH/REPLACE patch blocks from a patch string.

    Args:
        patch: A string containing one or more SEARCH/REPLACE blocks.
               Format:
               <<<<<<< SEARCH
               ...old text...
               =======
               ...new text...
               >>>>>>> REPLACE

    Returns:
        A list of (search, replace) tuples, one per block found.

    Raises:
        ValueError: If the patch format is invalid (missing separator,
                    missing REPLACE marker, empty SEARCH block,
                    or no blocks found).
    """
    MARKER_SEARCH = "<<<<<<< SEARCH"
    MARKER_SEP = "======="
    REPLACE_MARKERS = (">>>>>>> REPLACE", ">>>>>> REPLACE")

    lines = patch.replace("\r\n", "\n").splitlines()
    blocks: list[tuple[str, str]] = []
    i = 0

    while i < len(lines):
        if lines[i] != MARKER_SEARCH:
            i += 1
            continue

        i += 1
        search_lines: list[str] = []
        while i < len(lines) and lines[i] != MARKER_SEP:
            search_lines.append(lines[i])
            i += 1
        if i >= len(lines):
            raise ValueError("missing ======= separator")
        i += 1  # skip =======

        replace_lines: list[str] = []
        while i < len(lines) and lines[i] not in REPLACE_MARKERS:
            replace_lines.append(lines[i])
            i += 1
        if i >= len(lines):
            raise ValueError("missing >>>>>>> REPLACE marker")
        i += 1  # skip >>>>>>> REPLACE

        search = "\n".join(search_lines)
        replace = "\n".join(replace_lines)
        if not search:
            raise ValueError("empty SEARCH block")
        blocks.append((search, replace))

    if not blocks:
        preview = patch[:200].replace("\n", "\\n")
        raise ValueError(
            f"expected <<<<<<< SEARCH blocks; unified diff is not supported. "
            f"Got ({len(patch)} chars): {preview!r}"
        )
    return blocks


def run_apply_patch(path: str, patch: str) -> str:
    """Apply SEARCH/REPLACE patch blocks to a file.

    Args:
        path: Path to the file to patch (relative to WORKDIR).
        patch: Patch string containing one or more SEARCH/REPLACE blocks.
               Format:
               <<<<<<< SEARCH
               ...old text...
               =======
               ...new text...
               >>>>>>> REPLACE

    Returns:
        Success message with number of patches applied, or error message.
    """
    try:
        file_path = safe_path(path)
        text = file_path.read_text(encoding="utf-8")
        blocks = parse_search_replace_blocks(patch)
        for i, (search, replace) in enumerate(blocks, 1):
            count = text.count(search)
            if count == 0:
                return f"Error: block {i} not found in {path}:\n{search[:200]}"
            if count > 1:
                return (
                    f"Error: block {i} matches {count} locations; "
                    "add more context to SEARCH"
                )
            text = text.replace(search, replace, 1)
        file_path.write_text(text, encoding="utf-8")
        return f"Applied {len(blocks)} patch(es) to {path}"
    except ValueError as e:
        return f"Error: invalid patch: {e}"
    except Exception as e:
        return f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  NEW in s02: 工具定义（s01 只有一个 bash，现在扩展到 5 个）
# ═══════════════════════════════════════════════════════════


def run_json_read(path: str, key: str | None = None) -> str:
    """Read JSON data from a file, if a key is provided, return the value of the key, otherwise return the entire JSON object."""
    try:
        file_path = safe_path(path)
        text = file_path.read_text(encoding="utf-8")
        data = json.loads(text)
        value = data if key is None else data[key]
        return json.dumps(value, indent=2, ensure_ascii=False)
    except Exception as e:
        return f"Error: {e}"


def run_grep(pattern: str, path: str = ".", glob_pattern: str = "**/*") -> str:
    """Search for a pattern in files under a path.

    Args:
        pattern: Text pattern to search for (literal substring match).
        path: File or directory to search in (relative to WORKDIR).
        glob_pattern: Glob pattern for files when path is a directory.

    Returns:
        Matching lines as ``path:line: content``, or an error message.
    """
    try:
        root = safe_path(path)
        if root.is_file():
            files = [root]
        elif root.is_dir():
            files = [
                p
                for p in root.glob(glob_pattern)
                if p.is_file() and p.resolve().is_relative_to(WORKDIR)
            ]
        else:
            return f"Error: {path} not found"

        results: list[str] = []
        max_results = 500
        for file_path in files:
            if any(
                part in {".git", "__pycache__", "node_modules"}
                for part in file_path.parts
            ):
                continue
            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = file_path.relative_to(WORKDIR).as_posix()
            for line_no, line in enumerate(text.splitlines(), 1):
                if pattern in line:
                    results.append(f"{rel}:{line_no}: {line}")
                    if len(results) >= max_results:
                        results.append(f"... (truncated at {max_results} matches)")
                        return "\n".join(results)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


def run_file_info(path: str) -> str:
    """Get file size, line count, and last modified time."""
    try:
        file_path = safe_path(path)
        if not file_path.exists():
            return f"Error: {path} does not exist"
        if not file_path.is_file():
            return f"Error: {path} is not a file"
        stat = file_path.stat()
        content = file_path.read_text(encoding="utf-8", errors="replace")
        modified = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        return (
            f"path: {file_path.relative_to(WORKDIR).as_posix()}\n"
            f"size: {stat.st_size} bytes\n"
            f"lines: {len(content.splitlines())}\n"
            f"modified: {modified}"
        )
    except Exception as e:
        return f"Error: {e}"


TOOLS = [
    {
        "name": "bash",
        "description": "Run a shell command.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "command_timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (max 120)",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read file contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "limit": {"type": "integer"},
                "offset": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "Replace exact text in a file once or all occurrences.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "glob",
        "description": "Find files matching a glob pattern.",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string"}},
            "required": ["pattern"],
        },
    },
    {
        "name": "list_dir",
        "description": "List directory contents.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "recursive": {"type": "boolean"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "web_fetch",
        "description": "Fetch web contents from a URL.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "safe_delete_file",
        "description": "Delete a file safely.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "count_lines",
        "description": "Count lines in a file or files matching a glob pattern.",
        "input_schema": {
            "type": "object",
            "properties": {"glob_pattern": {"type": "string"}},
            "required": ["glob_pattern"],
        },
    },
    {
        "name": "python",
        "description": "Run Python code.",
        "input_schema": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    },
    {
        "name": "git_status",
        "description": "Get the status of the git repository.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "apply_patch",
        "description": "Apply SEARCH/REPLACE patch blocks to a file. "
        "Format: <<<<<<< SEARCH\\n...old...\\n=======\\n...new...\\n>>>>>>> REPLACE",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "patch": {"type": "string"}},
            "required": ["path", "patch"],
        },
    },
    {
        "name": "json_read",
        "description": "Read JSON data from a file, if a key is provided, return the value of the key, otherwise return the entire JSON object.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "key": {"type": "string"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "grep",
        "description": "Search for literal text in files. Returns lines as path:line: content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Text to search for"},
                "path": {
                    "type": "string",
                    "description": "File or directory to search (default: workspace root)",
                },
                "glob_pattern": {
                    "type": "string",
                    "description": "When path is a directory, which files to scan. Use **/*.py for Python only, **/* for all files. Do NOT use *.md unless you only want root-level markdown.",
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "file_info",
        "description": "Get information about a file.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "append_file",
        "description": "Append content to a file, creating parent directories if needed.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
]

# ═══════════════════════════════════════════════════════════
#  NEW in s02: 工具分发映射（s01 是硬编码 run_bash，现在改为查表）
# ═══════════════════════════════════════════════════════════

TOOL_HANDLERS = {
    "bash": run_bash,
    "read_file": run_read,
    "write_file": run_write,
    "edit_file": run_edit,
    "glob": run_glob,
    "list_dir": run_list_dir,
    "web_fetch": run_web_fetch,
    "safe_delete_file": run_safe_delete_file,
    "count_lines": run_count_lines,
    "python": run_python,
    "git_status": run_git_status,
    "apply_patch": run_apply_patch,
    "json_read": run_json_read,
    "grep": run_grep,
    "file_info": run_file_info,
    "append_file": run_append_file,
}


# ═══════════════════════════════════════════════════════════
#  NEW in s04: Hook System (s03 permission logic now via hooks)
# ═══════════════════════════════════════════════════════════

HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}


def register_hook(event: str, callback):
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:  # teaching shortcut: block this tool call
            return result
    return None


def permission_hook(block):
    """Return denial reason if blocked, else None to allow execution."""
    # Gate 1: bash 硬拒
    if block.name == "bash":
        if reason := check_deny_list(block.input.get("command", "")):
            print(f"\n⛔ {reason}")
            return reason

    # Gate 2a: 硬敏感 → 直接拒绝
    if block.name in PATH_MUTATING_TOOLS:
        path = block.input.get("path", "")
        if is_hard_sensitive(path):
            return f"Blocked: sensitive file: {path}"

    # Gate 2b: 软敏感 → 询问用户
    if block.name in PATH_MUTATING_TOOLS:
        path = block.input.get("path", "")
        if is_soft_sensitive(path):
            decision = ask_user(block.name, block.input, f"Sensitive file: {path}")
            if decision == "deny":
                return f"Permission denied by user: sensitive file {path}"

    # Gate 2c + 3: 其他规则（含工作区外）→ 询问
    if reason := check_rules(block.name, block.input):
        decision = ask_user(block.name, block.input, reason)
        if decision == "deny":
            return f"Permission denied by user: {reason}"

    return None


def log_hook(block):
    """PreToolUse: log every tool call."""
    args_preview = str(list(block.input.values())[:2])[:60]
    print(f"\033[90m[HOOK] {block.name}({args_preview})\033[0m")
    return None


def large_output_hook(block, output):
    """PostToolUse: warn on large output."""
    if len(str(output)) > 50000:
        print(
            f"\033[33m[HOOK] ⚠ Large output from {block.name}: {len(str(output))} chars\033[0m"
        )
        return str(output)[:50000] + "\n...(truncated)"
    return None


# UserPromptSubmit hook: log user input before it reaches the LLM
def context_inject_hook(query: str):
    if not query.strip():
        return "Please enter a valid query."
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None


# Stop hook: print summary when loop is about to exit
def summary_hook(messages: list):
    tool_count = sum(
        1
        for m in messages
        for b in (m.get("content") if isinstance(m.get("content"), list) else [])
        if isinstance(b, dict) and b.get("type") == "tool_result"
    )
    counts = Counter()
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if getattr(b, "type", None) == "tool_use":
                counts[b.name] += 1
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    print(f"\033[90m[HOOK] Stop: tool counts - {counts}\033[0m")
    return None


def tool_log_hook(block, output):
    """Print tool I/O to terminal; persist apply_patch input for inspection."""
    name = block.name
    inputs = block.input
    DEBUG_DIR.mkdir(exist_ok=True)
    with open(DEBUG_DIR / "session.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"timestamp": datetime.now().isoformat(), "name": name, "inputs": inputs, "output": output}, ensure_ascii=False) + "\n")
    print(f"\033[90m[HOOK] ToolLog: {name} | {inputs} | {output}\033[0m")
    return None


def bash_audit_hook(block):
    if block.name == "bash":
        DEBUG_DIR.mkdir(exist_ok=True)
        cmd = block.input.get("command", "")
        line = f"{datetime.now():%Y-%m-%d %H:%M:%S} | bash | {cmd}\n"
        with open(DEBUG_DIR / "bash.log", "a", encoding="utf-8") as f:
            f.write(line)
    return None

def pretool_use_time_hook(block):
    TOOL_USE_TIMES[block.id] = time.perf_counter()
    return None

def posttool_use_time_hook(block, output):
    if block.id in TOOL_USE_TIMES:
        time_diff = time.perf_counter() - TOOL_USE_TIMES[block.id]
        with open(DEBUG_DIR / "timing.log", "a", encoding="utf-8") as f:
            f.write(f"{block.name} | {time_diff:.2f}s\n")
            print(f"\033[90m[HOOK] {block.name} | {time_diff:.2f}s\033[0m")
    return None



register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)
register_hook("PostToolUse", tool_log_hook)
register_hook("PreToolUse", bash_audit_hook)
register_hook("PreToolUse", pretool_use_time_hook)
register_hook("PostToolUse", posttool_use_time_hook)


# ═══════════════════════════════════════════════════════════
#  agent_loop — same structure as s03, but no hard-coded check
#  s03: if not check_permission(block): ...
#  s04: if trigger_hooks("PreToolUse", block): ...
# ═══════════════════════════════════════════════════════════


def agent_loop(messages: list):
    while True:
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=8000,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return

        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            # s04 change: hook replaces hard-coded check_permission()
            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(blocked),
                    }
                )
                continue

            handler = TOOL_HANDLERS.get(block.name)
            output = handler(**block.input) if handler else f"Unknown: {block.name}"

            modified = trigger_hooks("PostToolUse", block, output)  # s04: post hook
            if modified is not None:
                output = modified

            results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": output}
            )

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("s04: Hooks — extension logic on hooks, loop stays clean")
    print("Type a question, press Enter. Type q to quit.\n")

    history = []
    while True:
        try:
            query = input("\033[36ms04 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit"):
            break
        user_query = trigger_hooks("UserPromptSubmit", query)
        if user_query:
            print(f"\033[31m[HOOK] Error: {user_query}\033[0m")
            continue
        history.append({"role": "user", "content": query})
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
