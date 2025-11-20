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
from .prefetch import PrefetchError, prefetch_for_bundle

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


def _calculate_page_size() -> int:
    """
    Calculate optimal page size based on terminal height.
    Each bundle takes ~7-8 lines (panel with 4-5 content lines + borders).
    Reserve space for page header, prompt, and some margin.
    """
    try:
        terminal_height = console.size.height
        # Reserve ~5 lines for page header, prompt, and spacing
        available_lines = max(terminal_height - 5, 10)
        # Each bundle takes about 8 lines (including panel borders)
        bundles_per_page = max(available_lines // 8, 1)
        return bundles_per_page
    except Exception:
        # Fallback to a reasonable default if terminal size can't be determined
        return 3


def _display_bundle_page(
    bundles: List[BundleInfo], start_idx: int, end_idx: int
) -> None:
    """Display a single page of bundles."""
    page_bundles = bundles[start_idx:end_idx]
    for i, bundle in enumerate(page_bundles, start=start_idx + 1):
        lines = [
            Text(f"[{i}]", style="bold cyan"),
            Text(f"Bundle:  {bundle.name}", style="bold white"),
            Text(f"Channel: {bundle.channel.name}"),
            Text(f"Size:    {_format_size(bundle.size_bytes)}"),
        ]
        if bundle.last_modified:
            modified = bundle.last_modified.isoformat(timespec="seconds")
            lines.append(Text(f"Date:    {modified}"))
        content = Text("\n").join(lines)
        console.print(Panel(content, border_style="dim", padding=(0, 1)))


def _build_pagination_prompt(
    current_page: int, total_pages: int, total_bundles: int
) -> str:
    """Build the pagination prompt text with available options."""
    options = []
    if current_page < total_pages - 1:
        options.append("[bold green]n[/] next page")
    if current_page > 0:
        options.append("[bold green]p[/] previous page")
    options.append("[bold green]q[/] quit")

    return f"Select bundle # (1-{total_bundles}), " + ", ".join(options) + ": "


def _handle_pagination_input(
    selection: str, current_page: int, total_pages: int, total_bundles: int
) -> tuple[Optional[int], Optional[int]]:
    """
    Handle pagination input and return (new_page, selected_bundle_num).
    Returns (None, None) for quit, (new_page, None) for navigation,
    (None, bundle_num) for selection, or raises for invalid input.
    """
    selection = selection.strip().lower()

    if selection == "q":
        return (None, None)  # Quit signal
    elif selection == "n" and current_page < total_pages - 1:
        return (current_page + 1, None)  # Next page
    elif selection == "p" and current_page > 0:
        return (current_page - 1, None)  # Previous page
    else:
        # Try to parse as bundle number
        try:
            bundle_num = int(selection)
            if 1 <= bundle_num <= total_bundles:
                return (None, bundle_num)  # Valid selection
            else:
                console.print(
                    f"[red]Invalid bundle number. Must be between 1 and {total_bundles}[/]"
                )
                return (current_page, None)  # Stay on current page
        except ValueError:
            console.print(
                "[red]Invalid input. Enter a bundle number, 'n' for next, "
                "'p' for previous, or 'q' to quit.[/]"
            )
            return (current_page, None)  # Stay on current page


def _pick_bundle(bundles: List[BundleInfo], bundle_name: Optional[str]) -> BundleInfo:
    if not bundles:
        raise typer.Exit(code=1)

    if bundle_name:
        for bundle in bundles:
            if bundle_name in bundle.name:
                return bundle
        console.print(f"[red]Bundle '{bundle_name}' not found.[/]")
        raise typer.Exit(code=1)

    # Interactive selection with pagination based on terminal size
    page_size = _calculate_page_size()
    total_pages = (len(bundles) + page_size - 1) // page_size
    current_page = 0

    while True:
        # Calculate page bounds
        start_idx = current_page * page_size
        end_idx = min(start_idx + page_size, len(bundles))

        # Display current page
        console.print(f"\n[bold cyan]Page {current_page + 1} of {total_pages}[/]")
        _display_bundle_page(bundles, start_idx, end_idx)

        # Get user input
        prompt_text = _build_pagination_prompt(current_page, total_pages, len(bundles))
        console.print(prompt_text, end="")
        selection = input()

        # Handle input
        new_page, bundle_num = _handle_pagination_input(
            selection, current_page, total_pages, len(bundles)
        )

        if new_page is None and bundle_num is None:
            # Quit signal
            console.print("[yellow]Selection cancelled[/]")
            raise typer.Exit(code=0)
        elif bundle_num is not None:
            # Valid selection
            return bundles[bundle_num - 1]
        else:
            # Navigation to new page
            current_page = new_page


def _pick_channel(bundles: List[BundleInfo]) -> str:
    """
    Prompt user to select a channel from available channels in bundles.
    Returns the selected channel name.
    """
    # Get unique channels from bundles
    channels = {}
    for bundle in bundles:
        channel_name = bundle.channel.name
        if channel_name not in channels:
            channels[channel_name] = bundle.channel

    if len(channels) == 1:
        # Only one channel, return it directly
        return next(iter(channels.keys()))

    # Display available channels
    console.print("\n[bold cyan]Available Channels:[/]")
    channel_list = sorted(channels.keys())
    for i, channel_name in enumerate(channel_list, start=1):
        console.print(f"[{i}] {channel_name}")

    # Prompt for selection
    while True:
        prompt = (
            f"\nSelect channel # (1-{len(channel_list)}) or [bold green]q[/] to quit: "
        )
        console.print(prompt, end="")
        selection = input().strip().lower()

        if selection == "q":
            console.print("[yellow]Selection cancelled[/]")
            raise typer.Exit(code=0)

        try:
            channel_num = int(selection)
            if 1 <= channel_num <= len(channel_list):
                return channel_list[channel_num - 1]
            else:
                console.print(
                    f"[red]Invalid channel number. "
                    f"Must be between 1 and {len(channel_list)}[/]"
                )
        except ValueError:
            console.print("[red]Invalid input. Enter a channel number or 'q' to quit.[/]")



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

    # If no channel specified and bundles are not pre-filtered, prompt for channel
    if not channel and not bundle_name:
        selected_channel = _pick_channel(bundles)
        bundles = [b for b in bundles if b.channel.name == selected_channel]

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
    prefetch: bool = typer.Option(
        True,
        "--prefetch/--no-prefetch",
        help="Download post-reboot packages ahead of time",
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

    # If no channel specified and bundles are not pre-filtered, prompt for channel
    if not channel and not bundle_name:
        selected_channel = _pick_channel(bundles)
        bundles = [b for b in bundles if b.channel.name == selected_channel]

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

    if prefetch and not dry_run:
        console.print("[cyan]Prefetching post-reboot packages[/]", highlight=False)
        try:
            result_prefetch = prefetch_for_bundle(result.path, result.sha256, console)
            if result_prefetch.skipped and result_prefetch.reason:
                console.print(f"[yellow]Prefetch skipped:[/] {result_prefetch.reason}")
        except PrefetchError as exc:
            console.print(f"[red]Prefetch failed:[/] {exc}")

    installer.run_rauc_install(
        result.path,
        rauc_binary=rauc_binary,
        sudo=sudo,
        dry_run=dry_run,
    )


if __name__ == "__main__":
    app()
