# CLAUDE.md - tybuild Project Guide

## Project Overview

`tybuild` is a Python-based build system that generates Visual Studio project files (.vcxproj) and solutions (.sln) from a source directory structure. It automatically discovers C++ projects, analyzes dependencies, and creates Visual Studio build configurations. It can also build the projects directly (`tybuild build`), compiling sources shared between projects only once.

### Key Features
- **Project Discovery**: Automatically finds projects in `./src/project/<type>/<ProjectName>.cpp`
- **Dependency Analysis**: Scans C++ files for `#include` statements and builds dependency graphs
- **Template-Based Generation**: Uses template .vcxproj files to generate project files
- **Incremental Builds**: Only regenerates files when necessary (caches state in `.tybuild`)
- **Deterministic GUIDs**: Projects get consistent GUIDs based on type and name

## Directory Structure

```
./src/tybuild/          # Python package source
├── __init__.py
├── __main__.py         # Entry point
├── cli.py              # Command-line interface
├── projects.py         # Project discovery logic
├── dependencies.py     # C++ dependency scanning
├── vs_templates.py     # Visual Studio file generation
├── cmake_export.py     # CMake project export
├── source_moves.py     # Updating includes after source files move
├── build.py            # Main build orchestration (generate)
├── native_build.py     # `tybuild build`: compiling and linking directly
├── command_templates.py # `tybuild build`: compile/link commands extracted from MSBuild
├── vs_install.py       # `tybuild build`: finding Visual Studio, compiler environment
└── templates/          # Templates shipped inside the package
    └── ONE_CHECK.vcxproj   # Regeneration step; see "Meta-projects" below

./src/project/          # User's C++ projects (not in repo)
└── <type>/             # Project type directories (console, sdl3, etc.)
    └── <Name>.cpp      # Each .cpp is a project entry point

./build_template/       # Template VS project files (not in repo, cmake output)
├── ZZZZZZZZ_<type>.vcxproj        # Templates for each project type
├── ZZZZZZZZ_<type>.vcxproj.filters
├── ALL_BUILD.vcxproj              # CMake-style meta-project
├── ALL_BUILD.vcxproj.filters
├── ZERO_CHECK.vcxproj             # CMake-style validation project
└── ZERO_CHECK.vcxproj.filters
                        # NOTE: ONE_CHECK is NOT here - it ships inside the
                        # package, at ./src/tybuild/templates/

./build/                # Generated output (not in repo)
├── .tybuild            # Build cache (JSON) - deleted when build dir is cleaned
├── Solution.sln        # Generated solution
├── <ProjectName>.vcxproj
└── <ProjectName>.vcxproj.filters

./build_tybuild/Debug/  # `tybuild build` output (not in repo); see module 7 below

./includes.cache        # Dependency cache (JSON, in repository root)
```

## Core Modules

### 1. `projects.py`
**Purpose**: Discovers projects from directory structure

**Key Function**: `discover_projects(base_path) -> List[Project]`
- Scans `./src/project/<type>/*.cpp`
- Returns `Project` dataclass with: `name`, `type`, `cpp_file`
- Example: `./src/project/console/Server.cpp` → `Project(name="Server", type="console")`

### 2. `dependencies.py`
**Purpose**: Analyzes C++ include dependencies

**Key Functions**:
- `get_cpp_dependencies(repo_root, start_file, refresh=False, include_headers=False)` → List of relative paths
  - `repo_root`: Repository root directory (scans under `./src`, cache stored at `./includes.cache`)
  - Scans for `#include "..."` statements
  - Builds transitive dependency graph
  - Includes implicit header→source relationships (same directory/stem)
  - Can filter to .cpp only or include .h files
  - Returns paths relative to `./src`

**Include Resolution Strategy** (`resolve_include()`): every `#include "..."` must be written
relative to the source root.
1. If the include names a file relative to the including file's own directory (and the includer
   is not directly in `./src`), that is error **TYB002**: the compiler would find that file first.
   `tybuild fix-includes` rewrites these.
2. Otherwise resolve it relative to the source root.
3. If that names no file, that is error **TYB001**.

**Include Errors**:
- A file with include errors is still cached, with the includes that did resolve, so one bad
  include doesn't drop the file's other dependencies. The failures go in the entry's `errors`
  list (`line`, `code`, `message`).
- A file whose entry has errors is rescanned on every `scan()`, so fixing it by adding or removing
  some *other* file clears the error.
- Nothing is printed during scanning. Callers collect errors afterwards with `include_errors()` or
  `find_include_errors()` and print them with `report_include_errors()`, so each distinct error is
  listed once per run however many projects reach it.
- Format is MSVC's canonical error format, with an absolute path so MSBuild resolves it correctly
  (ONE_CHECK runs `tybuild generate` as a custom build step, so these show up in Visual Studio's
  Error List): `D:\repo\src\foo\Bar.cpp(12): error TYB001: cannot find include "x.h" ...`
- The summary line after the list avoids the word "error", so MSBuild doesn't count it as another.

**Caching**: Uses `./includes.cache` (in repository root) for performance (tracks file size + mtime_ns).
Entries written before `errors` existed lack it; those versions never cached a file with an include
error, so a missing `errors` means none, and the format needed no version bump.

### 3. `vs_templates.py`
**Purpose**: Generates Visual Studio project and solution files

**Key Functions**:
- `generate_project_guid(project_type, project_name)` → Deterministic GUID (no braces)
- `generate_project_from_template(template_path, template_name, project_name, project_guid, source_root, sources_rel_to_root, output_path)`
  - Reads template .vcxproj and .vcxproj.filters
  - Replaces template name with project name (string replacement)
  - Replaces GUID (XML manipulation)
  - Replaces source file list (XML manipulation)

- `generate_solution(output_sln_path, solution_guid, all_build_guid, zero_check_guid, one_check_guid, projects_to_add)`
  - Generates .sln with ALL_BUILD, ZERO_CHECK, ONE_CHECK, and user projects
  - Dependency chain: ZERO_CHECK (first) → ONE_CHECK → user projects
  - ALL_BUILD depends on all user projects, ONE_CHECK and ZERO_CHECK
  - Four configurations: Debug, Release, MinSizeRel, RelWithDebInfo (all x64)

- `read_toolchain_settings(reference_vcxproj)` → `{tools_version, platform_toolset, windows_sdk_version}`
  - Reads the toolchain out of a cmake-generated .vcxproj (in practice
    `./build_template/ZERO_CHECK.vcxproj`), so the files tybuild writes itself cannot
    disagree with the ones cmake wrote. See "Meta-Projects" below.
  - Raises `RuntimeError` with a user-facing message if the file is missing, unreadable,
    incomplete, or names more than one platform toolset

**Important**: GUIDs are passed WITHOUT braces at API level, added internally for XML format

### 4. `build.py`
**Purpose**: Main build orchestration with incremental regeneration

**Key Function**: `generate_build_files(base_path, force=False)`

**Workflow**:
1. Copy ALL_BUILD and ZERO_CHECK from `./build_template/`, and ONE_CHECK from the
   package's own `templates/` directory, to the build dir. ALL_BUILD's `ProjectReference`s
   are removed on the way (`USER_TEMPLATE_TRANSFORMS`): they point at the projects in
   `./build_template/`, and ALL_BUILD gets its dependencies from the solution instead
2. Discover all projects
3. Load `.tybuild` cache
4. For each project:
   - Get cpp dependencies
   - Check if regeneration needed:
     - Template file changed (size/mtime)
     - Source file set changed (not content!)
   - Generate project files if needed
5. Regenerate solution if project set changed
6. Save updated cache

`cmd_generate` then reports any include errors and exits 1 if there were any. Everything is
generated first, so a Visual Studio user still gets a usable solution.

**Meta-project GUIDs**: not hardcoded in `build.py`. After the three meta-projects are
copied into the build dir, their GUIDs are read back out of the copied files with
`get_project_guid()`, so each one's GUID is whatever its template says. The values in
practice are:
- ALL_BUILD: `5C330799-6FA6-33C3-B12C-755A9CA12672` (from `./build_template/`)
- ZERO_CHECK: `46BE4EB3-B0FD-3982-8000-AE0905052172` (from `./build_template/`)
- ONE_CHECK: `1E71EEE3-975D-4B10-9620-A4C9F0B25EC9` (from the package template)

### 5. `cmake_export.py`
**Purpose**: Export project information for CMake integration

**Key Function**: `generate_cmake_file(repo_root, output_path)`
- Discovers all projects using `discover_projects()`
- For each project, gets dependencies using `get_cpp_dependencies()`
- Generates a CMake file with format:
  - `GENERATED_PROJECTS` - semicolon-separated list of project names
  - `<ProjectName>_TYPE` - project type for each project
  - `<ProjectName>_SOURCES` - list of source files relative to `./src`

### 6. `cli.py`
**Purpose**: Command-line interface

**Commands** (all run from repository root):
- `tybuild generate [--force]` - Generate Visual Studio build files (exits 1 on include errors)
- `tybuild check-includes [--refresh]` - Report include errors and exit 1 if any, without touching the build directory
- `tybuild list` - List discovered projects
- `tybuild deps START [--refresh]` - Show dependencies for a file
- `tybuild generate-cmake` - Generate CMake project list
- `tybuild build [--dry-run] [--allow-differing-compile-commands] [--refresh-commands] [-j N]` -
  Build Debug|x64 directly with cl/link, each shared object compiled once; executables in
  `./build_tybuild/Debug/bin/`, incremental. `--dry-run` shows the extracted commands and object
  counts without compiling. See "`tybuild build`" below

**Important**: All commands assume they are run from the repository root directory, which must contain a `./src` subdirectory. The `--root` parameter has been removed from all commands.

### 7. `tybuild build`: `native_build.py`, `command_templates.py`, `vs_install.py`
**Purpose**: Build every project (Debug|x64 only) directly with `cl` and `link`, compiling each
shared source once and linking the object into every project that needs it. An alternative to
building `./build/Solution.sln` with MSBuild, for scripts (first user: lockstep's `scripts/check.py`).
Visual Studio through `tybuild generate` stays the day to day workflow and is unaffected.

Background and the original plan: `TASK_BUILD_COMMAND.md`. On lockstep (13 projects), 659
per-project compiles become 233 objects, and a clean build took 34s (12 processes), where lockstep's
`BUILD_ISSUES.md` reports about 3 minutes through MSBuild (not measured side by side).

**Output** (fixed, documented path, so other scripts can name it):
```
./build_tybuild/Debug/
├── .tybuild                            # cache: extracted command templates, compiler environment
├── extract/<type>/                     # where each type's commands were extracted
├── obj/<command id>/<src path>.obj     # objects, by path under ./src (not base name)
├── obj/.../<src path>.obj.record.json  # what the object was compiled from
├── bin/<Project>.exe, .pdb, .ilk       # executables
└── link/<Project>.rsp, .lib, .record.json
```
Separate from `./build_template/Debug/`, where Visual Studio puts executables, so the two builds
don't overwrite each other's executables, PDBs and incremental link state.

**Flow** (`run_build()` in `native_build.py`):
1. `read_toolchain_settings()`, discover projects (excluding `wasm`)
2. Get command templates per project type (`get_type_templates()`, cached)
3. Compare compile commands across types (`compare_compile_commands()`)
4. Get the compiler environment (cached)
5. Plan objects: for each project, main cpp + `get_cpp_dependencies()`, keyed by
   `(source, compile_command_id)`
6. `--dry-run` stops here, after printing the commands and counts
7. Compile out of date objects in parallel, then link out of date projects in parallel

**Command templates** (`command_templates.py`). The flags are taken from MSBuild itself rather
than worked out from the vcxproj, so a `CMakeLists.txt` change reaches `tybuild build` with no
change here, in the same spirit as `read_toolchain_settings()`:
- A *copy* of `ZZZZZZZZ_<type>.vcxproj` is built with MSBuild in `extract/<type>/`, with `IntDir`
  and `OutDir` overridden to there. The copy differs from the template only in ways that don't
  affect compile or link switches, each for a reason found on lockstep:
  - Sources replaced by a stub defining `main` and `WinMain`: lockstep's `DummySource.cpp` has no
    entry point, the template doesn't link, and a failed link isn't reliably recorded.
  - ProjectReferences removed: even with `BuildProjectReferences=false`, MSBuild evaluates
    ZERO_CHECK with the overridden `IntDir` (warning MSB8028).
  - CustomBuild steps (cmake's check) and build events removed: the post-build
    `vcpkg z-applocal` names the executable by an absolute path in `build_template/Debug/`.
- `CL.command.*.tlog` and `link.command.*.tlog` (UTF-16, a `^inputs` line then the command) are
  read back. Exactly one compile and one link are expected.
- Per-file/per-project parts are removed: `/Fo`, `/Fd` and the source for `cl`; `/OUT`, `/ILK`,
  `/PDB`, `/IMPLIB`, `/MANIFESTFILE`, `/PGD` and the `.obj` inputs for `link`. Anything left that
  mentions the extraction directory is an error, so an unknown per-project part fails loudly.
- Tokens are kept raw, with their quoting as recorded (`/D "CMAKE_INTDIR=\"Debug\""`), and passed
  on unchanged. Switches taking a separate argument (`/D X`, `/external:I X`) are kept as pairs.
- Cached in `.tybuild` under `command_templates`, re-extracted when a template's size/mtime or the
  toolchain changes, or with `--refresh-commands`.
- **The one deliberate deviation**: `/Zi` → `/Z7`. `/Zi` writes a per-project PDB, which parallel
  `cl` processes would have to share through `/FS` and `mspdbsrv`. With `/Z7` each object carries
  its debug information, and the linker still writes the executable's PDB.

**Differing compile commands** are a build failure: otherwise a change could silently bring back
compiling every shared source once per type, and the only symptom would be a slower build.
- One line per differing switch, in MSVC's format, located at the first template that has it:
  `D:\repo\build_template\ZZZZZZZZ_console.vcxproj(1): error TYB101: compile command differs
  between project types: /D RTC_STATIC: console only`. Same switches in a different order are
  also reported.
- The build stops before compiling. `--allow-differing-compile-commands` makes these warnings and
  builds anyway. Objects are always keyed by source *and* `compile_command_id()` (hash of the
  switches as run plus the toolchain), never by source alone, so a source shared by differing
  types is then correctly compiled once per type.

**Compiler environment** (`vs_install.py`):
- Visual Studio found with vswhere, restricted to the major version named by `ToolsVersion`.
  MSBuild and `vcvarsall.bat` both come from that installation.
- `vcvarsall.bat x64 <sdk>`, with the SDK version from the templates, and the environment read
  by running `sys.executable` in the same `cmd` (rather than parsing `set`).
- `find_compiler()` checks `VCToolsVersion` against the toolset (`vNM` → `N.M*`, so v145 is
  14.5x), that `cl` is under `VCToolsInstallDir`, and `WindowsSDKVersion`. It doesn't check that
  vcvarsall's default compiler within 14.5x is MSBuild's default; with one installed, they are.
- Cached in `.tybuild` by toolchain, and captured again if its `cl` is gone or doesn't match.

**Running tools**:
- One `cl` per source, in a thread pool sized to the processor count (`-j N`). Every out of date
  object is attempted, so one run reports every error. Each process's output is buffered and
  printed whole, minus the source name `cl` always prints. Diagnostics keep MSVC's format.
- Projects whose objects all compiled are then linked in parallel, through UTF-16 response files.
- `cl` and `link` run with `./build_template/` as working directory, as under MSBuild.
- A tool that fails without output gets a `tybuild: ... failed with exit code N` line. The summary
  lists failures by name and ends with `BUILD FAILED: ...`, avoiding the word "error".
- The templates' `vcpkg z-applocal` post-build step is **not run**: it copies DLLs, and the static
  vcpkg triplet has none. A DLL-using project set would need it added.
- Ctrl+C: `_run_jobs()` waits with a timeout (an untimed wait can't be interrupted on Windows),
  then cancels queued jobs and kills running processes (`_ProcessRunner.stop()`).

**Incremental builds**. Identity is `[size, mtime_ns]`, as elsewhere in tybuild.
- An object is out of date unless its `.record.json` matches the hash of the full `cl` command
  (compiler path included), the object's identity, and the identity of every file
  `cl /sourceDependencies` listed. That list includes headers outside `./src` (vcpkg, SDK), which
  tybuild's own include scan can't see.
- A project is out of date unless its `link/<Project>.record.json` matches the hash of the link
  command plus response file contents, the executable's identity, and the identities of its
  objects and of libraries named by absolute path. Libraries named only by file name
  (`KERNEL32.LIB`) aren't tracked.
- A record is deleted before its tool runs and written only after it succeeds, so a killed build
  leaves nothing that a later run treats as up to date. An object whose inputs were modified while
  `cl` ran gets no record.
- A project not linked because objects failed, or whose link failed, has its executable deleted,
  so a stale executable can't be run by mistake.

**Known gaps**: no Release configurations. Objects and executables no longer needed (a removed
source or project) aren't cleaned up. A change to the environment other than the toolchain isn't
noticed, since the environment is cached. `get_cpp_dependencies()` is called once per project, as
`generate` does.

## Important Design Decisions

### GUID Handling
- **API Level**: GUIDs passed as strings WITHOUT braces (e.g., `"5C330799-6FA6-33C3-B12C-755A9CA12672"`)
- **Storage**: Braces added when writing to XML/solution files
- **Generation**: Deterministic based on SHA-256 hash of "tybuild:{type}:{name}"

### Template Naming Convention
- Templates must be named `ZZZZZZZZ_<type>.vcxproj`
- "ZZZZZZZZ" chosen as unique prefix unlikely to appear naturally in project files
- Simple string replacement used: `ZZZZZZZZ_<type>` → `<ProjectName>`

### Meta-Projects: Where Each One Lives

Three meta-projects end up in the solution, from two different places:

- **ALL_BUILD** and **ZERO_CHECK** come from the consuming repository's
  `./build_template/`, which is cmake output. A toolchain change reaches them by
  deleting that directory and re-running cmake.
- **ONE_CHECK** ships *inside this package*, at `./src/tybuild/templates/`, and is the
  project that re-runs `tybuild generate` as a build step. Each user project depends on
  it, and it depends on ZERO_CHECK.

Because ONE_CHECK does not come from cmake, a toolchain change has no way of reaching it
on its own. So it does not state a toolchain at all: `read_toolchain_settings()` reads
`ToolsVersion`, `PlatformToolset` and `WindowsTargetPlatformVersion` out of
`./build_template/ZERO_CHECK.vcxproj` (cmake output, always present, and a Utility project
like ONE_CHECK) and substitutes them in at generate time. The solution header's Visual
Studio version is derived from the same `ToolsVersion`.

**Do not reintroduce a literal toolset, SDK or tools version anywhere in this package.**
That drift is what `KNOWN_ISSUES.md` issue 2 was about, and it had already bitten twice
before it was fixed.

### Built-In Template Placeholders

Templates under `./src/tybuild/templates/` are not copied verbatim: `_render_builtin_template()`
in `build.py` substitutes placeholders first.

- `@TYBUILD_PYTHON@` → `sys.executable`, the interpreter running `tybuild generate`.
- `@TYBUILD_TOOLS_VERSION@` → MSBuild `ToolsVersion`, e.g. `18.0`.
- `@TYBUILD_PLATFORM_TOOLSET@` → platform toolset, e.g. `v145`.
- `@TYBUILD_WINDOWS_SDK@` → Windows SDK version, e.g. `10.0.26100.0`.

The interpreter is resolved rather than configured, so the regeneration step baked into
ONE_CHECK always runs under the same interpreter that generated it, whatever machine that
is. The placeholder is quoted in the template, so interpreter paths containing spaces are
fine. The last three come from `read_toolchain_settings()` — see above.

Because a package resource has no useful mtime, and because an interpreter path can change
without changing the file's length, these templates are compared against the **destination
file's contents** to decide whether to rewrite — not against a size recorded in the cache.
Add a placeholder here rather than an absolute path if you extend these templates.

### Incremental Build Strategy
Projects regenerated ONLY when:
1. Template file changes (detected by size + mtime_ns)
2. Source file SET changes (files added/removed, NOT content changes)

**Rationale**: Visual Studio handles source content changes during compilation. We only need to regenerate project structure when the included files actually change.

### Cache Locations
- **Build cache** (`./build/.tybuild`): Stores Visual Studio project generation state. Deleted when user cleans the build directory.
- **Dependency cache** (`./includes.cache`): Stores C++ include dependency scan results. Persists in repository root across build cleanups.
- **`tybuild build` cache** (`./build_tybuild/Debug/.tybuild`, plus per-object and per-project
  `.record.json` files): command templates, compiler environment, and what each output was built
  from. Separate from `./build/.tybuild`; deleting `./build_tybuild/` forces a full rebuild.

### Build Cache Format (`./build/.tybuild`)
```json
{
  "solution_guid": "...",
  "toolchain": {
    "tools_version": "18.0",
    "platform_toolset": "v145",
    "windows_sdk_version": "10.0.26100.0"
  },
  "projects": [
    {
      "name": "ProjectName",
      "type": "console",
      "template_identity": {"size": 12345, "mtime_ns": 1234567890},
      "sources": ["project/console/Main.cpp", "utils/helper.cpp"]
    }
  ]
}
```

## Path Handling

- **Project discovery**: Returns absolute paths for `Project.cpp_file`
- **Dependency output**: Returns POSIX-style relative paths from source root
- **VS project files**: Sources stored as relative to project directory, with Windows backslashes
- **Include resolution**: Works relative to both includer file and source root

## Common Workflows

### Adding a New Project Type
1. Create template in `./build_template/ZZZZZZZZ_<newtype>.vcxproj`
2. Create template filters file: `ZZZZZZZZ_<newtype>.vcxproj.filters`
3. Create projects in `./src/project/<newtype>/ProjectName.cpp`
4. Run `tybuild generate`

### Debugging Dependency Issues
- Run `tybuild check-includes` to list `TYB001`/`TYB002` include errors (also printed by
  `generate`, `deps`, `orphaned`, `show-include-chain` and `generate-cmake`)
- Run `tybuild deps ./src/project/<type>/<Name>.cpp` to see what's being found (from repository root)
- Use `--refresh` to rebuild dependency cache from scratch

### Force Full Rebuild
```bash
tybuild generate --force
```

### Building Without Visual Studio
```bash
tybuild build --dry-run   # show the extracted commands and object counts
tybuild build             # build; executables in ./build_tybuild/Debug/bin/
```
Exits 1 on compile/link failures, include errors, or differing compile commands between types.
Delete `./build_tybuild/` for a clean build.

### Generate CMake Project List
```bash
tybuild generate-cmake
```
Generates `./generated_projects.cmake` with project information for CMake integration.

## Testing Commands

The CLI includes test commands for development:
- `tybuild test-prj` - Test project generation from template

## Future Enhancements

Areas marked for future work:
- `tybuild build`: Release configurations; cleaning up objects and executables no longer needed
- Whether Visual Studio builds should also compile shared sources once (e.g. static libraries
  per type); out of scope for `tybuild build`
- `tybuild clean` command
- More sophisticated include path handling (multiple include directories)
- Support for header-only libraries
- Cross-platform support (currently Windows/MSVC focused)

## Notes for AI Assistants

- When modifying generation logic, remember GUIDs are WITHOUT braces at API boundaries
- Template file changes require updating both .vcxproj AND .vcxproj.filters
- Always maintain backward compatibility with existing `.tybuild` cache format
- The `include_headers` parameter was added later; older code may not use it
- Error messages should be user-friendly (avoid implementation details)
- `tybuild build` takes its compile and link switches from MSBuild's tlogs; don't add literal
  compiler or linker flags in the package (the `/Z7` swap is the one documented exception)
- In `tybuild build`, a record may only be written after its tool has succeeded, and must be
  deleted before the tool runs; that ordering is what makes killed builds safe
