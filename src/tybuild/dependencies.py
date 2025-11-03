"""
Dependency scanner for C++ source files.

Scans .cpp and .h files to extract include dependencies and build
dependency graphs including implicit header->source relationships.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Set, Tuple

INCLUDE_RE = re.compile(r'^#include\s+"([^"]+)"')  # must be at line start
SCAN_EXTS = {".cpp", ".h"}
CACHE_FILENAME = "includes.cache"


@dataclass(frozen=True)
class FileIdentity:
    size: int
    mtime_ns: int


CacheEntry = Dict[str, object]   # {"size": int, "mtime_ns": int, "includes": List[str]}
Cache = Dict[str, CacheEntry]    # keys are POSIX-style paths relative to root


def posix_relpath(path: Path, root: Path) -> str:
    """Convert a path to POSIX-style relative path from root."""
    return path.relative_to(root).as_posix()


def load_cache(cache_path: Path) -> Cache:
    """Load cached scan results from disk."""
    if cache_path.is_file():
        try:
            with cache_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data  # type: ignore[return-value]
        except Exception:
            pass
    return {}


def save_cache(cache_path: Path, cache: Cache) -> None:
    """Save scan results to cache file."""
    tmp = cache_path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, sort_keys=True)
    tmp.replace(cache_path)


def find_source_files(root: Path) -> List[Path]:
    """Find all .cpp and .h files under root directory."""
    files: List[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        for name in filenames:
            p = d / name
            if p.suffix in SCAN_EXTS:
                files.append(p)
    return files


def current_identity(p: Path) -> FileIdentity:
    """Get current file identity (size and modification time)."""
    st = p.stat()
    return FileIdentity(size=st.st_size, mtime_ns=st.st_mtime_ns)


def needs_rescan(entry: CacheEntry, ident: FileIdentity) -> bool:
    """Check if file needs to be rescanned based on size/mtime changes."""
    return entry.get("size") != ident.size or entry.get("mtime_ns") != ident.mtime_ns


def parse_includes(file_path: Path) -> List[str]:
    """Parse #include "..." statements from a file."""
    includes: List[str] = []
    try:
        with file_path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = INCLUDE_RE.match(line)
                if m:
                    inc = m.group(1).strip()
                    if inc:
                        includes.append(inc)
    except Exception:
        pass
    return includes


def resolve_include(root: Path, includer: Path, include_str: str) -> Path | None:
    """
    Resolve an include path, checking:
    1. Relative to the includer file
    2. Relative to the source root (if not found in step 1)
    """
    root_resolved = root.resolve()

    # Try 1: Relative to the includer file
    candidate = (includer.parent / include_str).resolve(strict=False)
    try:
        candidate = candidate.resolve(strict=True)
        # Check if it's within the root
        try:
            candidate.relative_to(root_resolved)
            return candidate
        except ValueError:
            # Outside root, continue to try relative to root
            pass
    except FileNotFoundError:
        # Not found relative to includer, continue to try relative to root
        pass

    # Try 2: Relative to the source root
    candidate = (root / include_str).resolve(strict=False)
    try:
        candidate = candidate.resolve(strict=True)
        # Check if it's within the root
        try:
            candidate.relative_to(root_resolved)
            return candidate
        except ValueError:
            # Outside root
            pass
    except FileNotFoundError:
        # Not found
        pass

    # Both attempts failed
    print(f"Warning: Could not resolve include '{include_str}' from '{includer}' "
          f"(tried relative to file and relative to root '{root}')",
          file=sys.stderr)
    return None


def prune_cache_to_existing_files(cache: Cache, root: Path) -> None:
    """Remove cache entries for files that no longer exist."""
    to_delete = []
    for rel in list(cache.keys()):
        if not (root / rel).is_file():
            to_delete.append(rel)
    for rel in to_delete:
        del cache[rel]


def scan(root: Path, cache_path: Path, refresh: bool = False) -> Cache:
    """
    Scan all source files under root and build/update the include cache.

    Args:
        root: Root directory to scan
        cache_path: Path to cache file
        refresh: If True, ignore existing cache and rescan everything

    Returns:
        Cache dictionary mapping relative paths to include information
    """
    root = root.resolve()
    cache = {} if refresh else load_cache(cache_path)
    prune_cache_to_existing_files(cache, root)

    files = find_source_files(root)
    for p in files:
        rel = posix_relpath(p, root)
        ident = current_identity(p)
        entry = cache.get(rel)
        if entry is not None and not needs_rescan(entry, ident):
            continue

        raw_includes = parse_includes(p)
        resolved: List[str] = []
        for inc in raw_includes:
            tgt = resolve_include(root, p, inc)
            if tgt and tgt.is_file():
                resolved.append(posix_relpath(tgt, root))

        cache[rel] = {
            "size": ident.size,
            "mtime_ns": ident.mtime_ns,
            "includes": sorted(set(resolved)),
        }

    save_cache(cache_path, cache)
    return cache


def build_graph(cache: Cache) -> Dict[str, Set[str]]:
    """Build a graph of direct include relationships."""
    graph: Dict[str, Set[str]] = {k: set() for k in cache.keys()}
    for src, entry in cache.items():
        incs = entry.get("includes", [])
        if isinstance(incs, list):
            for dst in incs:
                if isinstance(dst, str):
                    graph.setdefault(src, set()).add(dst)
                    graph.setdefault(dst, set())
    return graph


def build_pairs(cache: Cache) -> Dict[str, str]:
    """Build mapping of .h files to their corresponding .cpp files (same dir & stem)."""
    by_dir_stem: Dict[Tuple[str, str], Dict[str, str]] = {}
    for rel in cache.keys():
        p = Path(rel)
        if p.suffix not in {".h", ".cpp"}:
            continue
        key = (p.parent.as_posix(), p.stem)
        slot = by_dir_stem.setdefault(key, {})
        slot[p.suffix] = rel

    pairs: Dict[str, str] = {}
    for (_dir, _stem), kinds in by_dir_stem.items():
        if ".h" in kinds and ".cpp" in kinds:
            pairs[kinds[".h"]] = kinds[".cpp"]
    return pairs


def build_dependency_graph(cache: Cache) -> Dict[str, Set[str]]:
    """
    Build dependency graph including both direct includes and implicit header->source edges.

    Implicit edges connect .h files to their corresponding .cpp files when they share
    the same directory and stem.
    """
    graph = build_graph(cache)
    header_to_src = build_pairs(cache)
    for h, cpp in header_to_src.items():
        graph.setdefault(h, set()).add(cpp)  # implied dependency
        graph.setdefault(cpp, set())
    return graph


def transitive_reachable(graph: Dict[str, Set[str]], start_rel: str) -> Set[str]:
    """Find all files transitively reachable from start_rel in the graph."""
    visited: Set[str] = set()
    stack = deque([start_rel])
    while stack:
        node = stack.pop()
        if node in visited:
            continue
        visited.add(node)
        for nxt in graph.get(node, ()):
            if nxt not in visited:
                stack.append(nxt)
    visited.discard(start_rel)
    return visited


def ensure_file_in_cache(root: Path, cache: Cache, file_path: Path) -> str:
    """
    Ensure a file is in the cache, adding it temporarily if needed.

    Returns:
        Relative path of the file from root
    """
    try:
        rel_path = posix_relpath(file_path, root)
    except Exception:
        raise ValueError(f"File '{file_path}' is not under root '{root}'")

    if rel_path not in cache:
        if not file_path.is_file():
            raise FileNotFoundError(f"File '{rel_path}' does not exist")

        raw_includes = parse_includes(file_path)
        resolved: List[str] = []
        for inc in raw_includes:
            tgt = resolve_include(root, file_path, inc)
            if tgt and tgt.is_file():
                resolved.append(posix_relpath(tgt, root))

        cache[rel_path] = {
            "size": file_path.stat().st_size,
            "mtime_ns": file_path.stat().st_mtime_ns,
            "includes": sorted(set(resolved)),
        }

    return rel_path


def get_cpp_dependencies(root: Path, start_file: Path, refresh: bool = False) -> List[str]:
    """
    Get all .cpp file dependencies for a given start file.

    This includes all .cpp files that are transitively reachable through:
    - Direct #include relationships
    - Implicit header->source relationships (same directory and stem)

    Args:
        root: Root directory containing the source tree
        start_file: The starting .cpp or .h file (absolute path)
        refresh: If True, rebuild cache from scratch

    Returns:
        Sorted list of relative paths to .cpp dependencies
    """
    root = root.resolve()
    start_file = start_file.resolve()
    cache_path = root / CACHE_FILENAME

    # Scan and build cache
    cache = scan(root, cache_path, refresh=refresh)

    # Ensure start file is in cache
    start_rel = ensure_file_in_cache(root, cache, start_file)

    # Build dependency graph and find reachable files
    dep_graph = build_dependency_graph(cache)
    reachable = transitive_reachable(dep_graph, start_rel)

    # Filter to only .cpp files
    cpp_files = [f for f in reachable if Path(f).suffix == ".cpp"]

    # Exclude the start file itself if it's a .cpp
    if Path(start_rel).suffix == ".cpp" and start_rel in cpp_files:
        cpp_files.remove(start_rel)

    return sorted(cpp_files)
