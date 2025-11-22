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

**Phase 1: During RAUC Install** (via `cup-hook` in `slot-post-install` phase):
1. **Detect duplicates**: Identifies packages that exist in both the writable overlay and the new base image
2. **Two-phase duplicate removal**:
   - **Status-only duplicates**: Packages with no actual files in the upper layer are removed from the status file immediately (safe before reboot)
   - **Physical duplicates**: Packages with actual files in the upper layer are queued for removal after reboot
3. **Clean up OverlayFS whiteouts**: After removing packages, cleans up whiteout files that would block access to base image files (see below)
4. **Clean up opkg metadata whiteouts**: Removes whiteout files in `/var/lib/opkg/info/` that hide base image metadata
5. **Snapshot current state**: Records which packages exist in the currently-booted slot
6. **Plan reconciliation**: Computes which packages need to be reinstalled or upgraded after reboot
7. **Prefetch packages** (optional): Downloads packages that will be needed post-reboot so the system can work offline

**Phase 2: After Reboot** (via `cup-postreboot` systemd service):
1. **Remove physical duplicates**: Completes removal of packages that had files in the upper layer
2. Runs `opkg update` to refresh package feeds
3. **Reinstalls** packages that were present in the old image but missing from the new one
4. **Upgrades** all overlay packages to match versions in the new base image
5. Cleans up pending operation lists on success

### OverlayFS Whiteout Cleanup

Calculinux uses OverlayFS to provide a writable layer on top of the read-only base image. A specific edge case can occur during updates:

**The Problem:**
1. User installs a package (e.g., SDL) into the overlay that shadows files in the base image
2. A new RAUC update integrates a newer version of that same package (SDL) into the base image
3. During reconciliation, the duplicate package is removed from the overlay using `opkg remove`
4. OverlayFS creates "whiteout" files (character devices with major:minor 0:0) for each removed file that has a corresponding file in the lower layer
5. These whiteouts persist after package removal, blocking access to the newer version in the base image
6. Additionally, opkg creates metadata whiteouts in `/var/lib/opkg/info/` (e.g., `.wh.package.list`) that hide base image metadata files

**The Solution:**
The reconciliation system automatically detects and removes two types of whiteout files:

**Package File Whiteouts:**
After removing duplicate packages, the system:
1. Pre-fetches the file list for each package **before** calling `opkg remove` (since removal deletes the package from the database)
2. Checks each file path for whiteout files (character device 0:0)
3. Removes whiteout files to expose the base image files
4. Remounts the overlay to pick up the changes immediately

**Metadata Whiteouts:**
When `opkg remove` deletes metadata files in `/var/lib/opkg/info/`, OverlayFS creates whiteouts that hide the base image's metadata:
1. After package removal, scans `/var/lib/opkg/info/` for `.wh.package.*` files
2. Verifies they are actual whiteouts (character device 0:0)
3. Removes them to expose base image metadata files (`.list`, `.control`, etc.)
4. This allows commands like `opkg files package` to work even after the overlay version is removed

**Enhanced opkg Integration:**
The system now uses enhanced opkg functionality for proper split-status file handling:
- `opkg status --writable-only package`: Queries only the writable status file
- `opkg status --image-only package`: Queries only the base image status file
- `opkg status --show-source package`: Shows which status file contains the package

These flags eliminate the need for manual status file parsing and provide a proper API for dual-layer package management.

**Important Notes:**
- The overlay is automatically remounted after whiteout cleanup to ensure changes are immediately visible
- The system will also naturally remount during the reboot following the update
- File lists are pre-fetched **before** package removal to avoid querying removed packages
- Only whiteout files corresponding to removed packages are deleted, preserving other legitimate character devices
- Errors during whiteout cleanup are logged but don't prevent the update from proceeding
- This cleanup is essential for scenarios where user overlay packages are superseded by newer base image versions

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
