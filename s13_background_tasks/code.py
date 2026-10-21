#!/usr/bin/env python3
"""
s13: Background Tasks — s12 full agent + thread-based async execution.

  NEW in s13 (merged on top of s12):
    - threading.Thread for background execution of slow tools
    - background_tasks dict for lifecycle tracking (bg_id → tool_use_id, command, status)
    - background_results dict + threading.Lock for thread-safe storage
    - should_run_background: model explicit request via run_in_background param,
      is_slow_operation keyword heuristic as fallback
    - start_background_task: dispatch to daemon thread, return bg task id
    - collect_background_results: gather completed, return as <task_notification>
    - bash tool schema gains run_in_background: boolean
    - agent_loop: slow ops → background + placeholder tool_result, inject notifications

  From s12 (unchanged):
    - Persisted task graph: Task dataclass, create/list/get/claim/complete_task
    - S11 full agent: memory, compact, skills, subagent, todo, hooks, error recovery
    - Coexists with todo_write (current_todos.json) in .tasks/

Run: python s13_background_tasks/code.py
Needs: pip install anthropic python-dotenv requests + ANTHROPIC_API_KEY in .env
Optional: FALLBACK_MODEL_ID for 529 model switch
"""

import os, subprocess, sys, threading
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict
import requests
import glob
import json
import re
import time
import random

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
MEMORY_DIR = WORKDIR / ".memory"
MEMORY_DIR.mkdir(exist_ok=True)
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOOL_RESULTS_DIR = WORKDIR / ".task_outputs" / "tool-results"
TASKS_DIR = WORKDIR / ".tasks"
TASKS_DIR.mkdir(exist_ok=True)


# ═══════════════════════════════════════════════════════════
#  NEW in s12: Task System — persisted DAG in .tasks/task_*.json
# ═══════════════════════════════════════════════════════════


@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str  # pending | in_progress | completed
    owner: str | None  # Agent name (multi-agent scenarios)
    blockedBy: list[str]  # Dependency task IDs


def _task_path(task_id: str) -> Path:
    return TASKS_DIR / f"{task_id}.json"


def create_task(
    subject: str, description: str = "", blockedBy: list[str] | None = None
) -> Task:
    task = Task(
        id=f"task_{int(time.time())}_{random.randint(0, 9999):04d}",
        subject=subject,
        description=description,
        status="pending",
        owner=None,
        blockedBy=blockedBy or [],
    )
    save_task(task)
    return task


def save_task(task: Task) -> None:
    _task_path(task.id).write_text(
        json.dumps(asdict(task), indent=2, ensure_ascii=False), encoding="utf-8"
    )


def load_task(task_id: str) -> Task:
    return Task(**json.loads(_task_path(task_id).read_text(encoding="utf-8")))


def list_tasks() -> list[Task]:
    return [
        Task(**json.loads(p.read_text(encoding="utf-8")))
        for p in sorted(TASKS_DIR.glob("task_*.json"))
    ]


def get_task(task_id: str) -> str:
    """Return full task details as JSON."""
    task = load_task(task_id)
    return json.dumps(asdict(task), indent=2, ensure_ascii=False)


def can_start(task_id: str) -> bool:
    """Check if all blockedBy dependencies are completed.
    Missing dependencies are treated as blocked."""
    task = load_task(task_id)
    for dep_id in task.blockedBy:
        if not _task_path(dep_id).exists():
            return False
        if load_task(dep_id).status != "completed":
            return False
    return True


def claim_task(task_id: str, owner: str = "agent") -> str:
    task = load_task(task_id)
    if task.id in task.blockedBy:
        return f"Task {task_id} is blocked by itself, cannot claim"
    if task.status != "pending":
        return f"Task {task_id} is {task.status}, cannot claim"
    if not can_start(task_id):
        deps = [
            d
            for d in task.blockedBy
            if not _task_path(d).exists() or load_task(d).status != "completed"
        ]
        return f"Blocked by: {deps}"
    task.owner = owner
    task.status = "in_progress"
    save_task(task)
    print(f"  \033[36m[claim] {task.subject} → in_progress (owner: {owner})\033[0m")
    return f"Claimed {task.id} ({task.subject})"


def complete_task(task_id: str) -> str:
    task = load_task(task_id)
    if task.status != "in_progress":
        return f"Task {task_id} is {task.status}, cannot complete"
    task.status = "completed"
    save_task(task)
    unblocked = [
        t.subject
        for t in list_tasks()
        if t.status == "pending" and t.blockedBy and can_start(t.id)
    ]
    print(f"  \033[32m[complete] {task.subject} ✓\033[0m")
    msg = f"Completed {task.id} ({task.subject})"
    if unblocked:
        msg += f"\nUnblocked: {', '.join(unblocked)}"
        print(f"  \033[33m[unblocked] {', '.join(unblocked)}\033[0m")
    return msg


def run_create_task(
    subject: str, description: str = "", blockedBy: list[str] | None = None
) -> str:
    task = create_task(subject, description, blockedBy)
    deps = f" (blockedBy: {', '.join(blockedBy)})" if blockedBy else ""
    print(f"  \033[34m[create] {task.subject}{deps}\033[0m")
    return f"Created {task.id}: {task.subject}{deps}"


def run_list_tasks() -> str:
    tasks = list_tasks()
    if not tasks:
        return "No tasks. Use create_task to add some."
    lines = []
    for t in tasks:
        icon = {"pending": "○", "in_progress": "●", "completed": "✓"}.get(t.status, "?")
        deps = f" (blockedBy: {', '.join(t.blockedBy)})" if t.blockedBy else ""
        owner = f" [{t.owner}]" if t.owner else ""
        lines.append(f"  {icon} {t.id}: {t.subject} [{t.status}]{owner}{deps}")
    return "\n".join(lines)


def run_get_task(task_id: str) -> str:
    try:
        return get_task(task_id)
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"


def run_claim_task(task_id: str) -> str:
    try:
        return claim_task(task_id, owner="agent")
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"


def run_complete_task(task_id: str) -> str:
    try:
        return complete_task(task_id)
    except FileNotFoundError:
        return f"Error: Task {task_id} not found"


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
PRIMARY_MODEL = os.environ["MODEL_ID"]
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL_ID")
MODEL = PRIMARY_MODEL  # side-queries / compact / extract use primary

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
        SKILL_REGISTRY[name] = {
            "name": name,
            "description": desc,
            "content": raw,
            "body": body,
        }


def list_skills() -> str:
    """List all skills (name + one-line description)."""
    if not SKILL_REGISTRY:
        return "(no skills found)"
    return "\n".join(
        f"- **{s['name']}**: {s['description']}" for s in SKILL_REGISTRY.values()
    )


# ═══════════════════════════════════════════════════════════
#  NEW in s09: Memory System
# ═══════════════════════════════════════════════════════════

MEMORY_TYPES = ["user", "feedback", "project", "reference"]
CONSOLIDATE_THRESHOLD = 10


def write_memory_file(name: str, mem_type: str, description: str, body: str) -> Path:
    """Write a single memory file with YAML frontmatter."""
    slug = name.lower().replace(" ", "-").replace("/", "-")
    filename = f"{slug}.md"
    filepath = MEMORY_DIR / filename
    filepath.write_text(
        f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    _rebuild_memory_index()
    return filepath


def _rebuild_memory_index() -> None:
    """Rebuild MEMORY.md index from all memory files."""
    lines = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        raw = f.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(raw)
        mem_name = meta.get("name", f.stem)
        desc = meta.get("description", body.split("\n")[0][:80])
        lines.append(f"- [{mem_name}]({f.name}) — {desc}")
    MEMORY_INDEX.write_text(
        "\n".join(lines) + "\n" if lines else "",
        encoding="utf-8",
    )


def read_memory_index() -> str:
    """Read MEMORY.md index (injected into SYSTEM every turn)."""
    if not MEMORY_INDEX.exists():
        return ""
    text = MEMORY_INDEX.read_text(encoding="utf-8").strip()
    return text if text else ""


def read_memory_file(filename: str) -> str | None:
    """Read a single memory file's full content."""
    path = (MEMORY_DIR / filename).resolve()
    if not path.is_relative_to(MEMORY_DIR.resolve()) or not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def list_memory_files() -> list[dict]:
    """List all memory files with metadata."""
    result = []
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        raw = f.read_text(encoding="utf-8")
        meta, body = _parse_frontmatter(raw)
        result.append(
            {
                "filename": f.name,
                "name": meta.get("name", f.stem),
                "description": meta.get("description", ""),
                "type": meta.get("type", "user"),
                "body": body,
            }
        )
    return result


def select_relevant_memories(messages: list, max_items: int = 5) -> list[str]:
    """Select relevant memory filenames via LLM side-query (keyword fallback)."""
    files = list_memory_files()
    if not files:
        return []

    recent_texts = []
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(getattr(b, "text", ""))
                for b in content
                if getattr(b, "type", None) == "text"
            )
        if isinstance(content, str):
            recent_texts.append(content)
        if len(recent_texts) >= 3:
            break
    recent = " ".join(reversed(recent_texts))[:2000]
    if not recent.strip():
        return []

    catalog = "\n".join(
        f"{i}: {f['name']} — {f['description']}" for i, f in enumerate(files)
    )
    prompt = (
        "Given the recent conversation and the memory catalog below, "
        "select the indices of memories that are clearly relevant. "
        "Return ONLY a JSON array of integers, e.g. [0, 3]. "
        "If none are relevant, return [].\n\n"
        f"Recent conversation:\n{recent}\n\n"
        f"Memory catalog:\n{catalog}"
    )

    try:
        response = client.messages.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
        )
        text = response.content[0].text.strip()
        match = re.search(r"\[.*?\]", text, re.DOTALL)
        if match:
            indices = json.loads(match.group())
            selected = []
            for idx in indices:
                if isinstance(idx, int) and 0 <= idx < len(files):
                    selected.append(files[idx]["filename"])
                    if len(selected) >= max_items:
                        break
            return selected
    except Exception:
        pass

    keywords = [w.lower() for w in recent.split() if len(w) > 3]
    selected = []
    for f in files:
        haystack = (f["name"] + " " + f["description"]).lower()
        if any(kw in haystack for kw in keywords):
            selected.append(f["filename"])
            if len(selected) >= max_items:
                break
    return selected


def load_memories(messages: list) -> str:
    """Load relevant memory content for injection into system prompt."""
    selected_files = select_relevant_memories(messages)
    if not selected_files:
        return ""

    parts = ["<relevant_memories>"]
    for filename in selected_files:
        content = read_memory_file(filename)
        if content:
            parts.append(content)
    parts.append("</relevant_memories>")
    return "\n\n".join(parts)


def extract_memories(messages: list) -> None:
    """Extract new memories from recent dialogue. Runs after each turn ends."""
    dialogue_parts = []
    for msg in messages[-10:]:
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(getattr(b, "text", ""))
                for b in content
                if getattr(b, "type", None) == "text"
            )
        if isinstance(content, str) and content.strip():
            dialogue_parts.append(f"{role}: {content}")
    dialogue = "\n".join(dialogue_parts)
    if not dialogue.strip():
        return

    existing = list_memory_files()
    existing_desc = (
        "\n".join(f"- {m['name']}: {m['description']}" for m in existing)
        if existing
        else "(none)"
    )

    prompt = (
        "Extract user preferences, constraints, or project facts from this dialogue.\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n"
        "- name: short kebab-case identifier (e.g. 'user-preference-tabs')\n"
        "- type: one of 'user', 'feedback', 'project', 'reference'\n"
        "- description: one-line summary for index lookup\n"
        "- body: full detail in markdown\n"
        "If nothing new or already covered by existing memories, return [].\n\n"
        f"Existing memories:\n{existing_desc}\n\n"
        f"Dialogue:\n{dialogue[:4000]}"
    )

    try:
        response = client.messages.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=800,
        )
        text = response.content[0].text.strip()
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return
        items = json.loads(match.group())
        if not items:
            return
        count = 0
        for mem in items:
            name = mem.get("name", f"memory_{int(time.time())}")
            mem_type = mem.get("type", "user")
            desc = mem.get("description", "")
            body = mem.get("body", "")
            if desc and body:
                write_memory_file(name, mem_type, desc, body)
                count += 1
        if count:
            print(f"\n\033[33m[Memory: extracted {count} new memories]\033[0m")
    except Exception:
        pass


def consolidate_memories() -> None:
    """Merge duplicate/stale memories when file count ≥ threshold."""
    files = list_memory_files()
    if len(files) < CONSOLIDATE_THRESHOLD:
        return

    catalog = "\n\n".join(
        f"## {f['filename']}\nname: {f['name']}\ndescription: {f['description']}\n{f['body']}"
        for f in files
    )
    prompt = (
        "Consolidate the following memory files. Rules:\n"
        "1. Merge duplicates into one\n"
        "2. Remove outdated/contradicted memories\n"
        "3. Keep the total under 30 memories\n"
        "4. Preserve important user preferences above all\n"
        "Return a JSON array. Each item: {name, type, description, body}.\n\n"
        f"{catalog[:16000]}"
    )

    try:
        response = client.messages.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=3000,
        )
        text = response.content[0].text.strip()
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return
        items = json.loads(match.group())

        for f in MEMORY_DIR.glob("*.md"):
            if f.name != "MEMORY.md":
                f.unlink()

        for mem in items:
            name = mem.get("name", f"memory_{int(time.time())}")
            mem_type = mem.get("type", "user")
            desc = mem.get("description", "")
            body = mem.get("body", "")
            if desc and body:
                write_memory_file(name, mem_type, desc, body)

        print(
            f"\n\033[33m[Memory: consolidated {len(files)} → {len(items)} memories]\033[0m"
        )
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
#  NEW in s10: System prompt sections + assembly + cache
# ═══════════════════════════════════════════════════════════

PROMPT_SECTIONS = {
    "identity": (
        f"You are a coding agent at {WORKDIR}.\n"
        "For slow bash commands (installs, builds, tests), set "
        "run_in_background: true so you can keep working; results arrive as "
        "<task_notification> later.\n"
        "For multi-step work with dependencies, use the task system: "
        "create_task (with blockedBy), list_tasks, claim_task, complete_task. "
        "Only claim a task when can_start would pass (all blockedBy completed). "
        "Multi-step plans must include a Verify: step.\n"
        "For simple short checklists in one session, todo_write/todo_read are still OK.\n"
        "For complex sub-problems, use the task tool to spawn a subagent with isolated context. "
        "Complex sub-problems include research, planning, modularized coding, and exploring codebases.\n"
        "When delegating to a subagent, give a clear description and a Verify: step.\n"
        "Update task/todo status as you go.\n"
        "If the user asks to plan only without executing, use create_task/list_tasks "
        "(and optionally todo_write) and stop; do not claim/complete or use other tools "
        "until asked to execute.\n"
        "After finishing all the tasks, you may report to the user."
    ),
    "tools": "Available tools: {tool_list}. Use them to solve tasks. Act, don't explain.",
    "workspace": "Working directory: {workspace}",
    "skills": (
        "Skills available:\n{catalog}\n"
        "Use load_skill to get full skill content when needed. Choose skill wisely, for instance, "
        "before a safety audit, you should load the code-review skill. However, for simple tasks, "
        "do not load any skill."
    ),
    "memory_index": "Memories available:\n{index}",
    "memory_behavior": (
        "Relevant memories may be injected below. "
        "You may use read_memory when you need a specific memory file. "
        "Respect user preferences from memory.\n"
        "When the user says 'remember' or expresses a clear preference, note it for memory extraction."
    ),
    "plan_only": (
        "Plan-only mode: use create_task, list_tasks, get_task, todo_write, and todo_read only; "
        "do not claim_task, complete_task, or execute other tools."
    ),
}


def assemble_system_prompt(context: dict) -> str:
    """Select and join prompt sections based on current context (real state)."""
    sections = [
        PROMPT_SECTIONS["identity"],
        PROMPT_SECTIONS["tools"].format(
            tool_list=", ".join(context.get("enabled_tools", []))
        ),
        PROMPT_SECTIONS["workspace"].format(workspace=context.get("workspace", WORKDIR)),
    ]

    skill_catalog = context.get("skill_catalog", "")
    if skill_catalog and skill_catalog != "(no skills found)":
        sections.append(PROMPT_SECTIONS["skills"].format(catalog=skill_catalog))

    memory_index = context.get("memory_index", "")
    if memory_index:
        sections.append(PROMPT_SECTIONS["memory_index"].format(index=memory_index))
        sections.append(PROMPT_SECTIONS["memory_behavior"])
    
    plan_only = context.get("plan_only", False)
    if plan_only:
        sections.append(PROMPT_SECTIONS["plan_only"])

    return "\n\n".join(sections)


_last_context_key = None
_last_prompt = None


def _invalidate_prompt_cache() -> None:
    global _last_context_key, _last_prompt
    _last_context_key = None
    _last_prompt = None


def get_system_prompt(context: dict) -> str:
    """Cache wrapper — reassemble only when context changes."""
    global _last_context_key, _last_prompt
    key = json.dumps(context, sort_keys=True, ensure_ascii=False, default=str)
    if key == _last_context_key and _last_prompt:
        print("  \033[90m[cache hit] system prompt unchanged\033[0m")
        return _last_prompt
    _last_context_key = key
    _last_prompt = assemble_system_prompt(context)

    loaded = ["identity", "tools", "workspace"]
    if context.get("skill_catalog") and context.get("skill_catalog") != "(no skills found)":
        loaded.append("skills")
    if context.get("memory_index"):
        loaded.extend(["memory_index", "memory_behavior"])
    if context.get("plan_only"):
        loaded.append("plan_only")
    print(f"  \033[32m[assembled] sections: {', '.join(loaded)}\033[0m")
    return _last_prompt


def update_context(context: dict, messages: list) -> dict:
    """Derive context from real state: tools, skills, memory index."""
    return {
        "enabled_tools": sorted(TOOL_HANDLERS.keys()),
        "workspace": str(WORKDIR),
        "skill_catalog": list_skills(),
        "memory_index": read_memory_index(),
        "plan_only": plan_only_mode,
    }


_scan_skills()

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


def run_bash(command: str, command_timeout: int | None = None,
             run_in_background: bool = False) -> str:
    """Run a shell command in the workspace directory.

    Args:
        command: The shell command to execute.
        command_timeout: Maximum execution time in seconds (capped at 120).
        run_in_background: If True, agent_loop dispatches this call to a
            background thread instead of executing synchronously here.
            Kept as a no-op parameter so execute_tool(**block.input) stays valid.

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
        recursive = "**" in pattern
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR, recursive=recursive):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match.replace("\\", "/"))
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

def read_memory(filename: str) -> str | None:
    """Read a designated memory file's full content"""
    content = read_memory_file(filename)
    if content is None:
        return f"Error: memory file {filename} does not exist"
    return content

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
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "Set to true for slow commands (installs, builds, tests). "
                        "The command runs in a background thread and the result is "
                        "delivered later as a <task_notification>."
                    ),
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
    {
        "name": "read_memory",
        "description": "Read the full content of a designated memory file.",
        "input_schema": {
            "type": "object",
            "properties": {"filename": {"type": "string"}},
            "required": ["filename"],
        },
    },
    {
        "name": "create_task",
        "description": "Create a new persisted task with optional blockedBy dependencies.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "description": {"type": "string"},
                "blockedBy": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["subject"],
        },
    },
    {
        "name": "list_tasks",
        "description": "List all persisted tasks with status, owner, and dependencies.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_task",
        "description": "Get full details of a specific task by ID.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "claim_task",
        "description": "Claim a pending task. Sets owner, changes status to in_progress.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "complete_task",
        "description": "Complete an in-progress task. Reports unblocked downstream tasks.",
        "input_schema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
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
    "read_memory": read_memory,
    "create_task": run_create_task,
    "list_tasks": run_list_tasks,
    "get_task": run_get_task,
    "claim_task": run_claim_task,
    "complete_task": run_complete_task,
}


# ═══════════════════════════════════════════════════════════
#  NEW in s13: Background Tasks — thread-based async execution
# ═══════════════════════════════════════════════════════════

_bg_counter = 0
background_tasks: dict[str, dict] = {}   # bg_id → {tool_use_id, command, status}
background_results: dict[str, str] = {}   # bg_id → output
background_lock = threading.Lock()


def is_slow_operation(tool_name: str, tool_input: dict) -> bool:
    """Fallback heuristic: commands likely to take > 30s."""
    if tool_name != "bash":
        return False
    cmd = tool_input.get("command", "").lower()
    slow_keywords = ["install", "build", "test", "deploy", "compile",
                     "docker build", "pip install", "npm install",
                     "cargo build", "pytest", "make"]
    return any(kw in cmd for kw in slow_keywords)


def should_run_background(tool_name: str, tool_input: dict) -> bool:
    """Model explicit request takes priority; fallback to heuristic."""
    if tool_input.get("run_in_background"):
        return True
    return is_slow_operation(tool_name, tool_input)


def execute_tool(block) -> str:
    """Execute a tool call block, return output."""
    handler = TOOL_HANDLERS.get(block.name)
    if handler:
        return handler(**block.input)
    return f"Unknown tool: {block.name}"


def start_background_task(block) -> str:
    """Run tool in a daemon thread. Returns background task ID."""
    global _bg_counter
    _bg_counter += 1
    bg_id = f"bg_{_bg_counter:04d}"
    cmd = block.input.get("command", block.name)

    def worker():
        try:
            result = execute_tool(block)
        except Exception as e:
            result = f"Error: {e}"
            with background_lock:
                background_tasks[bg_id]["status"] = "error"
                background_results[bg_id] = result
        else:
            with background_lock:
                background_tasks[bg_id]["status"] = "completed"
                background_results[bg_id] = result

    with background_lock:
        background_tasks[bg_id] = {
            "tool_use_id": block.id,
            "command": cmd,
            "status": "running",
        }
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    print(f"  \033[33m[background] dispatched {bg_id}: {cmd[:40]}\033[0m")
    return bg_id


def collect_background_results() -> list[str]:
    """Collect completed background results as task_notification messages."""
    with background_lock:
        ready_ids = [bid for bid, task in background_tasks.items()
                     if task["status"] in ("completed", "error")]
    notifications = []
    for bg_id in ready_ids:
        with background_lock:
            task = background_tasks.pop(bg_id)
            output = background_results.pop(bg_id, "")
        summary = output[:200] if len(output) > 200 else output
        notifications.append(
            f"<task_notification>\n"
            f"  <task_id>{bg_id}</task_id>\n"
            f"  <status>{task['status']}</status>\n"
            f"  <command>{task['command']}</command>\n"
            f"  <summary>{summary}</summary>\n"
            f"</task_notification>")
        if task["status"] == "error":
            print(f"  \033[31m[background error] {bg_id}: {output}\033[0m")
        else:
            print(f"  \033[32m[background done] {bg_id}: {task['command'][:40]} "
                  f"({len(output)} chars)\033[0m")
    return notifications


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
                            "name": block.name,
                            "content": str(blocked),
                        }
                    )
                    continue

                handler = SUB_HANDLERS.get(block.name)
                output = handler(**block.input) if handler else f"Unknown: {block.name}"

                modified = trigger_hooks("PostToolUse", block, output)
                if modified is not None:
                    output = modified

                print(f"  \033[90m[sub] {block.name}: {str(output)[:100]}\033[0m")
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "name": block.name,
                        "content": output,
                    }
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
    _scan_skills()
    _invalidate_prompt_cache()
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
#  NEW in s08: Four-Layer Compaction Pipeline
# ═══════════════════════════════════════════════════════════

CONTEXT_LIMIT = 200000
KEEP_RECENT = 3
PERSIST_THRESHOLD = 30000
MAX_REACTIVE_RETRIES = 1
PRESERVE_RESULT_TOOLS = ["reload_skills", "load_skill"]

# ── s11 Error Recovery constants ──
ESCALATED_MAX_TOKENS = 64000
DEFAULT_MAX_TOKENS = 8000
MAX_RECOVERY_RETRIES = 3
MAX_RETRIES = 10
BASE_DELAY_MS = 500
MAX_CONSECUTIVE_529 = 3
CONTINUATION_PROMPT = (
    "Output token limit hit. Resume directly — "
    "no apology, no recap. Pick up mid-thought."
)


def estimate_size(msgs):
    return len(str(msgs))


def snip_compact(messages, max_messages=50):
    if len(messages) <= max_messages:
        return messages
    keep_head, keep_tail = 3, max_messages - 3
    snipped = len(messages) - keep_head - keep_tail
    placeholder = {
        "role": "user",
        "content": f"[snipped {snipped} messages from conversation middle]",
    }
    return messages[:keep_head] + [placeholder] + messages[-keep_tail:]


def collect_tool_results(messages):
    blocks = []
    for mi, msg in enumerate(messages):
        if msg.get("role") != "user" or not isinstance(msg.get("content"), list):
            continue
        for bi, block in enumerate(msg["content"]):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                blocks.append((mi, bi, block))
    return blocks


def micro_compact(messages):
    tool_results = collect_tool_results(messages)
    if len(tool_results) <= KEEP_RECENT:
        return messages
    for _, _, block in tool_results[:-KEEP_RECENT]:
        if (
            len(block.get("content", "")) > 120
            and block.get("name", "") not in PRESERVE_RESULT_TOOLS
        ):
            block["content"] = "[Earlier tool result compacted. Re-run if needed.]"
    return messages


def persist_large_output(tool_use_id, output):
    if len(output) <= PERSIST_THRESHOLD:
        return output
    TOOL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = TOOL_RESULTS_DIR / f"{tool_use_id}.txt"
    if not path.exists():
        path.write_text(output, encoding="utf-8")
    return f"<persisted-output>\nFull output: {path}\nPreview:\n{output[:2000]}\n</persisted-output>"


def tool_result_budget(messages, max_bytes=200_000):
    last = messages[-1] if messages else None
    if (
        not last
        or last.get("role") != "user"
        or not isinstance(last.get("content"), list)
    ):
        return messages
    blocks = [
        (i, b)
        for i, b in enumerate(last["content"])
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    if total <= max_bytes:
        return messages
    ranked = sorted(
        blocks, key=lambda p: len(str(p[1].get("content", ""))), reverse=True
    )
    for _, block in ranked:
        if total <= max_bytes:
            break
        content = str(block.get("content", ""))
        if len(content) <= PERSIST_THRESHOLD:
            continue
        tid = block.get("tool_use_id", "unknown")
        block["content"] = persist_large_output(tid, content)
        total = sum(len(str(b.get("content", ""))) for _, b in blocks)
    return messages


def write_transcript(messages):
    TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")
    return path


def list_transcripts() -> str:
    try:
        TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        paths = sorted(TRANSCRIPT_DIR.glob("*.jsonl"), reverse=True)
        if not paths:
            return "(no transcripts)"
        return "\n".join(p.relative_to(WORKDIR).as_posix() for p in paths[:20])
    except Exception as e:
        return f"Error: failed to list transcript files: {e}"

def read_transcript(path: str) -> str:
    try:
        file_path = safe_path(path)
        if not file_path.exists():
            return f"Error: transcript file does not exist: {path}"
        if not file_path.is_file():
            return f"Error: transcript file is not a file: {path}"
        return file_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Error: failed to read transcript file: {path}: {e}"


def get_transcript_size(path: Path) -> int:
    with path.open("r", encoding="utf-8") as f:
        return len(f.readlines())


def summarize_history(messages):
    conversation = json.dumps(messages, default=str, ensure_ascii=False)[:80000]
    prompt = (
        "Summarize this coding-agent conversation so work can continue.\n"
        "Preserve: 1. current goal, 2. key findings/decisions, 3. files read/changed, "
        "4. remaining work, 5. user constraints.\nBe compact but concrete.\n\n"
        + conversation
    )
    response = client.messages.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=2000,
    )
    return (
        "\n".join(
            getattr(block, "text", "")
            for block in response.content
            if getattr(block, "type", None) == "text"
        ).strip()
        or "(empty summary)"
    )


def compact_history(messages):
    transcript_path = write_transcript(messages)
    print(f"[transcript saved: {transcript_path}]")
    summary = summarize_history(messages)
    return [{"role": "user", "content": f"[Compacted]\n\n{summary}"}]


def reactive_compact(messages):
    write_transcript(messages)
    summary = summarize_history(messages)
    return [
        {"role": "user", "content": f"[Reactive compact]\n\n{summary}"},
        *messages[-5:],
    ]


TOOLS.extend(
    [
        {
            "name": "compact",
            "description": "Summarize earlier conversation to free context space.",
            "input_schema": {
                "type": "object",
                "properties": {"focus": {"type": "string"}},
            },
        },
        {
            "name": "read_transcript",
            "description": "Read the transcript of the conversation.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "list_transcripts",
            "description": "List all transcript files.",
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    ]
)
TOOL_HANDLERS["read_transcript"] = read_transcript
TOOL_HANDLERS["list_transcripts"] = list_transcripts


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
    if block.name in (
        "todo_write",
        "todo_read",
        "create_task",
        "list_tasks",
        "get_task",
    ):
        return None
    return (
        "Error: plan-only mode — use create_task/list_tasks/get_task "
        "(and optionally todo_write/todo_read) only; "
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
#  NEW in s11: Error Recovery — three paths + backoff
# ═══════════════════════════════════════════════════════════


class RecoveryState:
    """Track recovery attempts across the loop."""

    def __init__(self):
        self.has_escalated = False
        self.recovery_count = 0
        self.consecutive_529 = 0
        self.has_attempted_reactive_compact = False
        self.current_model = PRIMARY_MODEL


def retry_delay(attempt, retry_after=None):
    """Exponential backoff with jitter. Retry-After takes priority."""
    if retry_after:
        return retry_after
    base = min(BASE_DELAY_MS * (2**attempt), 32000) / 1000
    jitter = random.uniform(0, base * 0.25)
    return base + jitter


def with_retry(fn, state: RecoveryState):
    """Exponential backoff for transient errors (429/529).
    Non-transient errors are re-raised for the outer handler."""
    for attempt in range(MAX_RETRIES):
        try:
            result = fn()
            state.consecutive_529 = 0
            return result
        except Exception as e:
            name = type(e).__name__
            msg = str(e).lower()

            if "ratelimit" in name.lower() or "429" in msg:
                delay = retry_delay(attempt)
                print(
                    f"  \033[33m[429 rate limit] retry {attempt + 1}/{MAX_RETRIES},"
                    f" wait {delay:.1f}s\033[0m"
                )
                time.sleep(delay)
                continue

            if "overloaded" in name.lower() or "529" in msg or "overloaded" in msg:
                state.consecutive_529 += 1
                if state.consecutive_529 >= MAX_CONSECUTIVE_529:
                    if FALLBACK_MODEL:
                        state.current_model = FALLBACK_MODEL
                        state.consecutive_529 = 0
                        print(
                            f"  \033[31m[529 x{MAX_CONSECUTIVE_529}]"
                            f" switching to {FALLBACK_MODEL}\033[0m"
                        )
                    else:
                        state.consecutive_529 = 0
                        print(
                            f"  \033[31m[529 x{MAX_CONSECUTIVE_529}]"
                            f" no FALLBACK_MODEL_ID configured, continuing retry\033[0m"
                        )
                delay = retry_delay(attempt)
                print(
                    f"  \033[33m[529 overloaded] retry {attempt + 1}/{MAX_RETRIES},"
                    f" wait {delay:.1f}s\033[0m"
                )
                time.sleep(delay)
                continue
            if "timeout" in name.lower() or "timeout" in msg or "timed out" in msg:
                delay = retry_delay(attempt)
                print(
                    f"  \033[33m[timeout] retry {attempt + 1}/{MAX_RETRIES},"
                    f" wait {delay:.1f}s\033[0m"
                )
                time.sleep(delay)
                continue

            raise
    raise RuntimeError(f"Max retries ({MAX_RETRIES}) exceeded")


def is_prompt_too_long_error(e: Exception) -> bool:
    """Check whether an API error indicates prompt/context too long."""
    msg = str(e).lower()
    return (
        ("prompt" in msg and "long" in msg)
        or "prompt_is_too_long" in msg
        or "prompt_too_long" in msg
        or "too many tokens" in msg
        or "context_length_exceeded" in msg
        or "max_context_window" in msg
    )


# ═══════════════════════════════════════════════════════════
#  agent_loop — s04–s10 + s11 error recovery
# ═══════════════════════════════════════════════════════════

rounds_since_todo = 0


def _snapshot_messages(messages: list) -> list:
    """Shallow copy before compaction mutates tool_result blocks in place."""
    return json.loads(json.dumps(messages, default=str, ensure_ascii=False))


def agent_loop(messages: list, context: dict):
    global rounds_since_todo
    state = RecoveryState()
    max_tokens = DEFAULT_MAX_TOKENS
    while True:
        if rounds_since_todo >= 3 and messages:
            messages.append(
                {
                    "role": "user",
                    "content": "<reminder>Update your todos.</reminder>",
                }
            )
            rounds_since_todo = 0

        # s10: assemble system from sections + s09 Layer 2 inject (before compression)
        context = update_context(context, messages)
        system = get_system_prompt(context)
        memories_content = load_memories(messages)
        if memories_content:
            system += "\n\n" + memories_content

        # s09: snapshot for extract_memories (compaction mutates in place)
        pre_compress = _snapshot_messages(messages)

        # s08: compaction pipeline before LLM (cheap first, expensive last)
        messages[:] = tool_result_budget(messages)
        messages[:] = snip_compact(messages)
        messages[:] = micro_compact(messages)
        if estimate_size(messages) > CONTEXT_LIMIT:
            print("[auto compact]")
            messages[:] = compact_history(messages)

        # s11: LLM call with recovery paths
        try:
            response = with_retry(
                lambda mt=max_tokens, mdl=state.current_model: client.messages.create(
                    model=mdl,
                    system=system,
                    messages=messages,
                    tools=TOOLS,
                    max_tokens=mt,
                ),
                state,
            )
        except Exception as e:
            if is_prompt_too_long_error(e):
                if not state.has_attempted_reactive_compact:
                    print("  \033[31m[reactive compact]\033[0m")
                    messages[:] = reactive_compact(messages)
                    state.has_attempted_reactive_compact = True
                    continue
                print("  \033[31m[unrecoverable] still too long after compact\033[0m")
                messages.append(
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "text",
                                "text": "[Error] Context too large, cannot continue.",
                            }
                        ],
                    }
                )
                return

            name = type(e).__name__
            print(f"  \033[31m[unrecoverable] {name}: {str(e)[:100]}\033[0m")
            messages.append(
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": f"[Error] {name}: {str(e)[:200]}"}
                    ],
                }
            )
            return

        # Path 1: max_tokens -> escalate or continuation
        if response.stop_reason == "max_tokens":
            if not state.has_escalated:
                max_tokens = ESCALATED_MAX_TOKENS
                state.has_escalated = True
                print(
                    f"  \033[33m[max_tokens] escalating"
                    f" {DEFAULT_MAX_TOKENS} -> {ESCALATED_MAX_TOKENS}\033[0m"
                )
                continue
            messages.append({"role": "assistant", "content": response.content})
            if state.recovery_count < MAX_RECOVERY_RETRIES:
                messages.append({"role": "user", "content": CONTINUATION_PROMPT})
                state.recovery_count += 1
                print(
                    f"  \033[33m[max_tokens] continuation"
                    f" {state.recovery_count}/{MAX_RECOVERY_RETRIES}\033[0m"
                )
                continue
            print("  \033[31m[max_tokens] recovery limit reached\033[0m")
            return

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            # Surface completed background results before returning, even on
            # text-only rounds; otherwise the notification stays stuck in memory.
            notifications = collect_background_results()
            if notifications:
                messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text": n} for n in notifications],
                })
                print(f"  \033[32m[inject] {len(notifications)} background "
                      f"notification(s) before stopping\033[0m")
                continue
            extract_memories(pre_compress)
            consolidate_memories()
            return

        rounds_since_todo += 1
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            if block.name == "compact":
                # Summarize history *before* this turn's assistant tool_use.
                # Do not append a tool_result: compact_history replaces messages entirely,
                # and an orphaned tool_result would reference a missing tool_use block.
                messages[:] = compact_history(messages[:-1])
                break

            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "name": block.name,
                        "content": str(blocked),
                    }
                )
                continue

            handler = TOOL_HANDLERS.get(block.name)
            if handler and should_run_background(block.name, block.input):
                bg_id = start_background_task(block)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "name": block.name,
                        "content": (
                            f"[Background task {bg_id} started] "
                            f"Command: {block.input.get('command', '')}. "
                            f"Result will be delivered as a <task_notification> when complete."
                        ),
                    }
                )
                continue

            output = handler(**block.input) if handler else f"Unknown: {block.name}"

            modified = trigger_hooks("PostToolUse", block, output)
            if modified is not None:
                output = modified

            if block.name == "todo_write":
                rounds_since_todo = 0

            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "name": block.name,
                    "content": output,
                }
            )
        else:
            # Inject completed background results as notifications before this turn's results
            user_content = []
            for notif in collect_background_results():
                user_content.append({"type": "text", "text": notif})
            user_content.extend(results)
            messages.append({"role": "user", "content": user_content})
            continue
        continue


if __name__ == "__main__":
    print("s13: Background Tasks — s12 agent + thread-based async execution")
    print("Type a question, press Enter. Type q to quit.\n")

    history = []
    context = update_context({}, [])
    while True:
        try:
            query = input("\033[36ms13 >> \033[0m")
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
        _invalidate_prompt_cache()
        agent_loop(history, context)
        context = update_context(context, history)
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(block.text)
        print()
