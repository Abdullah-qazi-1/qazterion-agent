"""Startup banner."""
from __future__ import annotations

import subprocess

from qz_sandbox.backend import NO_WINDOW
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

VERSION = "2.3.0"

ASCII_ART = """
   ██████╗  █████╗ ███████╗████████╗███████╗██████╗ ██╗ ██████╗ ███╗   ██╗
  ██╔═══██╗██╔══██╗╚══███╔╝╚══██╔══╝██╔════╝██╔══██╗██║██╔═══██╗████╗  ██║
  ██║   ██║███████║  ███╔╝    ██║   █████╗  ██████╔╝██║██║   ██║██╔██╗ ██║
  ██║▄▄ ██║██╔══██║ ███╔╝     ██║   ██╔══╝  ██╔══██╗██║██║   ██║██║╚██╗██║
  ╚██████╔╝██║  ██║███████╗   ██║   ███████╗██║  ██║██║╚██████╔╝██║ ╚████║
   ╚══▀▀═╝ ╚═╝  ╚═╝╚══════╝   ╚═╝   ╚══════╝╚═╝  ╚═╝╚═╝ ╚═════╝ ╚═╝  ╚═══╝
"""


def get_git_branch(workspace: Path) -> str:
    try:
        from qz_sandbox.backend import sanitize_subprocess_env

        res = subprocess.run(
            ["git", "branch", "--show-current"], cwd=str(workspace), capture_output=True, text=True,
            timeout=2.0, env=sanitize_subprocess_env(str(workspace)),
            stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
        return res.stdout.strip() or "(not a git repository yet)"
    except Exception:
        return "(git unavailable)"


def print_banner(console: Console, workspace: Path) -> None:
    from qz_providers.gateway import get_gateway

    try:
        status = get_gateway().status()
        coder = status["roles"].get("coder", {}).get("usable") or []
        keys = sum(p["key_count"] for p in status["providers"])
        providers = sorted({p["provider_id"] for p in status["providers"] if p["key_count"]})
        models = f"{coder[0]} (+{len(coder) - 1} fallback)" if coder else "[red]no usable model — add a key with /keys add[/red]"
        accounts = f"{keys} key(s) across {', '.join(providers)}" if providers else "none"
    except Exception as error:
        models, accounts = f"[red]provider config error: {escape(str(error))}[/red]", "?"

    console.print(Panel(Text(ASCII_ART.strip("\n"), style="bold cyan"), border_style="cyan", padding=(0, 2)))
    console.print(f"  [bold white]Qazterion[/bold white] [dim]v{VERSION}[/dim]")
    console.print(f"  [dim white]Coder[/dim white]      [cyan]{models}[/cyan]")
    console.print(f"  [dim white]Accounts[/dim white]   [cyan]{escape(accounts)}[/cyan]")
    console.print(f"  [dim white]Directory[/dim white]  [green]{escape(str(workspace))}[/green]")
    console.print(f"  [dim white]Git[/dim white]        [yellow]{escape(get_git_branch(workspace))}[/yellow]")
    console.print("\n[dim]" + "─" * 72 + "[/dim]\n")
    console.print("  [bold cyan]/help /status /keys /model /diff /rollback /history /clear[/bold cyan]  [bold red]/exit[/bold red]\n")
