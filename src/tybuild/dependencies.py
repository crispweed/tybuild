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


# Error codes, reported in MSVC's canonical error format (see IncludeProblem)
INCLUDE_NOT_FOUND = "TYB001"
INCLUDE_RELATIVE_TO_INCLUDER = "TYB002"


class IncludeError(Exception):
    """Raised when there is an error relating to include file resolution."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, order=True)
class IncludeProblem:
    """An include that could not be resolved, located by includer and line."""
    path: Path      # absolute path of the including file
    line: int
    code: str
    message: str

    def __str__(self) -> str:
        # MSVC's canonical format, which MSBuild recognises in tool output and shows
        # in Visual Studio's Error List. The path is absolute because MSBuild would
        # otherwise resolve it against the project directory.
        return f"{self.path}({self.line}): error {self.code}: {self.message}"


@dataclass(frozen=True)
class FileIdentity:
    size: int
    mtime_ns: int


# {"size": int, "mtime_ns": int, "includes": List[str],
#  "errors": List[{"line": int, "code": str, "message": str}]}
# "errors" is absent in caches written before it was added. Those versions never
# cached a file with an include error, so an absent "errors" means there were none.
CacheEntry = Dict[str, object]
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
    """
    Check if file needs to be rescanned.

    A file is rescanned if its size or mtime changed, and also whenever its last
    scan found include errors: an include error can be fixed without touching the
    includer (by adding the missing file, say), so a cached error could go stale.
    """
    if entry.get("errors"):
        return True
    return entry.get("size") != ident.size or entry.get("mtime_ns") != ident.mtime_ns


def parse_includes(file_path: Path) -> List[Tuple[int, str]]:
    """Parse #include "..." statements from a file, as (1-based line number, include) pairs."""
    includes: List[Tuple[int, str]] = []
    try:
        with file_path.open("r", encoding="utf-8", errors="ignore") as f:
            for line_number, line in enumerate(f, start=1):
                m = INCLUDE_RE.match(line)
                if m:
                    inc = m.group(1).strip()
                    if inc:
                        includes.append((line_number, inc))
    except Exception:
        pass
    return includes


def resolve_include(root: Path, includer: Path, include_str: str) -> Path:
    """
    Resolve an include path relative to the source root.

    All #include "..." paths must be relative to the source root.
    For files in subdirectories, if the include string would resolve
    relative to the includer's directory, that is reported as an error
    because the compiler would find that match first, bypassing the
    intended source-root-relative resolution.

    Returns the resolved Path on success, or raises IncludeError.
    """
    root_resolved = root.resolve()

    # For files in subdirectories, check if the include would resolve
    # relative to the includer's directory. If so, that's an error
    # because the compiler searches relative to the file first.
    includer_dir = includer.parent.resolve()
    if includer_dir != root_resolved:
        relative_candidate = (includer_dir / include_str).resolve(strict=False)
        try:
            relative_candidate = relative_candidate.resolve(strict=True)
            try:
                root_relative = posix_relpath(relative_candidate, root_resolved)
                if relative_candidate.is_file():
                    # It exists relative to the file's directory — this is
                    # ambiguous because the compiler will find it before
                    # searching the source root include path.
                    raise IncludeError(
                        INCLUDE_RELATIVE_TO_INCLUDER,
                        f"include \"{include_str}\" resolves relative to the including "
                        f"file's directory; write it relative to the source root, as "
                        f"\"{root_relative}\" (tybuild fix-includes does this)"
                    )
            except ValueError:
                pass  # Outside root, not a concern
        except (FileNotFoundError, OSError):
            pass  # Doesn't exist relative to file dir, fine

    # Resolve relative to the source root
    candidate = (root / include_str).resolve(strict=False)
    try:
        candidate = candidate.resolve(strict=True)
        try:
            candidate.relative_to(root_resolved)
            if candidate.is_file():
                return candidate
        except ValueError:
            pass
    except (FileNotFoundError, OSError):
        pass

    raise IncludeError(
        INCLUDE_NOT_FOUND,
        f"cannot find include \"{include_str}\" relative to the source root ({root_resolved})"
    )


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

        cache[rel] = scan_file(root, p, ident)

    save_cache(cache_path, cache)
    return cache


def scan_file(root: Path, file_path: Path, ident: FileIdentity) -> CacheEntry:
    """
    Scan one file into a cache entry.

    Includes that resolve are recorded even if others in the same file do not, so
    that one bad include doesn't also drop the file's other dependencies. The ones
    that do not resolve are recorded under "errors", for include_errors() to report.
    """
    resolved: List[str] = []
    errors: List[Dict[str, object]] = []
    for line, inc in parse_includes(file_path):
        try:
            tgt = resolve_include(root, file_path, inc)
        except IncludeError as e:
            errors.append({"line": line, "code": e.code, "message": str(e)})
            continue
        resolved.append(posix_relpath(tgt, root))

    entry: CacheEntry = {
        "size": ident.size,
        "mtime_ns": ident.mtime_ns,
        "includes": sorted(set(resolved)),
    }
    if errors:
        entry["errors"] = errors
    return entry


def include_errors(root: Path, cache: Cache) -> List[IncludeProblem]:
    """Collect the include errors recorded in a cache, sorted by file and line."""
    root = root.resolve()
    problems: List[IncludeProblem] = []
    for rel, entry in cache.items():
        errors = entry.get("errors", [])
        if not isinstance(errors, list):
            continue
        path = root / Path(rel)
        for error in errors:
            problems.append(IncludeProblem(
                path=path,
                line=int(error["line"]),
                code=str(error["code"]),
                message=str(error["message"]),
            ))
    return sorted(problems)


def find_include_errors(repo_root: Path, refresh: bool = False) -> List[IncludeProblem]:
    """Scan everything under ./src (updating ./includes.cache) and return its include errors."""
    repo_root = repo_root.resolve()
    src_root = repo_root / "src"
    cache = scan(src_root, repo_root / CACHE_FILENAME, refresh=refresh)
    return include_errors(src_root, cache)


def report_include_errors(problems: List[IncludeProblem]) -> None:
    """
    Print include errors to stderr, one per line, followed by a count.

    Each distinct error is listed once: callers collect them from the cache after
    all scanning is done, rather than printing as files are scanned.
    """
    if not problems:
        return
    for problem in problems:
        print(problem, file=sys.stderr)
    # Worded so that MSBuild doesn't take the summary for another error
    print(f"tybuild found {len(problems)} include problem(s), listed above.", file=sys.stderr)


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


def find_include_chain(graph: Dict[str, Set[str]], start_rel: str, end_rel: str) -> List[str] | None:
    """
    Find the shortest chain of includes from start_rel to end_rel using BFS.

    Returns:
        List of relative paths forming the chain (including start and end),
        or None if no chain exists.
    """
    if start_rel == end_rel:
        return [start_rel]

    visited: Set[str] = {start_rel}
    queue: deque[list[str]] = deque([[start_rel]])

    while queue:
        path = queue.popleft()
        current = path[-1]
        for neighbor in sorted(graph.get(current, ())):
            if neighbor in visited:
                continue
            new_path = path + [neighbor]
            if neighbor == end_rel:
                return new_path
            visited.add(neighbor)
            queue.append(new_path)

    return None


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

        cache[rel_path] = scan_file(root, file_path, current_identity(file_path))

    return rel_path


def get_cpp_dependencies(repo_root: Path, start_file: Path, refresh: bool = False, include_headers: bool = False) -> List[str]:
    """
    Get all .cpp file dependencies for a given start file.

    This includes all .cpp files that are transitively reachable through:
    - Direct #include relationships
    - Implicit header->source relationships (same directory and stem)

    Args:
        repo_root: Repository root directory (contains ./src, cache stored here)
        start_file: The starting .cpp or .h file (absolute path)
        refresh: If True, rebuild cache from scratch
        include_headers: If True, also return .h files in dependencies

    Returns:
        Sorted list of relative paths to .cpp dependencies (and .h if include_headers=True)
    """
    repo_root = repo_root.resolve()
    src_root = repo_root / "src"
    start_file = start_file.resolve()
    cache_path = repo_root / CACHE_FILENAME

    # Scan and build cache (scan under src directory)
    cache = scan(src_root, cache_path, refresh=refresh)

    # Ensure start file is in cache
    start_rel = ensure_file_in_cache(src_root, cache, start_file)

    # Build dependency graph and find reachable files
    dep_graph = build_dependency_graph(cache)
    reachable = transitive_reachable(dep_graph, start_rel)

    # Filter based on include_headers flag
    if include_headers:
        # Include both .cpp and .h files
        filtered_files = [f for f in reachable if Path(f).suffix in {".cpp", ".h"}]
    else:
        # Only .cpp files
        filtered_files = [f for f in reachable if Path(f).suffix == ".cpp"]

    # Exclude the start file itself if it's a .cpp
    if Path(start_rel).suffix == ".cpp" and start_rel in filtered_files:
        filtered_files.remove(start_rel)

    return sorted(filtered_files)


def fix_includes(src_root: Path) -> int:
    """
    Fix includes that use file-relative paths to use source-root-relative paths.

    For each source file in a subdirectory, if an #include "..." resolves
    relative to the file's directory (but not via the source root path),
    rewrite the include to use the source-root-relative path.

    Returns the number of files modified.
    """
    src_root = src_root.resolve()
    files = find_source_files(src_root)
    files_modified = 0

    for file_path in files:
        file_path = file_path.resolve()
        includer_dir = file_path.parent

        # Only need to fix files in subdirectories
        if includer_dir == src_root:
            continue

        lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines(True)
        new_lines = []
        changed = False

        for line in lines:
            m = INCLUDE_RE.match(line)
            if m:
                include_str = m.group(1).strip()
                # Check if it resolves relative to the file's directory
                relative_candidate = (includer_dir / include_str).resolve(strict=False)
                try:
                    relative_candidate = relative_candidate.resolve(strict=True)
                    relative_candidate.relative_to(src_root)
                except (FileNotFoundError, OSError, ValueError):
                    new_lines.append(line)
                    continue

                # It resolves relative to file dir. Compute the source-root-relative path.
                root_relative = posix_relpath(relative_candidate, src_root)

                # Only fix if it's not already correct as a root-relative path
                # (i.e., if it also resolves from root to the same file, it's fine)
                root_candidate = (src_root / include_str).resolve(strict=False)
                try:
                    root_candidate = root_candidate.resolve(strict=True)
                    if root_candidate == relative_candidate:
                        # Both resolve to the same file — no ambiguity to fix
                        new_lines.append(line)
                        continue
                except (FileNotFoundError, OSError):
                    pass

                # Rewrite the include
                new_line = line[:m.start(1)] + root_relative + line[m.end(1):]
                new_lines.append(new_line)
                print(f"  {posix_relpath(file_path, src_root)}: "
                      f'"{include_str}" -> "{root_relative}"')
                changed = True
            else:
                new_lines.append(line)

        if changed:
            file_path.write_text("".join(new_lines), encoding="utf-8")
            files_modified += 1

    return files_modified
