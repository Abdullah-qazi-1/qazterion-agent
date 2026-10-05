"""Rich renderers for plans, diffs, permission prompts and status tables."""
from __future__ import annotations

import re
import sys
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

_LIST_PREFIX = re.compile(r"^[0-9.*\-\s]+")


def render_diff(console: Console, file_path: str, diff_text: str) -> None:
    """Render a git diff inside a box with green/red line highlighting."""
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
    console.print(Panel(text, title=f"[bold cyan]Diff: {file_path}[/bold cyan]", border_style="cyan", padding=(0, 1)))


def render_plan(console: Console, plan: str, architecture: str = "") -> None:
    console.print("\n[bold cyan]● Plan[/bold cyan]\n")
    lines = [line.strip() for line in (plan or "").splitlines() if line.strip()]
    for idx, line in enumerate(lines, 1):
        clean = _LIST_PREFIX.sub("", line)
        console.print(f"  [cyan]{idx}.[/cyan] [white]{escape(clean)}[/white]")
    if architecture.strip():
        console.print(Panel(architecture.strip(), title="[bold cyan]Structure[/bold cyan]", border_style="dim cyan"))
    console.print("")


def render_completion_bar(console: Console, files_changed: int, tests_passed: int, duration_ms: float = 0.0) -> None:
    console.print("\n[dim]" + "─" * 72 + "[/dim]")
    files_txt = "1 file changed" if files_changed == 1 else f"{files_changed} files changed"
    tests_txt = f"{tests_passed} verification run(s) passed" if tests_passed else "Verified"
    seconds = f"   [dim]{duration_ms / 1000:.0f}s[/dim]" if duration_ms else ""
    console.print(f"  [bold green]✓ Completed[/bold green]   [white]{files_txt}[/white]   [green]{tests_txt}[/green]{seconds}")
    console.print("[dim]" + "─" * 72 + "[/dim]\n")


def render_permission_request(console: Console, action: str, details: dict[str, Any], risk: str = "", reasons: list[str] | None = None) -> str:
    """Ask the user to approve a risky action. Returns "allow", "always" or "deny"."""
    if not sys.stdin or not sys.stdin.isatty():
        return "deny"
    text = Text()
    text.append("Action: ", style="bold yellow")
    text.append(f"{action}" + (f"  ({risk} risk)" if risk else "") + "\n", style="bold white")
    for key, value in details.items():
        text.append(f"  • {key}: ", style="dim cyan")
        text.append(f"{value}\n", style="white")
    for reason in reasons or []:
        text.append(f"  • why: {reason}\n", style="dim yellow")
    console.print(Panel(text, title="[bold red]⚠ Permission Required[/bold red]", border_style="red", padding=(0, 1)))
    console.print("[bold yellow]Allow?[/bold yellow] ([green]y[/green] yes / [yellow]a[/yellow] always this session / [red]N[/red] no): ", end="")
    try:
        choice = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "deny"
    if choice in ("y", "yes"):
        return "allow"
    if choice in ("a", "always"):
        return "always"
    return "deny"


def render_status_table(console: Console, providers: list[dict[str, Any]], keystore_backend: str) -> None:
    """Providers, their keys (masked) and live health."""
    table = Table(title="[bold cyan]Providers & Keys[/bold cyan]", border_style="cyan")
    table.add_column("Provider", style="bold white")
    table.add_column("Key", style="dim cyan")
    table.add_column("Source", style="dim")
    table.add_column("Status", style="bold")
    for provider in providers:
        name = provider.get("display_name") or provider.get("provider_id")
        if not provider.get("enabled", True):
            table.add_row(str(name), "-", "-", "[dim]disabled[/dim]")
            continue
        keys = provider.get("keys") or []
        if not keys:
            table.add_row(str(name), "-", "-", "[dim red]no key[/dim red]")
            continue
        for key in keys:
            if not key.get("enabled", True):
                state = "[dim]disabled[/dim]"
            elif key.get("disabled_reason"):
                state = "[red]rejected (invalid/expired)[/red]"
            elif not key.get("available"):
                state = f"[yellow]cooling down {key.get('cooldown_remaining_s', 0):.0f}s[/yellow]"
            else:
                state = "[green]ready[/green]"
            table.add_row(str(name), f"{key['key_id']} {key.get('masked_value', '')}", key.get("source", ""), state)
            name = ""
    console.print(table)
    console.print(f"[dim]Keystore encryption: {keystore_backend}[/dim]")


def render_roles_table(console: Console, roles: dict[str, Any]) -> None:
    table = Table(title="[bold cyan]Roles → models (in preference order)[/bold cyan]", border_style="cyan")
    table.add_column("Role", style="bold white")
    table.add_column("Usable now", style="green")
    table.add_column("Skipped", style="dim")
    for role, info in roles.items():
        table.add_row(role, "\n".join(info.get("usable") or ["-"]), "\n".join(info.get("skipped") or []))
    console.print(table)
