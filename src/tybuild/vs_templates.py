"""
Visual Studio project template processing for tybuild.

Handles generation of Visual Studio project files from templates,
including deterministic GUID generation for projects.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path
from typing import List, Optional, Tuple


GUID_SALT = "tybuild"
VC_PROJECT_TYPE_GUID = "{8BC9CEB8-8B4A-11D0-8D11-00A0C91BC942}"


def generate_project_guid(project_type: str, project_name: str, salt: Optional[str] = None) -> str:
    """
    Generate a deterministic GUID for a Visual Studio project.

    The GUID is generated deterministically based on the project type and name,
    ensuring that the same project type/name combination always produces the
    same GUID. This is useful for consistent project file generation.

    Args:
        project_type: The type of the project (e.g., 'console', 'gui', 'library')
        project_name: The name of the project (e.g., 'Server', 'Client')
        salt: Optional salt string (defaults to GUID_SALT constant)

    Returns:
        A GUID string in the format "{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}"
        (uppercase, with braces)

    Example:
        >>> generate_project_guid('console', 'Server')
        '{12345678-1234-5678-1234-567812345678}'  # deterministic result
    """
    if salt is None:
        salt = GUID_SALT

    # Combine salt, project type, and project name
    seed = f"{salt}:{project_type}:{project_name}"

    # Use SHA-256 to hash the seed
    hash_bytes = hashlib.sha256(seed.encode('utf-8')).digest()

    # Take the first 16 bytes and create a UUID from them
    # Using UUID version 5 format (name-based using SHA-1, but we're using our own hash)
    # We'll construct a valid UUID by setting the version and variant bits
    guid_bytes = bytearray(hash_bytes[:16])

    # Set version to 5 (name-based SHA-1) - bits 12-15 of time_hi_and_version
    guid_bytes[6] = (guid_bytes[6] & 0x0F) | 0x50

    # Set variant to RFC 4122 - bits 6-7 of clock_seq_hi_and_reserved
    guid_bytes[8] = (guid_bytes[8] & 0x3F) | 0x80

    # Create UUID from bytes
    generated_uuid = uuid.UUID(bytes=bytes(guid_bytes))

    # Format as uppercase with braces
    return "{" + str(generated_uuid).upper() + "}"


# ---------------------- Solution file utilities ----------------------

def generate_solution(
    output_sln_path: Path,
    solution_guid: str,
    all_build_guid: str,
    zero_check_guid: str,
    projects_to_add: List[Tuple[str, str]]
) -> None:
    """
    Generate a solution file like the one generate by cmake, with ALL_BUILD and ZERO_CHECK project, plus a number of our own projects.

    Args:
        output_sln_path: Path where the generated solution should be written
        all_build_guid: GUID for the ALL_BUILD project
        zero_check_guid: GUID for the ZERO_CHECK project
        projects_to_add: List of (project_name, project_guid) tuples for projects to add to the solution

    Example:
        generate_solution_from_template(
            Path('Generated.sln'),
            '5C330799-6FA6-33C3-B12C-755A9CA12672',
            '46BE4EB3-B0FD-3982-8000-AE0905052172',
            [
                ('Server', '954D3659-7E49-38DA-AAF3-DE9306D58F9B'),
                ('Client', '19EF89DE-8F64-33EA-8F28-40499A66EA07'),
            ]
        )
    """
    # Ensure GUIDs are in uppercase and have braces
    solution_guid = format_guid(solution_guid)
    all_build_guid = format_guid(all_build_guid)
    zero_check_guid = format_guid(zero_check_guid)

    # Configuration platforms
    configurations = ["Debug|x64", "Release|x64", "MinSizeRel|x64", "RelWithDebInfo|x64"]

    # Start building the solution content
    lines = []

    # BOM and header
    lines.append("\ufeff")  # UTF-8 BOM
    lines.append("Microsoft Visual Studio Solution File, Format Version 12.00")
    lines.append("# Visual Studio Version 17")

    # ALL_BUILD project (depends on all other projects)
    lines.append(f'Project("{VC_PROJECT_TYPE_GUID}") = "ALL_BUILD", "ALL_BUILD.vcxproj", "{all_build_guid}"')
    lines.append("\tProjectSection(ProjectDependencies) = postProject")
    # ALL_BUILD depends on all user projects and ZERO_CHECK
    for project_name, project_guid in projects_to_add:
        formatted_guid = format_guid(project_guid)
        lines.append(f"\t\t{formatted_guid} = {formatted_guid}")
    lines.append(f"\t\t{zero_check_guid} = {zero_check_guid}")
    lines.append("\tEndProjectSection")
    lines.append("EndProject")

    # User projects (each depends on ZERO_CHECK)
    for project_name, project_guid in projects_to_add:
        formatted_guid = format_guid(project_guid)
        lines.append(f'Project("{VC_PROJECT_TYPE_GUID}") = "{project_name}", "{project_name}.vcxproj", "{formatted_guid}"')
        lines.append("\tProjectSection(ProjectDependencies) = postProject")
        lines.append(f"\t\t{zero_check_guid} = {zero_check_guid}")
        lines.append("\tEndProjectSection")
        lines.append("EndProject")

    # ZERO_CHECK project (no dependencies)
    lines.append(f'Project("{VC_PROJECT_TYPE_GUID}") = "ZERO_CHECK", "ZERO_CHECK.vcxproj", "{zero_check_guid}"')
    lines.append("\tProjectSection(ProjectDependencies) = postProject")
    lines.append("\tEndProjectSection")
    lines.append("EndProject")

    # Global section
    lines.append("Global")

    # Solution configurations
    lines.append("\tGlobalSection(SolutionConfigurationPlatforms) = preSolution")
    for config in configurations:
        lines.append(f"\t\t{config} = {config}")
    lines.append("\tEndGlobalSection")

    # Project configurations
    lines.append("\tGlobalSection(ProjectConfigurationPlatforms) = postSolution")

    # ALL_BUILD: ActiveCfg only (no Build.0)
    for config in configurations:
        config_name = config.split('|')[0]
        lines.append(f"\t\t{all_build_guid}.{config}.ActiveCfg = {config}")

    # User projects: ActiveCfg and Build.0
    for project_name, project_guid in projects_to_add:
        formatted_guid = format_guid(project_guid)
        for config in configurations:
            config_name = config.split('|')[0]
            lines.append(f"\t\t{formatted_guid}.{config}.ActiveCfg = {config}")
            lines.append(f"\t\t{formatted_guid}.{config}.Build.0 = {config}")

    # ZERO_CHECK: ActiveCfg and Build.0
    for config in configurations:
        config_name = config.split('|')[0]
        lines.append(f"\t\t{zero_check_guid}.{config}.ActiveCfg = {config}")
        lines.append(f"\t\t{zero_check_guid}.{config}.Build.0 = {config}")

    lines.append("\tEndGlobalSection")

    # Extensibility sections
    lines.append("\tGlobalSection(ExtensibilityGlobals) = postSolution")
    lines.append(f"\t\tSolutionGuid = {solution_guid}")
    lines.append("\tEndGlobalSection")
    lines.append("\tGlobalSection(ExtensibilityAddIns) = postSolution")
    lines.append("\tEndGlobalSection")

    lines.append("EndGlobal")
    lines.append("")  # Trailing newline

    # Write to file
    content = "\n".join(lines)
    output_sln_path.write_text(content, encoding='utf-8-sig')


def format_guid(guid: str) -> str:
    """
    Format a GUID string to ensure it's uppercase and has braces.

    Args:
        guid: GUID string (with or without braces)

    Returns:
        GUID string in format "{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}"
    """
    # Remove braces if present
    guid_clean = guid.strip().strip('{}')
    # Add braces and convert to uppercase
    return "{" + guid_clean.upper() + "}"