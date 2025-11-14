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
└── build.py            # Main build orchestration

./src/project/          # User's C++ projects (not in repo)
└── <type>/             # Project type directories (console, sdl3, etc.)
    └── <Name>.cpp      # Each .cpp is a project entry point

./build_template/       # Template VS project files (not in repo)
├── ZZZZZZZZ_<type>.vcxproj        # Templates for each project type
├── ZZZZZZZZ_<type>.vcxproj.filters
├── ALL_BUILD.vcxproj              # CMake-style meta-project
├── ALL_BUILD.vcxproj.filters
├── ZERO_CHECK.vcxproj             # CMake-style validation project
└── ZERO_CHECK.vcxproj.filters

./build/                # Generated output (not in repo)
├── .tybuild            # Build cache (JSON)
├── Solution.sln        # Generated solution
├── <ProjectName>.vcxproj
└── <ProjectName>.vcxproj.filters
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
- `get_cpp_dependencies(root, build_root, start_file, include_headers=False)` → List of relative paths
  - Scans for `#include "..."` statements
  - Builds transitive dependency graph
  - Includes implicit header→source relationships (same directory/stem)
  - Can filter to .cpp only or include .h files

**Include Resolution Strategy**:
1. Try relative to including file's directory
2. Try relative to source root (if step 1 fails)
3. Warn if both fail

**Caching**: Uses `includes.cache` for performance (tracks file size + mtime_ns)

### 3. `vs_templates.py`
**Purpose**: Generates Visual Studio project and solution files

**Key Functions**:
- `generate_project_guid(project_type, project_name)` → Deterministic GUID (no braces)
- `generate_project_from_template(template_path, template_name, project_name, project_guid, source_root, sources_rel_to_root, output_path)`
  - Reads template .vcxproj and .vcxproj.filters
  - Replaces template name with project name (string replacement)
  - Replaces GUID (XML manipulation)
  - Replaces source file list (XML manipulation)

- `generate_solution(output_sln_path, solution_guid, all_build_guid, zero_check_guid, projects_to_add)`
  - Generates .sln with ALL_BUILD, ZERO_CHECK, and user projects
  - ALL_BUILD depends on all projects
  - User projects depend on ZERO_CHECK
  - Four configurations: Debug, Release, MinSizeRel, RelWithDebInfo (all x64)

**Important**: GUIDs are passed WITHOUT braces at API level, added internally for XML format

### 4. `build.py`
**Purpose**: Main build orchestration with incremental regeneration

**Key Function**: `generate_build_files(base_path, force=False)`

**Workflow**:
1. Copy ALL_BUILD and ZERO_CHECK files from template to build dir
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

**Hardcoded GUIDs**:
- ALL_BUILD: `5C330799-6FA6-33C3-B12C-755A9CA12672`
- ZERO_CHECK: `46BE4EB3-B0FD-3982-8000-AE0905052172`

### 5. `cli.py`
**Purpose**: Command-line interface

**Commands**:
- `tybuild generate [--root DIR] [--force]` - Generate build files
- `tybuild list [--root DIR]` - List discovered projects
- `tybuild deps ROOT START [--refresh]` - Show dependencies for a file
- `tybuild build TARGET [--clean]` - (Not implemented yet)

## Important Design Decisions

### GUID Handling
- **API Level**: GUIDs passed as strings WITHOUT braces (e.g., `"5C330799-6FA6-33C3-B12C-755A9CA12672"`)
- **Storage**: Braces added when writing to XML/solution files
- **Generation**: Deterministic based on SHA-256 hash of "tybuild:{type}:{name}"

### Template Naming Convention
- Templates must be named `ZZZZZZZZ_<type>.vcxproj`
- "ZZZZZZZZ" chosen as unique prefix unlikely to appear naturally in project files
- Simple string replacement used: `ZZZZZZZZ_<type>` → `<ProjectName>`

### Incremental Build Strategy
Projects regenerated ONLY when:
1. Template file changes (detected by size + mtime_ns)
2. Source file SET changes (files added/removed, NOT content changes)

**Rationale**: Visual Studio handles source content changes during compilation. We only need to regenerate project structure when the included files actually change.

### Cache Format (`.tybuild`)
```json
{
  "solution_guid": "...",
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
- Check warnings from `resolve_include()` in stderr
- Run `tybuild deps ./src ./src/project/<type>/<Name>.cpp` to see what's being found
- Use `--refresh` to rebuild dependency cache from scratch

### Force Full Rebuild
```bash
tybuild generate --force
```

## Testing Commands

The CLI includes test commands for development:
- `tybuild test-sln` - Test solution generation
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
