"""
`tybuild build`: building every project directly, compiling each shared source once.

Visual Studio builds, through `tybuild generate`, compile each project's full
transitive source list separately. This builds the same projects with cl and link
directly, knowing that projects share sources, so that each object is compiled
once and linked into every project that needs it. Debug|x64 only.

Objects are identified by source *and* compile command set (see
TypeTemplates.compile_command_id()), never by source alone, so that if project types'
compile commands differ (which is an error unless allowed), a source reached from
both is compiled once per type, correctly.

Output goes under ./build_tybuild/Debug/, separate from ./build_template/Debug/
where Visual Studio's builds put the executables, so that the two builds don't
overwrite each other's executables, PDBs and incremental link state:

    build_tybuild/Debug/
        .tybuild                        cache: command templates, compiler environment
        extract/<type>/                 where the command templates were extracted
        obj/<command id>/<src path>.obj objects, by path under ./src
        bin/<Project>.exe, .pdb, .ilk  executables
        link/<Project>.rsp, .lib        linker response files and import libraries

The templates' post-build step, `vcpkg z-applocal`, is not run: it copies DLLs next to
the executable, and with the static vcpkg triplet there are none to copy. A project
set that uses DLLs would need it added here.

Not incremental yet: every run compiles every object and links every project.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Any, List, Optional, Tuple

from tybuild.build import EXCLUDED_PROJECT_TYPES, TOOLCHAIN_REFERENCE
from tybuild.command_templates import (
    CONFIGURATION,
    CommandProblem,
    TypeTemplates,
    compare_compile_commands,
    get_type_templates,
    quote_argument,
)
from tybuild.dependencies import find_include_errors, get_cpp_dependencies, report_include_errors
from tybuild.projects import Project, discover_projects
from tybuild.vs_install import (
    capture_build_environment,
    find_compiler,
    find_visual_studio,
    msbuild_path,
)
from tybuild.vs_templates import read_toolchain_settings

BUILD_DIR_NAME = "build_tybuild"
CACHE_FILENAME = ".tybuild"
CACHE_VERSION = 1

ObjectKey = Tuple[str, str]     # (source path relative to ./src, compile command id)


@dataclass
class _ObjectJob:
    source: Path
    obj: Path
    switches: List[List[str]]


def _load_cache(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(cache, dict) or cache.get("version") != CACHE_VERSION:
        return {}
    return cache


def _save_cache(path: Path, cache: Dict[str, Any]) -> None:
    # Written to a temporary file and moved into place, so that a killed build
    # never leaves a truncated cache
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, sort_keys=True)
    tmp.replace(path)


def _report_command_problems(problems: List[CommandProblem]) -> None:
    for problem in problems:
        print(problem, file=sys.stderr)
    # Worded so that MSBuild-style parsers don't take the summary for another error
    print(f"tybuild found {len(problems)} compile command difference(s) between project types, "
          f"listed above.", file=sys.stderr)


def _print_templates(templates: Dict[str, TypeTemplates]) -> None:
    for project_type in sorted(templates):
        t = templates[project_type]
        compile_line = " ".join(" ".join(s) for s in t.effective_compile_switches())
        print(f"Compile command, {project_type} (recorded source was {t.dummy_source}):")
        print(f"  cl {compile_line} /Fo<OBJECT> <SOURCE>")
        print(f"Link command, {project_type}:")
        print(f"  link {' '.join(t.link_arguments)} /OUT:<EXE> /ILK:<ILK> /PDB:<PDB> /IMPLIB:<LIB> <OBJECTS>")
        print()
    print("(Deliberate deviation from the recorded compile commands: /Zi is replaced by /Z7, and")
    print(" /Fd dropped, so that each object carries its own debug information and parallel cl")
    print(" processes don't share a PDB.)")
    print()


def _get_build_environment(
    cache: Dict[str, Any],
    toolchain: Dict[str, str],
    get_installation: Callable[[], Path],
    save: Callable[[], None],
) -> Tuple[Dict[str, str], Path, Path]:
    """
    The compiler environment, and cl and link within it, cached by toolchain.

    A cached environment is only used if the compiler it names still exists and still
    matches; after a Visual Studio update it won't, and it is captured again.
    """
    cached = cache.get("environment")
    if isinstance(cached, dict) and cached.get("toolchain") == toolchain:
        environment = cached.get("variables", {})
        try:
            cl, link = find_compiler(environment, toolchain)
            if cl.is_file():
                return environment, cl, link
        except RuntimeError:
            pass

    print("Capturing the compiler environment from vcvarsall.bat...")
    environment = capture_build_environment(get_installation(), toolchain)
    cl, link = find_compiler(environment, toolchain)
    cache["environment"] = {"toolchain": toolchain, "variables": environment}
    save()
    return environment, cl, link


def _run(command: str, cwd: Path, environment: Dict[str, str]) -> Tuple[int, str]:
    """Run a command line, returning its exit code and its combined output."""
    try:
        result = subprocess.run(
            command, cwd=cwd, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
        )
    except OSError as e:
        return 1, f"tybuild: could not run {command}: {e}"
    return result.returncode, result.stdout


def _compile(job: _ObjectJob, cl: Path, cwd: Path, environment: Dict[str, str]) -> Tuple[bool, str]:
    job.obj.parent.mkdir(parents=True, exist_ok=True)
    parts = [quote_argument(str(cl))]
    parts.extend(" ".join(switch) for switch in job.switches)
    parts.append("/Fo" + quote_argument(str(job.obj)))
    parts.append(quote_argument(str(job.source)))
    code, output = _run(" ".join(parts), cwd, environment)

    # cl names each source it compiles, even with /nologo
    lines = output.splitlines()
    if lines and lines[0].strip().lower() == job.source.name.lower():
        lines = lines[1:]
    output = "\n".join(lines).strip("\n")

    if code != 0:
        # So that nothing can link an object left over from an earlier run
        try:
            job.obj.unlink()
        except OSError:
            pass
    return code == 0, output


def _link(
    project: Project,
    template: TypeTemplates,
    objects: List[Path],
    link: Path,
    cwd: Path,
    environment: Dict[str, str],
    bin_dir: Path,
    link_dir: Path,
) -> Tuple[bool, str]:
    exe = bin_dir / f"{project.name}.exe"
    arguments = list(template.link_arguments)
    arguments += [
        "/OUT:" + quote_argument(str(exe)),
        "/ILK:" + quote_argument(str(bin_dir / f"{project.name}.ilk")),
        "/PDB:" + quote_argument(str(bin_dir / f"{project.name}.pdb")),
        "/IMPLIB:" + quote_argument(str(link_dir / f"{project.name}.lib")),
    ]
    arguments += [quote_argument(str(o)) for o in objects]

    # A response file, since the object list can exceed the command line length limit.
    # link reads UTF-16 response files, marked by their byte order mark.
    response_file = link_dir / f"{project.name}.rsp"
    response_file.write_text("\n".join(arguments) + "\n", encoding="utf-16")

    command = f"{quote_argument(str(link))} @{quote_argument(str(response_file))}"
    code, output = _run(command, cwd, environment)
    return code == 0, output.strip("\n")


def run_build(
    repo_root: Path,
    dry_run: bool,
    allow_differing_compile_commands: bool,
    refresh_commands: bool,
    jobs: Optional[int] = None,
) -> int:
    """
    Run `tybuild build`.

    Args:
        repo_root: Repository root (contains ./src and ./build_template)
        dry_run: Stop after showing the commands and counting the objects
        allow_differing_compile_commands: Build even if project types' compile commands differ
        refresh_commands: Re-extract the command templates regardless of the cache
        jobs: Number of parallel cl/link processes (default: processor count)

    Returns:
        Process exit code: 0 on success, 1 on errors that have been reported

    Raises:
        RuntimeError, FileNotFoundError: With a user-facing message
    """
    started = time.monotonic()
    repo_root = repo_root.resolve()
    src_root = repo_root / "src"
    template_dir = repo_root / "build_template"
    if not src_root.is_dir():
        raise RuntimeError(f"Source directory not found: {src_root}")
    if not template_dir.is_dir():
        raise RuntimeError(f"Template directory not found: {template_dir}")

    toolchain = read_toolchain_settings(template_dir / TOOLCHAIN_REFERENCE)
    print(
        f"Toolchain: {toolchain['platform_toolset']}, "
        f"Windows SDK {toolchain['windows_sdk_version']}, "
        f"MSBuild tools {toolchain['tools_version']} "
        f"(from {TOOLCHAIN_REFERENCE})"
    )

    projects = [p for p in discover_projects(repo_root) if p.type not in EXCLUDED_PROJECT_TYPES]
    if not projects:
        raise RuntimeError("No projects found in ./src/project/")
    project_types = sorted({p.type for p in projects})

    config_dir = repo_root / BUILD_DIR_NAME / CONFIGURATION
    config_dir.mkdir(parents=True, exist_ok=True)
    cache_path = config_dir / CACHE_FILENAME
    cache = _load_cache(cache_path)
    cache["version"] = CACHE_VERSION

    installation: List[Path] = []

    def get_installation() -> Path:
        if not installation:
            installation.append(find_visual_studio(toolchain["tools_version"]))
        return installation[0]

    def find_msbuild() -> Path:
        msbuild = msbuild_path(get_installation())
        print(f"  MSBuild: {msbuild}")
        return msbuild

    def save_command_templates(part: Dict[str, object]) -> None:
        cache["command_templates"] = part
        _save_cache(cache_path, cache)

    print()
    print("Command templates:")
    templates = get_type_templates(
        template_dir=template_dir,
        project_types=project_types,
        toolchain=toolchain,
        extract_dir=config_dir / "extract",
        cached=cache.get("command_templates", {}),
        find_msbuild=find_msbuild,
        refresh=refresh_commands,
        save=save_command_templates,
    )
    print()
    if dry_run:
        _print_templates(templates)

    severity = "warning" if allow_differing_compile_commands else "error"
    command_problems = compare_compile_commands(templates, template_dir, severity)
    if command_problems:
        _report_command_problems(command_problems)
        print()
        if not dry_run and not allow_differing_compile_commands:
            print("Not building, because of the compile command differences above "
                  "(--allow-differing-compile-commands builds anyway).")
            return 1
    else:
        print(f"Compile commands are identical across all {len(project_types)} project type(s).")

    environment, cl, link = _get_build_environment(
        cache, toolchain, get_installation, lambda: _save_cache(cache_path, cache)
    )
    print(f"Compiler: {cl}")

    # Plan the objects, each compiled once, and each project's list of them
    objects_dir = config_dir / "obj"
    command_ids = {t: templates[t].compile_command_id(toolchain) for t in project_types}
    effective_switches = {t: templates[t].effective_compile_switches() for t in project_types}
    object_jobs: Dict[ObjectKey, _ObjectJob] = {}
    project_objects: Dict[str, List[ObjectKey]] = {}
    per_project_compiles = 0
    for project in projects:
        main_rel = project.cpp_file.relative_to(src_root).as_posix()
        project_sources = [main_rel] + get_cpp_dependencies(repo_root, project.cpp_file)
        per_project_compiles += len(project_sources)
        command_id = command_ids[project.type]
        keys = []
        for source_rel in project_sources:
            key = (source_rel, command_id)
            if key not in object_jobs:
                object_jobs[key] = _ObjectJob(
                    source=src_root / Path(source_rel),
                    obj=objects_dir / command_id / (source_rel + ".obj"),
                    switches=effective_switches[project.type],
                )
            keys.append(key)
        project_objects[project.name] = keys

    print(f"Projects: {len(projects)}, of {len(project_types)} type(s)")
    print(f"Compiles if each project compiled its own sources: {per_project_compiles}")
    print(f"Distinct sources: {len({key[0] for key in object_jobs})}")
    print(f"Distinct objects: {len(object_jobs)}, "
          f"in {len(set(command_ids.values()))} compile command set(s)")

    include_problems = find_include_errors(repo_root)
    failed = bool(include_problems) or (bool(command_problems) and not allow_differing_compile_commands)

    if dry_run:
        if include_problems:
            print()
            report_include_errors(include_problems)
        return 1 if failed else 0

    workers = jobs or os.cpu_count() or 4
    bin_dir = config_dir / "bin"
    link_dir = config_dir / "link"
    bin_dir.mkdir(parents=True, exist_ok=True)
    link_dir.mkdir(parents=True, exist_ok=True)

    # Compile. Every object is attempted, so that one run reports every error; each
    # process's output is printed whole, so parallel diagnostics don't interleave.
    print()
    print(f"Compiling {len(object_jobs)} object(s), {workers} at a time...")
    compile_started = time.monotonic()
    failed_objects = set()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_compile, job, cl, template_dir, environment): key
            for key, job in object_jobs.items()
        }
        for future in as_completed(futures):
            ok, output = future.result()
            if output:
                print(output, flush=True)
            if not ok:
                failed_objects.add(futures[future])
    compile_seconds = time.monotonic() - compile_started

    # Link each project whose objects all compiled
    print(f"Linking {len(projects)} project(s)...")
    link_started = time.monotonic()
    to_link = []
    not_linked = []
    for project in projects:
        failures = [key for key in project_objects[project.name] if key in failed_objects]
        if failures:
            not_linked.append((project, len(failures)))
        else:
            to_link.append(project)
    failed_links = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _link, project, templates[project.type],
                [object_jobs[key].obj for key in project_objects[project.name]],
                link, template_dir, environment, bin_dir, link_dir,
            ): project
            for project in to_link
        }
        for future in as_completed(futures):
            ok, output = future.result()
            if output:
                print(output, flush=True)
            if not ok:
                failed_links.append(futures[future])
    link_seconds = time.monotonic() - link_started

    print()
    for project, count in not_linked:
        print(f"Not linked: {project.name} ({count} of its objects did not compile)")
    print(f"Compiled {len(object_jobs) - len(failed_objects)} of {len(object_jobs)} object(s) "
          f"in {compile_seconds:.1f}s")
    print(f"Linked {len(to_link) - len(failed_links)} of {len(projects)} project(s) "
          f"in {link_seconds:.1f}s")
    print(f"Executables: {bin_dir}")
    print(f"Total time: {time.monotonic() - started:.1f}s")

    if include_problems:
        print()
        report_include_errors(include_problems)
    if failed_objects or failed_links or not_linked:
        failed = True
    return 1 if failed else 0
