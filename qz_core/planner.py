from __future__ import annotations

import json
import re
import sys
from openai import OpenAI
from qz_core.client import get_client
from qz_core.common import _persist_task
from qz_indexer import format_index_summary, load_or_build_index
from qz_tools import WORKSPACE
from qz_task_compiler import TaskCompiler
from qz_tasks.models import SubtaskStatus, TaskStatus
from qz_tasks.task_manager import create_subtasks, log_event, update_status

PLANNER_MODEL = "planner"

SUBTASK_DAG_SYSTEM_PROMPT = """You are a software project planner. Convert the provided task, plan, and architecture into a structured Directed Acyclic Graph (DAG) of actionable subtasks.
Output ONLY a valid JSON array of subtask objects. No markdown formatting, no code blocks, no preamble, no commentary.
Each subtask object MUST have the following schema:
- "id": integer index (0, 1, 2, ...) or string identifier (e.g. "task_0", "task_1")
- "title": short descriptive title (one sentence)
- "description": detailed instructions of what to implement/test in this subtask
- "depends_on": array of IDs/indices of prerequisites in this same list that must be completed before this subtask can begin (empty array [] if no prerequisites)

Rules:
1. Subtasks must form a valid DAG (no circular dependencies).
2. The graph must cover all necessary steps from the plan and architecture.
3. Reply ONLY with the raw JSON array."""


def _get_workspace() -> str:
    agent_mod = sys.modules.get("qz_agent")
    if agent_mod and hasattr(agent_mod, "WORKSPACE"):
        return getattr(agent_mod, "WORKSPACE")
    return WORKSPACE


def call_planner(task: str, index_summary: str | None = None, client: OpenAI | None = None) -> str:
    if index_summary is None:
        index_summary = format_index_summary(load_or_build_index(_get_workspace())[0])
    c = get_client(client)
    resp = c.chat.completions.create(
        model=PLANNER_MODEL,
        messages=[
            {"role": "system", "content": "You are a planning assistant. Break the user's task into short, clear, actionable steps. Give steps only, no code. Always respond in English only — never switch to Hindi, Roman Urdu, Devanagari script, or any other language, regardless of what language the task is written in."},
            {"role": "user", "content": f"Current project index:\n{index_summary}\n\nTask: {task}"},
        ],
    )
    return resp.choices[0].message.content


def call_architect(task: str, plan: str, index_summary: str | None = None, client: OpenAI | None = None) -> str:
    if index_summary is None:
        index_summary = format_index_summary(load_or_build_index(_get_workspace())[0])
    c = get_client(client)
    resp = c.chat.completions.create(
        model=PLANNER_MODEL,
        messages=[
            {"role": "system", "content": (
                "You are a software architect. Based on the given plan, decide: "
                "which new folders/files need to be created, which existing files will be modified, "
                "and what the overall structure will be. Give structure only (in tree format), no code. "
                "Always respond in English only — never switch to Hindi, Roman Urdu, Devanagari script, "
                "or any other language, regardless of what language the task is written in."
            )},
            {"role": "user", "content": f"Current project index:\n{index_summary}\n\nTask: {task}\n\nPlan:\n{plan}"},
        ],
    )
    return resp.choices[0].message.content


def generate_clarifying_questions(task: str, index_summary: str, client: OpenAI | None = None) -> list[str]:
    """Return 0-3 focused clarifying questions, or [] if the task is clear.

    Only asks when an answer would materially change implementation,
    behavior, security, data migration, or tests — never about style or
    things that can reasonably be assumed. A model outage yields no
    questions so a task is never blocked by an unrelated failure.
    """
    c = get_client(client)
    try:
        resp = c.chat.completions.create(
            model=PLANNER_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You review a coding task for ambiguity before planning. Ask a focused "
                        "clarifying question ONLY when the answer would materially change the "
                        "implementation, behavior, security, data migration, or required tests. "
                        "Do not ask about code style, naming preferences, or anything you could "
                        "reasonably assume. Reply with exactly NONE if the task is already clear "
                        "enough to implement as written. Otherwise reply with up to 3 short "
                        "numbered questions, one per line, and nothing else."
                    ),
                },
                {"role": "user", "content": f"Project context:\n{index_summary}\n\nTask: {task}"},
            ],
            temperature=0,
            max_tokens=200,
        )
        text = (resp.choices[0].message.content or "").strip()
    except Exception as error:
        print(f"\033[90m[clarify] clarification check unavailable ({error}); skipping\033[0m")
        return []

    if not text or text.strip().upper().startswith("NONE"):
        return []

    questions = []
    for line in text.splitlines():
        line = re.sub(r"^\s*\d+[\.\)]\s*", "", line.strip())
        if line:
            questions.append(line)
    return questions[:3]


def ask_clarifying_questions(questions: list[str]) -> list[tuple[str, str]]:
    """Interactively collect one answer per question. Never raises on Ctrl-C/EOF."""
    print("\033[93m[clarify]\033[0m This task has a few open questions before planning:")
    answers = []
    for question in questions:
        print(f"  - {question}")
        try:
            answer = input("    > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            answer = ""
        answers.append((question, answer or "(no answer given — use best judgment)"))
    return answers


def format_clarifications(qa_pairs: list[tuple[str, str]]) -> str:
    """Render resolved Q&A so it can be folded into the task text.

    Putting the resolved answers directly into the task the planner/executor
    see is what keeps the agent from asking the same question twice — the
    answer is now just part of the task description, not open anymore.
    """
    if not qa_pairs:
        return ""
    lines = ["Clarifications already resolved with the user (do not ask these again):"]
    for question, answer in qa_pairs:
        lines.append(f"- Q: {question}\n  A: {answer}")
    return "\n".join(lines)


def requires_plan_approval(mode: str, approval_setting: str) -> bool:
    """Decide whether the plan must pause for explicit user approval."""
    if approval_setting == "never":
        return False
    if mode == "quick":
        return False  # Quick tasks bypass the visible plan/approval entirely.
    if approval_setting == "always":
        return True
    # "complex" (default) and "auto" both currently pause only for complex tasks.
    return mode == "complex"


def prompt_plan_approval(plan: str, architecture: str) -> tuple[str, str, str]:
    """Show the plan/architecture and collect one of Approve/Edit/Reject/Best-judgment.

    Returns (decision, final_plan, final_architecture). 'edited' replaces the
    plan text with what the user typed; other decisions keep it unchanged.
    """
    print("\n\033[93m[plan approval]\033[0m Approve (a) / Edit (e) / Reject (r) / Proceed with best judgment (b)")
    while True:
        try:
            choice = input("Your choice [A/e/r/b]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return "rejected", plan, architecture

        if choice in ("", "a", "approve"):
            return "approved", plan, architecture

        if choice in ("e", "edit"):
            print("Type the replacement plan. Finish with a line containing only END:")
            lines: list[str] = []
            while True:
                try:
                    line = input()
                except (EOFError, KeyboardInterrupt):
                    break
                if line.strip() == "END":
                    break
                lines.append(line)
            edited = "\n".join(lines).strip()
            return "edited", (edited or plan), architecture

        if choice in ("r", "reject"):
            return "rejected", plan, architecture

        if choice in ("b", "best", "best-judgment", "proceed"):
            return "proceed_best_judgment", plan, architecture

        print("Please answer with A, E, R, or B.")


def _parse_subtasks_json(raw_text: str) -> list[dict] | None:
    """Parse and validate JSON subtask array from raw model response."""
    if not raw_text or not raw_text.strip():
        return None
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\[\s*\{.*\}\s*\]", cleaned, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
        else:
            return None

    if isinstance(data, dict):
        for key in ("subtasks", "tasks", "dag", "steps"):
            if key in data and isinstance(data[key], list):
                data = data[key]
                break

    if not isinstance(data, list) or not data:
        return None

    parsed: list[dict] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            return None
        title = str(item.get("title") or f"Subtask {i + 1}").strip()
        description = str(item.get("description") or title).strip()
        raw_deps = item.get("depends_on", [])
        if not isinstance(raw_deps, list):
            raw_deps = [raw_deps] if raw_deps is not None else []
        parsed.append({
            "id": item.get("id", i),
            "title": title,
            "description": description,
            "depends_on": raw_deps,
        })
    return parsed


def validate_subtask_dag(subtasks: list[dict]) -> bool:
    """Check that subtasks form a valid Directed Acyclic Graph (no cycles or self-loops)."""
    if not subtasks:
        return True

    dep_indices: dict[int, list[int]] = {i: [] for i in range(len(subtasks))}
    for i, s in enumerate(subtasks):
        raw_deps = s.get("depends_on") or []
        if not isinstance(raw_deps, list):
            raw_deps = [raw_deps]
        for dep in raw_deps:
            target_idx = None
            if isinstance(dep, int) and 0 <= dep < len(subtasks):
                target_idx = dep
            elif isinstance(dep, str) and dep.isdigit() and 0 <= int(dep) < len(subtasks):
                target_idx = int(dep)
            else:
                for j, target_subtask in enumerate(subtasks):
                    if target_subtask.get("id") == dep or str(target_subtask.get("id")) == str(dep):
                        target_idx = j
                        break
                    if target_subtask.get("temp_id") == dep or str(target_subtask.get("temp_id")) == str(dep):
                        target_idx = j
                        break

            if target_idx is not None:
                if target_idx == i:
                    return False  # Self-dependency is a cycle
                if target_idx not in dep_indices[i]:
                    dep_indices[i].append(target_idx)

    # 3-state DFS cycle detection: 0=unvisited, 1=visiting, 2=visited
    state = [0] * len(subtasks)

    def has_cycle(u: int) -> bool:
        state[u] = 1
        for v in dep_indices[u]:
            if state[v] == 1:
                return True
            if state[v] == 0:
                if has_cycle(v):
                    return True
        state[u] = 2
        return False

    for i in range(len(subtasks)):
        if state[i] == 0:
            if has_cycle(i):
                return False

    return True


def convert_plan_to_subtasks(
    task: str,
    plan: str,
    architecture: str,
    *,
    task_id: str | None = None,
    client: OpenAI | None = None,
) -> list[dict]:
    """Convert a finalized plan & architecture into a structured subtask DAG."""
    fallback_subtasks = [
        {
            "id": 0,
            "title": "Implement full plan",
            "description": f"{plan}\n\n{architecture}".strip(),
            "depends_on": [],
        }
    ]

    c = get_client(client)
    subtasks = None
    raw_content = ""
    try:
        resp = c.chat.completions.create(
            model=PLANNER_MODEL,
            messages=[
                {"role": "system", "content": SUBTASK_DAG_SYSTEM_PROMPT},
                {"role": "user", "content": f"Task: {task}\n\nPlan:\n{plan}\n\nArchitecture:\n{architecture}"},
            ],
            temperature=0,
            max_tokens=1500,
        )
        raw_content = (resp.choices[0].message.content or "").strip()
        subtasks = _parse_subtasks_json(raw_content)
    except Exception as error:
        print(f"\033[90m[planner] DAG generation attempt 1 failed ({error})\033[0m")

    # Retry once if initial parse failed
    if subtasks is None:
        try:
            resp = c.chat.completions.create(
                model=PLANNER_MODEL,
                messages=[
                    {"role": "system", "content": SUBTASK_DAG_SYSTEM_PROMPT},
                    {"role": "user", "content": f"Task: {task}\n\nPlan:\n{plan}\n\nArchitecture:\n{architecture}"},
                    {"role": "assistant", "content": raw_content},
                    {"role": "user", "content": "The previous output was not valid JSON matching the required schema. Output ONLY a valid raw JSON array of subtask objects."},
                ],
                temperature=0,
                max_tokens=1500,
            )
            retry_content = (resp.choices[0].message.content or "").strip()
            subtasks = _parse_subtasks_json(retry_content)
        except Exception as error:
            print(f"\033[90m[planner] DAG generation retry failed ({error})\033[0m")

    if subtasks is None:
        print("\033[90m[planner] Failed to parse subtasks JSON; falling back to single whole-plan subtask\033[0m")
        _persist_task(task_id, lambda: log_event(task_id, "DAG_PARSE_FAILED", {"fallback": "single_subtask"}))
        subtasks = fallback_subtasks

    # Validate DAG for cycles
    if not validate_subtask_dag(subtasks):
        print("\033[93m[planner] Cycle detected in subtask dependencies; falling back to single whole-plan subtask\033[0m")
        _persist_task(task_id, lambda: log_event(task_id, "DAG_CYCLE_DETECTED", {"fallback": "single_subtask"}))
        subtasks = fallback_subtasks

    # Persist subtasks
    if task_id:
        _persist_task(task_id, lambda: create_subtasks(task_id, subtasks))

    return subtasks


def resolve_plan(task: str, index_summary: str, approval_setting: str, mode: str, interactive_clarifications: bool = True, task_id: str | None = None) -> dict:
    """Run the clarify -> plan -> architecture -> approval -> DAG pipeline for one task.

    Returns a dict with: status ("ready" or "rejected"), task, plan, architecture, decision, mode, subtasks.
    """
    agent_mod = sys.modules.get("qz_agent")
    _call_planner = getattr(agent_mod, "call_planner", call_planner) if agent_mod else call_planner
    _call_architect = getattr(agent_mod, "call_architect", call_architect) if agent_mod else call_architect
    _generate_clarifying_questions = getattr(agent_mod, "generate_clarifying_questions", generate_clarifying_questions) if agent_mod else generate_clarifying_questions
    _ask_clarifying_questions = getattr(agent_mod, "ask_clarifying_questions", ask_clarifying_questions) if agent_mod else ask_clarifying_questions
    _format_clarifications = getattr(agent_mod, "format_clarifications", format_clarifications) if agent_mod else format_clarifications
    _requires_plan_approval = getattr(agent_mod, "requires_plan_approval", requires_plan_approval) if agent_mod else requires_plan_approval
    _prompt_plan_approval = getattr(agent_mod, "prompt_plan_approval", prompt_plan_approval) if agent_mod else prompt_plan_approval
    _convert_to_subtasks = getattr(agent_mod, "convert_plan_to_subtasks", convert_plan_to_subtasks) if agent_mod else convert_plan_to_subtasks

    # Compile locally before any provider call. This selects small, relevant
    # project facts and repository context without reviving prior chats or
    # spending an API request merely to optimize a prompt.
    compiled = TaskCompiler().compile(task, _get_workspace())
    memory_text = "\n".join(
        f"- [{entry.get('category', 'fact')}] {entry.get('content', '')}"
        for entry in compiled.project_memory
    )
    if memory_text:
        index_summary = f"{index_summary}\n\nRelevant project memory (repository remains source of truth):\n{memory_text}"
    result = {"mode": mode, "task": task, "decision": None, "compiler": compiled.to_dict()}
    _persist_task(task_id, lambda: log_event(task_id, "TASK_COMPILED", compiled.to_dict()))

    if mode == "quick":
        quick_plan = "Quick task — implement directly without a formal plan."
        quick_arch = "No structural plan needed; keep the existing project structure."
        quick_subtasks = [{"id": 0, "title": "Implement quick task", "description": task, "depends_on": []}]
        result.update(
            status="ready",
            plan=quick_plan,
            architecture=quick_arch,
            subtasks=quick_subtasks,
        )
        _persist_task(task_id, lambda: update_status(task_id, TaskStatus.PLANNING, current_step="planning", plan_text=result["plan"]))
        _persist_task(task_id, lambda: log_event(task_id, "PLAN_CREATED", {"mode": "quick"}))
        _persist_task(task_id, lambda: create_subtasks(task_id, quick_subtasks))
        return result

    _persist_task(task_id, lambda: update_status(task_id, TaskStatus.PLANNING, current_step="planning"))
    questions = _generate_clarifying_questions(task, index_summary)
    if questions and interactive_clarifications:
        qa_pairs = _ask_clarifying_questions(questions)
        clarification_text = _format_clarifications(qa_pairs)
        task = f"{task}\n\n{clarification_text}"
        result["task"] = task

    print("\033[93m=== CREATING PLAN ===\033[0m")
    plan = _call_planner(task, index_summary)
    print(plan)
    _persist_task(task_id, lambda: update_status(task_id, TaskStatus.PLANNING, current_step="planning", plan_text=plan))
    _persist_task(task_id, lambda: log_event(task_id, "PLAN_CREATED", {"mode": mode}))

    print("\n\033[93m=== DESIGNING ARCHITECTURE ===\033[0m")
    architecture = _call_architect(task, plan, index_summary)
    print(architecture)

    if _requires_plan_approval(mode, approval_setting):
        _persist_task(task_id, lambda: update_status(task_id, TaskStatus.WAITING_APPROVAL, current_step="waiting_approval"))
        _persist_task(task_id, lambda: log_event(task_id, "WAITING_APPROVAL", {"mode": mode}))
        decision, plan, architecture = _prompt_plan_approval(plan, architecture)
        result["decision"] = decision
        if decision == "rejected":
            result["status"] = "rejected"
            _persist_task(task_id, lambda: update_status(task_id, TaskStatus.CANCELLED, current_step="cancelled"))
            _persist_task(task_id, lambda: log_event(task_id, "TASK_CANCELLED", {"reason": "plan_rejected"}))
            return result
        _persist_task(task_id, lambda: log_event(task_id, "APPROVAL_RECEIVED", {"decision": decision}))

    # Plan is finalized -> convert to structured DAG
    subtasks = _convert_to_subtasks(task, plan, architecture, task_id=task_id)
    result.update(status="ready", plan=plan, architecture=architecture, subtasks=subtasks)
    return result
