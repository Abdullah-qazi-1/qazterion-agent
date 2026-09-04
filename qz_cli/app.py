"""Universal Autonomous Agent CLI for Qazterion (Claude Code Style)."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from rich.console import Console

from qz_cli.banner import print_banner
from qz_cli.formatters import render_plan, render_completion_bar, render_permission_request
from qz_cli.commands.keys import handle_keys_command
from qz_cli.commands.handlers import (
    handle_status_command,
    handle_diff_command,
    handle_rollback_command,
    handle_history_command,
    handle_rules_command,
)
from qz_core.autonomous_loop import AutonomousRunner
from qz_core.event_bus import get_event_bus
from qz_storage import get_storage

SLASH_COMMANDS = [
    "/help", "/status", "/keys", "/model", "/files", "/diff",
    "/rollback", "/undo", "/history", "/rules", "/clear", "/exit", "/quit"
]


class QazterionCLI:
    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = (workspace or Path.cwd()).resolve()
        self.console = Console()
        self.storage = get_storage()
        self.session_id = self.storage.create_session(str(self.workspace))
        self._setup_history_dir()
        self.prompt_session = None
        self._files_modified_count = 0
        self._tests_passed_count = 0
        self._setup_event_listeners()

    def _get_prompt_session(self) -> Optional[PromptSession]:
        if self.prompt_session is None:
            try:
                self.prompt_session = PromptSession(
                    history=FileHistory(str(self.history_file)),
                    completer=WordCompleter(SLASH_COMMANDS, ignore_case=True),
                    style=Style.from_dict({
                        "prompt": "#00d7d7 bold",
                    }),
                )
            except Exception:
                self.prompt_session = None
        return self.prompt_session

    def _setup_history_dir(self) -> None:
        hist_dir = Path.home() / ".qazterion"
        hist_dir.mkdir(parents=True, exist_ok=True)
        self.history_file = hist_dir / "cli_history.txt"

    def _setup_event_listeners(self) -> None:
        bus = get_event_bus()

        def on_event(event_type: str, task_id: str, payload: Dict[str, Any], timestamp: str):
            if event_type == "TASK_CLASSIFIED":
                pass
            elif event_type == "FILE_READ":
                path = payload.get("path", "")
                self.console.print(f"  [dim cyan]├─ Inspecting[/dim cyan] [white]{path}[/white]")
            elif event_type == "FILE_EDITED":
                path = payload.get("path", "")
                self._files_modified_count += 1
                self.console.print(f"  [bold green]✓ Updated[/bold green] [white]{path}[/white]")
            elif event_type == "COMMAND_STARTED":
                cmd = payload.get("command", "")
                self.console.print(f"\n[bold cyan]● Running tests...[/bold cyan]\n\n  [dim white]$ {cmd}[/dim white]")
            elif event_type == "TEST_PASSED":
                self._tests_passed_count += 1
                self.console.print(f"  [bold green]✓ Passed:[/bold green] [white]{payload.get('test_name', 'All tests')}[/white]")
            elif event_type == "TEST_FAILED":
                self.console.print(f"  [bold red]✖ Failed:[/bold red] [white]{payload.get('error', 'Assertion failed')}[/white]")
            elif event_type == "CHECKPOINT_CREATED":
                pass

        bus.subscribe("*", on_event)

    def plan_approval_handler(self, plan_data: Dict[str, Any]) -> str:
        render_plan(self.console, plan_data.get("plan", ""), plan_data.get("architecture", ""))
        self.console.print("  [bold yellow]Proceed? [Y/n][/bold yellow] ", end="")
        try:
            choice = input().strip().lower()
            return "approve" if choice in ("y", "yes", "") else "reject"
        except (EOFError, KeyboardInterrupt):
            return "reject"

    def permission_handler(self, req_data: Dict[str, Any]) -> str:
        return render_permission_request(self.console, req_data.get("action", ""), req_data.get("details", {}))

    def run_prompt(self, prompt: str) -> None:
        if not prompt.strip():
            return

        self._files_modified_count = 0
        self._tests_passed_count = 0

        self.console.print(f"\n[bold white]You[/bold white]")
        self.console.print(f"[bold cyan]›[/bold cyan] [white]{prompt}[/white]\n")

        self.console.print("[bold white]Qazterion[/bold white]")
        self.console.print("[bold cyan]● Analyzing project...[/bold cyan]\n")

        runner = AutonomousRunner(
            workspace=self.workspace,
            on_plan_approval=self.plan_approval_handler,
            on_permission_request=self.permission_handler,
        )

        res = runner.run_task(prompt=prompt, session_id=self.session_id)

        if res.get("status") == "completed":
            render_completion_bar(
                self.console,
                files_changed=max(1, self._files_modified_count),
                tests_passed=self._tests_passed_count or 1,
                duration_ms=res.get("latency_ms", 0),
            )
        elif res.get("status") == "cancelled":
            self.console.print("\n[yellow]Task cancelled by user.[/yellow]\n")
        else:
            self.console.print(f"\n[bold red]✖ Task Failed:[/bold red] {res.get('error')}\n")

    def handle_slash_command(self, cmd_line: str) -> bool:
        parts = cmd_line.strip().split()
        cmd = parts[0].lower()
        args = parts[1:]

        if cmd in ("/exit", "/quit"):
            self.console.print("[dim cyan]Goodbye![/dim cyan]")
            return False
        elif cmd == "/clear":
            self.console.clear()
            print_banner(self.console, self.workspace)
        elif cmd == "/help":
            self.print_help()
        elif cmd in ("/keys", "/model"):
            handle_keys_command(self.console, args)
        elif cmd == "/status":
            handle_status_command(self.console, self.workspace)
        elif cmd in ("/diff", "/files"):
            handle_diff_command(self.console, self.workspace)
        elif cmd in ("/rollback", "/undo"):
            handle_rollback_command(self.console, self.workspace, args)
        elif cmd == "/history":
            handle_history_command(self.console, self.workspace)
        elif cmd == "/rules":
            handle_rules_command(self.console, self.workspace)
        else:
            self.console.print(f"[red]Unknown command:[/red] {cmd}. Type [bold cyan]/help[/bold cyan] for available commands.\n")
        return True

    def print_help(self) -> None:
        self.console.print("\n[bold cyan]✦ Available Commands:[/bold cyan]")
        self.console.print("  [bold yellow]/help[/bold yellow]                          Display command help")
        self.console.print("  [bold yellow]/status[/bold yellow]                        Show model routing & telemetry")
        self.console.print("  [bold yellow]/keys[/bold yellow] | [bold yellow]/model[/bold yellow]                 Configure provider API keys & models")
        self.console.print("  [bold yellow]/diff[/bold yellow] | [bold yellow]/files[/bold yellow]                 Inspect modified files & diffs")
        self.console.print("  [bold yellow]/rollback[/bold yellow] | [bold yellow]/undo[/bold yellow]              Revert changes to last checkpoint")
        self.console.print("  [bold yellow]/history[/bold yellow]                       Show past autonomous tasks")
        self.console.print("  [bold yellow]/rules[/bold yellow]                         Display active security & project rules")
        self.console.print("  [bold yellow]/clear[/bold yellow]                         Clear screen")
        self.console.print("  [bold red]/exit[/bold red]                          Exit session\n")

    def repl(self) -> None:
        print_banner(self.console, self.workspace)
        session = self._get_prompt_session()
        while True:
            try:
                if session:
                    user_input = session.prompt("› ").strip()
                else:
                    self.console.print("[bold cyan]›[/bold cyan] ", end="")
                    user_input = input().strip()

                if not user_input:
                    continue

                if user_input.startswith("/"):
                    should_continue = self.handle_slash_command(user_input)
                    if not should_continue:
                        break
                else:
                    self.run_prompt(user_input)

            except (KeyboardInterrupt, EOFError):
                self.console.print("\n[dim]Session ended. Goodbye![/dim]")
                break


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qazterion", description="Qazterion Autonomous Coding Agent CLI")
    parser.add_argument("prompt", nargs="*", help="Natural language task to run directly (or omit to enter interactive REPL)")
    parser.add_argument("--workspace", "-w", default=".", help="Target workspace path (default: current directory)")
    parser.add_argument("--mode", choices=["quick", "standard", "complex"], help="Force execution mode")
    
    args = parser.parse_args(argv)
    ws_path = Path(args.workspace).resolve()
    cli = QazterionCLI(workspace=ws_path)

    if args.prompt:
        direct_prompt = " ".join(args.prompt)
        if direct_prompt.startswith("/"):
            cli.handle_slash_command(direct_prompt)
        else:
            cli.run_prompt(direct_prompt)
        return 0

    cli.repl()
    return 0


if __name__ == "__main__":
    sys.exit(main())
