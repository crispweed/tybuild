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

try:
    from importlib.resources import files
except ImportError:
    # Python < 3.9
    from importlib_resources import files

from tybuild.dependencies import get_cpp_dependencies
from tybuild.projects import discover_projects, Project
from tybuild.vs_templates import (
    generate_project_from_template,
    generate_project_guid,
    generate_solution,
    get_project_guid,
)

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


def _copy_special_projects(
    template_dir: Path,
    build_dir: Path,
    cache: Dict[str, Any],
    force: bool = False
) -> Dict[str, Dict[str, int]]:
    """
    Copy ALL_BUILD and ZERO_CHECK project files from template to build directory,
    and ONE_CHECK from package templates. Only copies if files have changed.

    Args:
        template_dir: Directory containing template files
        build_dir: Target directory for build files
        cache: Build cache dictionary
        force: If True, copy all files regardless of cache

    Returns:
        Dictionary mapping filenames to their file identities (size, mtime_ns)
    """
    special_projects_cache = cache.get("special_projects", {})
    new_identities = {}

    # Copy user-provided templates (ALL_BUILD and ZERO_CHECK)
    user_template_files = [
        "ALL_BUILD.vcxproj",
        "ALL_BUILD.vcxproj.filters",
        "ZERO_CHECK.vcxproj",
        "ZERO_CHECK.vcxproj.filters",
    ]

    for filename in user_template_files:
        src = template_dir / filename
        dst = build_dir / filename

        if not src.exists():
            print(f"  Warning: Template file not found: {filename}", file=sys.stderr)
            continue

        # Check if we need to copy
        needs_copy = force
        reason = "--force flag"

        if not needs_copy:
            cached_identity = special_projects_cache.get(filename, {})
            if _file_changed(src, cached_identity):
                needs_copy = True
                reason = "file changed"
            elif not dst.exists():
                needs_copy = True
                reason = "destination missing"

        if needs_copy:
            shutil.copy2(src, dst)
            print(f"  Copied: {filename} ({reason})")
        else:
            print(f"  Up to date: {filename}")

        # Store identity
        new_identities[filename] = _get_file_identity(src)

    # Copy built-in templates (ONE_CHECK) from package
    builtin_templates = ["ONE_CHECK.vcxproj"]

    templates_path = files("tybuild").joinpath("templates")
    for filename in builtin_templates:
        try:
            dst = build_dir / filename
            template_resource = templates_path.joinpath(filename)

            # For package resources, we need to check by content or always copy
            # Since we can't easily get mtime from package resources, we'll check
            # if destination exists and compare sizes
            needs_copy = force
            reason = "--force flag"

            if not needs_copy:
                # Get the content to check size
                template_content = template_resource.read_text(encoding="utf-8")
                content_size = len(template_content.encode("utf-8"))

                cached_identity = special_projects_cache.get(filename, {})
                if cached_identity.get("size") != content_size:
                    needs_copy = True
                    reason = "content size changed"
                elif not dst.exists():
                    needs_copy = True
                    reason = "destination missing"

                if needs_copy:
                    dst.write_text(template_content, encoding="utf-8")
                    print(f"  Copied: {filename} (built-in, {reason})")
                else:
                    print(f"  Up to date: {filename} (built-in)")

                # Store identity (size only for package resources)
                new_identities[filename] = {"size": content_size, "mtime_ns": 0}
            else:
                # Force copy
                template_content = template_resource.read_text(encoding="utf-8")
                dst.write_text(template_content, encoding="utf-8")
                print(f"  Copied: {filename} (built-in, {reason})")
                content_size = len(template_content.encode("utf-8"))
                new_identities[filename] = {"size": content_size, "mtime_ns": 0}

        except Exception as e:
            print(f"  Warning: Failed to copy built-in template {filename}: {e}", file=sys.stderr)

    return new_identities


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

    # Load build cache from build directory
    cache_path = build_dir / CACHE_FILENAME
    build_cache = {} if force else _load_build_cache(cache_path)

    # Copy special CMake project files (only if changed)
    print("Copying special project files...")
    special_projects_identities = _copy_special_projects(template_dir, build_dir, build_cache, force)
    print()

    # Discover projects
    projects = discover_projects(base_path)

    if not projects:
        raise RuntimeError("No projects found in ./src/project/")

    print(f"Found {len(projects)} project(s):")
    for project in projects:
        print(f"  {project.type:15} {project.name}")
    print()

    # Get or generate solution GUID
    solution_guid = build_cache.get("solution_guid")
    if not solution_guid:
        solution_guid = str(uuid.uuid4()).upper()
        print(f"Generated new solution GUID: {solution_guid}")
    else:
        print(f"Using existing solution GUID: {solution_guid}")

    # Extract GUIDs from copied project files
    all_build_path = build_dir / "ALL_BUILD.vcxproj"
    zero_check_path = build_dir / "ZERO_CHECK.vcxproj"
    one_check_path = build_dir / "ONE_CHECK.vcxproj"

    all_build_guid = get_project_guid(all_build_path)
    if all_build_guid is None:
        raise RuntimeError(
            f"Failed to extract GUID from {all_build_path}. "
            f"Ensure the file exists and contains a valid ProjectGuid element."
        )

    zero_check_guid = get_project_guid(zero_check_path)
    if zero_check_guid is None:
        raise RuntimeError(
            f"Failed to extract GUID from {zero_check_path}. "
            f"Ensure the file exists and contains a valid ProjectGuid element."
        )

    one_check_guid = get_project_guid(one_check_path)
    if one_check_guid is None:
        raise RuntimeError(
            f"Failed to extract GUID from {one_check_path}. "
            f"Ensure the file exists and contains a valid ProjectGuid element."
        )

    print(f"ALL_BUILD GUID: {all_build_guid}")
    print(f"ZERO_CHECK GUID: {zero_check_guid}")
    print(f"ONE_CHECK GUID: {one_check_guid}")
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
        "special_projects": special_projects_identities,
        "projects": []
    }

    for project in projects:
        print(f"Processing {project.type}/{project.name}...")

        # Generate deterministic GUID for this project
        project_guid = generate_project_guid(project.type, project.name)

        # Get dependencies for the main cpp file
        cpp_deps = get_cpp_dependencies(base_path, project.cpp_file, include_headers=False)

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
            # Check template file (size and mtime)
            template_identity = cached_project.get("template_identity", {})
            if _file_changed(template_vcxproj, template_identity):
                needs_regen = True
                reason = "template changed"

        if not needs_regen and cached_project:
            # Check if set of source files changed
            cached_sources = set(cached_project.get("sources", []))
            current_sources = set(sources)
            if current_sources != cached_sources:
                needs_regen = True
                reason = "source file set changed"

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
        new_cache["projects"].append({
            "name": project.name,
            "type": project.type,
            "template_identity": _get_file_identity(template_vcxproj),
            "sources": sources,  # Just the list of source file paths
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
            one_check_guid=one_check_guid,
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
