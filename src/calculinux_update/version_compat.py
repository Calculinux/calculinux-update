"""Version compatibility checking for updates.

Compares the running system's version manifest with the one embedded in
an update bundle (bundle extras).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, IntEnum
from pathlib import Path
from typing import Dict, List


class UpgradeType(Enum):
    PATCH = "patch"
    MINOR = "minor"
    MAJOR = "major"
    DOWNGRADE = "downgrade"


class CompatLevel(IntEnum):
    COMPATIBLE = 0
    MINOR_ISSUES = 1
    MAJOR_ISSUES = 2
    INCOMPATIBLE = 3


@dataclass
class CompatibilityIssue:
    level: CompatLevel
    category: str
    message: str
    recommendation: str | None = None


@dataclass
class CompatibilityReport:
    upgrade_type: UpgradeType
    overall_level: CompatLevel
    issues: List[CompatibilityIssue]

    def any_blockers(self) -> bool:
        return self.overall_level == CompatLevel.INCOMPATIBLE


# v1.0.0-alpha4  /  1.0.0-continuous+abc123  /  1.0.0
_VERSION_RE = re.compile(
    r"^v?(?P<x>\d+)(?:\.(?P<y>\d+))?(?:\.(?P<z>\d+))?"
    r"(?:-(?P<pre>[^+]*))?"
    r"(?:\+(?P<build>.*))?$"
)


def _split_version(ver: str) -> tuple[tuple[int, int, int], str]:
    """((x, y, z), tail). Tail is '-pre+build', '+build', '-pre', or ''."""
    ver = (ver or "").strip()
    match = _VERSION_RE.match(ver)
    if not match:
        digits = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", ver)
        if not digits:
            return (0, 0, 0), ver
        nums = tuple(int(part or 0) for part in digits.groups())
        return nums, ver[digits.end() :]  # type: ignore[return-value]
    nums = (
        int(match.group("x")),
        int(match.group("y") or 0),
        int(match.group("z") or 0),
    )
    pre = match.group("pre") or ""
    build = match.group("build") or ""
    if pre and build:
        tail = f"-{pre}+{build}"
    elif build:
        tail = f"+{build}"
    elif pre:
        tail = f"-{pre}"
    else:
        tail = ""
    return nums, tail


def _parse_version(ver: str) -> tuple[int, int, int]:
    """Leading X.Y.Z, ignoring prefixes (v) and suffixes (-dev+hash)."""
    return _split_version(ver)[0]


def version_meets_minimum(
    current: str,
    minimum: str,
    *,
    current_timestamp: str = "",
    minimum_timestamp: str = "",
) -> bool:
    """True if current is at least minimum.

    Bare X.Y.Z (``1.0.0``) matches any build of that release or newer.
    A tail (``1.0.0-continuous+abc``) must match exactly when the
    numeric parts are equal, unless both timestamps are set and current
    is not older — that is how unversioned 1.0.0-* builds compare.
    """
    if not minimum.strip():
        return True
    cur_n, cur_tail = _split_version(current)
    min_n, min_tail = _split_version(minimum)
    if cur_n != min_n:
        return cur_n > min_n
    if not min_tail or cur_tail == min_tail:
        return True
    if current_timestamp and minimum_timestamp:
        return current_timestamp >= minimum_timestamp
    return False


def load_version_manifest(path: Path) -> Dict[str, str]:
    """Parse a simple KEY="VALUE" env-style manifest file."""
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    except (OSError, IOError):
        return {}
    return out


def get_upgrade_type(old_ver: str, new_ver: str) -> UpgradeType:
    old_p = _parse_version(old_ver)
    new_p = _parse_version(new_ver)
    if new_p > old_p:
        if new_p[0] != old_p[0]:
            return UpgradeType.MAJOR
        if new_p[1] != old_p[1]:
            return UpgradeType.MINOR
        return UpgradeType.PATCH
    if new_p < old_p:
        return UpgradeType.DOWNGRADE
    return UpgradeType.PATCH


def check_compatibility(old: Dict[str, str], new: Dict[str, str]) -> CompatibilityReport:
    issues: List[CompatibilityIssue] = []

    upgrade_type = get_upgrade_type(
        old.get("CALCULINUX_VERSION", "0.0.0"),
        new.get("CALCULINUX_VERSION", "0.0.0"),
    )

    # Target bundle requires a minimum running version (e.g. wrynose
    # pointing at the last walnascar image that shipped this checker).
    minimum = new.get("MIN_CALCULINUX_VERSION", "").strip()
    current = old.get("CALCULINUX_VERSION", "").strip()
    if minimum and not version_meets_minimum(
        current,
        minimum,
        current_timestamp=old.get("BUILD_TIMESTAMP", "").strip(),
        minimum_timestamp=new.get("MIN_BUILD_TIMESTAMP", "").strip(),
    ):
        issues.append(
            CompatibilityIssue(
                level=CompatLevel.INCOMPATIBLE,
                category="min-version",
                message=(
                    f"This update requires Calculinux {minimum} or newer "
                    f"(running {current or 'unknown'})"
                ),
                recommendation=(
                    f"Install Calculinux {minimum} first, then retry this update"
                ),
            )
        )

    if old.get("KERNEL_VERSION") and new.get("KERNEL_VERSION"):
        old_k = _parse_version(old["KERNEL_VERSION"])
        new_k = _parse_version(new["KERNEL_VERSION"])
        if old_k[0] != new_k[0]:
            issues.append(
                CompatibilityIssue(
                    level=CompatLevel.MAJOR_ISSUES,
                    category="kernel",
                    message=f"Kernel major changed: {old['KERNEL_VERSION']} -> {new['KERNEL_VERSION']}",
                    recommendation="Out-of-tree kernel modules will need rebuild",
                )
            )

    if old.get("PYTHON_VERSION") and new.get("PYTHON_VERSION"):
        if old["PYTHON_VERSION"] != new["PYTHON_VERSION"]:
            issues.append(
                CompatibilityIssue(
                    level=CompatLevel.MAJOR_ISSUES,
                    category="python",
                    message=f"Python version changed: {old['PYTHON_VERSION']} -> {new['PYTHON_VERSION']}",
                    recommendation="Python packages may need reinstall",
                )
            )

    if old.get("YOCTO_VERSION") and new.get("YOCTO_VERSION"):
        if old["YOCTO_VERSION"] != new["YOCTO_VERSION"]:
            issues.append(
                CompatibilityIssue(
                    level=CompatLevel.MAJOR_ISSUES,
                    category="abi",
                    message=f"Yocto release changed: {old['YOCTO_VERSION']} -> {new['YOCTO_VERSION']}",
                    recommendation="Overlay packages should be upgraded/reinstalled",
                )
            )

    if old.get("CALCULINUX_CODENAME") and new.get("CALCULINUX_CODENAME"):
        if old["CALCULINUX_CODENAME"] != new["CALCULINUX_CODENAME"]:
            issues.append(
                CompatibilityIssue(
                    level=CompatLevel.MINOR_ISSUES,
                    category="feeds",
                    message=f"Codename changed: {old['CALCULINUX_CODENAME']} -> {new['CALCULINUX_CODENAME']}",
                    recommendation="Package feeds will be updated to new codename",
                )
            )

    overall = max((i.level for i in issues), default=CompatLevel.COMPATIBLE)
    return CompatibilityReport(upgrade_type=upgrade_type, overall_level=overall, issues=issues)
