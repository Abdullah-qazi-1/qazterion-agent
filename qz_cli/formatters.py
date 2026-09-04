"""Rich formatters for diffs, plan trees, tables, and permissions."""
from __future__ import annotations

import re
from typing import Any, Dict, List
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from rich.markdown import Markdown


def render_diff(console: Console, file_path: str, diff_text: str) -> None:
    """Render a git diff inside a formatted box with green/red line highlighting."""
    if not diff_text.strip():
        console.print(f"[dim yellow]No changes detected in {file_path}[/dim yellow]")
        return

    text = Text()
    for line in diff_text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            text.append(line + "\n", style="bold green")
        elif line.startswith("-") and not line.startswith("---"):
            text.append(line + "\n", style="bold red")
        elif line.startswith("@@"):
            text.append(line + "\n", style="cyan")
        else:
            text.append(line + "\n", style="dim white")

    console.print(
        Panel(
            text,
            title=f"[bold cyan]Diff: {file_path}[/bold cyan]",
            border_style="cyan",
            padding=(0, 1),
        )
    )


def render_plan(console: Console, plan: str, architecture: str = "") -> None:
    """Render the generated multi-step plan in Claude Code clean style."""
    console.print("\n[bold white]Qazterion[/bold white]")
    console.print("[bold cyan]● Plan[/bold cyan]\n")
    if plan:
        lines = [l.strip() for l in plan.splitlines() if l.strip()]
        for idx, line in enumerate(lines, 1):
            clean_line = re.sub(r"^[\d\.\-\*\s]+", "", line)
            console.print(f"  [cyan]{idx}.[/cyan] [white]{clean_line}[/white]")
    else:
        console.print("  [dim]1. Inspect workspace files\n  2. Apply modifications\n  3. Run verification tests[/dim]")
    console.print("")


def render_completion_bar(console: Console, files_changed: int, tests_passed: int, duration_ms: float = 0.0) -> None:
    """Render the completion bar at the end of a task."""
    console.print("\n[dim]────────────────────────────────────────────────────────────────────────[/dim]")
    files_txt = f"{files_changed} files changed" if files_changed != 1 else "1 file changed"
    tests_txt = f"{tests_passed} tests passed" if tests_passed else "Verified"
    console.print(f"  [bold green]✓ Completed[/bold green]   [white]{files_txt}[/white]   [green]{tests_txt}[/green]")
    console.print("[dim]────────────────────────────────────────────────────────────────────────[/dim]\n")



def render_permission_request(console: Console, action: str, details: Dict[str, Any]) -> str:
    """Prompt user for confirmation on dangerous operations."""
    text = Text()
    text.append("Action: ", style="bold yellow")
    text.append(f"{action}\n", style="bold white")
    for k, v in details.items():
        text.append(f"  • {k}: ", style="dim cyan")
        text.append(f"{v}\n", style="white")

    console.print(
        Panel(
            text,
            title="[bold red]⚠ Permission Required[/bold red]",
            border_style="red",
            padding=(0, 1),
        )
    )
    console.print("[bold yellow]Allow this action?[/bold yellow] ([green]y[/green]=Yes / [yellow]a[/yellow]=Always in session / [red]n[/red]=Deny): ", end="")
    try:
        choice = input().strip().lower()
        if choice in ("y", "yes"):
            return "allow"
        elif choice in ("a", "always"):
            return "always"
        return "deny"
    except (EOFError, KeyboardInterrupt):
        return "deny"


def render_status_table(console: Console, providers_data: List[Dict[str, Any]], keystore_backend: str) -> None:
    """Render Rich status table showing providers, models, keys, and health."""
    table = Table(title="[bold cyan]Qazterion Provider & Routing Matrix[/bold cyan]", border_style="cyan")
    table.add_column("Provider", style="bold white")
    table.add_column("Models", style="dim cyan")
    table.add_column("Keys Configured", style="yellow")
    table.add_column("Status", style="bold")
    table.add_column("Encryption", style="dim green")

    for p in providers_data:
        is_conf = p.get("configured", False) or p.get("key_count", 0) > 0
        status_text = "[bold green]✔ READY[/bold green]" if is_conf else "[dim red]✖ NOT CONFIGURED[/dim red]"
        key_count_str = f"{p.get('key_count', 1 if is_conf else 0)} Active Key(s)" if is_conf else "0 Keys"
        models_str = p.get("models_str") or ", ".join(p.get("models", [])) or "Auto-Discovered"
        
        table.add_row(
            str(p.get("display_name") or p.get("provider_id") or p.get("id")).capitalize(),
            models_str[:40],
            key_count_str,
            status_text,
            f"OS {keystore_backend.upper()}",
        )

    console.print(table)
