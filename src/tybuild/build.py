"""
Build file generation for tybuild.

Handles generation of Visual Studio project and solution files
from discovered projects and templates.
"""

from __future__ import annotations

import json
import shutil
import sys
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Any

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

CACHE_FILENAME = ".tybuild"


def _get_file_identity(path: Path) -> Dict[str, int]:
    """Get file identity (size and mtime) for cache comparison."""
    st = path.stat()
    return {
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
    }


def _file_changed(path: Path, cached_identity: Dict[str, int]) -> bool:
    """Check if file has changed compared to cached identity."""
    if not path.exists():
        return True
    current = _get_file_identity(path)
    return (current["size"] != cached_identity.get("size") or
            current["mtime_ns"] != cached_identity.get("mtime_ns"))


def _files_dict_changed(base_path: Path, files_dict: Dict[str, Dict[str, int]]) -> bool:
    """Check if any file in the dict has changed."""
    for rel_path, identity in files_dict.items():
        if _file_changed(base_path / rel_path, identity):
            return True
    return False


def _load_build_cache(cache_path: Path) -> Dict[str, Any]:
    """Load build cache from .tybuild file."""
    if not cache_path.exists():
        return {}
    try:
        with cache_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_build_cache(cache_path: Path, cache: Dict[str, Any]) -> None:
    """Save build cache to .tybuild file."""
    with cache_path.open("w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


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


def generate_build_files(base_path: Optional[Path] = None, force: bool = False) -> List[Project]:
    """
    Generate Visual Studio project and solution files for all discovered projects.

    This function:
    1. Discovers projects in ./src/project/
    2. For each project, determines dependencies
    3. Generates .vcxproj files from templates in ./build_template/
    4. Generates a Solution.sln file in ./build/

    Uses incremental regeneration:
    - Only regenerates projects if template or dependencies changed
    - Only regenerates solution if project set changed
    - Stores cache in ./build/.tybuild

    Args:
        base_path: Base directory (defaults to current working directory)
        force: If True, regenerate all files regardless of cache

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

    # Load build cache
    cache_path = build_dir / CACHE_FILENAME
    build_cache = {} if force else _load_build_cache(cache_path)

    # Get or generate solution GUID
    solution_guid = build_cache.get("solution_guid")
    if not solution_guid:
        solution_guid = str(uuid.uuid4()).upper()
        print(f"Generated new solution GUID: {solution_guid}")
    else:
        print(f"Using existing solution GUID: {solution_guid}")

    all_build_guid = ALL_BUILD_GUID
    zero_check_guid = ZERO_CHECK_GUID
    print(f"ALL_BUILD GUID: {all_build_guid}")
    print(f"ZERO_CHECK GUID: {zero_check_guid}")
    print()

    # Check if project set changed (for solution regeneration)
    current_project_set = [(p.name, p.type) for p in projects]
    cached_project_set = [(p["name"], p["type"]) for p in build_cache.get("projects", [])]
    solution_needs_regen = force or current_project_set != cached_project_set

    if solution_needs_regen:
        print("Solution needs regeneration (project set changed or --force)")
    else:
        print("Solution up to date (project set unchanged)")
    print()

    # Generate project files
    projects_to_add = []
    new_cache = {
        "solution_guid": solution_guid,
        "projects": []
    }

    for project in projects:
        print(f"Processing {project.type}/{project.name}...")

        # Generate deterministic GUID for this project
        project_guid = generate_project_guid(project.type, project.name)

        # Get dependencies for the main cpp file (cpp only for project, cpp+h for cache)
        cpp_deps = get_cpp_dependencies(src_root, project.cpp_file, include_headers=False)
        all_deps = get_cpp_dependencies(src_root, project.cpp_file, include_headers=True)

        # Build sources list: main cpp + cpp dependencies
        main_cpp_rel = project.cpp_file.relative_to(src_root).as_posix()
        sources = [main_cpp_rel] + cpp_deps

        # Generate template name
        template_name = f"ZZZZZZZZ_{project.type}"
        template_vcxproj = template_dir / f"{template_name}.vcxproj"

        if not template_vcxproj.exists():
            raise FileNotFoundError(
                f"Template not found: {template_vcxproj}\n"
                f"Expected template for project type '{project.type}'"
            )

        # Check if project needs regeneration
        cached_project = None
        for p in build_cache.get("projects", []):
            if p["name"] == project.name:
                cached_project = p
                break

        needs_regen = force
        reason = "--force flag"

        if not needs_regen and cached_project is None:
            needs_regen = True
            reason = "new project"
        elif not needs_regen:
            # Check template file
            template_identity = cached_project.get("template_identity", {})
            if _file_changed(template_vcxproj, template_identity):
                needs_regen = True
                reason = "template changed"

        if not needs_regen and cached_project:
            # Check source files (main cpp + cpp deps)
            cached_sources = cached_project.get("sources", {})
            current_sources = {s: _get_file_identity(src_root / s) for s in sources}
            if current_sources != cached_sources:
                needs_regen = True
                reason = "source files changed"

        if not needs_regen and cached_project:
            # Check header files
            cached_headers = cached_project.get("headers", {})
            # Get current headers (all_deps - cpp_deps - main cpp)
            header_files = [f for f in all_deps if f.endswith(".h")]
            current_headers = {h: _get_file_identity(src_root / h) for h in header_files}
            if current_headers != cached_headers:
                needs_regen = True
                reason = "header files changed"

        if needs_regen:
            print(f"  Regenerating ({reason})")
            print(f"  GUID: {project_guid}")
            print(f"  Sources: {len(sources)} file(s)")

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
        else:
            print(f"  Up to date (skipping)")

        # Update cache for this project
        header_files = [f for f in all_deps if f.endswith(".h")]
        new_cache["projects"].append({
            "name": project.name,
            "type": project.type,
            "template_identity": _get_file_identity(template_vcxproj),
            "sources": {s: _get_file_identity(src_root / s) for s in sources},
            "headers": {h: _get_file_identity(src_root / h) for h in header_files},
        })

        projects_to_add.append((project.name, project_guid))
        print()

    # Generate solution (only if needed)
    sln_path = build_dir / "Solution.sln"
    if solution_needs_regen:
        print("Regenerating solution...")
        generate_solution(
            output_sln_path=sln_path,
            solution_guid=solution_guid,
            all_build_guid=all_build_guid,
            zero_check_guid=zero_check_guid,
            projects_to_add=projects_to_add,
        )
        print(f"Generated solution: {sln_path}")
    else:
        print(f"Solution up to date: {sln_path}")

    # Save build cache
    _save_build_cache(cache_path, new_cache)
    print()
    print(f"Build files generation complete!")
    print(f"  {len(projects)} project(s)")
    print(f"  Cache saved: {cache_path}")

    return projects
