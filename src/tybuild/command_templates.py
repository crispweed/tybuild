"""
Compile and link command templates for `tybuild build`.

MSBuild records the exact command lines it runs in tracking logs (.tlog files) in a
project's intermediate directory. Rather than reimplementing MSBuild's CL and Link
tasks, which add switches of their own that are not in the project file, each
ZZZZZZZZ_<type> dummy project in ./build_template/ is built once with MSBuild, and
the commands it recorded are read back with the per-file and per-project parts
taken out. A change to CMakeLists.txt then reaches `tybuild build` with no change
here, in the same spirit as read_toolchain_settings().

What is built is a copy of the dummy in the tybuild build directory, with a stub
source and without its project references and cmake check (see
extract_type_templates()), and with IntDir and OutDir overridden to point there too.
Paths that the cmake-generated project states absolutely (the linker's PDB and import
library) still go to ./build_template/Debug/, but those belong to the dummy project,
which nothing else builds into ./build/.

Command lines are kept as raw tokens, with their quoting exactly as recorded, so
that they are passed on to cl and link unchanged rather than unquoted and requoted.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import xml.etree.ElementTree as ET

from tybuild.vs_templates import (
    _detect_ns,
    _ns_tag,
    _remove_custom_build_in_vcxproj,
    _replace_sources_in_vcxproj,
    remove_project_references_in_vcxproj,
)

CONFIGURATION = "Debug"
PLATFORM = "x64"

# The source the extraction copy of a dummy project compiles. It defines both entry
# points, so that it links whichever subsystem the project type uses. cl treats main
# and WinMain as extern "C" without being told, so no headers are needed.
EXTRACTION_STUB_NAME = "tybuild_extraction_stub.cpp"
EXTRACTION_STUB_SOURCE = """\
// Written by tybuild, to find out the compile and link commands for a project type
int main() { return 0; }
int __stdcall WinMain(struct HINSTANCE__*, struct HINSTANCE__*, char*, int) { return 0; }
"""

DIFFERING_COMPILE_COMMANDS = "TYB101"

# cl switches whose argument can be the following token, as MSBuild writes them
# ('/D _MBCS', '/external:I "path"')
CL_SWITCHES_WITH_SEPARATE_ARGUMENT = {"D", "U", "I", "FI", "FU", "AI", "external:I"}

# cl switches naming a per-project output, removed from the template (case-sensitive,
# as cl's switches are)
CL_PER_PROJECT_PREFIXES = ("Fo", "Fd")

# link switches naming a per-project output (case-insensitive, as link's switches are)
LINK_PER_PROJECT_PREFIXES = ("OUT:", "ILK:", "PDB:", "IMPLIB:", "MANIFESTFILE:", "PGD:")

# The one deliberate deviation from the extracted compile command. /Zi writes debug
# information to a per-project PDB, which works for MSBuild because one cl process
# compiles a whole project; parallel cl processes sharing a PDB would need /FS, which
# serialises through mspdbsrv. /Z7 puts the debug information in each object
# instead, and the linker still writes the executable's PDB.
DEBUG_INFO_REPLACEMENTS = {"/Zi": "/Z7", "-Zi": "/Z7", "/ZI": "/Z7", "-ZI": "/Z7"}


# ---------------------- Command line tokens ----------------------

def split_command_line(command: str) -> List[str]:
    """
    Split a Windows command line into tokens, keeping each token's quoting as written.

    Follows the Microsoft C runtime's rules for where one argument ends: whitespace
    outside quotes, where a quote preceded by an odd number of backslashes is literal.
    """
    tokens: List[str] = []
    current: List[str] = []
    in_token = False
    in_quotes = False
    backslashes = 0
    for ch in command:
        if ch == "\\":
            backslashes += 1
            current.append(ch)
            in_token = True
            continue
        if ch == '"':
            if backslashes % 2 == 0:
                in_quotes = not in_quotes
            current.append(ch)
            in_token = True
        elif ch in " \t" and not in_quotes:
            if in_token:
                tokens.append("".join(current))
                current = []
                in_token = False
        else:
            current.append(ch)
            in_token = True
        backslashes = 0
    if in_token:
        tokens.append("".join(current))
    return tokens


def unquote_token(token: str) -> str:
    """The argument value a token stands for, by the Microsoft C runtime's rules."""
    out: List[str] = []
    backslashes = 0
    for ch in token:
        if ch == "\\":
            backslashes += 1
            continue
        if ch == '"':
            out.append("\\" * (backslashes // 2))
            if backslashes % 2:
                out.append('"')
            backslashes = 0
            continue
        out.append("\\" * backslashes)
        backslashes = 0
        out.append(ch)
    out.append("\\" * backslashes)
    return "".join(out)


def quote_argument(value: str) -> str:
    """Quote a single argument value (such as a path) for a Windows command line."""
    return subprocess.list2cmdline([value])


def _is_switch(token: str) -> bool:
    return len(token) > 1 and token[0] in "/-"


def _path_key(path: str, base_dir: Path) -> str:
    """Comparable form of a path as MSBuild records it (which may be upper-cased)."""
    return os.path.normcase(os.path.normpath(os.path.join(base_dir, path)))


def _mentions_directory(token: str, directories: Sequence[Path]) -> bool:
    value = os.path.normcase(unquote_token(token).replace("/", "\\"))
    return any(os.path.normcase(str(d)) in value for d in directories)


# ---------------------- Templates ----------------------

@dataclass
class TypeTemplates:
    """
    The compile and link commands for one project type, per-file and per-project
    parts removed.

    compile_switches holds each cl switch as a list of raw tokens: one token, or two
    for a switch with a separate argument ('/D', 'RTC_STATIC'). link_arguments holds
    link's raw tokens, libraries included, objects excluded.
    """
    template_identity: Dict[str, int]
    dummy_source: str
    compile_switches: List[List[str]]
    link_arguments: List[str]

    @staticmethod
    def from_json(data: Dict[str, object]) -> "TypeTemplates":
        return TypeTemplates(
            template_identity=dict(data["template_identity"]),
            dummy_source=str(data["dummy_source"]),
            compile_switches=[list(s) for s in data["compile_switches"]],
            link_arguments=list(data["link_arguments"]),
        )

    def effective_compile_switches(self) -> List[List[str]]:
        """The compile switches as tybuild runs them, with DEBUG_INFO_REPLACEMENTS applied."""
        out = []
        for switch in self.compile_switches:
            if len(switch) == 1 and switch[0] in DEBUG_INFO_REPLACEMENTS:
                out.append([DEBUG_INFO_REPLACEMENTS[switch[0]]])
            else:
                out.append(switch)
        return out

    def compile_command_id(self, toolchain: Dict[str, str]) -> str:
        """
        Identity of the compile command set: objects compiled with the same id are
        interchangeable. Covers the switches as run and the toolchain.
        """
        data = json.dumps(
            {"toolchain": toolchain, "switches": self.effective_compile_switches()},
            sort_keys=True,
        )
        return hashlib.sha256(data.encode("utf-8")).hexdigest()[:12]


def read_command_tlog(path: Path) -> List[Tuple[List[str], str]]:
    """
    Read an MSBuild command tlog.

    Each entry is a '^' line naming the inputs, joined by '|', followed by the
    command line. The files are UTF-16 with a byte order mark.

    Returns:
        List of (inputs, command line) pairs
    """
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8-sig", errors="replace")

    entries: List[Tuple[List[str], List[str]]] = []
    for line in text.splitlines():
        if line.startswith("^"):
            entries.append(([p for p in line[1:].split("|") if p], []))
        elif line.strip() and entries:
            entries[-1][1].append(line.strip())
    return [(inputs, " ".join(lines)) for inputs, lines in entries]


def _find_tlogs(int_dir: Path, prefix: str) -> List[Path]:
    """Command tlogs under int_dir whose names start with prefix (case-insensitively)."""
    return sorted(
        p for p in int_dir.rglob("*")
        if p.is_file() and p.name.lower().startswith(prefix) and p.name.lower().endswith(".tlog")
    )


def _single_tlog_entry(int_dir: Path, prefix: str, what: str, template_name: str) -> Tuple[List[str], str]:
    entries = []
    for tlog in _find_tlogs(int_dir, prefix):
        entries.extend(read_command_tlog(tlog))
    if len(entries) != 1:
        raise RuntimeError(
            f"Expected MSBuild to record exactly one {what} command for {template_name}, "
            f"but found {len(entries)} (in {prefix}*.tlog under {int_dir})."
        )
    return entries[0]


def parse_compile_command(
    command: str, inputs: List[str], base_dir: Path, output_dirs: Sequence[Path]
) -> Tuple[List[List[str]], str]:
    """
    Take the per-file and per-project parts out of a recorded cl command line.

    Args:
        command: The command line, as recorded
        inputs: The sources the tlog entry names
        base_dir: Directory relative paths are relative to (the project's)
        output_dirs: The intermediate and output directories the dummy was built with.
            Anything left in the template that mentions one of these is a per-project
            part that this function doesn't know about, and is an error.

    Returns:
        (switches, source path as recorded)
    """
    input_keys = {_path_key(p, base_dir) for p in inputs}
    tokens = split_command_line(command)
    switches: List[List[str]] = []
    sources: List[str] = []
    unexpected: List[str] = []

    i = 0
    while i < len(tokens):
        token = tokens[i]
        if _is_switch(token):
            name = token[1:]
            if name in CL_SWITCHES_WITH_SEPARATE_ARGUMENT and i + 1 < len(tokens):
                switches.append([token, tokens[i + 1]])
                i += 2
                continue
            if not name.startswith(CL_PER_PROJECT_PREFIXES):
                switches.append([token])
        elif _path_key(unquote_token(token), base_dir) in input_keys:
            sources.append(unquote_token(token))
        else:
            unexpected.append(token)
        i += 1

    if len(sources) != 1:
        raise RuntimeError(
            f"Expected the recorded compile command to name exactly one source, "
            f"but it names {len(sources)}: {command}"
        )
    leftovers = [" ".join(s) for s in switches if any(_mentions_directory(t, output_dirs) for t in s)]
    if unexpected or leftovers:
        raise RuntimeError(
            f"The recorded compile command has parts that tybuild doesn't know how to "
            f"substitute per file: {', '.join(unexpected + leftovers)}\n"
            f"Command: {command}"
        )
    return switches, sources[0]


def parse_link_command(
    command: str, inputs: List[str], base_dir: Path, output_dirs: Sequence[Path]
) -> List[str]:
    """
    Take the per-project parts out of a recorded link command line: the objects (those
    of the tlog entry's inputs that are .obj files) and the output paths.

    Arguments are as for parse_compile_command(). Returns the remaining raw tokens.
    """
    object_keys = {_path_key(p, base_dir) for p in inputs if p.lower().endswith(".obj")}
    arguments: List[str] = []
    objects = 0
    for token in split_command_line(command):
        if _is_switch(token):
            if token[1:].upper().startswith(LINK_PER_PROJECT_PREFIXES):
                continue
        elif _path_key(unquote_token(token), base_dir) in object_keys:
            objects += 1
            continue
        arguments.append(token)

    if objects != 1:
        raise RuntimeError(
            f"Expected the recorded link command to name exactly one object, "
            f"but it names {objects}: {command}"
        )
    leftovers = [t for t in arguments if _mentions_directory(t, output_dirs)]
    if leftovers:
        raise RuntimeError(
            f"The recorded link command has parts that tybuild doesn't know how to "
            f"substitute per project: {', '.join(leftovers)}\n"
            f"Command: {command}"
        )
    return arguments


BUILD_EVENT_ELEMENTS = ("PreBuildEvent", "PreLinkEvent", "PostBuildEvent")


def _remove_build_events_in_vcxproj(xml_text: str) -> str:
    """Remove pre-build, pre-link and post-build events from a vcxproj file."""
    root = ET.fromstring(xml_text)
    ns = _detect_ns(root)
    ET.register_namespace("", ns)
    for group in root.iter(_ns_tag(ns, "ItemDefinitionGroup")):
        for name in BUILD_EVENT_ELEMENTS:
            for event in group.findall(_ns_tag(ns, name)):
                group.remove(event)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8")


def extract_type_templates(template_vcxproj: Path, msbuild: Path, work_dir: Path) -> TypeTemplates:
    """
    Build a copy of a ZZZZZZZZ_<type> dummy project with MSBuild and read back its commands.

    The copy, in work_dir, differs from the template only in ways that don't affect the
    compile and link switches, which come from the project's settings:
    - Its sources are replaced by EXTRACTION_STUB_SOURCE. The template's own
      DummySource.cpp has no entry point, so the template doesn't link, and MSBuild
      doesn't reliably record a failed link.
    - Its ProjectReferences are removed. Even with BuildProjectReferences=false,
      MSBuild evaluates the referenced ZERO_CHECK, with this build's IntDir.
    - Its CustomBuild steps (cmake's regeneration check) are removed.
    - Its build events are removed. The post-build event runs `vcpkg z-applocal` on
      the executable at an absolute path in ./build_template/Debug/, which OutDir
      doesn't change, so it fails on the copy.

    Args:
        template_vcxproj: The dummy project, in ./build_template/
        msbuild: Path to MSBuild.exe
        work_dir: Directory for this extraction's files. Emptied first, so that the
            tlogs read are the ones this build wrote.

    Raises:
        RuntimeError: If the build fails, or its commands aren't in the expected form
    """
    int_dir = work_dir / "int"
    out_dir = work_dir / "out"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    int_dir.mkdir(parents=True)
    out_dir.mkdir()

    # Recorded before reading, so that a template regenerated from here on is noticed
    # by the next run
    st = template_vcxproj.stat()
    identity = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}

    stub = work_dir / EXTRACTION_STUB_NAME
    stub.write_text(EXTRACTION_STUB_SOURCE, encoding="utf-8")

    project_text = template_vcxproj.read_text(encoding="utf-8-sig", errors="replace")
    project_text = _remove_custom_build_in_vcxproj(project_text)
    project_text = remove_project_references_in_vcxproj(project_text)
    project_text = _remove_build_events_in_vcxproj(project_text)
    project_text = _replace_sources_in_vcxproj(project_text, [str(stub)])
    project_copy = work_dir / template_vcxproj.name
    project_copy.write_text(project_text, encoding="utf-8")

    command = [
        str(msbuild), str(project_copy),
        "/nologo", "/verbosity:minimal", "/nodeReuse:false",
        "/t:Build",
        f"/p:Configuration={CONFIGURATION}",
        f"/p:Platform={PLATFORM}",
        f"/p:IntDir={int_dir}\\",
        f"/p:OutDir={out_dir}\\",
    ]
    result = subprocess.run(
        command, cwd=work_dir,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"MSBuild failed to build a copy of {template_vcxproj.name} ({project_copy}), "
            f"which tybuild builds to find out the compile and link commands to use. "
            f"MSBuild's output:\n{result.stdout}"
        )

    base_dir = work_dir
    output_dirs = [work_dir]

    cl_inputs, cl_command = _single_tlog_entry(int_dir, "cl.command.", "compile", template_vcxproj.name)
    compile_switches, source = parse_compile_command(cl_command, cl_inputs, base_dir, output_dirs)

    link_inputs, link_command = _single_tlog_entry(int_dir, "link.command.", "link", template_vcxproj.name)
    link_arguments = parse_link_command(link_command, link_inputs, base_dir, output_dirs)

    return TypeTemplates(
        template_identity=identity,
        dummy_source=source,
        compile_switches=compile_switches,
        link_arguments=link_arguments,
    )


def get_type_templates(
    template_dir: Path,
    project_types: Sequence[str],
    toolchain: Dict[str, str],
    extract_dir: Path,
    cached: Dict[str, object],
    find_msbuild: Callable[[], Path],
    refresh: bool,
    save: Callable[[Dict[str, object]], None],
    log: Callable[[str], None] = print,
) -> Dict[str, TypeTemplates]:
    """
    Get the command templates for each project type, extracting only those whose
    template changed since they were last extracted.

    Args:
        template_dir: ./build_template/
        project_types: Types to get templates for
        toolchain: From read_toolchain_settings(); a change re-extracts everything
        extract_dir: Parent of the per-type extraction directories
        cached: The 'command_templates' part of the build cache, as last saved
        find_msbuild: Called at most once, and only if something needs extracting
        refresh: Re-extract everything
        save: Called with the updated 'command_templates' part after each extraction,
            so that a run killed part way keeps what it finished
        log: Progress output
    """
    if cached.get("toolchain") != toolchain:
        cached = {}
    cached_types = dict(cached.get("types", {}))
    new_cached: Dict[str, object] = {"toolchain": toolchain, "types": cached_types}

    msbuild: Optional[Path] = None
    templates: Dict[str, TypeTemplates] = {}
    for project_type in project_types:
        template_vcxproj = template_dir / f"ZZZZZZZZ_{project_type}.vcxproj"
        if not template_vcxproj.exists():
            raise FileNotFoundError(
                f"Template not found: {template_vcxproj}\n"
                f"Expected template for project type '{project_type}'"
            )

        entry = cached_types.get(project_type)
        reason = None
        if refresh:
            reason = "--refresh-commands"
        elif entry is None:
            reason = "not extracted yet"
        else:
            st = template_vcxproj.stat()
            identity = entry.get("template_identity", {})
            if identity.get("size") != st.st_size or identity.get("mtime_ns") != st.st_mtime_ns:
                reason = "template changed"

        if reason is None:
            try:
                templates[project_type] = TypeTemplates.from_json(entry)
                log(f"  {project_type}: up to date")
                continue
            except (KeyError, TypeError, ValueError):
                reason = "cached commands unreadable"

        log(f"  {project_type}: building {template_vcxproj.name} ({reason})")
        if msbuild is None:
            msbuild = find_msbuild()
        extracted = extract_type_templates(template_vcxproj, msbuild, extract_dir / project_type)
        templates[project_type] = extracted
        cached_types[project_type] = asdict(extracted)
        save(new_cached)

    return templates


# ---------------------- Comparison across types ----------------------

@dataclass(frozen=True, order=True)
class CommandProblem:
    """A difference in compile commands between project types, in MSVC's format."""
    path: Path
    severity: str   # 'error' or 'warning'
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}(1): {self.severity} {self.code}: {self.message}"


def compare_compile_commands(
    templates: Dict[str, TypeTemplates], template_dir: Path, severity: str
) -> List[CommandProblem]:
    """
    Report each compile switch that some project types have and others don't.

    A difference means every source reached from projects of both types is compiled
    once per type, and the only other symptom of that would be a slower build. Each
    problem names the switch and the types that have it, and is located at the first
    such type's template.

    Args:
        templates: Per-type templates
        template_dir: ./build_template/, for locating problems
        severity: 'error', or 'warning' when the build has been told to go ahead anyway
    """
    types = sorted(templates)
    if len(types) < 2:
        return []

    switches = {t: [" ".join(s) for s in templates[t].compile_switches] for t in types}
    if all(switches[t] == switches[types[0]] for t in types):
        return []

    def location(project_type: str) -> Path:
        return template_dir / f"ZZZZZZZZ_{project_type}.vcxproj"

    prefix = "compile command differs between project types: "
    counts = {t: Counter(switches[t]) for t in types}
    ordered_union: List[str] = []
    for t in types:
        for s in switches[t]:
            if s not in ordered_union:
                ordered_union.append(s)

    problems: List[CommandProblem] = []
    for switch in ordered_union:
        per_type = {t: counts[t][switch] for t in types}
        if len(set(per_type.values())) == 1:
            continue
        having = [t for t in types if per_type[t]]
        if all(n <= 1 for n in per_type.values()):
            detail = f"{', '.join(having)} only"
        else:
            detail = ", ".join(f"{t} {per_type[t]} times" for t in types)
        problems.append(CommandProblem(location(having[0]), severity, DIFFERING_COMPILE_COMMANDS,
                                       f"{prefix}{switch}: {detail}"))

    if not problems:
        # Same switches, different order
        first = types[0]
        for t in types[1:]:
            if switches[t] != switches[first]:
                problems.append(CommandProblem(
                    location(t), severity, DIFFERING_COMPILE_COMMANDS,
                    f"{prefix}{t} has the same switches as {first}, in a different order",
                ))
    return problems
