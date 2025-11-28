"""
CMake project export for tybuild.

Exports discovered projects and their dependencies to a CMake-friendly format.
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from tybuild.dependencies import get_cpp_dependencies
from tybuild.projects import Project, discover_projects


def generate_cmake_file(repo_root: Path, output_path: Path) -> None:
    """
    Generate a CMake file listing all projects and their sources.

    Args:
        repo_root: Repository root directory (contains ./src)
        output_path: Path to write generated_projects.cmake file

    The generated file contains:
    - GENERATED_PROJECTS variable with semicolon-separated project names
    - For each project:
      - <ProjectName>_TYPE variable with the project type
      - <ProjectName>_SOURCES variable with list of source files
    """
    repo_root = repo_root.resolve()
    src_root = repo_root / "src"

    # Discover all projects
    projects = discover_projects(repo_root)

    if not projects:
        raise RuntimeError(f"No projects found under {repo_root / 'src' / 'project'}")

    # Build CMake content
    lines: List[str] = []

    # Generate project list
    project_names = [p.name for p in projects]
    lines.append(f'set(GENERATED_PROJECTS "{";".join(project_names)}")')
    lines.append("")

    # Generate settings for each project
    for project in projects:
        # Get dependencies (all .cpp files this project needs)
        try:
            deps = get_cpp_dependencies(repo_root, project.cpp_file, include_headers=False)

            # Build source list: start with the project's main .cpp file, then dependencies
            # Both main file and deps are relative to src_root
            sources = [project.cpp_file.relative_to(src_root).as_posix()]
            sources.extend(deps)

        except Exception as e:
            print(f"Warning: Could not get dependencies for {project.name}: {e}")
            # Fallback to just the main file
            sources = [project.cpp_file.relative_to(src_root).as_posix()]

        # Write project type
        lines.append(f'set({project.name}_TYPE "{project.type}")')

        # Write project sources
        lines.append(f'set({project.name}_SOURCES')
        for src in sources:
            lines.append(f'    {src}')
        lines.append(')')
        lines.append('')

    # Write to file
    content = '\n'.join(lines)
    output_path.write_text(content, encoding='utf-8')
