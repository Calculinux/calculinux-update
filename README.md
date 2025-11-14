# Calculinux Update Frontend

A lightweight Python CLI that discovers RAUC update bundles published to the Calculinux mirror, lets you pick the desired image, downloads it locally, and invokes `rauc install` to deploy it.

## Why Python?

* Python is already present on Calculinux development hosts and devices, so no extra runtime is required.
* `typer` and `rich` make it easy to ship an ergonomic, colorful CLI that works over SSH.
* `httpx` provides robust HTTP/HTTPS handling for the mirror endpoints.

## Features

- Reads mirror configuration from `/etc/calculinux-update/config.toml` (override via `~/.config` or `CALCULINUX_UPDATE_CONFIG`).
- Lists RAUC bundles per channel (continuous, release, etc.).
- Downloads the selected bundle with a progress bar and checksum validation.
- Calls `rauc install <bundle>` with optional `--dry-run` to preview actions.
- Designed to be extended with new machines/channels by editing the config file only.

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

Copy the default configuration into place (requires sudo for `/etc`).

```bash
sudo install -D -m 0644 config/calculinux-update.toml /etc/calculinux-update/config.toml
```

## Configuration

`/etc/calculinux-update/config.toml` example:

```toml
mirror_base_url = "https://opkg.calculinux.org"
machine = "luckfox-lyra"
download_dir = "/var/tmp/calculinux-update"

[[channels]]
name = "Luckfox Lyra Continuous"
path = "/update/luckfox-lyra/continuous"

[[channels]]
name = "Luckfox Lyra Release"
path = "/update/luckfox-lyra/release"
```

* `mirror_base_url` – host serving bundles.
* `machine` – used for display/hints only.
* `download_dir` – where bundles are stored before installation (must exist or be creatable).
* `channels` – mirror-relative directories to scan for `.raucb` bundles.

## Usage

List bundles (all channels):

```bash
calculinux-update list
```

List only the release channel:

```bash
calculinux-update list --channel "Luckfox Lyra Release"
```

Install interactively (downloads and prompts for confirmation):

```bash
sudo calculinux-update install
```

Install a specific bundle directly:

```bash
sudo calculinux-update install --channel continuous --bundle calculinux-bundle-luckfox-lyra.raucb
```

Dry run (download only, skip `rauc install`):

```bash
calculinux-update install --dry-run
```

Environment overrides:

- `CALCULINUX_UPDATE_CONFIG` – absolute path to a config file for testing.
- `HTTP_PROXY` / `HTTPS_PROXY` – honored automatically by `httpx`.

## Development

```bash
pip install -e .[dev]
pytest
```

## Roadmap

- Optional daemon mode to watch the mirror for updates.
- Systemd unit + timer for scheduled checks.
- Integration with RAUC status reporting.

## License

Released under the GNU General Public License v3.0 (see `LICENSE`).
