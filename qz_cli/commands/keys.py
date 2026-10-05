"""/keys: manage API keys/accounts for any configured provider."""
from __future__ import annotations

import getpass

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from qz_keystore import KeyStore, UnsupportedProviderError

USAGE = (
    "Usage: /keys [list | add <provider> [index] | delete <provider> <index> | "
    "enable|disable <provider> <index> | test <provider> [index] | reset]"
)


def _gateway():
    from qz_providers.gateway import get_gateway

    return get_gateway()


def _parse_index(args: list[str], position: int) -> int | None:
    if len(args) > position:
        try:
            return int(args[position])
        except ValueError:
            return None
    return None


def handle_keys_command(console: Console, args: list[str]) -> None:
    ks = KeyStore()
    gateway = _gateway()
    sub = args[0].lower() if args else "list"

    if sub in ("list", "ls", "show"):
        table = Table(title="[bold cyan]API Keys[/bold cyan]", border_style="cyan")
        for column in ("Provider", "Key", "Masked", "Source", "Enabled"):
            table.add_column(column)
        keys = gateway.keys.all_keys()
        for key in sorted(keys, key=lambda k: (k.provider, k.index)):
            table.add_row(key.provider, key.key_id, key.masked, key.source, "yes" if key.enabled else "no")
        if not keys:
            console.print("[yellow]No API keys configured.[/yellow] Add one with [bold cyan]/keys add <provider>[/bold cyan] "
                          f"(providers: {', '.join(gateway.catalog.providers)}).\n")
            return
        console.print(table)
        console.print(f"[dim]Keystore: {ks.backend_name()} ({escape(str(ks.path))}). Keys from .env/environment are read-only here.[/dim]\n")
        return

    if sub in ("add", "set"):
        provider = (args[1] if len(args) > 1 else input(f"Provider ({', '.join(gateway.catalog.providers)}): ")).strip().lower()
        if not provider:
            console.print("[red]Provider cannot be empty.[/red]")
            return
        if len(args) > 3 or (len(args) > 2 and _parse_index(args, 2) is None):
            # Never accept secrets on the command line: they end up in shell/REPL history.
            console.print("[red]For safety, enter the key at the hidden prompt instead of on the command line.[/red]")
            return
        index = _parse_index(args, 2)
        try:
            if index is None:
                used = {e.index for e in ks.list_entries(provider=provider)}
                index = 1
                while index in used:
                    index += 1
            value = getpass.getpass(f"API key for {provider} (#{index}, input hidden): ").strip()
            if not value:
                console.print("[red]Key value cannot be empty.[/red]")
                return
            env_name = ks.set_key(provider, index, value, enabled=True)
        except (UnsupportedProviderError, ValueError) as error:
            console.print(f"[red]{escape(str(error))}[/red]\n")
            return
        gateway.keys.invalidate()
        gateway.health.reset_key(env_name)
        console.print(f"[bold green]✔ Stored {env_name} for {provider} ({ks.backend_name()} encrypted).[/bold green]")
        console.print(f"[dim]Check it with /keys test {provider} {index}[/dim]\n")
        return

    if sub in ("delete", "remove", "rm", "enable", "disable"):
        provider = args[1].lower() if len(args) > 1 else ""
        index = _parse_index(args, 2)
        if not provider or index is None:
            console.print(f"[yellow]{USAGE}[/yellow]\n")
            return
        try:
            if sub in ("delete", "remove", "rm"):
                ok = ks.delete_key(provider, index)
                message = "Removed" if ok else "No stored key for"
            else:
                ks.set_enabled(provider, index, sub == "enable")
                message = "Enabled" if sub == "enable" else "Disabled"
                if sub == "enable":
                    gateway.health.reset_key(f"{ks._family_for(provider)}_{index}")
        except (UnsupportedProviderError, KeyError, ValueError) as error:
            console.print(f"[red]{escape(str(error))}[/red]\n")
            return
        gateway.keys.invalidate()
        console.print(f"[green]{message} {provider} key #{index}.[/green]\n")
        return

    if sub in ("test", "ping"):
        from qz_providers.connectivity import test_provider_connectivity

        provider = args[1].lower() if len(args) > 1 else ""
        candidates = gateway.keys.keys_for(provider, include_disabled=True) if provider else gateway.keys.all_keys()
        index = _parse_index(args, 2)
        if index is not None:
            candidates = [k for k in candidates if k.index == index]
        if not candidates:
            console.print("[red]No matching key found.[/red]\n")
            return
        for key in candidates:
            result = test_provider_connectivity(key.provider, gateway.keys.secret(key.key_id) or "", catalog=gateway.catalog)
            if result.connected:
                gateway.health.reset_key(key.key_id)
                console.print(f"[green]✔ {key.key_id}: {escape(result.message)} ({result.latency_ms:.0f} ms)[/green]")
            else:
                console.print(f"[red]✖ {key.key_id}: {escape(result.message)}[/red]")
        console.print()
        return

    if sub == "reset":
        gateway.health.reset_all()
        console.print("[green]Cleared all key/model cooldowns and rejections.[/green]\n")
        return

    console.print(f"[yellow]{USAGE}[/yellow]\n")
