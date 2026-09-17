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
        .tybuild                            cache: command templates, compiler environment
        extract/<type>/                     where the command templates were extracted
        obj/<command id>/<src path>.obj     objects, by path under ./src
        obj/.../<src path>.obj.record.json  what the object was compiled from (see below)
        bin/<Project>.exe, .pdb, .ilk       executables
        link/<Project>.rsp, .lib            linker response files and import libraries
        link/<Project>.record.json          what the executable was linked from

Incremental builds: an object is out of date if its command line, its source, or any
file it included changed, or the object itself changed; the included files come from
`cl /sourceDependencies`, so headers outside ./src (vcpkg, the SDK) count. A link is out
of date if its command line, any object, any library named by absolute path, or the
executable changed. Identity is size plus mtime.

Records are deleted before the tool runs and written only after it succeeds, so a
build that is killed part way leaves nothing that a later run treats as up to date.

The templates' post-build step, `vcpkg z-applocal`, is not run: it copies DLLs next to
the executable, and with the static vcpkg triplet there are none to copy. A project
set that uses DLLs would need it added here.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Any, List, Optional, Set, Tuple

from tybuild.build import EXCLUDED_PROJECT_TYPES, TOOLCHAIN_REFERENCE
from tybuild.command_templates import (
    CONFIGURATION,
    CommandProblem,
    TypeTemplates,
    compare_compile_commands,
    get_type_templates,
    quote_argument,
    unquote_token,
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
RECORD_VERSION = 1

ObjectKey = Tuple[str, str]     # (source path relative to ./src, compile command id)
Identity = Optional[List[int]]  # [size, mtime_ns], or None for a missing file


@dataclass
class _ObjectJob:
    source: Path
    obj: Path
    record: Path
    source_dependencies: Path   # written by cl /sourceDependencies
    command: str


@dataclass
class _LinkJob:
    project: Project
    exe: Path
    record: Path
    response_file: Path
    response_text: str
    command: str
    command_identity: str       # the command plus the response file's contents
    inputs: List[Path]          # objects, and libraries named by absolute path


# ---------------------- Cache and records ----------------------

def _load_cache(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(cache, dict) or cache.get("version") != CACHE_VERSION:
        return {}
    return cache


def _write_json_atomically(path: Path, data: Dict[str, Any]) -> None:
    # Written to a temporary file and moved into place, so that a killed build
    # never leaves a truncated file
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    tmp.replace(path)


def _read_record(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("version") != RECORD_VERSION:
        return None
    return record


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _identity(path: str) -> Identity:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_up_to_date(record_path: Path, command: str, output: Path, stat: Callable[[str], Identity]) -> bool:
    """Whether a record matches the command, the output, and every input it lists."""
    record = _read_record(record_path)
    if record is None or record.get("command") != _hash(command):
        return False
    if record.get("output") is None or stat(str(output)) != record["output"]:
        return False
    inputs = record.get("inputs")
    if not isinstance(inputs, dict):
        return False
    return all(stat(path) == identity for path, identity in inputs.items())


# ---------------------- Reporting ----------------------

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


# ---------------------- Environment ----------------------

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


# ---------------------- Running tools ----------------------

class _ProcessRunner:
    """
    Runs tool command lines from worker threads, and can stop them all at once.

    Once stopped, running processes are killed and no new ones start: each reports
    failure, so that no record is written for it and the next build does it again.
    """

    def __init__(self, cwd: Path, environment: Dict[str, str]):
        self.cwd = cwd
        self.environment = environment
        self._lock = threading.Lock()
        self._processes: Set[subprocess.Popen] = set()
        self._stopped = False

    def run(self, command: str) -> Tuple[int, str]:
        """Run a command line, returning its exit code and its combined output."""
        with self._lock:
            if self._stopped:
                return 1, "tybuild: not run, because the build was interrupted"
            try:
                process = subprocess.Popen(
                    command, cwd=self.cwd, env=self.environment,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
                )
            except OSError as e:
                return 1, f"tybuild: could not run {command}: {e}"
            self._processes.add(process)
        try:
            output, _ = process.communicate()
        finally:
            with self._lock:
                self._processes.discard(process)
        return process.returncode, output

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            processes = list(self._processes)
        for process in processes:
            try:
                process.kill()
            except OSError:
                pass


def _silent_failure(what: str, code: int) -> str:
    """Output for a tool that failed without saying anything, so the failure isn't invisible."""
    return f"tybuild: {what} failed with exit code {code}, and printed nothing"


def _compile(job: _ObjectJob, runner: _ProcessRunner) -> Tuple[bool, str]:
    job.obj.parent.mkdir(parents=True, exist_ok=True)
    _remove(job.record)
    _remove(job.source_dependencies)

    started_ns = time.time_ns()
    code, output = runner.run(job.command)
    finished_ns = time.time_ns()

    # cl names each source it compiles, even with /nologo
    lines = output.splitlines()
    if lines and lines[0].strip().lower() == job.source.name.lower():
        lines = lines[1:]
    output = "\n".join(lines).strip("\n")

    if code != 0:
        # So that nothing can link an object left over from an earlier run
        _remove(job.obj)
        return False, output or _silent_failure(f"compile of {job.source}", code)

    note = _record_object(job, started_ns, finished_ns)
    if note:
        output = f"{output}\n{note}" if output else note
    return True, output


def _record_object(job: _ObjectJob, started_ns: int, finished_ns: int) -> Optional[str]:
    """
    Write the record that makes an object up to date, from cl's /sourceDependencies
    output. Returns a note to print if it couldn't be written.

    No record is written if an input was modified while cl ran, since the object may
    have been compiled from its earlier contents; the next build compiles it again.
    """
    try:
        data = json.loads(job.source_dependencies.read_text(encoding="utf-8-sig"))["Data"]
        paths = [data["Source"]] + list(data.get("Includes", []))
    except (OSError, ValueError, KeyError, TypeError) as e:
        return (f"tybuild: could not read {job.source_dependencies} ({e}), "
                f"so {job.source.name} will be compiled again next time")

    inputs: Dict[str, Identity] = {}
    for path in paths:
        identity = _identity(path)
        if identity is None or started_ns <= identity[1] <= finished_ns:
            return None
        inputs[path] = identity

    output = _identity(str(job.obj))
    if output is None:
        return f"tybuild: cl reported success but did not write {job.obj}"
    _write_json_atomically(job.record, {
        "version": RECORD_VERSION,
        "command": _hash(job.command),
        "output": output,
        "inputs": inputs,
    })
    _remove(job.source_dependencies)
    return None


def _link(job: _LinkJob, runner: _ProcessRunner) -> Tuple[bool, str]:
    _remove(job.record)
    # link reads UTF-16 response files, marked by their byte order mark
    job.response_file.write_text(job.response_text, encoding="utf-16")

    code, output = runner.run(job.command)
    output = output.strip("\n")
    if code != 0:
        return False, output or _silent_failure(f"link of {job.project.name}", code)

    exe = _identity(str(job.exe))
    if exe is not None:
        _write_json_atomically(job.record, {
            "version": RECORD_VERSION,
            "command": _hash(job.command_identity),
            "output": exe,
            "inputs": {str(path): _identity(str(path)) for path in job.inputs},
        })
    return True, output


def _make_link_job(
    project: Project,
    template: TypeTemplates,
    objects: List[Path],
    link: Path,
    bin_dir: Path,
    link_dir: Path,
) -> _LinkJob:
    exe = bin_dir / f"{project.name}.exe"
    arguments = list(template.link_arguments)
    arguments += [
        "/OUT:" + quote_argument(str(exe)),
        "/ILK:" + quote_argument(str(bin_dir / f"{project.name}.ilk")),
        "/PDB:" + quote_argument(str(bin_dir / f"{project.name}.pdb")),
        "/IMPLIB:" + quote_argument(str(link_dir / f"{project.name}.lib")),
    ]
    arguments += [quote_argument(str(o)) for o in objects]
    # A response file, since the object list can exceed the command line length limit
    response_text = "\n".join(arguments) + "\n"
    response_file = link_dir / f"{project.name}.rsp"

    libraries = []
    for token in template.link_arguments:
        if token[:1] not in ("/", "-"):
            value = unquote_token(token)
            if os.path.isabs(value):
                libraries.append(Path(value))

    command = f"{quote_argument(str(link))} @{quote_argument(str(response_file))}"
    return _LinkJob(
        project=project,
        exe=exe,
        record=link_dir / f"{project.name}.record.json",
        response_file=response_file,
        response_text=response_text,
        command=command,
        command_identity=f"{command}\n{response_text}",
        inputs=list(objects) + libraries,
    )


def _run_jobs(
    jobs: List[Any],
    run: Callable[[Any], Tuple[bool, str]],
    workers: int,
    runner: _ProcessRunner,
) -> List[Any]:
    """
    Run jobs in parallel, printing each one's output whole as it finishes, so that
    parallel diagnostics don't interleave. Every job is run. Returns those that failed.

    On Ctrl+C, queued jobs are cancelled and running processes killed before the
    KeyboardInterrupt is passed on. The wait for jobs to finish uses a timeout, since
    on Windows an untimed wait can't be interrupted by Ctrl+C.
    """
    failed = []
    pool = ThreadPoolExecutor(max_workers=workers)
    pending = {pool.submit(run, job): job for job in jobs}
    try:
        while pending:
            done, _ = wait(list(pending), timeout=0.25, return_when=FIRST_COMPLETED)
            for future in done:
                job = pending.pop(future)
                ok, output = future.result()
                if output:
                    print(output, flush=True)
                if not ok:
                    failed.append(job)
    except KeyboardInterrupt:
        print("\nInterrupted: stopping the processes still running...", flush=True)
        for future in pending:
            future.cancel()
        runner.stop()
        raise
    finally:
        pool.shutdown(wait=True)
    return failed


# ---------------------- Build ----------------------

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
        _write_json_atomically(cache_path, cache)

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
        cache, toolchain, get_installation, lambda: _write_json_atomically(cache_path, cache)
    )
    print(f"Compiler: {cl}")

    # Plan the objects, each compiled once, and each project's list of them
    objects_dir = config_dir / "obj"
    command_ids = {t: templates[t].compile_command_id(toolchain) for t in project_types}
    compile_switches = {
        t: " ".join(" ".join(s) for s in templates[t].effective_compile_switches())
        for t in project_types
    }
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
                source = src_root / Path(source_rel)
                obj = objects_dir / command_id / (source_rel + ".obj")
                source_dependencies = obj.with_name(obj.name + ".srcdeps.json")
                object_jobs[key] = _ObjectJob(
                    source=source,
                    obj=obj,
                    record=obj.with_name(obj.name + ".record.json"),
                    source_dependencies=source_dependencies,
                    command=" ".join([
                        quote_argument(str(cl)),
                        compile_switches[project.type],
                        "/sourceDependencies " + quote_argument(str(source_dependencies)),
                        "/Fo" + quote_argument(str(obj)),
                        quote_argument(str(source)),
                    ]),
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
    runner = _ProcessRunner(template_dir, environment)
    bin_dir = config_dir / "bin"
    link_dir = config_dir / "link"
    bin_dir.mkdir(parents=True, exist_ok=True)
    link_dir.mkdir(parents=True, exist_ok=True)

    # Identities are looked up once per run for the up-to-date checks, since the same
    # headers are included from most objects
    identities: Dict[str, Identity] = {}

    def stat(path: str) -> Identity:
        if path not in identities:
            identities[path] = _identity(path)
        return identities[path]

    # Compile what is out of date. Every out of date object is attempted, so that one
    # run reports every error.
    print()
    to_compile = [
        job for job in object_jobs.values()
        if not _is_up_to_date(job.record, job.command, job.obj, stat)
    ]
    compile_started = time.monotonic()
    failed_compiles = []
    if to_compile:
        print(f"Compiling {len(to_compile)} of {len(object_jobs)} object(s), {workers} at a time...")
        failed_compiles = _run_jobs(to_compile, lambda job: _compile(job, runner), workers, runner)
    compile_seconds = time.monotonic() - compile_started
    failed_objects = {job.obj for job in failed_compiles}

    # Link each project whose objects all compiled, if out of date. Compiled objects
    # have changed identity, so the cached identities no longer apply.
    identities.clear()
    link_jobs = []
    not_linked = []
    for project in projects:
        objects = [object_jobs[key].obj for key in project_objects[project.name]]
        job = _make_link_job(project, templates[project.type], objects, link, bin_dir, link_dir)
        failures = sum(1 for o in objects if o in failed_objects)
        if failures:
            not_linked.append((job, failures))
        elif not _is_up_to_date(job.record, job.command_identity, job.exe, stat):
            link_jobs.append(job)

    # An executable that wasn't relinked because its objects didn't compile is out of
    # date, so it is removed rather than left to be run by mistake
    for job, _ in not_linked:
        _remove(job.record)
        _remove(job.exe)

    link_started = time.monotonic()
    failed_links = []
    if link_jobs:
        print(f"Linking {len(link_jobs)} of {len(projects)} project(s)...")
        failed_links = _run_jobs(link_jobs, lambda job: _link(job, runner), workers, runner)
        for job in failed_links:
            _remove(job.exe)
    link_seconds = time.monotonic() - link_started

    print()
    for job, count in not_linked:
        print(f"Not linked: {job.project.name} ({count} of its objects did not compile)")
    for job in failed_links:
        print(f"Link failed: {job.project.name}")
    if failed_compiles or failed_links:
        # Worded so that MSBuild-style parsers don't take the summary for another error
        print(f"BUILD FAILED: {len(failed_compiles)} object(s) did not compile, "
              f"{len(failed_links)} link(s) did not succeed")
    print(f"Compiled {len(to_compile) - len(failed_compiles)} of {len(to_compile)} out of date "
          f"object(s) in {compile_seconds:.1f}s ({len(object_jobs) - len(to_compile)} up to date)")
    print(f"Linked {len(link_jobs) - len(failed_links)} of {len(link_jobs)} out of date "
          f"project(s) in {link_seconds:.1f}s "
          f"({len(projects) - len(link_jobs) - len(not_linked)} up to date)")
    print(f"Executables: {bin_dir}")
    print(f"Total time: {time.monotonic() - started:.1f}s")

    if include_problems:
        print()
        report_include_errors(include_problems)
    if failed_compiles or failed_links or not_linked:
        failed = True
    return 1 if failed else 0
