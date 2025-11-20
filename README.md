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
- Requires each channel to expose an `index.json` manifest
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
download_dir = "/var/cache/calculinux-update"

[[channels]]
name = "Luckfox Lyra Release"
path = "/update/luckfox-lyra/release"
enable = true

[[channels]]
name = "Luckfox Lyra Continuous"
path = "/update/luckfox-lyra/continuous"
enable = false

[[channels]]
name = "Luckfox Lyra PR Builds"
path = "/update/luckfox-lyra/pr"
enable = false
```

* `mirror_base_url` – host serving bundles.
* `machine` – machine/board type for filtering compatible bundles. If omitted, the tool will attempt to auto-detect from RAUC or the device tree. On target devices, auto-detection usually works; for development machines, set this explicitly.
* `download_dir` – where bundles are stored before installation (must exist or be creatable).
* `channels` – mirror-relative directories to scan for `.raucb` bundles.
* `enable` – set to `false` to temporarily hide a channel.

Release bundles stay enabled out of the box, while the fast-moving continuous channel ships disabled so you opt-in intentionally. Pull-request bundles are also configured but disabled by default. Copy the config to `/etc/calculinux-update` or `~/.config/calculinux-update`, flip `enable = true` for whichever extra channels you want (continuous and/or PR), and they will show up in `cup list`.

## Usage

List bundles (all channels):

```bash
cup list
```

List only the release channel:

```bash
cup list --channel "Luckfox Lyra Release"
```

Install interactively (downloads and prompts for confirmation):

```bash
sudo cup install
```

Install a specific bundle directly:

```bash
sudo cup install --channel continuous --bundle calculinux-bundle-luckfox-lyra.raucb
```

Run non-interactively (skips the confirmation prompt):

```bash
sudo cup install --bundle calculinux-bundle-luckfox-lyra.raucb --yes
```

Dry run (download only, skip `rauc install`):

```bash
cup install --dry-run
```
Dry runs never invoke `rauc`, so the binary does not need to be present on the host.

Test a pull-request build (after enabling the channel as described above, each bundle is named after the PR number):

```bash
cup list --channel "Luckfox Lyra PR Builds"
sudo cup install --channel "Luckfox Lyra PR Builds" --bundle calculinux-pr123.raucb
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
