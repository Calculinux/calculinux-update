"""Helpers for working with opkg status-format files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Set

__all__ = [
    "StatusEntry",
    "load_status_entries",
    "load_status_index",
    "load_package_names",
    "write_status_entries",
    "filter_entries",
]


@dataclass(slots=True)
class StatusEntry:
    """Represents a single paragraph in an opkg status file."""

    name: str
    raw: str


def _iter_paragraphs(text: str) -> Iterator[str]:
    paragraph: List[str] = []
    for line in text.splitlines():
        if line.strip() == "":
            if paragraph:
                yield "\n".join(paragraph).rstrip() + "\n"
                paragraph = []
            continue
        paragraph.append(line.rstrip("\n"))
    if paragraph:
        yield "\n".join(paragraph).rstrip() + "\n"


def load_status_entries(path: Path) -> List[StatusEntry]:
    """Load all entries from an opkg status-style file."""

    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8", errors="ignore")
    entries: List[StatusEntry] = []
    for paragraph in _iter_paragraphs(text):
        name = _extract_package(paragraph)
        if not name:
            continue
        entries.append(StatusEntry(name=name, raw=paragraph + "\n\n"))
    return entries


def load_status_index(path: Path) -> Dict[str, StatusEntry]:
    """Load status entries keyed by package name."""

    return {entry.name: entry for entry in load_status_entries(path)}


def load_package_names(path: Path) -> Set[str]:
    """Convenience helper returning only the package names from a status file."""

    return set(load_status_index(path).keys())


def write_status_entries(path: Path, entries: Iterable[StatusEntry]) -> None:
    """Rewrite a status file from the provided entries."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(entry.raw.rstrip() + "\n\n")


def filter_entries(entries: Iterable[StatusEntry], keep_names: Iterable[str]) -> List[StatusEntry]:
    """Return only entries whose name exists in *keep_names*."""

    keep = set(keep_names)
    return [entry for entry in entries if entry.name in keep]


def _extract_package(paragraph: str) -> str:
    for line in paragraph.splitlines():
        if line.startswith("Package: "):
            return line.split(": ", 1)[1].strip()
    return ""
