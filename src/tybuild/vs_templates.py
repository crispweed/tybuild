"""
Visual Studio project template processing for tybuild.

Handles generation of Visual Studio project files from templates,
including deterministic GUID generation for projects.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

VC_PROJECT_TYPE_GUID = "{8BC9CEB8-8B4A-11D0-8D11-00A0C91BC942}"

def generate_project_guid(project_type: str, project_name: str) -> str:
    """
    Generate a deterministic GUID for a Visual Studio project.

    The GUID is generated deterministically based on the project type and name,
    ensuring that the same project type/name combination always produces the
    same GUID. This is useful for consistent project file generation.

    Args:
        project_type: The type of the project (e.g., 'console', 'gui', 'library')
        project_name: The name of the project (e.g., 'Server', 'Client')

    Returns:
        A GUID string in the format "{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}"
        (uppercase, without braces)

    Example:
        >>> generate_project_guid('console', 'Server')
        '12345678-1234-5678-1234-567812345678'  # deterministic result
    """
    salt = "tybuild"

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
    return str(generated_uuid).upper()


# ---------------------- Solution file utilities ----------------------

def generate_solution(
    output_sln_path: Path,
    solution_guid: str,
    all_build_guid: str,
    zero_check_guid: str,
    one_check_guid: str,
    projects_to_add: List[Tuple[str, str]],
    tools_version: str
) -> None:
    """
    Generate a solution file with ALL_BUILD, ZERO_CHECK, and ONE_CHECK projects, plus user projects.

    Dependency chain:
    - ZERO_CHECK (no dependencies, runs first)
    - ONE_CHECK (depends on ZERO_CHECK)
    - User projects (depend on ONE_CHECK)
    - ALL_BUILD (depends on all user projects, ONE_CHECK, and ZERO_CHECK)

    Args:
        output_sln_path: Path where the generated solution should be written
        all_build_guid: GUID for the ALL_BUILD project
        zero_check_guid: GUID for the ZERO_CHECK project
        one_check_guid: GUID for the ONE_CHECK project
        projects_to_add: List of (project_name, project_guid) tuples for projects to add to the solution
        tools_version: MSBuild ToolsVersion of the projects in the solution, e.g. '18.0'.
            Its major component is what the solution's version comment names, so that
            the solution and the projects it contains cannot disagree about which
            Visual Studio they are for. Read from the cmake-generated templates by
            read_toolchain_settings() rather than written down here.

    Example:
        generate_solution(
            Path('Generated.sln'),
            'SOLUTION-GUID-HERE',
            '5C330799-6FA6-33C3-B12C-755A9CA12672',
            '46BE4EB3-B0FD-3982-8000-AE0905052172',
            '1E71EEE3-975D-4B10-9620-A4C9F0B25EC9',
            [
                ('Server', '954D3659-7E49-38DA-AAF3-DE9306D58F9B'),
                ('Client', '19EF89DE-8F64-33EA-8F28-40499A66EA07'),
            ],
            '18.0'
        )
    """
    solution_guid = "{" + solution_guid + "}"
    all_build_guid = "{" + all_build_guid + "}"
    zero_check_guid = "{" + zero_check_guid + "}"
    one_check_guid = "{" + one_check_guid + "}"

    # Configuration platforms
    configurations = ["Debug|x64", "Release|x64", "MinSizeRel|x64", "RelWithDebInfo|x64"]

    # Start building the solution content
    lines = []

    # BOM and header. The version comment tracks the toolchain the projects were
    # generated for, rather than being written down separately and drifting from it.
    vs_major_version = tools_version.split(".")[0]

    # Visual Studio's own solution files start with a blank line. The BOM before it
    # comes from the 'utf-8-sig' encoding the file is written with.
    lines.append("")
    lines.append("Microsoft Visual Studio Solution File, Format Version 12.00")
    lines.append(f"# Visual Studio Version {vs_major_version}")

    # ALL_BUILD project (depends on all other projects)
    lines.append(f'Project("{VC_PROJECT_TYPE_GUID}") = "ALL_BUILD", "ALL_BUILD.vcxproj", "{all_build_guid}"')
    lines.append("\tProjectSection(ProjectDependencies) = postProject")
    # ALL_BUILD depends on all user projects, ONE_CHECK, and ZERO_CHECK
    for project_name, project_guid in projects_to_add:
        formatted_guid = '{' + project_guid + '}'
        lines.append(f"\t\t{formatted_guid} = {formatted_guid}")
    lines.append(f"\t\t{one_check_guid} = {one_check_guid}")
    lines.append(f"\t\t{zero_check_guid} = {zero_check_guid}")
    lines.append("\tEndProjectSection")
    lines.append("EndProject")

    # User projects (each depends on ONE_CHECK only)
    for project_name, project_guid in projects_to_add:
        formatted_guid = '{' + project_guid + '}'
        lines.append(f'Project("{VC_PROJECT_TYPE_GUID}") = "{project_name}", "{project_name}.vcxproj", "{formatted_guid}"')
        lines.append("\tProjectSection(ProjectDependencies) = postProject")
        lines.append(f"\t\t{one_check_guid} = {one_check_guid}")
        lines.append("\tEndProjectSection")
        lines.append("EndProject")

    # ONE_CHECK project (depends on ZERO_CHECK)
    lines.append(f'Project("{VC_PROJECT_TYPE_GUID}") = "ONE_CHECK", "ONE_CHECK.vcxproj", "{one_check_guid}"')
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
        formatted_guid = '{' + project_guid + '}'
        for config in configurations:
            config_name = config.split('|')[0]
            lines.append(f"\t\t{formatted_guid}.{config}.ActiveCfg = {config}")
            lines.append(f"\t\t{formatted_guid}.{config}.Build.0 = {config}")

    # ONE_CHECK: ActiveCfg and Build.0
    for config in configurations:
        config_name = config.split('|')[0]
        lines.append(f"\t\t{one_check_guid}.{config}.ActiveCfg = {config}")
        lines.append(f"\t\t{one_check_guid}.{config}.Build.0 = {config}")

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

# ---------------------- Project file utilities ----------------------

def _detect_ns(root) -> str:
    """Detect the XML namespace from the root element."""
    if root.tag.startswith("{") and "}" in root.tag:
        return root.tag[1:].split("}")[0]
    return "http://schemas.microsoft.com/developer/msbuild/2003"


def _ns_tag(ns: str, tag: str) -> str:
    """Create a namespaced XML tag."""
    return f"{{{ns}}}{tag}"


def _backslash(path: str) -> str:
    """Convert path to Windows backslash format."""
    return path.replace("/", "\\").replace(os.sep, "\\")


def get_project_guid(project_file_path: Path) -> Optional[str]:
    """
    Extract the ProjectGuid from a Visual Studio project file.

    Args:
        project_file_path: Path to the .vcxproj file

    Returns:
        The project GUID without braces, or None if not found

    Example:
        >>> get_project_guid(Path('build/Server.vcxproj'))
        '954D3659-7E49-38DA-AAF3-DE9306D58F9B'
    """
    try:
        xml_text = project_file_path.read_text(encoding="utf-8", errors="replace")
        root = ET.fromstring(xml_text)
        ns = _detect_ns(root)

        # Find Globals PropertyGroup
        for pg in root.findall(_ns_tag(ns, "PropertyGroup")):
            if pg.get("Label") == "Globals":
                # Find ProjectGuid element
                guid_el = pg.find(_ns_tag(ns, "ProjectGuid"))
                if guid_el is not None and guid_el.text:
                    # Strip curly braces if present
                    guid_text = guid_el.text.strip()
                    if guid_text.startswith("{") and guid_text.endswith("}"):
                        return guid_text[1:-1]
                    return guid_text
        return None
    except Exception:
        return None


def read_toolchain_settings(reference_vcxproj: Path) -> Dict[str, str]:
    """
    Read the toolchain settings out of a cmake-generated project file.

    ONE_CHECK ships inside this package rather than being cmake output, so it has no
    way of its own to learn about a toolchain upgrade. Rather than restating these
    values in the package template, where they drift silently against their
    cmake-generated siblings, they are read back out of a file that cmake does
    generate and substituted in at generate time.

    ZERO_CHECK is the natural reference: it is cmake output, it is always present,
    and it is a Utility project like ONE_CHECK rather than a compiling one.

    Args:
        reference_vcxproj: Path to a cmake-generated .vcxproj to read settings from

    Returns:
        Dict with keys 'tools_version', 'platform_toolset' and 'windows_sdk_version'

    Raises:
        RuntimeError: If the file is missing or unreadable, does not carry all three
            settings, or names more than one platform toolset

    Example:
        >>> read_toolchain_settings(Path('build_template/ZERO_CHECK.vcxproj'))
        {'tools_version': '18.0', 'platform_toolset': 'v145',
         'windows_sdk_version': '10.0.26100.0'}
    """
    if not reference_vcxproj.exists():
        raise RuntimeError(
            f"Cannot determine the build settings to use: {reference_vcxproj} "
            f"does not exist.\n"
            f"This file is normally generated by cmake into ./build_template/."
        )

    try:
        root = ET.fromstring(reference_vcxproj.read_text(encoding="utf-8", errors="replace"))
    except ET.ParseError as e:
        raise RuntimeError(
            f"Cannot determine the build settings to use: {reference_vcxproj} "
            f"is not a readable project file ({e})."
        )

    ns = _detect_ns(root)

    tools_version = root.get("ToolsVersion")

    windows_sdk_version = None
    toolsets = set()
    for pg in root.findall(_ns_tag(ns, "PropertyGroup")):
        label = pg.get("Label")
        if label == "Globals":
            el = pg.find(_ns_tag(ns, "WindowsTargetPlatformVersion"))
            if el is not None and el.text:
                windows_sdk_version = el.text.strip()
        elif label == "Configuration":
            el = pg.find(_ns_tag(ns, "PlatformToolset"))
            if el is not None and el.text:
                toolsets.add(el.text.strip())

    missing = []
    if not tools_version:
        missing.append("ToolsVersion")
    if not toolsets:
        missing.append("PlatformToolset")
    if not windows_sdk_version:
        missing.append("WindowsTargetPlatformVersion")
    if missing:
        raise RuntimeError(
            f"Cannot determine the build settings to use: {reference_vcxproj.name} "
            f"does not specify {', '.join(missing)}.\n"
            f"Delete ./build_template/ and re-run cmake to regenerate it."
        )

    if len(toolsets) > 1:
        raise RuntimeError(
            f"Cannot determine the build settings to use: {reference_vcxproj.name} "
            f"names more than one platform toolset ({', '.join(sorted(toolsets))}), "
            f"so there is no single value to apply to the other projects."
        )

    return {
        "tools_version": tools_version,
        "platform_toolset": toolsets.pop(),
        "windows_sdk_version": windows_sdk_version,
    }


def _replace_guid_in_vcxproj(xml_text: str, new_guid: str) -> str:
    """
    Replace the ProjectGuid in a vcxproj XML file.

    Args:
        xml_text: The vcxproj file content as XML string
        new_guid: New GUID without braces

    Returns:
        Updated XML text with new GUID
    """
    root = ET.fromstring(xml_text)
    ns = _detect_ns(root)
    ET.register_namespace("", ns)

    # Find or create Globals PropertyGroup
    globals_pg = None
    for pg in root.findall(_ns_tag(ns, "PropertyGroup")):
        if pg.get("Label") == "Globals":
            globals_pg = pg
            break
    if globals_pg is None:
        globals_pg = ET.SubElement(root, _ns_tag(ns, "PropertyGroup"), {"Label": "Globals"})

    # Find or create ProjectGuid element
    guid_el = globals_pg.find(_ns_tag(ns, "ProjectGuid"))
    if guid_el is None:
        guid_el = ET.SubElement(globals_pg, _ns_tag(ns, "ProjectGuid"))

    # Set GUID with braces for XML format
    guid_el.text = "{" + new_guid + "}"

    return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")


def _replace_sources_in_vcxproj(xml_text: str, sources: List[str]) -> str:
    """
    Replace all ClCompile (source file) entries in a vcxproj file.

    Args:
        xml_text: The vcxproj file content as XML string
        sources: List of source file paths to include

    Returns:
        Updated XML text with new sources
    """
    root = ET.fromstring(xml_text)
    ns = _detect_ns(root)
    ET.register_namespace("", ns)

    # Remove all existing ClCompile elements
    for ig in root.findall(_ns_tag(ns, "ItemGroup")):
        for cc in list(ig.findall(_ns_tag(ns, "ClCompile"))):
            ig.remove(cc)

    # Add new sources if any
    if sources:
        ig = ET.SubElement(root, _ns_tag(ns, "ItemGroup"))
        for s in sources:
            ET.SubElement(ig, _ns_tag(ns, "ClCompile"), {"Include": _backslash(s)})

    return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")


def _remove_custom_build_in_vcxproj(xml_text: str) -> str:
    """
    Remove all CustomBuild entries from a vcxproj file.

    This removes CMake-generated custom build steps (typically for CMakeLists.txt)
    that are not needed in tybuild-generated projects.

    Args:
        xml_text: The vcxproj file content as XML string

    Returns:
        Updated XML text with CustomBuild elements removed
    """
    root = ET.fromstring(xml_text)
    ns = _detect_ns(root)
    ET.register_namespace("", ns)

    # Remove all CustomBuild elements and clean up empty ItemGroups
    for ig in list(root.findall(_ns_tag(ns, "ItemGroup"))):
        for cb in list(ig.findall(_ns_tag(ns, "CustomBuild"))):
            ig.remove(cb)
        # Remove ItemGroup if it's now empty
        if len(ig) == 0:
            root.remove(ig)

    return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")


def remove_project_references_in_vcxproj(xml_text: str) -> str:
    """
    Remove all ProjectReference entries from a vcxproj file.

    cmake's ALL_BUILD references the projects in ./build_template/ (ZERO_CHECK and the
    ZZZZZZZZ_<type> dummies), so building the copy in ./build/ would also build those.
    In the generated solution, ALL_BUILD gets its dependencies from the solution
    instead.

    Args:
        xml_text: The vcxproj file content as XML string

    Returns:
        Updated XML text with ProjectReference elements removed
    """
    root = ET.fromstring(xml_text)
    ns = _detect_ns(root)
    ET.register_namespace("", ns)

    for ig in list(root.findall(_ns_tag(ns, "ItemGroup"))):
        refs = ig.findall(_ns_tag(ns, "ProjectReference"))
        for ref in refs:
            ig.remove(ref)
        # Remove the ItemGroup if it only held references
        if refs and len(ig) == 0:
            root.remove(ig)

    return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")


def _replace_sources_in_filters(xml_text: str, sources: List[str]) -> str:
    """
    Replace all ClCompile (source file) entries in a vcxproj.filters file.

    Args:
        xml_text: The .filters file content as XML string
        sources: List of source file paths to include

    Returns:
        Updated XML text with new sources
    """
    root = ET.fromstring(xml_text)
    ns = _detect_ns(root)
    ET.register_namespace("", ns)

    # Remove all existing ClCompile elements
    for ig in root.findall(_ns_tag(ns, "ItemGroup")):
        for cc in list(ig.findall(_ns_tag(ns, "ClCompile"))):
            ig.remove(cc)

    # Ensure "Source Files" filter exists
    def ensure_source_filter():
        for ig in root.findall(_ns_tag(ns, "ItemGroup")):
            for flt in ig.findall(_ns_tag(ns, "Filter")):
                if flt.get("Include") == "Source Files":
                    return
        ig = ET.SubElement(root, _ns_tag(ns, "ItemGroup"))
        flt = ET.SubElement(ig, _ns_tag(ns, "Filter"), {"Include": "Source Files"})
        uid = ET.SubElement(flt, _ns_tag(ns, "UniqueIdentifier"))
        uid.text = "{" + str(uuid.uuid4()).upper() + "}"

    ensure_source_filter()

    # Add new sources if any
    if sources:
        ig = ET.SubElement(root, _ns_tag(ns, "ItemGroup"))
        for s in sources:
            cc = ET.SubElement(ig, _ns_tag(ns, "ClCompile"), {"Include": _backslash(s)})
            flt = ET.SubElement(cc, _ns_tag(ns, "Filter"))
            flt.text = "Source Files"

    return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")


def _make_relative(paths: List[str], to_dir: Path) -> List[str]:
    """
    Make absolute paths relative to a target directory.

    Args:
        paths: List of absolute paths
        to_dir: Directory to make paths relative to

    Returns:
        List of relative paths
    """
    out: List[str] = []
    for p in paths:
        try:
            out.append(os.path.relpath(p, start=to_dir))
        except ValueError:
            # Different drive on Windows
            out.append(p)
    return out


def _resolve_from_source_root(source_root: Path, rel_sources: List[str]) -> List[str]:
    """
    Resolve relative source paths from a source root directory.

    Args:
        source_root: Root directory for source files
        rel_sources: List of paths relative to source_root

    Returns:
        List of absolute paths
    """
    return [str((source_root / s).resolve()) for s in rel_sources]


def generate_project_from_template(
    template_path: Path,
    template_name: str,
    project_name: str,
    project_guid: str,
    source_root: Path, sources_rel_to_root: List[str],
    output_path: Path
) -> None:
    """
    Generate a project file from a template by replacing placeholders.

    Args:
        template_path: Directory containing the template files
        template_name: Base name of the template file (e.g., 'ZZZZZZ' for 'ZZZZZZ.vcxproj' and 'ZZZZZZ.vcxproj.filters', and also the name the project has set in the project file)
        project_name: Name of the project to generate (base name for generated files and also the name set in the project file)
        project_guid: GUID of the project to set in the project file
        source_root: Path to the root directory containing source files
        sources_rel_to_root: List of source file paths relative to source_root to include in the project
        output_path: Path where the generated project should be written

    Example:
        generate_project_from_template(
            Path('template_dir/'),
            'ZZZZZZ',
            'Client_console',
            '19EF89DE-8F64-33EA-8F28-40499A66EA07',
            Path('src/'),
            [
                'main.cpp',
                'utils/helper.cpp',
            ],
            Path('build_dir/')
        )
    """
    # Locate template files
    template_vcxproj = template_path / f"{template_name}.vcxproj"
    template_filters = template_path / f"{template_name}.vcxproj.filters"

    # Read template files
    vcx_text = template_vcxproj.read_text(encoding="utf-8", errors="replace")
    filt_text = None
    if template_filters.exists():
        filt_text = template_filters.read_text(encoding="utf-8", errors="replace")

    # Replace template name with project name everywhere (simple string replacement)
    vcx_text = vcx_text.replace(template_name, project_name)
    if filt_text is not None:
        filt_text = filt_text.replace(template_name, project_name)

    # Remove CMake-generated CustomBuild elements
    vcx_text = _remove_custom_build_in_vcxproj(vcx_text)

    # Replace GUID using XML manipulation
    vcx_text = _replace_guid_in_vcxproj(vcx_text, project_guid)

    # Resolve sources: (sources relative to source_root) → absolute → relative to output_path
    abs_sources = _resolve_from_source_root(source_root, sources_rel_to_root)
    rel_sources_for_proj = _make_relative(abs_sources, output_path)

    # Replace sources in vcxproj and filters using XML manipulation
    vcx_text = _replace_sources_in_vcxproj(vcx_text, rel_sources_for_proj)
    if filt_text is not None:
        filt_text = _replace_sources_in_filters(filt_text, rel_sources_for_proj)

    # Create output directory if needed
    output_path.mkdir(parents=True, exist_ok=True)

    # Write output files
    new_vcx = output_path / f"{project_name}.vcxproj"
    new_vcx.write_text(vcx_text, encoding="utf-8")

    if filt_text is not None:
        new_filters = output_path / f"{project_name}.vcxproj.filters"
        new_filters.write_text(filt_text, encoding="utf-8")
