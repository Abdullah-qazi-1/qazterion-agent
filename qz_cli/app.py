"""Qazterion command-line interface (``qazterion`` / ``qz``)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markup import escape

from qz_paths import data_dir, load_environment

SLASH_COMMANDS = [
    "/help", "/status", "/keys", "/model", "/files", "/diff",
    "/rollback", "/undo", "/history", "/rules", "/clear", "/exit", "/quit",
]

_STREAMS_CONFIGURED = False


def _configure_streams() -> None:
    """Windows consoles default to cp1252; model output must never crash printing."""
    global _STREAMS_CONFIGURED
    if _STREAMS_CONFIGURED:
        return
    _STREAMS_CONFIGURED = True
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _make_history(path: Path):
    """Prompt history that never records lines which could contain secrets."""
    from prompt_toolkit.history import FileHistory

    class _SafeHistory(FileHistory):
        def store_string(self, string: str) -> None:
            if string.strip().lower().startswith("/keys"):
                return
            super().store_string(string)

    return _SafeHistory(str(path))


class QazterionCLI:
    def __init__(self, workspace: Path | None = None, mode: str | None = None,
                 approval: str | None = None, auto_setup: bool = False) -> None:
        self.workspace = (workspace or Path.cwd()).resolve()
        self.console = Console()
        self.mode = mode
        self.approval = approval
        self.auto_setup = auto_setup
        self.history_file = data_dir() / "cli_history.txt"
        self.prompt_session = None
        self._always_allowed: set[str] = set()
        self._active_task: str | None = None
        self._files_modified: set[str] = set()
        self._verifications_passed = 0
        self._subscribed = False

    # ---- live progress -------------------------------------------------------

    def _on_event(self, task_id: str, event_type: str, payload: dict | None, _timestamp: str) -> None:
        if self._active_task is None or task_id != self._active_task:
            return
        p = payload or {}
        c = self.console
        if event_type == "TASK_CLASSIFIED":
            c.print(f"  [dim]mode: {escape(str(p.get('mode')))}[/dim]")
        elif event_type == "NODE_STARTED":
            c.print(f"\n[bold cyan]● {escape(str(p.get('title', 'Subtask')))}[/bold cyan]")
        elif event_type == "TOOL_STARTED":
            tool = p.get("tool")
            if tool == "run_command":
                c.print(f"  [dim white]$ {escape(str(p.get('command', '')))}[/dim white]")
            elif tool == "read_file" and p.get("path"):
                c.print(f"  [dim cyan]├─ reading[/dim cyan] {escape(str(p['path']))}")
        elif event_type == "TOOL_FINISHED" and p.get("tool") == "run_command":
            if p.get("ok"):
                self._verifications_passed += 1
                c.print("  [green]✓ command succeeded[/green]")
            else:
                c.print("  [red]✖ command failed[/red]")
        elif event_type == "FILE_EDITED":
            path = str(p.get("path", ""))
            self._files_modified.add(path)
            c.print(f"  [bold green]✓ updated[/bold green] {escape(path)}")
        elif event_type == "MODEL_FALLBACK":
            c.print(f"  [dim]↪ model failover: {escape(str(p.get('from')))} → {escape(str(p.get('to')))}[/dim]")
        elif event_type == "MODEL_ESCALATED":
            c.print(f"  [dim]↑ escalating to the '{escape(str(p.get('role')))}' role after repeated failures[/dim]")
        elif event_type == "NODE_RETRYING":
            c.print(f"  [yellow]↻ validation failed, repairing (attempt {p.get('attempt')})[/yellow]")
        elif event_type == "NODE_FAILED":
            c.print(f"  [red]✖ {escape(str(p.get('error', 'subtask failed')))}[/red]")
        elif event_type == "CHECKPOINT_CREATED" and p.get("git_commit_hash"):
            c.print(f"  [dim]checkpoint {str(p['git_commit_hash'])[:8]}[/dim]")

    def _set_active_task(self, task_id: str) -> None:
        self._active_task = task_id

    def _subscribe(self) -> None:
        if not self._subscribed:
            from qz_tasks.task_manager import subscribe_events

            subscribe_events(self._on_event)
            self._subscribed = True

    # ---- approvals ---------------------------------------------------------------

    def plan_approval_handler(self, plan_data: dict[str, Any]) -> Any:
        from qz_cli.formatters import render_plan

        if not sys.stdin or not sys.stdin.isatty():
            self.console.print("[yellow]Plan needs approval but no terminal is attached; rejecting. "
                               "Use --approval never for unattended runs.[/yellow]")
            return "reject"
        render_plan(self.console, plan_data.get("plan", ""), plan_data.get("architecture", ""))
        self.console.print("  [bold yellow]Proceed? [Y]es / [e]dit / [n]o:[/bold yellow] ", end="")
        try:
            choice = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "reject"
        if choice in ("", "y", "yes"):
            return "approve"
        if choice in ("e", "edit"):
            self.console.print("Type the replacement plan; finish with a line containing only END:")
            lines: list[str] = []
            while True:
                try:
                    line = input()
                except (EOFError, KeyboardInterrupt):
                    break
                if line.strip() == "END":
                    break
                lines.append(line)
            return {"decision": "edit", "plan": "\n".join(lines)} if any(l.strip() for l in lines) else "approve"
        return "reject"

    def permission_handler(self, request: dict[str, Any]) -> bool:
        from qz_cli.formatters import render_permission_request

        details = request.get("details", {})
        signature = f"{request.get('action')}:{details.get('command') or details.get('path') or ''}"
        if signature in self._always_allowed:
            return True
        decision = render_permission_request(
            self.console, str(request.get("action", "")), details,
            risk=str(request.get("risk", "")), reasons=request.get("reasons") or [],
        )
        if decision == "always":
            self._always_allowed.add(signature)
        return decision in ("allow", "always")

    # ---- running tasks -------------------------------------------------------------

    def run_prompt(self, prompt: str) -> dict[str, Any] | None:
        if not prompt.strip():
            return None
        from qz_cli.formatters import render_completion_bar
        from qz_core.autonomous_loop import AutonomousRunner

        self._subscribe()
        self._files_modified.clear()
        self._verifications_passed = 0
        self.console.print(f"\n[bold cyan]›[/bold cyan] [white]{escape(prompt)}[/white]\n")
        self.console.print("[bold cyan]● Analyzing project...[/bold cyan]")

        runner = AutonomousRunner(
            workspace=self.workspace,
            on_plan_approval=self.plan_approval_handler,
            on_permission_request=self.permission_handler,
            approval_policy=self.approval,
            auto_setup=self.auto_setup,
            on_task_created=self._set_active_task,
        )
        try:
            res = runner.run_task(prompt=prompt, mode_override=self.mode)
        finally:
            self._active_task = None

        if res.get("status") == "completed":
            if res.get("result"):
                self.console.print(f"\n{escape(str(res['result']))}")
            render_completion_bar(self.console, files_changed=len(self._files_modified),
                                  tests_passed=self._verifications_passed, duration_ms=res.get("latency_ms", 0))
        elif res.get("status") == "cancelled":
            self.console.print(f"\n[yellow]{escape(str(res.get('message', 'Task cancelled.')))}[/yellow]\n")
        else:
            self.console.print(f"\n[bold red]✖ Task failed:[/bold red] {escape(str(res.get('error')))}\n")
        return res

    # ---- slash commands ------------------------------------------------------------------

    def handle_slash_command(self, cmd_line: str) -> bool:
        from qz_cli.commands import handlers
        from qz_cli.commands.keys import handle_keys_command

        parts = cmd_line.strip().split()
        cmd, args = parts[0].lower(), parts[1:]
        if cmd in ("/exit", "/quit"):
            self.console.print("[dim cyan]Goodbye![/dim cyan]")
            return False
        if cmd == "/clear":
            from qz_cli.banner import print_banner

            self.console.clear()
            print_banner(self.console, self.workspace)
        elif cmd == "/help":
            self.print_help()
        elif cmd == "/keys":
            handle_keys_command(self.console, args)
        elif cmd == "/model":
            handlers.handle_model_command(self.console, args)
        elif cmd == "/status":
            handlers.handle_status_command(self.console, self.workspace)
        elif cmd in ("/diff", "/files"):
            handlers.handle_diff_command(self.console, self.workspace)
        elif cmd in ("/rollback", "/undo"):
            handlers.handle_rollback_command(self.console, self.workspace, args)
        elif cmd == "/history":
            handlers.handle_history_command(self.console, self.workspace)
        elif cmd == "/rules":
            handlers.handle_rules_command(self.console, self.workspace)
        else:
            self.console.print(f"[red]Unknown command:[/red] {escape(cmd)}. Type [bold cyan]/help[/bold cyan].\n")
        return True

    def print_help(self) -> None:
        rows = [
            ("/status", "Providers, keys, roles, health and usage"),
            ("/keys", "list | add <provider> [index] | delete|enable|disable <provider> <index> | test [provider] | reset"),
            ("/model", "Show roles; /model <role> <provider/model>; /model strategy balanced|priority"),
            ("/diff", "Show uncommitted changes"),
            ("/rollback", "List checkpoints; /rollback <id> to undo Qazterion commits"),
            ("/history", "Recent tasks in this workspace"),
            ("/rules", "Project rules (.qazterion/rules.md)"),
            ("/clear", "Clear the screen"),
            ("/exit", "Quit"),
        ]
        self.console.print("\n[bold cyan]Commands[/bold cyan]")
        for name, text in rows:
            self.console.print(f"  [bold yellow]{name:<10}[/bold yellow] {escape(text)}")
        self.console.print()

    def repl(self) -> None:
        from qz_cli.banner import print_banner
        from qz_tasks.task_manager import announce_interrupted_tasks

        print_banner(self.console, self.workspace)
        announce_interrupted_tasks(stream=sys.stderr)
        session = None
        if sys.stdin and sys.stdin.isatty():
            try:
                from prompt_toolkit import PromptSession
                from prompt_toolkit.completion import WordCompleter

                session = PromptSession(history=_make_history(self.history_file),
                                        completer=WordCompleter(SLASH_COMMANDS, ignore_case=True))
            except Exception:
                session = None
        while True:
            try:
                user_input = (session.prompt("› ") if session else input("› ")).strip()
                if not user_input:
                    continue
                if user_input.startswith("/"):
                    if not self.handle_slash_command(user_input):
                        break
                else:
                    self.run_prompt(user_input)
            except (KeyboardInterrupt, EOFError):
                self.console.print("\n[dim]Session ended. Goodbye![/dim]")
                break


def main(argv: list[str] | None = None) -> int:
    _configure_streams()
    load_environment()
    parser = argparse.ArgumentParser(prog="qazterion", description="Qazterion multi-provider coding agent")
    parser.add_argument("prompt", nargs="*", help="Task to run directly (omit for the interactive REPL)")
    parser.add_argument("--workspace", "-w", default=".", help="Project directory (default: current directory)")
    parser.add_argument("--mode", choices=["quick", "standard", "complex"], help="Force the task mode")
    parser.add_argument("--approval", choices=["always", "complex", "never"],
                        help="When to pause for plan approval (default: QAZTERION_PLAN_APPROVAL or 'complex')")
    parser.add_argument("--auto-setup", action="store_true", help="Create a project .venv without asking")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    if not workspace.is_dir():
        print(f"Workspace does not exist: {workspace}", file=sys.stderr)
        return 2
    cli = QazterionCLI(workspace=workspace, mode=args.mode, approval=args.approval, auto_setup=args.auto_setup)

    if args.prompt:
        prompt = " ".join(args.prompt)
        if prompt.startswith("/"):
            cli.handle_slash_command(prompt)
            return 0
        result = cli.run_prompt(prompt) or {}
        return 0 if result.get("status") == "completed" else 1
    cli.repl()
    return 0


if __name__ == "__main__":
    sys.exit(main())
