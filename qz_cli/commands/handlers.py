"""Status, Diff, Rollback, History, and Rules slash commands."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.markdown import Markdown

from qz_keystore import KeyStore
from qz_storage import get_storage
from qz_cli.formatters import render_status_table, render_diff
from qz_security.rules_loader import load_project_rules


def handle_status_command(console: Console, workspace: Path) -> None:
    ks = KeyStore()
    entries = ks.list_entries()
    storage = get_storage()
    metrics = storage.get_metrics_summary()

    configured_providers = {e.provider.lower(): e for e in entries}
    all_providers = [
        {"provider_id": "gemini", "display_name": "Google Gemini", "models": ["gemini-3.6-flash", "gemini-3.6-pro"], "configured": "gemini" in configured_providers},
        {"provider_id": "groq", "display_name": "Groq LPU Accelerator", "models": ["llama-3.3-70b-versatile"], "configured": "groq" in configured_providers},
        {"provider_id": "mistral", "display_name": "Mistral AI", "models": ["codestral-latest", "mistral-large"], "configured": "mistral" in configured_providers},
        {"provider_id": "openrouter", "display_name": "OpenRouter Mesh", "models": ["anthropic/claude-3.5-sonnet", "deepseek-r1"], "configured": "openrouter" in configured_providers},
        {"provider_id": "deepseek", "display_name": "DeepSeek AI", "models": ["deepseek-chat", "deepseek-reasoner"], "configured": "deepseek" in configured_providers},
    ]

    render_status_table(console, all_providers, ks.backend_name())

    # Print usage summary panel
    usage_text = Text()
    usage_text.append(f"• Total Model Invocations: {metrics['total_calls']}\n", style="bold white")
    usage_text.append(f"• Overall Success Rate: {metrics['success_rate']:.1f}%\n", style="green" if metrics['success_rate'] >= 80 else "red")
    usage_text.append(f"• Total Tokens Processed: {metrics['total_tokens']:,}\n", style="cyan")
    usage_text.append(f"• Average Model Latency: {metrics['avg_latency_ms']:.0f}ms\n", style="yellow")
    usage_text.append(f"• Storage Database: {storage.db_path}\n", style="dim white")

    console.print(Panel(usage_text, title="[bold cyan]Telemetry & Health Metrics[/bold cyan]", border_style="cyan"))


def handle_diff_command(console: Console, workspace: Path) -> None:
    try:
        res = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        diff_out = res.stdout
        if not diff_out.strip():
            # Check untracked files
            status_res = subprocess.run(
                ["git", "status", "--short"],
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=3.0,
            )
            if status_res.stdout.strip():
                console.print(Panel(status_res.stdout, title="[bold yellow]Git Status (Untracked Files)[/bold yellow]", border_style="yellow"))
            else:
                console.print("[green]✔ Working tree clean — no uncommitted changes.[/green]\n")
            return

        render_diff(console, "Working Tree vs HEAD", diff_out)
    except Exception as e:
        console.print(f"[red]Failed to get git diff:[/red] {e}\n")


def handle_rollback_command(console: Console, workspace: Path, args: List[str]) -> None:
    from qz_recovery import get_resume_manager
    mgr = get_resume_manager(str(workspace))
    storage = get_storage()
    checkpoints = storage.list_checkpoints(limit=10)

    if not checkpoints:
        console.print("[yellow]No recent checkpoints recorded in storage.[/yellow]\n")
        return

    table = Table(title="[bold cyan]Recent Task Checkpoints[/bold cyan]", border_style="cyan")
    table.add_column("ID", style="bold white")
    table.add_column("Task ID", style="dim cyan")
    table.add_column("Git Commit", style="yellow")
    table.add_column("Step Name", style="bold green")
    table.add_column("Timestamp", style="dim white")

    for cp in checkpoints:
        table.add_row(
            str(cp["id"]),
            str(cp["task_id"])[:8],
            str(cp["git_hash"])[:8],
            str(cp["step_name"]),
            str(cp["timestamp"])[:19].replace("T", " "),
        )
    console.print(table)

    if args:
        target_cp = args[0]
        console.print(f"[bold yellow]Reverting workspace to checkpoint {target_cp}...[/bold yellow]")
        # Git rollback logic
        try:
            matched = next((c for c in checkpoints if str(c["id"]) == target_cp or c["git_hash"].startswith(target_cp)), None)
            if matched:
                subprocess.run(["git", "checkout", matched["git_hash"]], cwd=str(workspace), check=True)
                console.print(f"[bold green]✔ Successfully reverted to {matched['git_hash'][:8]}.[/bold green]\n")
            else:
                console.print(f"[red]Checkpoint {target_cp} not found.[/red]\n")
        except Exception as e:
            console.print(f"[red]Rollback failed:[/red] {e}\n")


def handle_history_command(console: Console, workspace: Path) -> None:
    storage = get_storage()
    tasks = storage.list_recent_tasks(limit=10)

    if not tasks:
        console.print("[yellow]No task history recorded yet.[/yellow]\n")
        return

    table = Table(title="[bold cyan]Recent Autonomous Tasks[/bold cyan]", border_style="cyan")
    table.add_column("Task ID", style="bold white")
    table.add_column("Prompt", style="white")
    table.add_column("Status", style="bold")
    table.add_column("Tokens", style="yellow")
    table.add_column("Duration", style="dim white")

    for t in tasks:
        status_color = "green" if t["status"] == "completed" else ("red" if t["status"] == "failed" else "yellow")
        table.add_row(
            str(t["id"])[:8],
            str(t["prompt"])[:45] + ("..." if len(t["prompt"]) > 45 else ""),
            f"[{status_color}]{t['status'].upper()}[/{status_color}]",
            f"{t.get('tokens_used', 0):,}",
            f"{t.get('latency_ms', 0):.0f}ms",
        )
    console.print(table)


def handle_rules_command(console: Console, workspace: Path) -> None:
    rules = load_project_rules(workspace)
    rules_file = workspace / ".qazterion" / "rules.md"
    
    console.print(f"[bold cyan]Active Project Rules[/bold cyan] (Source: {rules_file if rules_file.exists() else 'Default Built-in'})\n")
    if rules.raw_content:
        console.print(Panel(Markdown(rules.raw_content), title="[bold green].qazterion/rules.md[/bold green]", border_style="green"))
    else:
        console.print("[dim]No custom .qazterion/rules.md found. Using standard autonomous safe practices.[/dim]\n")
