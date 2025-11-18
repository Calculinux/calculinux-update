"""Typer CLI entrypoint."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.table import Table

from .config import UpdateConfig, load_config
from .installer import UpdateInstaller
from .mirror import BundleInfo, MirrorClient

app = typer.Typer(help="Calculinux RAUC update helper")
console = Console()


def _load_config(config_path: Optional[Path]) -> UpdateConfig:
    return load_config(config_path)


def _bundle_table(bundles: List[BundleInfo], show_index: bool = False) -> Table:
    table = Table(show_header=True, header_style="bold cyan")
    if show_index:
        table.add_column("#", justify="right")
    table.add_column("Bundle")
    table.add_column("Channel")
    table.add_column("Size", justify="right")
    table.add_column("Last Modified")
    table.add_column("SHA256", overflow="fold")

    for idx, bundle in enumerate(bundles, start=1):
        table.add_row(
            str(idx) if show_index else "",
            bundle.name,
            bundle.channel.name,
            _format_size(bundle.size_bytes),
            bundle.last_modified.isoformat(timespec="seconds") if bundle.last_modified else "?",
            (bundle.sha256 or "?")[:12] + "…" if bundle.sha256 else "?",
        )
    return table


def _format_size(size: Optional[int]) -> str:
    if size is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f}{unit}" if unit != "B" else f"{size}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _pick_bundle(bundles: List[BundleInfo], bundle_name: Optional[str]) -> BundleInfo:
    if not bundles:
        raise typer.Exit(code=1)

    if bundle_name:
        for bundle in bundles:
            if bundle_name in bundle.name:
                return bundle
        console.print(f"[red]Bundle '{bundle_name}' not found.[/]")
        raise typer.Exit(code=1)

    console.print(_bundle_table(bundles, show_index=True))
    selection = typer.prompt("Select bundle #", type=int)
    if selection < 1 or selection > len(bundles):
        console.print("[red]Invalid selection[/]")
        raise typer.Exit(code=1)
    return bundles[selection - 1]


@app.command()
def list(
    channel: Optional[str] = typer.Option(
        None,
        "-c",
        "--channel",
        help="Channel name or path",
    ),
    config_path: Optional[Path] = typer.Option(
        None,
        exists=True,
        resolve_path=True,
        help="Explicit config path",
    ),
):
    """List available bundles."""

    config = _load_config(config_path)
    with MirrorClient(config) as client:
        bundles = client.list_bundles(channel_selector=channel)
    if not bundles:
        console.print("[yellow]No bundles found[/]")
        raise typer.Exit()

    console.print(_bundle_table(bundles))


@app.command()
def download(
    channel: Optional[str] = typer.Option(
        None,
        "-c",
        "--channel",
        help="Channel name or path",
    ),
    bundle_name: Optional[str] = typer.Option(
        None,
        "-b",
        "--bundle",
        help="Bundle filename (substring match)",
    ),
    config_path: Optional[Path] = typer.Option(
        None,
        exists=True,
        resolve_path=True,
        help="Explicit config path",
    ),
):
    """Download a bundle to the configured directory."""

    config = _load_config(config_path)
    with MirrorClient(config) as client:
        bundles = client.list_bundles(channel_selector=channel)
    if not bundles:
        console.print("[yellow]No bundles found[/]")
        raise typer.Exit()

    bundle = _pick_bundle(bundles, bundle_name)
    installer = UpdateInstaller(config)
    result = installer.download(bundle, expected_sha256=bundle.sha256)
    message = (
        "[green]Downloaded[/] "
        f"{result.bundle.name} → {result.path} (sha256 {result.sha256[:12]}...)"
    )
    console.print(message)


@app.command()
def install(
    channel: Optional[str] = typer.Option(
        None,
        "-c",
        "--channel",
        help="Channel name or path",
    ),
    bundle_name: Optional[str] = typer.Option(
        None,
        "-b",
        "--bundle",
        help="Bundle filename (substring match)",
    ),
    config_path: Optional[Path] = typer.Option(
        None,
        exists=True,
        resolve_path=True,
        help="Explicit config path",
    ),
    rauc_binary: str = typer.Option("rauc", help="Path to rauc binary"),
    dry_run: bool = typer.Option(False, help="Download but skip rauc install"),
    sudo: bool = typer.Option(
        True,
        help="Prefix install command with sudo when not already root",
    ),
    assume_yes: bool = typer.Option(
        False,
        "-y",
        "--yes",
        "--assume-yes",
        help="Run non-interactively and skip the confirmation prompt",
    ),
):
    """Download and install a bundle via RAUC."""

    config = _load_config(config_path)
    if not dry_run:
        UpdateInstaller.ensure_binary_available(rauc_binary)

    with MirrorClient(config) as client:
        bundles = client.list_bundles(channel_selector=channel)
    if not bundles:
        console.print("[yellow]No bundles found[/]")
        raise typer.Exit()

    bundle = _pick_bundle(bundles, bundle_name)
    installer = UpdateInstaller(config)
    result = installer.download(bundle, expected_sha256=bundle.sha256)
    console.print(f"[green]Downloaded[/] {result.bundle.name} (sha256 {result.sha256})")

    confirm = True if assume_yes else typer.confirm(
        "Proceed with rauc install?",
        default=not dry_run,
    )
    if not confirm:
        console.print("[yellow]Installation skipped[/]")
        raise typer.Exit()

    installer.run_rauc_install(
        result.path,
        rauc_binary=rauc_binary,
        sudo=sudo,
        dry_run=dry_run,
    )


if __name__ == "__main__":
    app()
