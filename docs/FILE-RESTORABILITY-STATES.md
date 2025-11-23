# OverlayFS File Restorability States

## Overview

The `check_file_restorability()` function uses the kernel's `OVL_IOC_IS_RESTORABLE` ioctl to determine the precise state of a file in an overlay filesystem. The kernel returns specific error codes that we map to three distinct states via the `FileRestorability` enum.

## FileRestorability Enum States

### WHITEOUT (ioctl returns 0)
The file has a whiteout character device (0:0) in the upper layer, which means:
- A file with this path exists in the lower layer
- The whiteout is hiding that lower layer file
- Removing the whiteout will restore access to the lower file
- The file is **restorable**

**Common scenario:** After `opkg remove` of a package that shadows a base image package, whiteouts are created for each file.

### IN_UPPER (ioctl returns -EINVAL)
A real file exists in the upper layer (not a whiteout), which means:
- The file has been created or modified in the upper layer
- This represents actual user changes or overlay-installed packages
- The file is **not restorable** (no whiteout exists)

**Common scenario:** User has installed a package that doesn't exist in base image, or has modified a file.

### IN_LOWER_ONLY (ioctl returns -ENOENT)
The file doesn't exist in the upper layer at all, which means:
- Either the file only exists in the lower layer (unmodified base image file)
- Or the file doesn't exist anywhere in the filesystem
- The file is **not restorable** (no whiteout to remove)

**Common scenario:** File from base image that hasn't been touched in the upper layer.

## Why Enum-Based Detection?

### The Problem with path.exists()

Initially, we checked restorability and then used `path.exists()` to verify:

```python
# BROKEN: path.exists() returns True for BOTH upper and lower!
if not is_file_restorable(mount_point, path):
    if path.exists():
        return True  # WRONG: Could be in lower layer
```

The issue: `path.exists()` returns `True` for files in either the upper OR lower layer. After determining a file is not restorable, we couldn't distinguish between:
- Real file in upper layer (should return `True`)
- File only in lower layer (should return `False`)

### The Enum Solution

The kernel ioctl already gives us this information via error codes:

```python
# CORRECT: Enum captures all three states
restorability = check_file_restorability(mount_point, path)

if restorability == FileRestorability.IN_UPPER:
    return True  # Real file in upper
elif restorability == FileRestorability.WHITEOUT:
    return False  # Whiteout, not a real file
else:  # IN_LOWER_ONLY
    return False  # Not in upper layer
```

This is more robust because:
1. **Accurate**: Distinguishes upper vs lower layer files
2. **Atomic**: Single kernel call, no race conditions
3. **Efficient**: One ioctl instead of ioctl + stat
4. **Semantically clear**: Enum names document intent

## Kernel Implementation

From `fs/overlayfs/ioctl.c`:

```c
static int ovl_check_restorable(struct dentry *dentry, const char *pathname, ...)
{
    // ... setup code ...
    
    /* Get the upper dentry */
    upper_dentry = ovl_dentry_upper(overlay_dentry);
    if (!upper_dentry) {
        /* No upper dentry means no whiteout to remove */
        err = -ENOENT;  // Maps to IN_LOWER_ONLY
        goto out_path_put;
    }

    /* Verify it's actually a whiteout */
    if (!ovl_is_whiteout(upper_dentry)) {
        err = -EINVAL;  // Maps to IN_UPPER
        goto out_path_put;
    }

    /* Success - file is restorable */
    return 0;  // Maps to WHITEOUT
}
```

## Example Usage

### Checking if Package Has Real Files in Upper

```python
def has_files_in_upper(package_name: str) -> bool:
    """Check if package has any actual files in upper layer."""
    file_paths = get_package_files(package_name)
    
    for file_path in file_paths:
        mount_point = find_overlay_mount_point(str(file_path))
        restorability = check_file_restorability(mount_point, str(file_path))
        
        if restorability == FileRestorability.IN_UPPER:
            # Found a real file in upper layer
            return True
        # WHITEOUT and IN_LOWER_ONLY both mean "not in upper"
    
    return False
```

### Restoring Files

```python
def restore_package_files(package_name: str) -> int:
    """Restore lower layer files by removing whiteouts."""
    file_paths = get_package_files(package_name)
    restored = 0
    
    for file_path in file_paths:
        mount_point = find_overlay_mount_point(str(file_path))
        restorability = check_file_restorability(mount_point, str(file_path))
        
        if restorability == FileRestorability.WHITEOUT:
            # Only restore if it's actually a whiteout
            if restore_lower_via_ioctl(mount_point, str(file_path)):
                restored += 1
    
    return restored
```

## Convenience Function

For code that only needs a boolean "can this be restored?", we provide:

```python
def is_file_restorable(mount_point: str, path: str) -> bool:
    """Convenience wrapper that returns True if file is restorable."""
    return check_file_restorability(mount_point, path) == FileRestorability.WHITEOUT
```

This is used by functions like `find_restorable_files()` that filter lists of files.

## Testing

Tests mock `check_file_restorability()` to return enum values:

```python
def test_has_files_in_upper_mixed_files(self):
    """Test package with both real files and whiteouts."""
    mock_files = ["/usr/bin/app", "/etc/config"]
    
    def mock_restorability(mount, path):
        if path == "/usr/bin/app":
            return FileRestorability.IN_UPPER  # Real file
        return FileRestorability.WHITEOUT  # Whiteout
    
    with patch("...check_file_restorability", side_effect=mock_restorability):
        result = has_files_in_upper("test-package")
    
    assert result is True  # Because at least one file is IN_UPPER
```

## See Also

- [KERNEL-IOCTL-BEHAVIOR.md](./KERNEL-IOCTL-BEHAVIOR.md) - Detailed kernel ioctl documentation
- [ARCHITECTURE-REVIEW.md](./ARCHITECTURE-REVIEW.md) - Overall system architecture
- [overlayfs.py](../src/calculinux_update/opkg/overlayfs.py) - Implementation
