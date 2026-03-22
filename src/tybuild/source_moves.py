"""
Detect moved/renamed source files via git and update includes accordingly.

Since all #include "..." paths are relative to the source root, updating
after moves only requires:
1. Detecting renamed .h files (old path -> new path) via git
2. Rewriting #include lines in all source files that reference old paths
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

from tybuild.dependencies import INCLUDE_RE, SCAN_EXTS, find_source_files, posix_relpath


def detect_header_moves(repo_root: Path, src_rel: str = "src") -> Dict[str, str]:
    """
    Use git to detect moved/renamed .h files under the source directory.

    Stages all changes first (so git can detect renames), then parses
    git diff --cached --name-status -M for rename entries.

    Args:
        repo_root: Repository root directory
        src_rel: Relative path to source directory from repo root

    Returns:
        Dict mapping old source-root-relative paths to new source-root-relative
        paths, for .h files only.
    """
    # Stage everything so git can detect renames
    subprocess.run(
        ["git", "add", "-A", src_rel],
        cwd=repo_root, check=True, capture_output=True,
    )

    # Ask git for renames (100% match since we assume no content changes)
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-status", "-M100%", "--diff-filter=R", "--", src_rel],
        cwd=repo_root, check=True, capture_output=True, text=True,
    )

    moves: Dict[str, str] = {}
    src_prefix = src_rel.rstrip("/") + "/"

    for line in result.stdout.strip().splitlines():
        # Format: R100\told/path\tnew/path
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        status, old_path, new_path = parts
        if not status.startswith("R"):
            continue

        # Only care about .h files
        if not new_path.endswith(".h"):
            continue

        # Convert from repo-relative to source-root-relative
        if old_path.startswith(src_prefix) and new_path.startswith(src_prefix):
            old_rel = old_path[len(src_prefix):]
            new_rel = new_path[len(src_prefix):]
            moves[old_rel] = new_rel

    return moves


def update_includes_for_moves(src_root: Path, moves: Dict[str, str]) -> List[Tuple[str, str, str]]:
    """
    Rewrite #include lines in all source files to reflect moved headers.

    Args:
        src_root: Source root directory
        moves: Dict mapping old source-root-relative .h paths to new paths

    Returns:
        List of (file, old_include, new_include) for each replacement made
    """
    if not moves:
        return []

    files = find_source_files(src_root)
    replacements: List[Tuple[str, str, str]] = []

    for file_path in files:
        lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines(True)
        new_lines = []
        changed = False

        for line in lines:
            m = INCLUDE_RE.match(line)
            if m:
                include_str = m.group(1).strip()
                # Normalize to forward slashes for matching
                normalized = include_str.replace("\\", "/")
                if normalized in moves:
                    new_include = moves[normalized]
                    new_line = line[:m.start(1)] + new_include + line[m.end(1):]
                    new_lines.append(new_line)
                    replacements.append((
                        posix_relpath(file_path, src_root),
                        include_str,
                        new_include,
                    ))
                    changed = True
                    continue
            new_lines.append(line)

        if changed:
            file_path.write_text("".join(new_lines), encoding="utf-8")

    return replacements


def build_commit_message(moves: Dict[str, str]) -> str:
    """Build a git commit message describing the moves."""
    lines = ["Move/rename source files\n"]
    for old, new in sorted(moves.items()):
        lines.append(f"  {old} -> {new}")
    return "\n".join(lines)


def run_source_files_moved(repo_root: Path, commit: bool = False, push: bool = False) -> bool:
    """
    Main entry point: detect moves, update includes, optionally commit/push.

    Args:
        repo_root: Repository root directory
        commit: If True, commit the changes
        push: If True, push after committing (implies commit)

    Returns:
        True if any moves were detected and processed
    """
    if push:
        commit = True

    src_root = repo_root / "src"
    if not src_root.is_dir():
        print("Error: No ./src directory found", file=sys.stderr)
        return False

    # Detect moves via git
    moves = detect_header_moves(repo_root)

    if not moves:
        print("No header file moves detected.")
        return False

    print(f"Detected {len(moves)} moved header(s):")
    for old, new in sorted(moves.items()):
        print(f"  {old} -> {new}")

    # Update includes
    replacements = update_includes_for_moves(src_root, moves)

    if replacements:
        print(f"\nUpdated {len(replacements)} include(s):")
        for file_rel, old_inc, new_inc in replacements:
            print(f'  {file_rel}: "{old_inc}" -> "{new_inc}"')
    else:
        print("\nNo includes needed updating.")

    if commit:
        # Stage the include updates
        subprocess.run(
            ["git", "add", "-A", "src"],
            cwd=repo_root, check=True, capture_output=True,
        )
        msg = build_commit_message(moves)
        subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=repo_root, check=True,
        )
        print(f"\nCommitted.")

        if push:
            subprocess.run(
                ["git", "push"],
                cwd=repo_root, check=True,
            )
            print("Pushed.")

    return True
