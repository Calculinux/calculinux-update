"""Version compatibility checking for updates.

This module compares a version manifest embedded in the running system with the
version manifest embedded in an update bundle (bundle extras).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List


class UpgradeType(Enum):
    PATCH = "patch"
    MINOR = "minor"
    MAJOR = "major"
    DOWNGRADE = "downgrade"


class CompatLevel(Enum):
    COMPATIBLE = "compatible"
    MINOR_ISSUES = "minor_issues"
    MAJOR_ISSUES = "major_issues"
    INCOMPATIBLE = "incompatible"


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


def _parse_version(ver: str) -> tuple[int, int, int]:
    parts = re.sub(r"[^0-9.]", "", ver).split(".") if ver else []
    padded = (parts + ["0", "0", "0"])[:3]
    return tuple(int(x) if x.isdigit() else 0 for x in padded)  # type: ignore[return-value]


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

    # Kernel major version change
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

    # Python version change
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

    # Yocto release change
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

    # Feed/codename change
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
