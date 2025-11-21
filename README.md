# Calculinux Update Frontend

A lightweight Python CLI that discovers RAUC update bundles published to the Calculinux mirror, lets you pick the desired image, downloads it locally, and invokes `rauc install` to deploy it.

## Why Python?

* Python is already present on Calculinux development hosts and devices, so no extra runtime is required.
* `typer` and `rich` make it easy to ship an ergonomic, colorful CLI that works over SSH.
* `httpx` provides robust HTTP/HTTPS handling for the mirror endpoints.

## Features

- Reads mirror configuration from `/etc/calculinux-update.toml` or `~/.config/calculinux-update.toml` (override via `CALCULINUX_UPDATE_CONFIG`).
- Lists RAUC bundles per channel (continuous, release, etc.).
- Downloads the selected bundle with a progress bar and checksum validation.
- Calls `rauc install <bundle>` with optional `--dry-run` to preview actions.
- **OPKG Package Reconciliation**: Automatically manages user-installed packages across RAUC updates via hooks
  - Removes duplicate packages when the new image provides them
  - Reinstalls packages that were user-installed but removed from the new image
  - Upgrades packages that exist in both image and overlay to prevent version conflicts
  - Prefetches packages before reboot for offline post-update reconciliation
- Requires each channel to expose an `index.json` manifest
- Designed to be extended with new machines/channels by editing the config file only.

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
```

This installs three command-line tools:
- **`cup`**: Main CLI for browsing, downloading, and installing RAUC bundles
- **`cup-hook`**: RAUC hook entrypoint for slot-post-install reconciliation
- **`cup-postreboot`**: Post-reboot package reconciliation service

Copy the default configuration into place (requires sudo for `/etc`).

```bash
sudo install -D -m 0644 config/calculinux-update.toml /etc/calculinux-update.toml
```

Alternatively, install to your user config directory:

```bash
install -D -m 0644 config/calculinux-update.toml ~/.config/calculinux-update.toml
```

## Configuration

An example configuration is provided in `config/calculinux-update.toml`. Copy it to one of the following locations:

* `/etc/calculinux-update.toml` (system-wide, requires sudo)
* `~/.config/calculinux-update.toml` (user-specific)
* Or set `CALCULINUX_UPDATE_CONFIG` to point to a custom location

Example configuration:

```toml
mirror_base_url = "https://opkg.calculinux.org"
machine = "luckfox-lyra"
download_dir = "/var/cache/calculinux-update"

[[channels]]
name = "Release"
path = "/update/walnascar/release"
enable = true

[[channels]]
name = "Continuous"
path = "/update/walnascar/continuous"
enable = false

[[channels]]
name = "Builds"
path = "/update/walnascar/pr"
enable = false
```

* `mirror_base_url` – host serving bundles.
* `machine` – machine/board type for filtering compatible bundles. If omitted, the tool will attempt to auto-detect from RAUC or the device tree. On target devices, auto-detection usually works; for development machines, set this explicitly.
* `download_dir` – where bundles are stored before installation (must exist or be creatable).
* `channels` – mirror-relative directories to scan for `.raucb` bundles.
* `enable` – set to `false` to temporarily hide a channel.

Release bundles stay enabled out of the box, while the fast-moving continuous channel ships disabled so you opt-in intentionally. Pull-request bundles are also configured but disabled by default. Copy the config to `/etc/calculinux-update.toml` or `~/.config/calculinux-update.toml`, flip `enable = true` for whichever extra channels you want (continuous and/or PR), and they will show up in `cup list`.

## Usage

List bundles (all channels):

```bash
cup list
```

List only the release channel:

```bash
cup list --channel "Release"
```

Install interactively (downloads and prompts for confirmation):

```bash
sudo cup install
```

Install a specific bundle directly:

```bash
sudo cup install --channel Continuous --bundle calculinux-bundle-luckfox-lyra.raucb
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
cup list --channel "Builds"
sudo cup install --channel "Builds" --bundle luckfox-lyra-pr123.raucb
```

To skip the pre-download step (for example on development hosts without `opkg` configured), pass `--no-prefetch` when invoking `cup install`.

Environment overrides:

- `CALCULINUX_UPDATE_CONFIG` – absolute path to a config file for testing.
- `HTTP_PROXY` / `HTTPS_PROXY` – honored automatically by `httpx`.

## OPKG Package Reconciliation

Calculinux uses a dual-layer package management strategy: base system packages are baked into the read-only RAUC image, while user-installed packages live in an overlay. When you perform a RAUC update, the base image changes but the overlay persists. This can lead to several problems:

1. **Shadowing**: User-installed packages that are now provided by the new image create duplicates
2. **Missing dependencies**: Packages the user installed may have depended on packages from the old image that are gone in the new one
3. **Version conflicts**: User overlay packages may conflict with newer versions in the base image

The `calculinux-update` tool solves this with automatic package reconciliation through three integrated components:

- **`cup`**: The main CLI tool for listing, downloading, and installing updates
- **`cup-hook`**: A RAUC hook that runs during bundle installation to prepare reconciliation
- **`cup-postreboot`**: A post-reboot service that completes package reconciliation after the system boots into the new slot

### How It Works

**During RAUC Install** (via `cup-hook` in `slot-post-install` phase):
1. **Prune duplicates**: Removes any packages from the overlay that are now provided by the new image
2. **Snapshot current state**: Records which packages exist in the currently-booted slot
3. **Plan reconciliation**: Computes which packages need to be reinstalled or upgraded after reboot
4. **Prefetch packages** (optional): Downloads packages that will be needed post-reboot so the system can work offline

**After Reboot** (via `cup-postreboot` systemd service):
1. Runs `opkg update` to refresh package feeds
2. **Reinstalls** packages that were present in the old image but missing from the new one
3. **Upgrades** all overlay packages to match versions in the new base image
4. Cleans up pending operation lists on success

### Configuration

The RAUC hook (`cup-hook`) is automatically invoked by RAUC when configured in the bundle's `hook-install` handler. 

**RAUC Bundle Configuration** (`manifest.raucm`):
```ini
[hooks]
filename=hook
hooks=install

[handler]
filename=install.sh
args=--foo
```

The hook script in your bundle should call `cup-hook`:
```bash
#!/bin/sh
# install.sh in your RAUC bundle

case "$1" in
    slot-post-install)
        if [ "$RAUC_SLOT_CLASS" = "rootfs" ]; then
            cup-hook "$1" "$RAUC_SLOT_NAME"
        fi
        ;;
esac
```

**Systemd Service Setup:**

The post-reboot service (`cup-postreboot`) should be enabled as a systemd service:

```ini
# /etc/systemd/system/calculinux-update-postreboot.service
[Unit]
Description=Calculinux post-reboot package reconciliation
After=network-online.target opkg-configured.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/cup-postreboot
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
```

Enable it with:
```bash
systemctl enable calculinux-update-postreboot.service
```

### Skipping Prefetch

On development machines without a properly configured opkg environment, you can skip the prefetch step:

```bash
sudo cup install --no-prefetch
```

This prevents errors when the opkg feeds aren't available, though post-reboot reconciliation will require network access.

### File Locations

- **Writable status**: `/var/lib/opkg/status` (overlay packages)
- **Image status**: `/var/lib/opkg/status.image` (base image packages)
- **Pending reinstalls**: `/var/lib/opkg/opkg-status-hook.pending-reinstalls`
- **Pending upgrades**: `/var/lib/opkg/opkg-status-hook.pending-upgrades`
- **Prefetch cache**: `/var/cache/calculinux-update/prefetch/`

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
