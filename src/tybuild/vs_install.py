"""
Locating the Visual Studio installation that matches the cmake-generated templates.

The templates in ./build_template/ name an MSBuild ToolsVersion (read by
vs_templates.read_toolchain_settings()), and its major component is the Visual Studio
major version. The installation is found through vswhere, restricted to that major
version, so that a machine with several Visual Studio versions installed doesn't
silently build with the wrong one.

The same installation's vcvarsall.bat supplies the environment cl and link run in,
and find_compiler() checks that it is the toolset and Windows SDK the templates name.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple


def _vswhere_path() -> Path:
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    return Path(program_files_x86) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"


def find_visual_studio(tools_version: str) -> Path:
    """
    Find the installation directory of the Visual Studio that the templates are for.

    Args:
        tools_version: MSBuild ToolsVersion from read_toolchain_settings(), e.g. '18.0'

    Returns:
        The installation directory, e.g. Path('C:/Program Files/Microsoft Visual Studio/18/Community')

    Raises:
        RuntimeError: If vswhere is missing, or no matching installation with MSBuild is found
    """
    vswhere = _vswhere_path()
    if not vswhere.is_file():
        raise RuntimeError(
            f"Cannot find vswhere.exe at {vswhere}. It is installed with Visual Studio, "
            f"and is needed to find MSBuild."
        )

    major = int(tools_version.split(".")[0])
    command = [
        str(vswhere),
        "-version", f"[{major}.0,{major + 1}.0)",
        "-latest",
        "-prerelease",
        "-products", "*",
        "-requires", "Microsoft.Component.MSBuild",
        "-property", "installationPath",
        "-utf8",
    ]
    result = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        encoding="utf-8", errors="replace",
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode != 0 or not lines:
        raise RuntimeError(
            f"No installation of Visual Studio {major} with MSBuild was found. "
            f"The templates in ./build_template/ are for MSBuild tools version "
            f"{tools_version}, which comes with Visual Studio {major}."
        )
    return Path(lines[0])


def msbuild_path(installation: Path) -> Path:
    """Path to MSBuild.exe in a Visual Studio installation."""
    path = installation / "MSBuild" / "Current" / "Bin" / "MSBuild.exe"
    if not path.is_file():
        raise RuntimeError(f"MSBuild was not found where expected, at {path}.")
    return path


# ---------------------- Compiler environment ----------------------

# Marks the line of vcvarsall's child process output that holds the environment
ENVIRONMENT_MARKER = "TYBUILD_ENVIRONMENT:"


def get_environment_variable(environment: Dict[str, str], name: str) -> Optional[str]:
    """Look up an environment variable by name, case-insensitively as Windows does."""
    upper = name.upper()
    for key, value in environment.items():
        if key.upper() == upper:
            return value
    return None


def capture_build_environment(installation: Path, toolchain: Dict[str, str]) -> Dict[str, str]:
    """
    The environment `vcvarsall.bat x64 <sdk>` sets up, in which cl and link find the
    standard library and Windows SDK headers and libraries.

    MSBuild supplies these paths itself, so the commands recorded from it don't
    include them. The SDK version is passed to vcvarsall so that it matches the
    templates, rather than being whichever SDK is newest.

    The environment is read by running this interpreter after vcvarsall, in the same
    cmd process, rather than by parsing `set`, which is ambiguous for values that
    contain newlines.

    Raises:
        RuntimeError: If vcvarsall is missing or fails
    """
    vcvarsall = installation / "VC" / "Auxiliary" / "Build" / "vcvarsall.bat"
    if not vcvarsall.is_file():
        raise RuntimeError(
            f"vcvarsall.bat was not found where expected, at {vcvarsall}. "
            f"Is the C++ workload installed in this Visual Studio?"
        )

    sdk = toolchain["windows_sdk_version"]
    dump = (
        "import json, os, sys; "
        f"sys.stdout.write('{ENVIRONMENT_MARKER}' + json.dumps(dict(os.environ)) + '\\n')"
    )
    inner = f'"{vcvarsall}" x64 {sdk} && "{sys.executable}" -c "{dump}"'
    # Passed as a string, which subprocess hands to CreateProcess unchanged; cmd /s
    # strips only the outermost quotes
    command = f'cmd /d /s /c "{inner}"'

    environment = dict(os.environ)
    environment["VSCMD_SKIP_SENDTELEMETRY"] = "1"
    result = subprocess.run(
        command, env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
    )
    for line in result.stdout.splitlines():
        if line.startswith(ENVIRONMENT_MARKER):
            return json.loads(line[len(ENVIRONMENT_MARKER):])
    raise RuntimeError(
        f"Setting up the compiler environment with '{vcvarsall}' x64 {sdk} failed. "
        f"Its output:\n{result.stdout}"
    )


def find_compiler(environment: Dict[str, str], toolchain: Dict[str, str]) -> Tuple[Path, Path]:
    """
    Find cl and link in a captured environment, and check that they are the toolset
    and Windows SDK the templates name, rather than silently using another one.

    Platform toolset vNM corresponds to compiler version N.M* (v145 is 14.5x).

    Returns:
        (path to cl.exe, path to link.exe)

    Raises:
        RuntimeError: If they aren't found, or don't match
    """
    path = get_environment_variable(environment, "PATH") or ""
    cl = shutil.which("cl.exe", path=path)
    if cl is None:
        raise RuntimeError("cl.exe is not on the PATH that vcvarsall.bat set up.")
    cl_path = Path(cl)
    link_path = cl_path.parent / "link.exe"
    if not link_path.is_file():
        raise RuntimeError(f"link.exe was not found beside cl.exe, in {cl_path.parent}.")

    toolset = toolchain["platform_toolset"]
    match = re.fullmatch(r"v(\d\d)(\d)", toolset)
    if match is None:
        raise RuntimeError(f"Don't know which compiler version platform toolset '{toolset}' is.")
    expected_prefix = f"{match.group(1)}.{match.group(2)}"

    tools_version = get_environment_variable(environment, "VCToolsVersion") or ""
    tools_dir = get_environment_variable(environment, "VCToolsInstallDir") or ""
    if not tools_version.startswith(expected_prefix):
        raise RuntimeError(
            f"The templates use platform toolset {toolset} (compiler version "
            f"{expected_prefix}), but vcvarsall.bat set up compiler version "
            f"{tools_version or 'unknown'}."
        )
    if not tools_dir or not os.path.normcase(str(cl_path)).startswith(os.path.normcase(tools_dir)):
        raise RuntimeError(
            f"The cl.exe found on the PATH ({cl_path}) is not the one vcvarsall.bat "
            f"set up, in {tools_dir or 'an unknown directory'}."
        )

    sdk = (get_environment_variable(environment, "WindowsSDKVersion") or "").rstrip("\\")
    if sdk != toolchain["windows_sdk_version"]:
        raise RuntimeError(
            f"The templates use Windows SDK {toolchain['windows_sdk_version']}, but "
            f"vcvarsall.bat set up {sdk or 'none'}."
        )

    return cl_path, link_path
