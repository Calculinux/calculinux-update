"""Typer CLI entrypoint."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .config import UpdateConfig, load_config
from .installer import UpdateInstaller
from .mirror import BundleInfo, MirrorClient

app = typer.Typer(help="Calculinux RAUC update helper")
console = Console()


def _load_config(config_path: Optional[Path]) -> UpdateConfig:
    return load_config(config_path)


def _display_bundles(bundles: List[BundleInfo], show_index: bool = False) -> None:
    """Display bundles in a compact format suitable for narrow terminals."""
    for idx, bundle in enumerate(bundles, start=1):
        # Build the display text
        lines = []

        if show_index:
            lines.append(Text(f"[{idx}]", style="bold cyan"))

        lines.append(Text(f"Bundle:  {bundle.name}", style="bold white"))
        lines.append(Text(f"Channel: {bundle.channel.name}"))
        lines.append(Text(f"Size:    {_format_size(bundle.size_bytes)}"))

        if bundle.last_modified:
            modified = bundle.last_modified.isoformat(timespec="seconds")
            lines.append(Text(f"Date:    {modified}"))

        # Combine lines into one text object
        content = Text("\n").join(lines)

        # Display with panel for separation
        console.print(Panel(content, border_style="dim", padding=(0, 1)))


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

    _display_bundles(bundles, show_index=True)
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

    _display_bundles(bundles)


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
