# Overlay upper-layer states

`calculinux_update.opkg.overlayfs` asks the kernel what the overlayfs upper
layer holds for a path, using the ioctls of Calculinux's overlayfs module
([Calculinux/overlayfs](https://github.com/Calculinux/overlayfs), see its
`docs/ioctl.md`). Every Calculinux machine builds overlayfs from that module.

| `UpperState` | Meaning | Used for |
|---|---|---|
| `NONE` | no upper entry (only the image's copy, or nothing) | the file belongs to the image |
| `WHITEOUT` | a whiteout hides the image's copy | restore after removing an overlay duplicate |
| `UPPER` | a real file in the upper layer (`mode` has its type) | the package has files in the overlay |

`UpperInfo` also carries `opaque` (an opaque directory) and `no_lower_dir`
(nothing below the parent, so restoring would reveal nothing).

## How it is used

- **Planning (`cup-hook`, `compute_reconcile_plan`)**: a package has files in
  the overlay when any non-directory entry of its `.list` is `UPPER`.
  Directories are ignored because parents like `/usr` exist in the upper layer
  as soon as anything below them is written. If a state cannot be read, the
  package counts as having upper files, so it gets a real `opkg remove`
  rather than a status-only prune.
- **Restore (`cup-postreboot`)**: after `opkg remove` of an overlay duplicate,
  every `WHITEOUT` among its files and its `/var/lib/opkg/info` metadata is
  removed with `OVL_IOC_RESTORE_LOWER`, which also refreshes the overlay's
  caches.

## ABI

The ioctls are issued on the overlay's mount point opened with
`O_RDONLY | O_DIRECTORY`. The argument structs are packed with `struct`
(the image's Python has no ctypes) and the path is passed by the address of
an `array` buffer.

| ioctl | Number | Struct |
|---|---|---|
| `OVL_IOC_RESTORE_LOWER` | `0x40104f01` | `QII` (16 bytes) |
| `OVL_IOC_IS_RESTORABLE` | `0x40104f02` | `QII` (16 bytes) |
| `OVL_IOC_UPPER_STATE` | `0xc0204f03` | `QIIIIII` (32 bytes) |

A kernel without the ioctls answers `ENOTTY`, which raises
`OverlayIoctlUnsupported`. There is no fallback: guessing wrong either leaves
overlay files shadowing the new image or deletes files the user installed.
