"""Claude Code / Qazterion style header banner."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from qz_keystore import KeyStore


def get_git_branch(workspace: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        branch = res.stdout.strip()
        return f"{branch} ✓" if branch else "main ✓"
    except Exception:
        return "main ✓"


ASCII_ART = """
   ██████╗  █████╗ ███████╗████████╗███████╗██████╗ ██╗ ██████╗ ███╗   ██╗
  ██╔═══██╗██╔══██╗╚══███╔╝╚══██╔══╝██╔════╝██╔══██╗██║██╔═══██╗████╗  ██║
  ██║   ██║███████║  ███╔╝    ██║   █████╗  ██████╔╝██║██║   ██║██╔██╗ ██║
  ██║▄▄ ██║██╔══██║ ███╔╝     ██║   ██╔══╝  ██╔══██╗██║██║   ██║██║╚██╗██║
  ╚██████╔╝██║  ██║███████╗   ██║   ███████╗██║  ██║██║╚██████╔╝██║ ╚████║
   ╚══▀▀═╝ ╚═╝  ╚═╝╚══════╝   ╚═╝   ╚══════╝╚═╝  ╚═╝╚═╝ ╚═════╝ ╚═╝  ╚═══╝

                    AI SOFTWARE ENGINEERING AGENT                     
"""


def print_banner(console: Console, workspace: Path) -> None:
    branch = get_git_branch(workspace)
    ks = KeyStore()
    providers = ks.configured_providers()
    
    if "gemini" in providers:
        primary_model = "gemini-3.6-flash"
    elif providers:
        primary_model = f"{providers[0]}-model"
    else:
        primary_model = "gemini-3.6-flash (auto-fallback)"

    art_text = Text(ASCII_ART.strip("\n"), style="bold cyan")

    console.print(
        Panel(
            art_text,
            border_style="cyan",
            padding=(0, 2),
        )
    )

    console.print(f"  [bold white]Qazterion[/bold white] [dim]v2.0.0[/dim]")
    console.print(f"  [dim white]Model[/dim white]      [cyan]{primary_model}[/cyan]")
    console.print(f"  [dim white]Directory[/dim white]  [green]{workspace}[/green]")
    console.print(f"  [dim white]Git[/dim white]        [yellow]{branch}[/yellow]")
    console.print("\n[dim]────────────────────────────────────────────────────────────────────────[/dim]\n")
    console.print("  [bold cyan]/help[/bold cyan]  [bold cyan]/status[/bold cyan]  [bold cyan]/keys[/bold cyan]  [bold cyan]/diff[/bold cyan]  [bold cyan]/rollback[/bold cyan]  [bold cyan]/history[/bold cyan]  [bold cyan]/clear[/bold cyan]  [bold red]/exit[/bold red]\n")

