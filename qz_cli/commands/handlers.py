"""/status, /model, /diff, /rollback, /history and /rules slash commands."""
from __future__ import annotations

import subprocess

from qz_sandbox.backend import NO_WINDOW
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from qz_cli.formatters import render_diff, render_roles_table, render_status_table
from qz_security.rules_loader import load_project_rules


def _gateway():
    from qz_providers.gateway import get_gateway

    return get_gateway()


def handle_status_command(console: Console, workspace: Path) -> None:
    from qz_keystore import KeyStore
    from qz_usage_tracker import get_usage_tracker

    status = _gateway().status()
    render_status_table(console, status["providers"], KeyStore().backend_name())
    render_roles_table(console, status["roles"])
    console.print(f"[dim]Key strategy: {status['key_strategy']}  •  transport: direct (no proxy)[/dim]")

    usage = get_usage_tracker().summary()
    pool = usage["global_pool"]
    text = Text()
    text.append(f"• Requests (recent): {usage['request_count']}  ", style="bold white")
    text.append(f"success {pool['success_rate']:.1f}%\n", style="green" if pool["success_rate"] >= 80 else "red")
    text.append(f"• Tokens: {usage['total_tokens']:,}   failovers: {usage['fallback_count']}\n", style="cyan")
    text.append(f"• Average latency: {pool['latency_ms']:.0f} ms\n", style="yellow")
    if usage.get("last_error"):
        text.append(f"• Last error: {escape(str(usage['last_error'])[:150])}\n", style="dim red")
    console.print(Panel(text, title="[bold cyan]Usage[/bold cyan]", border_style="cyan"))


def handle_model_command(console: Console, args: list[str]) -> None:
    """/model                      show roles
    /model <role> <provider/model>  put a model first for a role
    /model strategy balanced|priority"""
    from qz_providers.catalog import CatalogError, parse_model_ref

    gateway = _gateway()
    if not args:
        render_roles_table(console, gateway.status()["roles"])
        console.print("[dim]Use /model <role> <provider/model> to prefer a model, or /model strategy balanced|priority.[/dim]\n")
        return
    try:
        if args[0].lower() == "strategy" and len(args) > 1:
            console.print(f"[green]Key strategy set to {gateway.catalog.set_key_strategy(args[1])}.[/green]\n")
            return
        if len(args) < 2:
            raise CatalogError("Usage: /model <role> <provider/model>")
        provider, model = parse_model_ref(args[1])
        gateway.catalog.set_role_preference(args[0], provider, model)
        role = gateway.catalog.resolve_role(args[0]) or args[0]
        console.print(f"[green]Role '{role}' now prefers {provider}/{model}.[/green]\n")
    except CatalogError as error:
        console.print(f"[red]{escape(str(error))}[/red]\n")


def _git(workspace: Path, *args: str, timeout: float = 5.0) -> subprocess.CompletedProcess:
    from qz_sandbox.backend import sanitize_subprocess_env

    return subprocess.run(
        ["git", *args], cwd=str(workspace), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, env=sanitize_subprocess_env(str(workspace)),
        stdin=subprocess.DEVNULL,
        creationflags=NO_WINDOW,
    )


def handle_diff_command(console: Console, workspace: Path) -> None:
    try:
        diff_out = _git(workspace, "diff", "HEAD").stdout
        if diff_out.strip():
            render_diff(console, "Working Tree vs HEAD", diff_out)
            return
        status = _git(workspace, "status", "--short").stdout
        if status.strip():
            console.print(Panel(escape(status), title="[bold yellow]Git Status (Untracked Files)[/bold yellow]", border_style="yellow"))
        else:
            console.print("[green]✔ Working tree clean — no uncommitted changes.[/green]\n")
    except Exception as e:
        console.print(f"[red]Failed to get git diff:[/red] {escape(str(e))}\n")


def handle_rollback_command(console: Console, workspace: Path, args: list[str]) -> None:
    """/rollback            list this workspace's checkpoints
    /rollback <id>       undo the checkpoint's step and everything after it"""
    from qz_recovery import get_resume_manager
    from qz_tasks.task_manager import get_checkpoint, list_checkpoints

    if args:
        try:
            checkpoint = get_checkpoint(int(args[0]))
        except ValueError:
            checkpoint = None
        if checkpoint is None:
            console.print(f"[red]Checkpoint {escape(args[0])} not found.[/red]\n")
            return
        outcome = get_resume_manager(workspace).safe_rollback_to_checkpoint(
            task_id=str(checkpoint["task_id"]), checkpoint_id=int(checkpoint["id"]), workspace=workspace,
        )
        style = "bold green" if outcome.success else "bold red"
        console.print(f"[{style}]{escape(outcome.message)}[/{style}]\n")
        return

    checkpoints = list_checkpoints(workspace=str(workspace), limit=10)
    if not checkpoints:
        console.print("[yellow]No Qazterion checkpoints recorded for this workspace.[/yellow]\n")
        return
    table = Table(title="[bold cyan]Recent Checkpoints[/bold cyan]", border_style="cyan")
    for column in ("ID", "Task", "Commit", "Step", "Time"):
        table.add_column(column)
    for cp in checkpoints:
        table.add_row(
            str(cp["id"]),
            escape(str(cp.get("user_request") or cp["task_id"])[:40]),
            str(cp.get("git_commit_hash") or "")[:8],
            escape(str(cp.get("summary") or "")[:50]),
            str(cp.get("created_at") or "")[:19].replace("T", " "),
        )
    console.print(table)
    console.print("[dim]/rollback <ID> undoes that step and every later Qazterion commit (your own commits and uncommitted work are never discarded).[/dim]\n")


def handle_history_command(console: Console, workspace: Path) -> None:
    from qz_tasks.task_manager import list_recent_tasks
    from qz_usage_tracker import get_usage_tracker

    tasks = list_recent_tasks(workspace=str(workspace), limit=10)
    if not tasks:
        console.print("[yellow]No task history recorded yet.[/yellow]\n")
        return
    tracker = get_usage_tracker()
    table = Table(title="[bold cyan]Recent Tasks[/bold cyan]", border_style="cyan")
    for column in ("Task ID", "Request", "Status", "Tokens", "Started"):
        table.add_column(column)
    for task in tasks:
        status = str(task.get("status") or "").upper()
        color = "green" if status == "COMPLETED" else ("red" if status == "FAILED" else "yellow")
        request = str(task.get("user_request") or "")
        table.add_row(
            str(task["id"])[:8],
            escape(request[:45] + ("..." if len(request) > 45 else "")),
            f"[{color}]{status}[/{color}]",
            f"{tracker.get_task_usage(task['id'])['total_tokens']:,}",
            str(task.get("created_at") or "")[:19].replace("T", " "),
        )
    console.print(table)


def handle_rules_command(console: Console, workspace: Path) -> None:
    rules = load_project_rules(workspace)
    rules_file = workspace / ".qazterion" / "rules.md"
    source = rules_file if rules_file.exists() else "built-in defaults"
    console.print(f"[bold cyan]Active Project Rules[/bold cyan] (source: {escape(str(source))})\n")
    if rules.raw_content:
        console.print(Panel(Markdown(rules.raw_content), title="[bold green].qazterion/rules.md[/bold green]", border_style="green"))
    else:
        console.print("[dim]No .qazterion/rules.md found. Using standard safe practices.[/dim]\n")
