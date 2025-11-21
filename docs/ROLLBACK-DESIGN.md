# Rollback Detection and Package Restoration

## Executive Summary

**Key Insight**: Rollback detection cannot rely solely on slot state (good/bad) or boot order, because:

- A slot marked "bad" doesn't always mean we rolled back (could be policy/expiration)
- Boot order can change for legitimate reasons (bootloader updates, multi-slot systems)
- We need positive identification of which direction the boot transition went

**Solution**: Track three pieces of information:
1. **Pre-update slot** - Which slot we were on before the update
2. **Updated slot** - Which slot we updated to
3. **Booted slot** - Which slot we're currently running

Rollback is detected when: `booted_slot == pre_update_slot AND booted_slot != updated_slot`

This unambiguously identifies rollback vs. forward update vs. other transitions.

**State File Location**: All update state is stored in `/var/lib/calculinux-update/` (following the pattern established by `prefetch.json`). This keeps calculinux-update implementation details separate from OPKG's `/var/lib/opkg/` directory, which should only contain OPKG-managed files (`status`, `status.image`).

## Problem Statement

When a user performs an update and then rolls back (via `rauc status mark-bad booted`), the OPKG package state becomes inconsistent:

1. **Forward update pruned packages**: During the forward update, `cup-hook` removes duplicate packages from the writable overlay (packages now in the new base image)
2. **Rollback doesn't restore them**: When rolling back to the old slot, the hook doesn't run (not a new installation), so pruned packages aren't restored
3. **User must manually reinstall**: Users need to manually identify and reinstall missing packages

## Goals

1. **Automatic detection**: Detect when system has rolled back to a previous slot
2. **State restoration**: Automatically restore the package state that existed before the update
3. **No manual intervention**: Users shouldn't need to manually reinstall packages after rollback
4. **Maintain compatibility**: Don't break existing update/rollback workflows

## Non-Goals

- Rollback of user-installed packages after the update (only restore pre-update state)
- Automatic rollback triggers (RAUC handles this via boot counters)
- Cross-slot state synchronization

## Proposed Solution

### Phase 1: Save Pre-Update State (during hook)

When `cup-hook` runs during `slot-post-install`, save the current state before making changes:

```python
# New files to create:
PRE_UPDATE_WRITABLE_STATUS = Path("/var/lib/calculinux-update/update-state.pre-update-writable")
PRE_UPDATE_SLOT_NAME = Path("/var/lib/calculinux-update/update-state.pre-update-slot")
UPDATED_SLOT_NAME = Path("/var/lib/calculinux-update/update-state.updated-slot")
UPDATE_BOOT_ID = Path("/var/lib/calculinux-update/update-state.boot-id")
```

**Hook workflow changes**:

```python
def run_slot_hook(hook: str, slot: str) -> None:
    if hook != "slot-post-install":
        return
    
    # ... existing checks ...
    
    # NEW: Save pre-update state
    _save_pre_update_state(slot, WRITABLE_STATUS)
    
    # Existing: prune and queue operations
    _prune_writable_status(image_status)
    plan = compute_reconcile_plan(...)
    _remove_duplicates(plan.duplicates)
    _write_pending(...)


def _save_pre_update_state(updated_slot: str, writable_status: Path) -> None:
    """Save writable status and slot information before making changes."""
    
    # Copy current writable status
    if writable_status.exists():
        shutil.copy2(writable_status, PRE_UPDATE_WRITABLE_STATUS)
        LOG.info("saved pre-update writable status")
    
    # Record which slot we're currently booted from
    current_slot = _get_booted_slot_name()
    if current_slot:
        PRE_UPDATE_SLOT_NAME.write_text(current_slot + "\n")
        LOG.info(f"recorded pre-update slot: {current_slot}")
    
    # Record which slot we're updating to
    UPDATED_SLOT_NAME.write_text(updated_slot + "\n")
    LOG.info(f"recorded updated slot: {updated_slot}")
```

### Phase 2: Detect Rollback (during post-reboot)

The post-reboot service needs to detect if we've rolled back instead of moving forward.

**Challenge**: We cannot simply check if the booted slot differs from the updated slot, because:

1. **Legitimate state transitions exist**:
   - Other slot marked invalid due to age/policy (not a rollback)
   - Bootloader changes or slot renaming
   - Multiple updates before first reboot

2. **RAUC slot states** (`rauc status --detailed`):
   - `booted` - Currently running from this slot
   - `active` - Will be booted next (may differ from booted)
   - `good` - Slot is marked as good
   - `bad` - Slot is marked as bad
   - State alone doesn't tell us if we rolled back

**Proposed Detection Strategy**:

Use a combination of markers to reliably detect rollback:

```python
# Files to track update state
PRE_UPDATE_WRITABLE_STATUS = Path("/var/lib/calculinux-update/update-state.pre-update-writable")
PRE_UPDATE_SLOT_NAME = Path("/var/lib/calculinux-update/update-state.pre-update-slot")  # Renamed for clarity
UPDATED_SLOT_NAME = Path("/var/lib/calculinux-update/update-state.updated-slot")
UPDATE_BOOT_ID = Path("/var/lib/calculinux-update/update-state.boot-id")


def postreboot_entrypoint() -> int:
    # Check for rollback first
    rollback_info = _detect_rollback()
    if rollback_info["is_rollback"]:
        LOG.info(f"rollback detected: {rollback_info['reason']}")
        return _handle_rollback()
    
    # Forward update case
    if rollback_info["is_forward_update"]:
        LOG.info("forward update detected, processing reconciliation")
        # ... existing forward-update code ...
    
    # No update state found
    return 0


def _detect_rollback() -> dict:
    """
    Detect if we've rolled back to a previous slot.
    
    Returns dict with:
        is_rollback: bool - True if this is a rollback
        is_forward_update: bool - True if this is a forward update
        reason: str - Explanation of detection logic
    """
    
    # No saved state = no update happened
    if not PRE_UPDATE_SLOT_NAME.exists() or not UPDATED_SLOT_NAME.exists():
        return {
            "is_rollback": False,
            "is_forward_update": False,
            "reason": "no update state found"
        }
    
    # Read state
    pre_update_slot = PRE_UPDATE_SLOT_NAME.read_text().strip()
    updated_slot = UPDATED_SLOT_NAME.read_text().strip()
    booted_slot = _get_booted_slot_name()
    current_boot_id = _get_boot_id()
    
    if not booted_slot:
        LOG.warning("cannot determine booted slot")
        return {
            "is_rollback": False,
            "is_forward_update": False,
            "reason": "cannot determine booted slot"
        }
    
    # Case 1: Booted into updated slot = forward update
    if booted_slot == updated_slot:
        # Additional check: has this already been processed?
        if UPDATE_BOOT_ID.exists():
            last_boot_id = UPDATE_BOOT_ID.read_text().strip()
            if last_boot_id == current_boot_id:
                return {
                    "is_rollback": False,
                    "is_forward_update": False,
                    "reason": "forward update already processed this boot"
                }
        
        return {
            "is_rollback": False,
            "is_forward_update": True,
            "reason": f"booted {booted_slot} == updated {updated_slot}"
        }
    
    # Case 2: Booted into pre-update slot = rollback
    if booted_slot == pre_update_slot:
        return {
            "is_rollback": True,
            "is_forward_update": False,
            "reason": f"booted {booted_slot} == pre-update {pre_update_slot}, not updated {updated_slot}"
        }
    
    # Case 3: Booted into neither slot = ambiguous
    # This could happen if:
    # - Slot was renamed
    # - Third slot exists and was activated
    # - Bootloader configuration changed
    LOG.warning(
        f"ambiguous state: booted={booted_slot}, "
        f"pre_update={pre_update_slot}, updated={updated_slot}"
    )
    
    # Conservative approach: compare writable status to detect if packages were pruned
    if PRE_UPDATE_WRITABLE_STATUS.exists():
        pre_update_entries = load_status_entries(PRE_UPDATE_WRITABLE_STATUS)
        current_entries = load_status_entries(WRITABLE_STATUS)
        
        pre_pkgs = {e["Package"] for e in pre_update_entries}
        curr_pkgs = {e["Package"] for e in current_entries}
        
        # If we're missing packages that were in pre-update state,
        # and we're not on the updated slot, assume rollback
        missing = pre_pkgs - curr_pkgs
        if missing:
            return {
                "is_rollback": True,
                "is_forward_update": False,
                "reason": f"ambiguous slots, but {len(missing)} packages missing (likely rollback)"
            }
    
    # Unknown state - don't make assumptions
    return {
        "is_rollback": False,
        "is_forward_update": False,
        "reason": "ambiguous state, cannot determine update direction"
    }


def _get_booted_slot_name() -> Optional[str]:
    """Get the name of the currently booted slot from RAUC."""
    try:
        result = subprocess.run(
            ["rauc", "status", "--output-format=shell"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        
        env = _parse_shell_assignments(result.stdout)
        
        # Find the booted slot by state
        for idx in range(1, 10):  # Support up to 9 slots
            state = env.get(f"RAUC_SLOT_STATE_{idx}")
            if state == "booted":
                slot_name = env.get(f"RAUC_SLOT_NAME_{idx}")
                if slot_name:
                    return slot_name
        
        # Fallback: check bootname
        for key, value in env.items():
            if key.endswith("_STATE") and value == "booted":
                prefix = key[:-6]  # Remove "_STATE"
                name_key = f"{prefix}_NAME"
                if name_key in env:
                    return env[name_key]
        
        LOG.warning("could not find booted slot in RAUC status")
        return None
        
    except Exception as e:
        LOG.warning(f"failed to get booted slot: {e}")
        return None


def _get_boot_id() -> str:
    """Get current boot ID from /proc/sys/kernel/random/boot_id."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except Exception as e:
        LOG.warning(f"failed to read boot_id: {e}")
        return ""
```

### Phase 3: Restore State (on rollback)

When rollback is detected, restore the pre-update package state:

```python
def _handle_rollback() -> int:
    """Restore package state after rollback."""
    LOG.info("restoring package state after rollback")
    
    try:
        # Load pre-update writable status
        pre_update_entries = load_status_entries(PRE_UPDATE_WRITABLE_STATUS)
        current_entries = load_status_entries(WRITABLE_STATUS)
        
        # Find packages that were removed during update
        pre_update_pkgs = {e["Package"] for e in pre_update_entries}
        current_pkgs = {e["Package"] for e in current_entries}
        missing_pkgs = pre_update_pkgs - current_pkgs
        
        LOG.info(f"found {len(missing_pkgs)} packages to restore: {sorted(missing_pkgs)}")
        
        # Restore writable status to pre-update state
        write_status_entries(WRITABLE_STATUS, pre_update_entries)
        LOG.info("restored writable status to pre-update state")
        
        # Clean up pending forward-update operations (no longer relevant)
        PENDING_REINSTALL_FILE.unlink(missing_ok=True)
        PENDING_UPGRADE_FILE.unlink(missing_ok=True)
        LOG.info("removed pending forward-update operations")
        
        # Clean up rollback state files
        PRE_UPDATE_WRITABLE_STATUS.unlink(missing_ok=True)
        PRE_UPDATE_SLOT_NAME.unlink(missing_ok=True)
        UPDATED_SLOT_NAME.unlink(missing_ok=True)
        UPDATE_BOOT_ID.unlink(missing_ok=True)
        LOG.info("cleaned up rollback state files")
        
        LOG.info("rollback restoration complete")
        return 0
        
    except Exception as e:
        LOG.error(f"rollback restoration failed: {e}")
        LOG.error("manual package restoration may be required")
        # Don't clean up state files - leave for debugging
        return 1


def _handle_forward_update() -> int:
    """Process forward update reconciliation."""
    if not PENDING_REINSTALL_FILE.exists() and not PENDING_UPGRADE_FILE.exists():
        LOG.info("no pending operations for forward update")
        _cleanup_update_state()
        return 0
    
    # Run opkg update
    if not _run_opkg(["update"]):
        LOG.error("opkg update failed; will retry next boot")
        return 1
    
    # Process pending operations
    reinstall_ok = _process_pending(PENDING_REINSTALL_FILE, _install_reinstall_pkg)
    upgrade_ok = _process_pending(PENDING_UPGRADE_FILE, _upgrade_pkg)
    
    if reinstall_ok and upgrade_ok:
        LOG.info("forward update reconciliation complete")
        # Save boot ID to prevent re-processing
        UPDATE_BOOT_ID.write_text(_get_boot_id() + "\n")
        _cleanup_update_state()
        return 0
    else:
        LOG.error("forward update reconciliation incomplete; will retry")
        return 1


def _cleanup_update_state() -> None:
    """Remove all update state files after successful processing."""
    PRE_UPDATE_WRITABLE_STATUS.unlink(missing_ok=True)
    PRE_UPDATE_SLOT_NAME.unlink(missing_ok=True)
    UPDATED_SLOT_NAME.unlink(missing_ok=True)
    PENDING_REINSTALL_FILE.unlink(missing_ok=True)
    PENDING_UPGRADE_FILE.unlink(missing_ok=True)
    # Note: Keep UPDATE_BOOT_ID until next update to prevent re-processing
```

## Implementation Details

### File Locations

State files stored in calculinux-update's data directory (writable, persists across reboots):

```
/var/lib/opkg/
├── status                                        # Current packages (OPKG standard)
└── status.image                                  # Base image packages (RAUC convention)

/var/lib/calculinux-update/
├── prefetch.json                                 # Existing: Prefetch state
├── update-state.pre-update-writable              # NEW: Saved writable status before update
├── update-state.pre-update-slot                  # NEW: Which slot we were on before update (e.g., "rootfs.0")
├── update-state.updated-slot                     # NEW: Which slot was updated (e.g., "rootfs.1")
├── update-state.boot-id                          # NEW: Boot ID when update was processed (prevents double-processing)
├── update-state.pending-reinstalls               # MOVED: Forward-update reinstalls (from /var/lib/opkg/)
└── update-state.pending-upgrades                 # MOVED: Forward-update upgrades (from /var/lib/opkg/)
```

**Note**: The `update-state.*` files replace the previous `opkg-status-hook.*` files in `/var/lib/opkg/`. This follows the pattern established by `prefetch.json` and keeps calculinux-update implementation details separate from OPKG's directory.

### State Transitions

**Normal Update Flow:**

```
1. Boot slot A (rootfs.0)
2. Install update to slot B (rootfs.1)
   └─ cup-hook saves:
      - current writable status
      - pre-update slot name: "rootfs.0"
      - updated slot name: "rootfs.1"
   └─ cup-hook prunes duplicates, queues pending ops
3. Reboot to slot B (rootfs.1)
   └─ cup-postreboot detects forward update:
      - booted slot (rootfs.1) == updated slot (rootfs.1)
      - processes pending reinstalls/upgrades
   └─ cup-postreboot saves boot ID, prevents re-processing
   └─ Cleanup: remove saved state files
```

**Rollback Flow:**

```
1. Boot slot A (rootfs.0)
2. Install update to slot B (rootfs.1)
   └─ cup-hook saves:
      - current writable status
      - pre-update slot name: "rootfs.0"
      - updated slot name: "rootfs.1"
   └─ cup-hook prunes duplicates, queues pending ops
3. Reboot to slot B (rootfs.1) - fails or marked bad
4. Reboot to slot A (rootfs.0) - automatic rollback
   └─ cup-postreboot detects rollback:
      - booted slot (rootfs.0) == pre-update slot (rootfs.0)
      - booted slot (rootfs.0) != updated slot (rootfs.1)
   └─ cup-postreboot restores pre-update writable status
   └─ Cleanup: remove saved state + pending ops
```

**Ambiguous Case (Third Slot or Renamed):**

```
1. Boot slot A (rootfs.0)
2. Install update to slot B (rootfs.1)
   └─ cup-hook saves state
3. Boot slot C (rootfs.2) - unexpected
   └─ cup-postreboot checks:
      - booted slot (rootfs.2) != updated slot (rootfs.1)
      - booted slot (rootfs.2) != pre-update slot (rootfs.0)
   └─ Falls back to package comparison:
      - If packages missing from writable status -> treat as rollback
      - If packages intact -> unknown state, skip processing
```

### Rollback Detection Strategy Summary

The detection uses a **three-level strategy**:

1. **Primary: Slot name comparison**
   - Compare booted slot name vs. pre-update and updated slot names
   - Most reliable when slot naming is stable

2. **Secondary: Boot ID tracking**
   - Prevent re-processing the same boot
   - Handles service restarts correctly

3. **Tertiary: Package state comparison**
   - Fallback for ambiguous slot configurations
   - Check if packages are missing from writable status

This layered approach handles:
- Standard A/B updates and rollbacks
- Multiple boots before marking good
- Service restarts (via boot ID)
- Ambiguous slot configurations (via package comparison)
- Legitimate slot marking as bad (not confused with rollback)

### Edge Cases

#### 1. Multiple Updates Before Reboot

If user installs update A, then update B before rebooting:

- Second hook overwrites saved state (correct - we want to restore to before update B)
- Updated slot marker points to slot B
- Behavior: Works correctly

#### 2. Service Disabled

If `calculinux-update-postreboot.service` is disabled:

- Saved state files persist
- On next boot (after enabling service), rollback will be detected late
- Mitigation: Document that service should stay enabled

#### 3. Manual Status File Edits

If user manually edits `/var/lib/opkg/status` after update:

- Pre-update state won't reflect manual changes
- Rollback will restore to state before update (losing manual edits)
- Acceptable: Rare edge case, user should re-apply edits

#### 4. Failed Hook

If hook fails to save pre-update state:

- Rollback detection will fail (no marker file)
- Falls back to current behavior (manual restoration needed)
- Acceptable: Hook failures are already problematic

#### 5. Disk Full

If disk is full when saving state:

- Hook will log error but continue (update proceeds)
- Rollback detection will fail
- Acceptable: Disk full is already a critical issue

#### 6. Slot Marked Bad Without Rollback

If other slot is marked bad for reasons other than failure (e.g., policy, age):

- We check booted slot vs. pre-update/updated slots, not bad/good state
- Marking unused slot as bad doesn't trigger rollback detection
- Correct behavior: Only actual boot to different slot triggers rollback

#### 7. Third Slot or Renamed Slots

If system boots into a slot that doesn't match either saved name:

- Fallback to package comparison strategy
- If packages missing -> treat as rollback
- If packages intact -> unknown state, skip processing
- Conservative: Avoids incorrect restorations

#### 8. Multiple Reboots Before Processing

System reboots multiple times before post-reboot service runs:

- Boot ID tracking prevents re-processing same boot
- State files persist until successfully processed
- Handles delayed reconciliation correctly

#### 9. Service Restart Without Reboot

If post-reboot service restarts during same boot:

- Boot ID check detects same boot, skips re-processing
- Prevents duplicate reconciliation operations
- Idempotent behavior

### Cleanup Strategy

**When to remove saved state:**

1. **After successful forward update reconciliation**
   ```python
   # In _handle_forward_update(), after successful processing:
   UPDATE_BOOT_ID.write_text(_get_boot_id())  # Save boot ID first
   _cleanup_update_state()                     # Remove other state files
   # Keep UPDATE_BOOT_ID to prevent re-processing
   ```

2. **After successful rollback restoration**
   ```python
   # In _handle_rollback(), after restoring state:
   # Remove ALL state files including boot ID
   _cleanup_update_state()
   UPDATE_BOOT_ID.unlink(missing_ok=True)
   ```

3. **On next update**
   ```python
   # In run_slot_hook(), before saving new state:
   # Previous boot ID is no longer relevant, overwrite
   _save_pre_update_state(...)  # Overwrites all state files
   ```

4. **Never remove prematurely** - Keep until reconciliation completes or new update starts

**State File Lifecycle:**

```
Update install:
  └─ Create: pre-update-writable, pre-update-slot, updated-slot

Forward update reboot:
  └─ Create: update-boot-id
  └─ Remove: pre-update-writable, pre-update-slot, updated-slot, pending-*
  └─ Keep: update-boot-id (prevents re-processing)

Rollback reboot:
  └─ Remove: ALL state files (update-boot-id, pre-update-*, updated-slot, pending-*)

Next update install:
  └─ Overwrite: ALL state files with new update data
```

### Testing Strategy

**Unit Tests:**

```python
def test_detect_rollback_when_booted_different_slot(tmp_path):
    """Test rollback detection when booted slot != updated slot."""
    marker = tmp_path / "updated-slot"
    marker.write_text("rootfs.1\n")
    
    with patch("hooks._get_booted_slot", return_value="rootfs.0"):
        assert _detect_rollback() is True


def test_no_rollback_when_booted_updated_slot(tmp_path):
    """Test no rollback when booted slot == updated slot."""
    marker = tmp_path / "updated-slot"
    marker.write_text("rootfs.1\n")
    
    with patch("hooks._get_booted_slot", return_value="rootfs.1"):
        assert _detect_rollback() is False


def test_restore_packages_after_rollback(tmp_path):
    """Test package restoration removes duplicates and restores status."""
    # Setup pre-update state with package A and B
    pre_update = [
        {"Package": "foo", "Version": "1.0"},
        {"Package": "bar", "Version": "2.0"},
    ]
    
    # Current state only has package B (A was pruned)
    current = [
        {"Package": "bar", "Version": "2.0"},
    ]
    
    # Save and restore
    write_status_entries(PRE_UPDATE_WRITABLE_STATUS, pre_update)
    write_status_entries(WRITABLE_STATUS, current)
    
    _handle_rollback()
    
    # Verify restoration
    restored = load_status_entries(WRITABLE_STATUS)
    assert len(restored) == 2
    assert any(e["Package"] == "foo" for e in restored)
```

**Integration Tests:**

```python
def test_full_update_rollback_cycle():
    """Test complete update->rollback cycle."""
    # 1. Install update to slot B
    # 2. Verify hook saved state
    # 3. Simulate rollback boot to slot A
    # 4. Run post-reboot service
    # 5. Verify packages restored
```

## Implementation Considerations

### File Path Migration

Since we're moving from `/var/lib/opkg/` to `/var/lib/calculinux-update/`, we need to:

1. **Update `hooks.py` constants:**
   ```python
   # Old paths (current implementation)
   PENDING_REINSTALL_FILE = Path("/var/lib/opkg/opkg-status-hook.pending-reinstalls")
   PENDING_UPGRADE_FILE = Path("/var/lib/opkg/opkg-status-hook.pending-upgrades")
   
   # New paths (following prefetch.json pattern)
   PENDING_REINSTALL_FILE = Path("/var/lib/calculinux-update/update-state.pending-reinstalls")
   PENDING_UPGRADE_FILE = Path("/var/lib/calculinux-update/update-state.pending-upgrades")
   
   # New rollback tracking files
   PRE_UPDATE_WRITABLE_STATUS = Path("/var/lib/calculinux-update/update-state.pre-update-writable")
   PRE_UPDATE_SLOT_NAME = Path("/var/lib/calculinux-update/update-state.pre-update-slot")
   UPDATED_SLOT_NAME = Path("/var/lib/calculinux-update/update-state.updated-slot")
   UPDATE_BOOT_ID = Path("/var/lib/calculinux-update/update-state.boot-id")
   ```

2. **Migration strategy:**
   - Code should check for old file paths on startup and migrate them
   - Add migration helper in `hooks.py`:
     ```python
     def _migrate_legacy_state_files():
         """Migrate state files from old /var/lib/opkg/ location."""
         OLD_NEW_PAIRS = [
             (Path("/var/lib/opkg/opkg-status-hook.pending-reinstalls"),
              PENDING_REINSTALL_FILE),
             (Path("/var/lib/opkg/opkg-status-hook.pending-upgrades"),
              PENDING_UPGRADE_FILE),
         ]
         
         for old_path, new_path in OLD_NEW_PAIRS:
             if old_path.exists() and not new_path.exists():
                 LOG.info(f"migrating {old_path} -> {new_path}")
                 new_path.parent.mkdir(parents=True, exist_ok=True)
                 shutil.copy2(old_path, new_path)
                 old_path.unlink()  # Remove old file after successful copy
     ```

3. **Ensure directory exists:**
   ```python
   # At module initialization
   STATE_DIR = Path("/var/lib/calculinux-update")
   STATE_DIR.mkdir(parents=True, exist_ok=True)
   ```

### Security Considerations

1. **File Permissions**: Saved state files should have same permissions as `/var/lib/opkg/status` (root:root, 0644)
2. **No Sensitive Data**: Status files don't contain secrets, only package metadata
3. **Atomic Operations**: Use temp files + rename for atomic writes
4. **Validation**: Validate saved status file format before using

## Performance Impact

- **Hook time**: +10-50ms (copy status file, write marker)
- **Post-reboot time**: +50-200ms (detect rollback, restore status)
- **Disk space**: ~50-500KB (saved status file)

All impacts are negligible for typical update workflows.

## Documentation Updates

### User Documentation

Update `docs/user-guide/updates.md`:

```markdown
### Rolling Back an Update

After rolling back, the system automatically restores your package state:

```bash
# Mark current slot as bad and reboot to previous
sudo rauc status mark-bad booted
sudo reboot
```

The post-reboot service will detect the rollback and restore packages that
were removed during the forward update.
```

### Developer Documentation

Update `docs/developer/calculinux-update.md`:

- Add rollback detection algorithm description
- Document state file format and locations
- Add rollback handling to reconciliation flowchart

## Future Enhancements

1. **Multi-generation rollback**: Support rolling back multiple updates (save history)
2. **Selective restoration**: Allow user to opt-out of specific package restorations
3. **Rollback analytics**: Log rollback events for debugging/monitoring
4. **Status comparison**: Add `cup status --compare-slots` to preview rollback effects

## Implementation Checklist

- [ ] Add `_save_pre_update_state()` to `hooks.py`
- [ ] Add `_detect_rollback()` to `hooks.py`
- [ ] Add `_handle_rollback()` to `hooks.py`
- [ ] Add `_get_booted_slot()` helper
- [ ] Update `run_slot_hook()` to save state
- [ ] Update `postreboot_entrypoint()` to detect and handle rollback
- [ ] Add unit tests for rollback detection
- [ ] Add unit tests for state restoration
- [ ] Add integration test for full cycle
- [ ] Update user documentation
- [ ] Update developer documentation
- [ ] Update man page with rollback behavior

## Open Questions

1. **Should we support partial rollback?** (e.g., restore only specific packages)
   - **Decision**: No, keep it simple. Full state restoration only.

2. **What if status file is corrupted?**
   - **Decision**: Log error, skip restoration, leave files for manual inspection.

3. **Should we notify user of rollback?**
   - **Decision**: Yes, log prominently to journalctl with INFO level. Consider future notification mechanism.

4. **Cleanup old saved states?**
   - **Decision**: Always cleanup after forward update or rollback. Keep boot ID after forward update to prevent re-processing.

5. **How to handle RAUC slot name changes?**
   - **Decision**: ✅ RESOLVED - Use three-level detection strategy:
     1. Primary: Slot name comparison (handles standard cases)
     2. Secondary: Boot ID tracking (prevents re-processing)
     3. Tertiary: Package comparison (handles ambiguous slot configurations)
   
   If slot names are consistent (typical case), detection is straightforward. If slot names change or third slot exists, fallback to package state comparison.

6. **What about multi-generational updates?**
   - **Decision**: Current design only tracks one generation (immediate previous). Multi-generation rollback would require history tracking - defer to future enhancement.

7. **Should we validate slot names from RAUC?**
   - **Decision**: Yes, log warning if unable to determine slot names but continue. System should be resilient to RAUC query failures.
