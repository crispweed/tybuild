# Task: `tybuild build` — Release, and building only some projects

Written 2026-11-19 from the lockstep repo, by an agent in the agent VM, at the operator's request.
This is a task description to start from, not a design to follow to the letter: the session doing
the work should push back where the code says otherwise.

## Goal

Two options for `tybuild build`:

- **`--config Release`**: build Release|x64, the same way Debug is built now, into
  `./build_tybuild/Release/`, beside `./build_tybuild/Debug/`. Debug stays the default, so
  lockstep's check, which runs a bare `tybuild build`, is unchanged.
- **`--project NAME`**, repeatable: build only the named projects, and the objects they need,
  rather than every project. With either configuration.

`CLAUDE.md` lists Release configurations under `tybuild build`'s known gaps and future
enhancements; this closes the first.

## Why

Lockstep has a console project, `VehiclePathfindingScenarios`, a scenario harness for a
pathfinding prototype, which runs planning queries headlessly and prints a comparison. Its
figures come from Release builds: in Debug, its searches run about ten times slower, and the
route field it builds for each query takes seconds rather than about half a second, so a run
over a sample of queries goes from a couple of minutes to tens of minutes.

In the agent VM, the agent builds only through lockstep's check, which runs `tybuild build`, so
Debug only. The operator's Release builds come from the Visual Studio solution, on the host, so at
the moment every Release run of the harness is handed to the operator to build and run, and its
output pasted back. With these options, lockstep can allow the agent one exact command line,
`tybuild build --config Release --project VehiclePathfindingScenarios`, and the agent can run the
harness itself.

`--project` matters as much as `--config` for that: a Release build of all of lockstep's projects
would be a long build of a dozen executables nobody asked for, to get one.

## What the code looks like now (from reading it, not from running anything)

- `command_templates.py` has `CONFIGURATION = "Debug"`, used for the extraction build's
  `/p:Configuration=` and, through `native_build.py`, for the output directory
  (`config_dir = repo_root / BUILD_DIR_NAME / CONFIGURATION`). Making it a parameter, carried from
  the CLI through `run_build()` and `get_type_templates()`, looks like most of `--config`.
- Each configuration's directory has its own `.tybuild` cache, so cached command templates and
  compiler environment can't be mixed up between configurations, and objects, records and
  executables are already separate by directory. Object identity also includes the hash of the
  compile command, which differs between configurations anyway.
- The extraction builds a copy of each `ZZZZZZZZ_<type>.vcxproj`, which as cmake writes it has all
  four configurations, so Release needs no new template. Things to check in the Release
  extraction:
  - the absolute paths the cmake-generated project states for the linker's PDB and import library,
    noted for Debug as going to `./build_template/Debug/`; for Release, presumably
    `./build_template/Release/`, still belonging to the dummy project;
  - the post-build `vcpkg z-applocal` step, removed as for Debug;
  - that exactly one compile and one link are still recorded in the tlogs;
  - `CMAKE_INTDIR` in the recorded compile command, which will say Release.
- The `/Zi` → `/Z7` swap: cmake's Release flags normally have no debug information at all, so
  the swap probably has nothing to do. If the templates do ask for debug information in Release,
  the same swap applies, for the same reason.
- The vcpkg triplet is static (`x64-windows-static`), and Release links its release libraries;
  those come from the extracted link command, so nothing is expected to be needed here, but it is
  the first place to look if Release fails to link.
- `run_build()` plans objects for every discovered project. `--project` would filter that list
  after discovery, with an unknown name an error listing the projects found. The compile command
  comparison between project types could stay as it is, over every type, since templates are
  cached and the comparison is cheap; or be limited to the selected projects' types. Either is
  defensible; the first keeps a partial build from hiding a difference that a full one would fail
  on.
- Incremental builds and records: nothing configuration-specific is expected, since everything is
  per directory, but a Release build following a Debug one, and the other way round, should each
  be incremental within its own directory and leave the other's untouched.

## Done when

- `tybuild build` with no options does exactly what it does now.
- `tybuild build --config Release --project VehiclePathfindingScenarios`, from lockstep's root,
  builds that one executable into `./build_tybuild/Release/bin/`, and a second run with nothing
  changed compiles and links nothing.
- `--config Debug --project NAME` builds only that project's objects and executable.
- `CLAUDE.md` (module 7, the CLI list, the output layout, the known gaps and future enhancements)
  describes the options.

## Follow-up in the lockstep repo

Not part of this task, for the lockstep side once these options exist:

- The root `AGENTS.md` names the Release build command and the harness's command lines, as
  commands an agent may need.
- `.claude/settings.json` allows them, in exactly those spellings, so that the agent in the VM may
  run them itself.
