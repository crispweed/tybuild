"""
Build file generation for tybuild.

Handles generation of Visual Studio project and solution files
from discovered projects and templates.
"""

from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path
from typing import List, Optional

from tybuild.dependencies import get_cpp_dependencies
from tybuild.projects import discover_projects, Project
from tybuild.vs_templates import (
    generate_project_from_template,
    generate_project_guid,
    generate_solution,
)

# Hardcoded GUIDs for special CMake projects
ALL_BUILD_GUID = "5C330799-6FA6-33C3-B12C-755A9CA12672"
ZERO_CHECK_GUID = "46BE4EB3-B0FD-3982-8000-AE0905052172"


def _copy_special_projects(template_dir: Path, build_dir: Path) -> None:
    """
    Copy ALL_BUILD and ZERO_CHECK project files from template to build directory.

    Args:
        template_dir: Directory containing template files
        build_dir: Target directory for build files
    """
    special_files = [
        "ALL_BUILD.vcxproj",
        "ALL_BUILD.vcxproj.filters",
        "ZERO_CHECK.vcxproj",
        "ZERO_CHECK.vcxproj.filters",
    ]

    for filename in special_files:
        src = template_dir / filename
        dst = build_dir / filename

        if src.exists():
            shutil.copy2(src, dst)
            print(f"  Copied: {filename}")
        else:
            print(f"  Warning: Template file not found: {filename}", file=sys.stderr)


def generate_build_files(base_path: Optional[Path] = None) -> List[Project]:
    """
    Generate Visual Studio project and solution files for all discovered projects.

    This function:
    1. Discovers projects in ./src/project/
    2. For each project, determines dependencies
    3. Generates .vcxproj files from templates in ./build_template/
    4. Generates a Solution.sln file in ./build/

    Args:
        base_path: Base directory (defaults to current working directory)

    Returns:
        List of projects that were processed

    Raises:
        RuntimeError: If no projects are found or required directories don't exist
        FileNotFoundError: If template files are missing
    """
    if base_path is None:
        base_path = Path.cwd()
    else:
        base_path = Path(base_path).resolve()

    # Define paths
    src_root = base_path / "src"
    template_dir = base_path / "build_template"
    build_dir = base_path / "build"

    # Validate required directories exist
    if not src_root.exists():
        raise RuntimeError(f"Source directory not found: {src_root}")
    if not template_dir.exists():
        raise RuntimeError(f"Template directory not found: {template_dir}")

    # Create build directory
    build_dir.mkdir(exist_ok=True)

    # Copy special CMake project files
    print("Copying special project files...")
    _copy_special_projects(template_dir, build_dir)
    print()

    # Discover projects
    projects = discover_projects(base_path)

    if not projects:
        raise RuntimeError("No projects found in ./src/project/")

    print(f"Found {len(projects)} project(s):")
    for project in projects:
        print(f"  {project.type:15} {project.name}")
    print()

    # Generate GUIDs
    solution_guid = str(uuid.uuid4()).upper()
    all_build_guid = ALL_BUILD_GUID
    zero_check_guid = ZERO_CHECK_GUID

    print(f"Solution GUID: {solution_guid}")
    print(f"ALL_BUILD GUID: {all_build_guid}")
    print(f"ZERO_CHECK GUID: {zero_check_guid}")
    print()

    # Generate project files
    projects_to_add = []

    for project in projects:
        print(f"Processing {project.type}/{project.name}...")

        # Generate deterministic GUID for this project
        project_guid = generate_project_guid(project.type, project.name)
        print(f"  GUID: {project_guid}")

        # Get dependencies for the main cpp file
        deps = get_cpp_dependencies(src_root, project.cpp_file)
        print(f"  Dependencies: {len(deps)} file(s)")

        # Build sources list: main cpp + dependencies
        main_cpp_rel = project.cpp_file.relative_to(src_root).as_posix()
        sources = [main_cpp_rel] + deps
        print(f"  Total sources: {len(sources)} file(s)")

        # Generate template name
        template_name = f"ZZZZZZZZ_{project.type}"
        template_vcxproj = template_dir / f"{template_name}.vcxproj"

        if not template_vcxproj.exists():
            raise FileNotFoundError(
                f"Template not found: {template_vcxproj}\n"
                f"Expected template for project type '{project.type}'"
            )

        # Generate project files
        generate_project_from_template(
            template_path=template_dir,
            template_name=template_name,
            project_name=project.name,
            project_guid=project_guid,
            source_root=src_root,
            sources_rel_to_root=sources,
            output_path=build_dir,
        )

        print(f"  Generated: {build_dir / project.name}.vcxproj")

        projects_to_add.append((project.name, project_guid))
        print()

    # Generate solution
    sln_path = build_dir / "Solution.sln"
    generate_solution(
        output_sln_path=sln_path,
        solution_guid=solution_guid,
        all_build_guid=all_build_guid,
        zero_check_guid=zero_check_guid,
        projects_to_add=projects_to_add,
    )

    print(f"Generated solution: {sln_path}")
    print()
    print(f"Build files generated successfully!")
    print(f"  {len(projects)} project(s)")
    print(f"  1 solution file")

    return projects
