"""Keys management slash command (/keys)."""
from __future__ import annotations

import getpass
from typing import List
from rich.console import Console
from rich.table import Table
from qz_keystore import KeyStore, SUPPORTED_PROVIDERS, mask_key
from qz_providers.connectivity import test_provider_connectivity


def handle_keys_command(console: Console, args: List[str]) -> None:
    ks = KeyStore()
    subcmd = args[0].lower() if args else "list"

    if subcmd in ("list", "ls", "show"):
        entries = ks.list_entries()
        table = Table(title="[bold cyan]Configured Provider API Keys[/bold cyan]", border_style="cyan")
        table.add_column("Provider", style="bold white")
        table.add_column("Env Name", style="dim cyan")
        table.add_column("Masked Secret", style="yellow")
        table.add_column("Enabled", style="bold green")

        if not entries:
            console.print("[yellow]No API keys currently configured in keystore.[/yellow]")
            console.print("Use [bold cyan]/keys add <provider>[/bold cyan] to register an API key.\n")
            return

        for e in entries:
            table.add_row(
                e.provider.upper(),
                e.env_name,
                e.masked_value,
                "✔ Yes" if e.enabled else "✖ No",
            )
        console.print(table)
        console.print(f"[dim]Keystore encryption: {ks.backend_name().upper()} ({ks.path})[/dim]\n")

    elif subcmd in ("add", "set"):
        prov = args[1].lower() if len(args) > 1 else ""
        if not prov:
            console.print("[bold cyan]Supported providers:[/bold cyan] " + ", ".join(SUPPORTED_PROVIDERS))
            prov = input("Enter provider name (e.g. gemini, groq, mistral, openrouter): ").strip().lower()

        if not prov:
            console.print("[red]Provider cannot be empty.[/red]")
            return

        # Determine index
        target_index = None
        key_val = ""

        # Check if 2nd arg is an integer index (e.g. /keys add gemini 2 <key>)
        if len(args) > 2:
            try:
                target_index = int(args[2])
                if len(args) > 3:
                    key_val = args[3]
            except ValueError:
                # 2nd arg is the key itself
                key_val = args[2]

        # Auto-detect next available index if not explicitly provided
        existing_entries = ks.list_entries(provider=prov)
        existing_indices = {e.index for e in existing_entries}
        if target_index is None:
            target_index = 1
            while target_index in existing_indices:
                target_index += 1

        if not key_val:
            key_val = getpass.getpass(f"Enter API key for {prov.upper()} (Key #{target_index}): ").strip()

        if not key_val:
            console.print("[red]Key value cannot be empty.[/red]")
            return

        try:
            env_name = ks.set_key(prov, target_index, key_val, enabled=True)
            console.print(f"[bold green]✔ Successfully stored {prov.upper()} Key #{target_index} ({env_name}) in OS DPAPI keystore.[/bold green]\n")
        except Exception as e:
            console.print(f"[bold red]✖ Failed to store key:[/bold red] {e}\n")

    elif subcmd in ("delete", "remove", "rm"):
        prov = args[1].lower() if len(args) > 1 else ""
        if not prov:
            prov = input("Enter provider name to delete: ").strip().lower()
        if not prov:
            return

        target_index = 1
        if len(args) > 2:
            try:
                target_index = int(args[2])
            except ValueError:
                pass

        removed = ks.delete_key(prov, target_index)
        if removed:
            console.print(f"[bold green]✔ Removed {prov.upper()}_{target_index} key from keystore.[/bold green]\n")
        else:
            console.print(f"[yellow]No stored key found for {prov.upper()} index {target_index}.[/yellow]\n")

    elif subcmd in ("test", "ping"):
        prov = args[1].lower() if len(args) > 1 else "gemini"
        target_index = 1
        if len(args) > 2:
            try:
                target_index = int(args[2])
            except ValueError:
                pass

        console.print(f"[cyan]Testing connectivity for {prov.upper()} (Key #{target_index})...[/cyan]")
        try:
            key_val = ks.get_key(prov, target_index)
            if not key_val:
                console.print(f"[red]No API key found for {prov.upper()} index {target_index} in keystore.[/red]")
                return
            res = test_provider_connectivity(prov, key_val)
            if res.connected:
                console.print(f"[bold green]✔ Connected to {prov.upper()} Key #{target_index} successfully![/bold green] (Latency: {res.latency_ms:.0f}ms, Models: {res.models_found})\n")
            else:
                console.print(f"[bold red]✖ Connection failed for {prov.upper()}:[/bold red] {res.error_message}\n")
        except Exception as e:
            console.print(f"[bold red]✖ Connectivity test error:[/bold red] {e}\n")

    else:
        console.print("[yellow]Usage: /keys [list | add <provider> [index] [key] | delete <provider> [index] | test <provider> [index]][/yellow]\n")
