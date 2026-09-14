# CLAUDE.md - tybuild Project Guide

## Project Overview

`tybuild` is a Python-based build system that generates Visual Studio project files (.vcxproj) and solutions (.sln) from a source directory structure. It automatically discovers C++ projects, analyzes dependencies, and creates Visual Studio build configurations.

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
├── build.py            # Main build orchestration
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
- `tybuild build TARGET [--clean]` - (Not implemented yet)

**Important**: All commands assume they are run from the repository root directory, which must contain a `./src` subdirectory. The `--root` parameter has been removed from all commands.

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
- `tybuild build` command (actual compilation)
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
