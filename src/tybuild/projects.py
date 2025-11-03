"""
Project discovery for tybuild.

Discovers projects by scanning the ./src/project directory structure.
Each immediate subdirectory of src/project represents a project type,
and each .cpp file within that type directory represents a project.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List


@dataclass
class Project:
    """Represents a discovered project."""
    name: str
    type: str
    cpp_file: Path

    def __str__(self) -> str:
        return f"{self.type}/{self.name}"


def discover_projects(base_path: Path | None = None) -> List[Project]:
    """
    Discover all projects under the src/project directory.

    Args:
        base_path: Base directory to search from. Defaults to current directory.

    Returns:
        List of discovered Project objects.

    Example:
        If ./src/project/console/Server.cpp exists, this will return:
        [Project(name='Server', type='console', cpp_file=Path('...'))]
    """
    if base_path is None:
        base_path = Path.cwd()
    else:
        base_path = Path(base_path)

    project_dir = base_path / "src" / "project"

    if not project_dir.exists() or not project_dir.is_dir():
        return []

    projects: List[Project] = []

    # Iterate through immediate subdirectories (project types)
    for type_dir_path in project_dir.iterdir():
        if not type_dir_path.is_dir():
            continue

        project_type = type_dir_path.name

        # Find all .cpp files in this type directory
        for entry in type_dir_path.iterdir():
            if entry.is_file() and entry.suffix == ".cpp":
                project_name = entry.stem
                projects.append(Project(
                    name=project_name,
                    type=project_type,
                    cpp_file=entry
                ))

    # Sort by type first, then by name for consistent output
    projects.sort(key=lambda p: (p.type, p.name))

    return projects
