#!/usr/bin/env python3
"""
s07: Skill Loading — s06 subagent + two-level skill injection.

  Layer 1 (cheap, always in SYSTEM):
    Skill catalog from skills/*/SKILL.md frontmatter (~100 tokens/skill)

  Layer 2 (expensive, on demand):
    load_skill(name) → full SKILL.md via tool_result (~2000 tokens/skill)

  + All s06: 18 tools, task/subagent, in_subagent hooks, s05 todo/plan-only
  + Subagent has no task / load_skill (no recursion, no skill loading)

Run: python s07_skill_loading/code.py
Needs: pip install anthropic python-dotenv requests + ANTHROPIC_API_KEY in .env
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
SKILLS_DIR = WORKDIR / "skills"
TASKS_DIR = WORKDIR / ".tasks"
TASKS_DIR.mkdir(exist_ok=True)


def _read_tasks_file(path: Path) -> str:
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")


PLAN_ONLY_MARKERS = (
    "只列计划",
    "不要执行",
    "仅规划",
    "plan only",
    "don't execute",
    "do not execute",
)


def is_plan_only_query(query: str) -> bool:
    q = query.lower()
    return any(marker.lower() in q for marker in PLAN_ONLY_MARKERS)


plan_only_mode = False
in_subagent = False

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))
MODEL = os.environ["MODEL_ID"]

# ═══════════════════════════════════════════════════════════
#  NEW in s07: Skill registry — catalog in SYSTEM, content on demand
# ═══════════════════════════════════════════════════════════

SKILL_REGISTRY: dict[str, dict] = {}


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from SKILL.md. Returns (meta, body)."""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, parts[2].strip()


def _scan_skills() -> None:
    """Scan skills/ dir, populate SKILL_REGISTRY with name/description/content."""
    if not SKILLS_DIR.exists():
        return
    for d in sorted(SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        manifest = d / "SKILL.md"
        if not manifest.exists():
            continue
        raw = manifest.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(raw)
        name = meta.get("name", d.name)
        desc = meta.get("description", raw.split("\n")[0].lstrip("#").strip())
        SKILL_REGISTRY[name] = {"name": name, "description": desc, "content": raw, "body": body}


def list_skills() -> str:
    """List all skills (name + one-line description)."""
    if not SKILL_REGISTRY:
        return "(no skills found)"
    return "\n".join(
        f"- **{s['name']}**: {s['description']}" for s in SKILL_REGISTRY.values()
    )


def build_system() -> str:
    """Build SYSTEM with s06 agent rules + skill catalog."""
    catalog = list_skills()
    return (
        f"You are a coding agent at {WORKDIR}. \\n"
        "Before starting any multi-step (more than 1 step) task, use todo_write to plan your steps. When unsure of progress, use todo_read. Multi-step plans must end with a Verify: step. \\n"
        "For complex sub-problems, use the task tool to spawn a subagent with isolated context. Complex sub-problems are defined as problems that require operations including but not limited to research, planning, modularized coding tasks, exploring code base, etc.\\n"
        "When delegating a sub-problem to a subagent, you must provide a clear and concise description of the sub-problem. The description should be enough for the subagent to understand the sub-problem and complete the task. You must also include a Verify: step in the plan to ensure the subagent has completed the task correctly.\\n"
        "Update status as you go. You must update status after you finish a task.\\n"
        "If the user asks to plan only without executing, use todo_write and stop; do not use other tools until asked to execute.\\n"
        "After finishing all the tasks, you may report to the user.\\n"
        f"Skills available:\\n{catalog}\\n"
        "Use load_skill to get full skill content when needed. Choose skill wisely, for instance, before a safety audit, you should load the code-review skill. However, for simple tasks, do not load any skill."
    )


_scan_skills()
SYSTEM = build_system()

SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Complete the task you were given, then return a concise summary. \\n"
    "When provided with a Verify: step, you must verify the task has been completed correctly before returning a summary. \\n"
    "Return a concise summary with: findings, files changed, and anything unfinished. Do not mention that you are a subagent.\\n"
    "Do not delegate further."
)

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


# ═══════════════════════════════════════════════════════════
#  NEW in s05: todo_write tool — plan only, no execution
# ═══════════════════════════════════════════════════════════


def run_todo_write(todos: list) -> str:
    if not todos:
        return f"Error: todos is empty"
    VERIFY_PREFIX = "Verify:"

    def _last_todo_is_verify(todos, VERIFY_PREFIX):
        return todos[-1]["content"].startswith(VERIFY_PREFIX)

    if len(todos) >= 2 and not _last_todo_is_verify(todos, VERIFY_PREFIX):
        return f"Error: the last todo is not a verify todo. You must end your plan with a Verify: step."
    for i, t in enumerate(todos):
        if "content" not in t or "status" not in t:
            return f"Error: todos[{i}] missing 'content' or 'status'"
        if t["status"] not in ("pending", "in_progress", "completed"):
            return f"Error: todos[{i}] has invalid status '{t['status']}'"
    tasks_file = TASKS_DIR / "current_todos.json"
    if sum(1 for t in todos if t["status"] == "in_progress") > 1:
        return "Error: only one task may be in_progress at a time"
    tasks_file.write_text(
        json.dumps(todos, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    lines = ["\n\033[33m## Current Tasks\033[0m"]
    for t in todos:
        icon = {
            "pending": " ",
            "in_progress": "\033[36m▸\033[0m",
            "completed": "\033[32m✓\033[0m",
        }[t["status"]]
        lines.append(f"  [{icon}] {t['content']}")
    print("\n".join(lines))
    return json.dumps(
        {"updated": len(todos), "todos": todos}, ensure_ascii=False, indent=2
    )


def run_todo_read() -> str:
    path = TASKS_DIR / "current_todos.json"
    if not path.exists():
        return "(no todos yet)"
    return _read_tasks_file(path)


def extract_text(content) -> str:
    """Extract text from message content blocks."""
    if not isinstance(content, list):
        return str(content)
    return "\n".join(
        getattr(b, "text", "") for b in content if getattr(b, "type", None) == "text"
    )


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
    {
        "name": "todo_write",
        "description": "Create and manage a task list for your current coding session.",
        "input_schema": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                            },
                        },
                        "required": ["content", "status"],
                    },
                }
            },
            "required": ["todos"],
        },
    },
    {
        "name": "todo_read",
        "description": "Read the current task list.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
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
    "todo_write": run_todo_write,
    "todo_read": run_todo_read,
}


# ═══════════════════════════════════════════════════════════
#  NEW in s06: Subagent — fresh messages[], summary only
# ═══════════════════════════════════════════════════════════

SUB_TOOL_NAMES = ("bash", "read_file", "write_file", "edit_file", "glob", "grep")
SUB_TOOLS = [t for t in TOOLS if t["name"] in SUB_TOOL_NAMES]
SUB_HANDLERS = {name: TOOL_HANDLERS[name] for name in SUB_TOOL_NAMES}
SUB_AGENT_MAX_ITER = 30


def spawn_subagent(description: str) -> str:
    """Spawn a subagent with fresh messages[], return summary only."""
    global in_subagent
    print(f"\n\033[35m[Subagent spawned]\033[0m")
    messages = [{"role": "user", "content": description}]
    in_subagent = True
    try:
        for _ in range(SUB_AGENT_MAX_ITER):
            response = client.messages.create(
                model=MODEL,
                system=SUB_SYSTEM,
                messages=messages,
                tools=SUB_TOOLS,
                max_tokens=8000,
            )
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason != "tool_use":
                break

            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

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

                handler = SUB_HANDLERS.get(block.name)
                output = (
                    handler(**block.input) if handler else f"Unknown: {block.name}"
                )

                modified = trigger_hooks("PostToolUse", block, output)
                if modified is not None:
                    output = modified

                print(f"  \033[90m[sub] {block.name}: {str(output)[:100]}\033[0m")
                results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": output}
                )
            messages.append({"role": "user", "content": results})
    finally:
        in_subagent = False

    result = extract_text(messages[-1]["content"])
    if not result:
        for msg in reversed(messages):
            if msg["role"] == "assistant":
                result = extract_text(msg["content"])
                if result:
                    break
        if not result:
            result = "Subagent stopped after 30 turns without final answer."
    print(f"\033[35m[Subagent done]\033[0m")
    return result


TOOLS.append(
    {
        "name": "task",
        "description": "Launch a subagent to handle a complex subtask. Returns only the final conclusion.",
        "input_schema": {
            "type": "object",
            "properties": {"description": {"type": "string"}},
            "required": ["description"],
        },
    }
)
TOOL_HANDLERS["task"] = spawn_subagent


def load_skill(name: str) -> str:
    """Load full skill content. Lookup via registry — no path traversal."""
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        available = ", ".join(sorted(SKILL_REGISTRY)) or "(none)"
        return f"Skill not found: {name}. Available: {available}"
    return skill["body"]

def reload_skills() -> str:
    global SYSTEM
    _scan_skills()
    SYSTEM = build_system()
    return "Skills reloaded."

TOOLS.append(
    {
        "name": "reload_skills",
        "description": "Reload the skills from the skills directory.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    }
)
TOOL_HANDLERS["reload_skills"] = reload_skills


TOOLS.append(
    {
        "name": "load_skill",
        "description": "Load the full content of a skill by name.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    }
)
TOOL_HANDLERS["load_skill"] = load_skill


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


def plan_only_hook(block):
    if in_subagent:
        return None
    if not plan_only_mode:
        return None
    if block.name in ("todo_write", "todo_read"):
        return None
    return (
        "Error: plan-only mode — use todo_write (and optionally todo_read) only; "
        "wait for the user to ask you to execute."
    )


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
    # check if there is any pending/in-progress todos
    todos = TASKS_DIR / "current_todos.json"
    if not todos.exists():
        return None
    todos = json.loads(_read_tasks_file(todos))
    if sum(1 for t in todos if t["status"] in ("pending", "in_progress")) > 0:
        if plan_only_mode:
            print(
                "\033[33m[HOOK] Stop: plan-only mode — allowing exit with pending todos.\033[0m"
            )
            return None
        # red print, report to user
        print(
            f"\033[31m[HOOK] Stop: Please be aware that there are still pending/in-progress todos.\033[0m"
        )
        # prompt agent to run another round
        pending_todos = [
            t["content"] for t in todos if t["status"] in ("pending", "in_progress")
        ]
        return f"Pending/inprogress todos {pending_todos}, please update the status of the todos. You may report to the user after you finish all the todos."
    # green print, report to user
    print(
        f"\033[32m[HOOK] Stop: all todos are completed. You may report to the user.\033[0m"
    )
    return None


def tool_log_hook(block, output):
    """Print tool I/O to terminal; persist apply_patch input for inspection."""
    if in_subagent:
        return None
    name = block.name
    inputs = block.input
    DEBUG_DIR.mkdir(exist_ok=True)
    with open(DEBUG_DIR / "session.jsonl", "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "timestamp": datetime.now().isoformat(),
                    "name": name,
                    "inputs": inputs,
                    "output": output,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
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
    if in_subagent:
        return None
    TOOL_USE_TIMES[block.id] = time.perf_counter()
    return None


def posttool_use_time_hook(block, output):
    if in_subagent:
        return None
    if block.id in TOOL_USE_TIMES:
        time_diff = time.perf_counter() - TOOL_USE_TIMES[block.id]
        with open(DEBUG_DIR / "timing.log", "a", encoding="utf-8") as f:
            f.write(f"{block.name} | {time_diff:.2f}s\n")
            print(f"\033[90m[HOOK] {block.name} | {time_diff:.2f}s\033[0m")
    return None


def todo_inject_hook(block, output):
    if in_subagent:
        return None
    if block.name in ("todo_write", "reload_skills", "load_skill"):
        return None
    path = TASKS_DIR / "current_todos.json"
    if not path.exists():
        return None
    todos = _read_tasks_file(path)
    return output + f"\n\n<current_todos>\n{todos}\n</current_todos>"


register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", log_hook)  # 可选：改到 permission 前
register_hook("PreToolUse", plan_only_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", bash_audit_hook)
register_hook("PreToolUse", pretool_use_time_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("PostToolUse", todo_inject_hook)
register_hook("PostToolUse", tool_log_hook)
register_hook("PostToolUse", posttool_use_time_hook)
register_hook("Stop", summary_hook)


# ═══════════════════════════════════════════════════════════
#  agent_loop — s04 hooks + s05 nag + s06 task + s07 skills
# ═══════════════════════════════════════════════════════════

rounds_since_todo = 0


def agent_loop(messages: list):
    global rounds_since_todo
    while True:
        if rounds_since_todo >= 3 and messages:
            messages.append(
                {
                    "role": "user",
                    "content": "<reminder>Update your todos.</reminder>",
                }
            )
            rounds_since_todo = 0

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

        rounds_since_todo += 1
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

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

            modified = trigger_hooks("PostToolUse", block, output)
            if modified is not None:
                output = modified

            if block.name == "todo_write":
                rounds_since_todo = 0

            results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": output}
            )

        messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    print("s07: Skill Loading — s06 subagent + catalog in SYSTEM, content on demand")
    print("Type a question, press Enter. Type q to quit.\n")

    history = []
    while True:
        try:
            query = input("\033[36ms07 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit"):
            break
        user_query = trigger_hooks("UserPromptSubmit", query)
        if user_query:
            print(f"\033[31m[HOOK] Error: {user_query}\033[0m")
            continue
        history.append({"role": "user", "content": query})
        plan_only_mode = is_plan_only_query(query)
        rounds_since_todo = 0
        if plan_only_mode:
            print("\033[33m[HOOK] plan-only mode enabled for this prompt\033[0m")
        (TASKS_DIR / "current_todos.json").unlink(missing_ok=True)
        agent_loop(history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
