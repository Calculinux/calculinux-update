# Config File Handling in RAUC Updates

## Problem

When updating the system image using RAUC (or direct image flashing), package files in the lower OverlayFS layer get replaced, but modified config files in the upper layer persist and shadow the new versions from the updated image.

The opkg package manager's CONFFILES mechanism only applies when upgrading packages through opkg itself, not when replacing the entire root filesystem image.

## Solution

### Overview

The solution detects modified config files after RAUC updates by:

1. Reading opkg's CONFFILES metadata for all packages
2. Comparing MD5 checksums between upper and lower OverlayFS layers
3. Creating `.dpkg-new` files for updated configs that were modified by the user
4. Reporting these files to the user after reboot

### Implementation

#### New Module: `opkg/conffiles.py`

Located in `src/calculinux_update/opkg/conffiles.py`, this module provides:

- **`ConffileInfo`**: NamedTuple holding config file metadata (path, package, MD5)
- **`get_package_conffiles(package_name, info_dir)`**: Reads `.conffiles` metadata for a package
- **`get_all_conffiles(package_list, info_dir)`**: Aggregates conffiles from multiple packages
- **`detect_modified_conffiles(image_packages, overlay_mount)`**: Compares upper/lower MD5s
- **`create_dpkg_new_files(modified_conffiles, overlay_mount, dry_run)`**: Copies lower versions to `.dpkg-new`

#### Integration with Hooks

Modified `hooks.py` to integrate conffile detection into the RAUC update workflow:

**Phase 3 in `run_slot_hook()` (cup-hook)**:
- Runs after package reconciliation (Phase 1-2)
- Detects modified config files
- Creates `.dpkg-new` files in the filesystem
- Saves list to state file for post-reboot reporting

**Post-reboot reporting in `postreboot_entrypoint()` (cup-postreboot)**:
- Reads modified conffiles from state file
- Logs formatted output showing affected packages and file paths
- Helps users identify config files that need manual merging

### CONFFILES Format

The opkg `.conffiles` format (in `/var/lib/opkg/info/<package>.conffiles`) is:

```
/path/to/config/file [md5sum]
```

Examples:
```
/etc/network/interfaces
/etc/ssh/sshd_config d41d8cd98f00b204e9800998ecf8427e
```

- Lines starting with `#` are comments and ignored
- Empty lines are ignored
- MD5 checksum is optional (older opkg versions don't include it)
- Paths should be absolute (relative paths are converted to absolute)

### OverlayFS Structure

The solution leverages the OverlayFS structure:

- **Lower layer**: `/data/overlay/<dir>/lower/` - Read-only base image
- **Upper layer**: `/data/overlay/<dir>/upper/` - User modifications
- **Merged view**: `/` - Combined filesystem

When a config file exists in both layers:
1. Compute MD5 of upper layer file (user's modified version)
2. Compute MD5 of lower layer file (new version from image update)
3. If different: user modified the config, copy lower version to `.dpkg-new`

### User Workflow

After a RAUC update:

1. System reboots into new image
2. `cup-postreboot` service runs
3. User sees log output listing modified config files:
   ```
   Modified config files detected (user changes shadow new image versions):
   Package: openssh-server
     /etc/ssh/sshd_config -> /etc/ssh/sshd_config.dpkg-new
   Package: network-manager
     /etc/network/interfaces -> /etc/network/interfaces.dpkg-new
   ```
4. User manually reviews and merges changes using tools like `diff`, `vimdiff`, etc.

### Testing

Comprehensive test suite in `tests/test_opkg_conffiles.py` with 17 test cases covering:

- MD5 computation (success, missing files, permission errors)
- CONFFILES parsing (with/without checksums, comments, relative paths)
- Modified file detection (various scenarios)
- `.dpkg-new` file creation (including dry-run mode)
- Error handling

Run tests with:
```bash
python -m pytest tests/test_opkg_conffiles.py -v
```

### Integration Points

#### State Files

- **Modified conffiles list**: `/var/lib/calculinux-update/state/update-state.modified-conffiles`
  - JSON lines format: `{"path": "/etc/file", "package": "pkg-name"}`
  - Written by `cup-hook` during RAUC post-install
  - Read by `cup-postreboot` after reboot for reporting

#### Dependencies

- Requires `image-packages.txt` from package reconciliation (Phase 1)
- Uses existing `STATE_DIR` and state file infrastructure
- Integrates with OverlayFS structure from `opkg.overlayfs` module

#### Configuration

No additional configuration needed - uses existing:
- `/var/lib/opkg/info/` for CONFFILES metadata
- OverlayFS mount points (auto-detected)
- State directory (`/var/lib/calculinux-update/state/`)

### Future Enhancements

Potential improvements:

1. **Automatic three-way merge**: Use tools like `diff3` to attempt automatic merging
2. **Interactive mode**: Prompt user during update to handle conflicts
3. **Backup original**: Keep `.dpkg-old` copy of user's modified version
4. **GUI notification**: Desktop notification for modified config files
5. **Config file database**: Track config file versions and merge history

### Files Modified

- `src/calculinux_update/opkg/conffiles.py` - New module (244 lines)
- `src/calculinux_update/hooks.py` - Added Phase 3 and reporting
- `src/calculinux_update/opkg/__init__.py` - Added exports
- `tests/test_opkg_conffiles.py` - New test suite (351 lines)

### References

- RAUC: https://rauc.io/
- OverlayFS: https://www.kernel.org/doc/html/latest/filesystems/overlayfs.html
- opkg: https://git.yoctoproject.org/opkg/
- Debian dpkg conffile handling: https://www.debian.org/doc/manuals/debian-faq/ch-pkg_basics.en.html#s-conffile
