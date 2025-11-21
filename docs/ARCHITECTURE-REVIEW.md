# Architecture Review - Calculinux Update Tool

**Date:** 2024
**Coverage:** 82.92% (100/100 tests passing)
**Status:** ✅ All features implemented, tests passing

## Executive Summary

The rollback detection feature has been fully implemented with comprehensive tests. This review identifies architectural strengths, potential issues, edge cases, and recommendations for improvement.

---

## 1. Rollback Detection Architecture

### Implementation Strategy: Three-Level Detection

The rollback detection uses a multi-level approach for reliability:

1. **Primary: Slot Name Comparison**
   - Compare booted slot vs. updated slot vs. pre-update slot
   - Most reliable indicator of rollback

2. **Secondary: Boot ID Check**
   - Prevent re-processing same boot session
   - Protects against duplicate executions

3. **Tertiary: Package State Comparison**
   - Fallback when slot names are ambiguous
   - Detects missing packages indicating rollback

### ✅ Strengths

- **Robust**: Multiple detection levels prevent false positives/negatives
- **Idempotent**: Boot ID prevents duplicate processing
- **Self-healing**: Automatically migrates legacy state files
- **Graceful Degradation**: Falls back when information unavailable

### ⚠️ Potential Issues Identified

#### 1. Race Conditions & Atomicity

**Issue**: State file writes are not atomic
```python
# Current implementation (hooks.py:145-160)
UPDATED_SLOT_NAME.write_text(updated_slot + "\n")
PRE_UPDATE_SLOT_NAME.write_text(current_slot + "\n")
PRE_UPDATE_WRITABLE_STATUS # write_status_entries()
```

**Risk**: Power loss between writes could leave inconsistent state
- Only `UPDATED_SLOT_NAME` written → partial state
- State files out of sync → incorrect detection

**Recommendation**:
```python
# Write to temporary files first, then atomic rename
import tempfile
temp = Path(tempfile.mktemp(dir=STATE_DIR))
temp.write_text(data)
temp.replace(final_path)  # Atomic on POSIX systems
```

#### 2. File Permission Issues

**Issue**: State directory creation has soft failure (hooks.py:51-56)
```python
try:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
except (OSError, PermissionError) as e:
    LOG.debug("could not create state directory: %s", e)
    # Continues anyway!
```

**Risk**: Silent failure could cause subsequent operations to fail mysteriously
- Writing state files will fail later with unhelpful errors
- Tests pass because they mock paths

**Recommendation**:
- Create state dir during package installation (in postinst script)
- Verify permissions early in critical paths
- Add explicit check before operations that require it:
```python
def _ensure_state_dir() -> None:
    """Ensure state directory exists with proper permissions."""
    if not STATE_DIR.exists():
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
        except (OSError, PermissionError) as e:
            raise RuntimeError(f"Cannot create state directory {STATE_DIR}: {e}") from e
    
    # Verify writable
    if not os.access(STATE_DIR, os.W_OK):
        raise RuntimeError(f"State directory {STATE_DIR} not writable")
```

#### 3. Slot Name Assumptions

**Issue**: Assumes RAUC slot naming patterns (hooks.py:88-93)
```python
# Parsing: RAUC_SLOT_STATE_rootfs.0=booted
slot_name = slot_var.replace("RAUC_SLOT_STATE_", "")
# Assumes format: <class>.<number> e.g. "rootfs.0", "rootfs.1"
```

**Risk**: Different RAUC configurations might use different naming
- Custom slot names: "system-a", "system-b"
- Multi-part names: "rootfs.primary.a"
- Names without dots: "rootfs_0"

**Recommendation**: No code change needed, but document assumptions:
```python
def _get_booted_slot_name() -> Optional[str]:
    """
    Get the name of the currently booted slot from RAUC.
    
    Parses RAUC_SLOT_STATE_<name>=booted from `rauc status --output-format=shell`.
    Works with any slot naming scheme (e.g., rootfs.0, system-a, etc.).
    
    Returns:
        Slot name string (e.g., "rootfs.0") or None if not found.
    """
```

#### 4. Boot ID Failure Handling

**Issue**: Boot ID read failure is logged but doesn't prevent update (hooks.py:108)
```python
def _get_current_boot_id() -> Optional[str]:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except (OSError, IOError) as e:
        LOG.warning("failed to read boot ID: %s", e)
        return None  # Continues without it!
```

**Risk**: Without boot ID:
- May re-process same boot multiple times
- Could cause duplicate package operations

**Current Mitigation**: State files are deleted after successful processing
**Recommendation**: Consider this acceptable - boot ID is optimization, not critical

#### 5. Ambiguous Slot State Handling

**Issue**: When booted slot doesn't match either expected slot (hooks.py:210-228)
```python
if booted_slot == updated_slot:
    return {"is_rollback": False, ...}
elif booted_slot == pre_update_slot:
    return {"is_rollback": True, ...}
else:
    # Ambiguous: booted into third slot?
    # Falls back to package comparison
```

**Scenarios**:
- Three-slot systems: A, B, C
- Slot name changed after update
- RAUC booted into rescue/recovery slot

**Current Behavior**: Falls back to package comparison
**Risk**: Package comparison might not be reliable if:
- Packages manually installed/removed
- Writable overlay corrupted
- Third slot has similar package set

**Recommendation**: Add explicit handling for common edge cases:
```python
# Detect rescue/recovery slots
if booted_slot in ["rescue", "recovery", "factory"]:
    LOG.warning("booted into recovery slot %s", booted_slot)
    return {"is_rollback": False, "reason": "recovery slot"}

# Three-slot scenario detection
if pre_update_slot and updated_slot and booted_slot not in [pre_update_slot, updated_slot]:
    LOG.warning("booted into unexpected slot %s (expected %s or %s)",
                booted_slot, updated_slot, pre_update_slot)
```

---

## 2. Root Access Security

### Implementation

Root checks added in three entry points:
- `cli.py:_require_root()` - Install command
- `hooks.py:hook_entrypoint()` - RAUC hooks
- `hooks.py:postreboot_entrypoint()` - Post-reboot service

### ✅ Strengths

- Prevents unprivileged operations
- Clear error messages
- Tests properly mock root checks

### ⚠️ Inconsistency

**Issue**: Root check placement inconsistent

```python
# cli.py - check in command function
def install(...):
    _require_root("Installing bundles")
    # Rest of function

# hooks.py - check in entry point
def hook_entrypoint():
    if os.geteuid() != 0:
        LOG.error("hook must run as root")
        raise SystemExit(1)
    # Calls run_slot_hook() which doesn't check
```

**Recommendation**: Consistent approach - check at entry points only
- Entry points: `hook_entrypoint()`, `postreboot_entrypoint()`, CLI commands
- Internal functions: No checks (assume caller validated)
- Document in docstrings: "Requires root. Called from X."

---

## 3. State File Management

### Current Structure

```
/var/lib/calculinux-update/
├── update-state.pre-update-writable  # Full opkg status backup
├── update-state.pre-update-slot      # "rootfs.0"
├── update-state.updated-slot         # "rootfs.1"
├── update-state.boot-id              # "abc-123-..."
├── update-state.pending-reinstalls   # Package list
└── update-state.pending-upgrades     # Package list
```

### ✅ Strengths

- Clear naming convention
- Separate directory from opkg state
- Legacy migration for upgrades

### ⚠️ Issues

#### 1. No State Locking

**Issue**: Concurrent access not protected
```python
# Two processes could run simultaneously:
# Process A: Read state files
# Process B: Delete state files
# Process A: Try to use data → files gone!
```

**Risk**: 
- Multiple systemd service invocations
- Manual hook execution during service run
- Race between cleanup and detection

**Recommendation**: Add file locking
```python
import fcntl

LOCK_FILE = STATE_DIR / ".lock"

@contextmanager
def state_lock():
    """Acquire exclusive lock on state directory."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_FILE.open("w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

def postreboot_entrypoint():
    with state_lock():
        # All state operations here
        ...
```

#### 2. State Cleanup Timing

**Issue**: State files cleaned up after successful processing, not after boot ID match
```python
# hooks.py:188-191
if current_boot_id == saved_boot_id:
    return {"is_rollback": False, "reason": "already processed this boot"}
    # Files NOT cleaned up here!
```

**Risk**: State files accumulate over boots
- Disk space waste (minimal)
- Confusion during debugging

**Recommendation**: Clean up when boot ID matches
```python
if current_boot_id == saved_boot_id:
    _cleanup_update_state()  # Safe to clean up
    return {"is_rollback": False, "reason": "already processed this boot"}
```

#### 3. Orphaned State Files

**Issue**: If process crashes before cleanup, files persist forever
- No TTL or expiration
- No cleanup mechanism

**Recommendation**: Add cleanup script or systemd timer
```python
def cleanup_stale_state():
    """Remove state files older than 7 days."""
    import time
    cutoff = time.time() - (7 * 24 * 3600)
    for path in STATE_DIR.glob("update-state.*"):
        if path.stat().st_mtime < cutoff:
            LOG.info("removing stale state file: %s", path)
            path.unlink(missing_ok=True)
```

---

## 4. Error Handling & Recovery

### Current Behavior

**Partial Failures**: Continue with warnings
```python
# hooks.py:145
except (OSError, IOError) as e:
    LOG.warning("failed to save pre-update slot: %s", e)
    # Continues without this state!
```

### ✅ Strengths

- Graceful degradation
- System doesn't break on minor failures
- Extensive logging for debugging

### ⚠️ Issues

#### 1. Silent Data Loss

**Issue**: Warnings don't prevent execution
```python
_save_pre_update_state(slot)
# If this fails, rollback detection won't work!
# But hook continues anyway
```

**Risk**: False negatives - rollbacks not detected

**Recommendation**: Classify errors by criticality
```python
class CriticalStateError(RuntimeError):
    """State operation failed - rollback detection will not work."""

def _save_pre_update_state(updated_slot: str) -> None:
    _ensure_state_dir()
    
    # Critical operations
    try:
        # Save slot names - essential for detection
        UPDATED_SLOT_NAME.write_text(updated_slot + "\n")
        current_slot = _get_booted_slot_name()
        if current_slot:
            PRE_UPDATE_SLOT_NAME.write_text(current_slot + "\n")
        else:
            raise CriticalStateError("Cannot determine current slot")
    except (OSError, IOError) as e:
        raise CriticalStateError(f"Failed to save slot state: {e}") from e
    
    # Best-effort operations
    if WRITABLE_STATUS.exists():
        try:
            entries = load_status_entries(WRITABLE_STATUS)
            write_status_entries(PRE_UPDATE_WRITABLE_STATUS, entries)
        except (OSError, IOError) as e:
            LOG.warning("failed to save pre-update status: %s", e)
            # Package comparison won't work, but slot comparison will
```

#### 2. No Rollback of Rollback

**Issue**: If `_handle_rollback()` fails, system is in bad state
```python
def _handle_rollback() -> bool:
    # Restore pre-update state
    write_status_entries(WRITABLE_STATUS, pre_update_entries)
    # What if this fails halfway through?
    _cleanup_update_state()  # State files deleted!
    return True
```

**Risk**: Corrupted writable status, no state to retry
- System boots with wrong package state
- No way to recover

**Recommendation**: Write to temporary file first
```python
def _handle_rollback() -> bool:
    if not PRE_UPDATE_WRITABLE_STATUS.exists():
        LOG.warning("cannot restore: pre-update status not found")
        return False
    
    try:
        pre_update_entries = load_status_entries(PRE_UPDATE_WRITABLE_STATUS)
        LOG.info("restoring pre-update state (%d packages)", len(pre_update_entries))
        
        # Write to temp file first
        temp = WRITABLE_STATUS.with_suffix(".tmp")
        write_status_entries(temp, pre_update_entries)
        
        # Atomic replace
        temp.replace(WRITABLE_STATUS)
        LOG.info("restored pre-update package state")
        
        # Only clean up after successful restore
        _cleanup_update_state()
        return True
    
    except (OSError, IOError) as e:
        LOG.error("failed to restore pre-update state: %s", e)
        # Don't clean up - leave for retry
        return False
```

---

## 5. Test Suite Quality

### Coverage: 82.92% (Target: 80%)

### ✅ Strengths

- Comprehensive coverage of new rollback functions
- Good use of mocking (monkeypatch)
- Tests isolated and fast (0.56s for 100 tests)

### ⚠️ Brittleness Issues

#### 1. Exact String Matching

**Example**: test_hooks.py
```python
# Brittle: breaks if log message changes slightly
def test_run_slot_hook_missing_mount_point(monkeypatch, caplog):
    caplog.set_level("WARNING", logger="calculinux_update.hooks")
    # If log message changes from "RAUC_SLOT_MOUNT_POINT not provided"
    # to "Missing RAUC_SLOT_MOUNT_POINT", test breaks
```

**Recommendation**: Check for key terms, not exact strings
```python
assert "RAUC_SLOT_MOUNT_POINT" in caplog.text
assert "not provided" in caplog.text.lower() or "missing" in caplog.text.lower()
```

#### 2. Hardcoded Paths

**Example**: Tests use real path constants
```python
monkeypatch.setattr(hooks, "WRITABLE_STATUS", writable)
# Better: parametrize paths or use fixtures
```

**Recommendation**: Use fixtures for path setup
```python
@pytest.fixture
def mock_opkg_paths(tmp_path, monkeypatch):
    paths = {
        'writable': tmp_path / "status",
        'current': tmp_path / "current",
        'state_dir': tmp_path / "state"
    }
    monkeypatch.setattr(hooks, "WRITABLE_STATUS", paths['writable'])
    monkeypatch.setattr(hooks, "CURRENT_IMAGE_STATUS", paths['current'])
    monkeypatch.setattr(hooks, "STATE_DIR", paths['state_dir'])
    return paths
```

#### 3. Mock Leakage

**Issue**: Autouse fixtures affect all tests
```python
# test_cli.py
@pytest.fixture(autouse=True)
def mock_root_check(monkeypatch):
    monkeypatch.setattr("os.geteuid", lambda: 0)
    # ALL tests now run as "root"
```

**Risk**: Masks actual permission issues
- Tests pass even if permission logic breaks
- False confidence in security checks

**Recommendation**: Be explicit about mocking
```python
@pytest.fixture
def mock_root(monkeypatch):
    monkeypatch.setattr("os.geteuid", lambda: 0)

@pytest.fixture
def mock_non_root(monkeypatch):
    monkeypatch.setattr("os.geteuid", lambda: 1000)

def test_install_requires_root(mock_non_root):
    # Explicitly test that non-root is rejected
    result = runner.invoke(app, ["install", ...])
    assert result.exit_code == 1
    assert "root" in result.stdout.lower()

def test_install_success(mock_root):
    # Test with root permissions
    ...
```

#### 4. Missing Integration Tests

**Gap**: All tests are unit tests with heavy mocking
- No tests with real RAUC output
- No tests with actual filesystem operations
- No tests of full update cycle

**Recommendation**: Add integration test suite
```python
# tests/integration/test_real_update.py
@pytest.mark.integration
@pytest.mark.skipif(not Path("/usr/bin/rauc").exists(), reason="RAUC not installed")
def test_full_update_cycle(tmp_path):
    """Test complete update including RAUC interaction."""
    # Use real RAUC commands, real filesystem
    # Verify end-to-end behavior
```

---

## 6. OPKG Reconciliation Logic

### Current Implementation (opkg/reconcile.py)

```python
def compute_reconcile_plan(...) -> ReconcilePlan:
    duplicates = writable_packages & image_packages
    reinstall = current - image - writable
    upgrade = writable_packages
```

### ⚠️ Issues

#### 1. Upgrade All Writable Packages

**Issue**: `upgrade = sorted(writable_packages)` upgrades EVERYTHING
```python
# If user installed custom package version, it gets forcibly upgraded
# No way to pin packages
# No way to skip specific packages
```

**Risk**: Breaking user customizations
- Custom package versions overwritten
- User-held packages upgraded anyway

**Recommendation**: Respect opkg hold flags
```python
def compute_reconcile_plan(...) -> ReconcilePlan:
    # Load hold flags from /var/lib/opkg/status
    held_packages = {
        e["Package"] 
        for e in load_status_entries(writable_status)
        if e.get("Status", "").startswith("hold")
    }
    
    upgrade = sorted(writable_packages - held_packages)
    held = sorted(writable_packages & held_packages)
    
    return ReconcilePlan(
        duplicates=duplicates,
        reinstall=reinstall,
        upgrade=upgrade,
        held=held,  # Report but don't touch
    )
```

#### 2. Snapshot Mounting

**Issue**: `snapshot_current_slot_status()` mounts slot read-only
```python
# opkg/reconcile.py:76-83
subprocess.run(["mount", "-o", "ro", device, str(mount_dir)])
# ...
finally:
    subprocess.run(["umount", str(mount_dir)])
```

**Risks**:
- Mount failure leaves orphaned mount
- Concurrent mounts to same device
- Missing cleanup on exception

**Recommendation**: Use context manager
```python
@contextmanager
def mount_slot(device: str, readonly: bool = True) -> Path:
    mount_dir = Path(tempfile.mkdtemp(prefix="opkg-slot-"))
    try:
        opts = "ro" if readonly else "rw"
        subprocess.run(
            ["mount", "-o", opts, device, str(mount_dir)],
            check=True, capture_output=True
        )
        yield mount_dir
    finally:
        subprocess.run(["umount", str(mount_dir)], check=False)
        shutil.rmtree(mount_dir, ignore_errors=True)

# Usage:
with mount_slot(device) as mount_dir:
    lower_status = mount_dir / "var/lib/opkg/status"
    # ...
```

---

## 7. Missing Features & Edge Cases

### 1. Network Failures During Prefetch

**Current**: Prefetch errors logged but don't stop update
```python
except PrefetchError as exc:
    console.print(f"[red]Prefetch failed:[/] {exc}")
# Install proceeds anyway
```

**Issue**: Post-reboot package install will fail without network
**Recommendation**: Add retry logic or option to abort on prefetch failure

### 2. Disk Space Checks

**Missing**: No verification that sufficient space exists
- Downloading large bundles
- Saving state files
- Writing new opkg status

**Recommendation**: Pre-flight checks
```python
def check_disk_space(path: Path, required_bytes: int) -> bool:
    stat = os.statvfs(path)
    available = stat.f_bavail * stat.f_frsize
    return available >= required_bytes * 1.5  # 50% margin
```

### 3. Multiple Simultaneous Updates

**Issue**: No locking prevents concurrent updates
- User runs manual update while systemd service runs
- Multiple bundles installed in parallel

**Recommendation**: Add global lock file
```python
GLOBAL_LOCK = Path("/var/lock/calculinux-update.lock")

@contextmanager
def update_lock():
    with GLOBAL_LOCK.open("w") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        except BlockingIOError:
            raise RuntimeError("Another update is in progress")
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
```

### 4. RAUC Status Parsing Fragility

**Issue**: Parsing shell output is fragile
```python
# hooks.py:88-93
for line in result.stdout.splitlines():
    if "RAUC_SLOT_STATE_" in line and line.endswith("=booted"):
        slot_var = line.split("=")[0]
        slot_name = slot_var.replace("RAUC_SLOT_STATE_", "")
```

**Risks**:
- RAUC output format changes
- Locale affects output
- Shell escaping issues

**Recommendation**: Use JSON output if available
```python
# Check if RAUC supports JSON
result = subprocess.run(
    ["rauc", "status", "--output-format=json"],
    capture_output=True, text=True, check=False
)
if result.returncode == 0:
    import json
    status = json.loads(result.stdout)
    # Structured parsing
else:
    # Fall back to shell format
```

### 5. Logging Configuration

**Issue**: Hardcoded logging setup
```python
# hooks.py:23-26
LOG.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("cup-hook: %(message)s"))
```

**Problems**:
- No log file rotation
- No syslog integration
- No verbosity control

**Recommendation**: Use systemd journal
```python
try:
    from systemd import journal
    handler = journal.JournalHandler(SYSLOG_IDENTIFIER="calculinux-update")
except ImportError:
    handler = logging.StreamHandler()

LOG.addHandler(handler)
```

---

## 8. Priority Recommendations

### Critical (Fix Before Production)

1. **Add atomic state file writes** (Section 1.1)
   - Prevents data corruption on power loss
   - Simple fix with tempfile + rename

2. **Add state directory locking** (Section 3.1)
   - Prevents race conditions
   - Essential for reliability

3. **Improve error handling in rollback** (Section 4.2)
   - Prevent data loss on rollback failure
   - Use temp files + atomic replace

### High Priority

4. **Add integration tests** (Section 5.4)
   - Catch issues that unit tests miss
   - Verify real RAUC interaction

5. **Implement disk space checks** (Section 7.2)
   - Prevent failed updates due to full disk
   - Better user experience

6. **Add global update lock** (Section 7.3)
   - Prevent concurrent update conflicts
   - Simple to implement

### Medium Priority

7. **Respect opkg hold flags** (Section 6.1)
   - Prevent breaking user customizations
   - Good practice

8. **Add state cleanup mechanism** (Section 3.3)
   - Prevent orphaned files accumulating
   - Low risk, nice to have

9. **Improve test robustness** (Section 5.1-5.3)
   - Make tests less brittle
   - Improve maintainability

### Low Priority

10. **Add JSON RAUC parsing** (Section 7.4)
    - Future-proof, not urgent
    - Shell format works fine

11. **Improve logging** (Section 7.5)
    - Better observability
    - Not critical for functionality

---

## 9. Code Quality Assessment

### Metrics

- **Lines of Code**: ~1,100 (production)
- **Test Coverage**: 82.92%
- **Test Count**: 100 tests
- **Test Execution**: 0.56s (fast!)
- **Complexity**: Moderate (no deeply nested logic)

### ✅ Good Practices

- Type hints throughout
- Dataclasses for data structures
- Path objects instead of strings
- Context managers for resources
- Comprehensive docstrings
- Separation of concerns

### ⚠️ Areas for Improvement

- Error handling granularity (critical vs. non-critical)
- Atomic operations for state changes
- Resource locking mechanisms
- Integration test coverage
- Documentation of assumptions

---

## 10. Conclusion

The implementation is **solid and functional** with good test coverage. The rollback detection feature is well-designed with multiple levels of verification.

**Main risks**:
1. Non-atomic state updates (power loss vulnerability)
2. Lack of locking (race condition vulnerability)
3. Silent error degradation (failures don't always fail)

**Recommended immediate actions**:
1. Add atomic state file writes (1-2 hours)
2. Add state directory locking (2-3 hours)
3. Improve rollback error handling (1-2 hours)

With these fixes, the system will be production-ready with excellent reliability.

**Overall Grade**: B+ (Good architecture, needs hardening)

---

## Appendix: Test Commands

```bash
# Run full test suite
python -m pytest tests/ -v

# Run with coverage
python -m pytest tests/ --cov=src/calculinux_update --cov-report=html

# Run only rollback tests
python -m pytest tests/test_hooks.py -k rollback -v

# Run integration tests (when implemented)
python -m pytest tests/integration/ -v -m integration
```
