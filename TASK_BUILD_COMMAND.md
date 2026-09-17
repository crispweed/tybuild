# Task: `tybuild build` — compile each shared object once

Written 2026-09-17 from the lockstep repo, after looking at how that repo builds. This is a task
description to start from, not a design to follow to the letter: where it says "lean", that is a
recommendation, and the session doing the work should push back if the code says otherwise.

## Goal

An optional `tybuild build` that performs the native Windows build itself, knowing that projects
share sources, so that each object file is compiled once and linked into every project that needs
it. The first user is lockstep's `scripts/check.py`, which currently builds `build/Solution.sln`
with MSBuild. Visual Studio, through `tybuild generate`, stays the day to day workflow and is not
changed by this.

Only Debug|x64 is needed to start with. Wasm projects are out of scope, as they already are for
`generate`.

## Why

From lockstep's `docs/tasks/BUILD_ISSUES.md`: a full build is 644 compiles of 227 distinct sources,
because each generated project compiles its own copy of its full transitive source list. From
clean, the build is about 3 minutes of a check run with a 9 minute deadline, and a change to a
widely included header costs close to that again. MSBuild also only parallelises across projects
here (there is no `/MP`), so an incremental build that touches one big project compiles serially;
a file-level scheduler fixes that as a side effect.

## What was found in lockstep (the facts this plan rests on)

### Compile settings are currently identical across project types

They are identical within a project type, since every project of a type comes from the same
`build_template/ZZZZZZZZ_<type>.vcxproj`. The console and sdl3 types used to differ in
LibDataChannel's interface definitions (`RTC_STATIC;RTC_ENABLE_WEBSOCKET=1;RTC_ENABLE_MEDIA=0`),
which only console projects got. On 2026-09-17 lockstep's `CMakeLists.txt` was changed to apply
them to every target, and the regenerated templates now have the same definitions in the same
order.

**Decided:** a difference between project types' compile commands, with the per-file and
per-project parts taken out, is a build failure. Otherwise a dependency change could silently bring
back compiling every shared source once per type, and the only symptom would be slower builds. The
failure names the switches that differ and the types that have each one, for example
`/D RTC_STATIC: console only`, in MSVC's error format so that lockstep's check script picks it up.
The comparison is between the extracted commands, not the templates' XML, since MSBuild adds
switches of its own.

An option lets the build go ahead anyway (for example `--allow-differing-compile-commands`; the
name is the implementing session's call), for when the difference is expected, or its fix is
waiting on something else. So objects are still identified by their source *and* their compile
command (for instance a hash of the command with the per-file parts taken out), never by the source
alone: with the option, a source reached from two differing types is compiled once per type,
correctly. Report the number of distinct objects per run.

Nothing is lost by linking objects directly rather than through a static library: the concern in
BUILD_ISSUES.md about static registrations being dropped applies to libraries only.

### The exact commands MSBuild runs are recorded in tlog files

After a build, each project's intermediate directory holds them, for example
`build/Tests.dir/Debug/Tests.tlog/`:

- `CL.command.1.tlog` — for each source, a `^<SOURCE PATH>` line followed by its full `cl` command
  line, for example (Tests, a console project):

  ```
  /c /ID:\LOCKSTEP\SRC /Zi /nologo /W1 /WX /diagnostics:column /Od /Ob0 /D _MBCS /D WIN32
  /D _WINDOWS /D RTC_STATIC /D RTC_ENABLE_WEBSOCKET=1 /D RTC_ENABLE_MEDIA=0
  /D "CMAKE_INTDIR=\"Debug\"" /EHsc /RTC1 /MTd /std:c++20 /Fo"TESTS.DIR\DEBUG\\"
  /Fd"TESTS.DIR\DEBUG\VC145.PDB" /external:W0 /TP  /external:I "D:/lockstep/vcpkg/installed/x64-windows-static/include"
  D:\LOCKSTEP\SRC\BOUNDARYLOOPSTOCONTROLGRAPH.CPP
  ```

- `link.command.1.tlog` — a `^<objects joined by |>` line followed by the `link` command line:
  `/OUT:... /INCREMENTAL /ILK:... /NOLOGO <libs> /MANIFEST /MANIFESTUAC:"level='asInvoker' uiAccess='false'" /manifest:embed /DEBUG /PDB:... /SUBSYSTEM:CONSOLE /TLBID:1 /IMPLIB:... /MACHINE:X64 /machine:x64 <objects>`

The files are UTF-16 with a BOM, and MSBuild upper-cases paths in them (harmless on Windows, but
don't be surprised by it). The per-file and per-project parts to substitute are the source path,
`/Fo`, `/Fd`, and on the link side the object list, `/OUT`, `/ILK`, `/PDB` and `/IMPLIB`.

**Lean:** get the command templates by building the `ZZZZZZZZ_<type>` dummy projects in
`build_template/` once with MSBuild (they compile `src/DummySource.cpp` and link against the
type's full library set), and reading the two tlogs per type. Redo it when the template's identity
changes, as `generate` already tracks. This takes the flags from exactly the tool that Visual
Studio uses, so a change to `CMakeLists.txt` (such as the warning level fix BUILD_ISSUES.md wants)
reaches `tybuild build` with no change here, in the same spirit as `read_toolchain_settings()`.

Alternatives considered, and why not the lean:

- Translating the vcxproj's `ClCompile` / `Link` properties into switches: that is reimplementing
  MSBuild's CL and Link tasks, including defaults it injects that aren't in the file
  (`/diagnostics:column`, `/TP`, `/W1`, the manifest switches). Fragile.
- A separate cmake configure with the Ninja generator, reading `compile_commands.json`: a
  different generator, so the flags can diverge from what Visual Studio builds.
- Parsing MSBuild's console log at normal verbosity, which also prints the command lines: workable
  as a fallback if the tlog format turns out to be awkward, but noisier.

## Things the implementation has to deal with

- **Compiler environment.** The recorded commands don't include the standard library or SDK
  include and library paths; MSBuild supplies them through the environment. Capture the
  environment from `vcvarsall.bat x64` (found through `vswhere`, as `check.py` already finds
  MSBuild), cache it keyed by the toolchain, and check that the `cl` it finds matches the template's
  `PlatformToolset` rather than silently using another installed toolset.
- **Debug information with a shared object directory.** The commands use `/Zi` with a per-project
  `/Fd` PDB, which works for MSBuild because one `cl` process compiles a whole project. Parallel
  `cl` processes writing one PDB need `/FS`, which serialises through `mspdbsrv` and is slow.
  **Lean:** replace `/Zi` with `/Z7` and drop `/Fd`, so debug info goes into each object and the
  linker still writes the executable's PDB. That is a deliberate, documented deviation from the
  extracted command, the only one this plan calls for.
- **Incremental correctness.** An object is out of date if its source, any header it included, or
  its command line changed. tybuild's own include graph only sees quoted includes under `src/`, so
  it can't see vcpkg or system headers. **Lean:** use `cl /sourceDependencies <file>.json`, which
  writes the included files as stable JSON (more robust than parsing `/showIncludes`, whose prefix
  is localised), and store per object the command hash plus the identity of each dependency. A link
  is out of date if its command or any object changed.
- **Scheduling.** A process pool of `cl` invocations, one source each, sized to the processor count,
  then links once their objects are done. Keep going after a failed compile so that one run reports
  every error, but don't link a project with a failed object.
- **Output.** Buffer each process's output and print it whole, so parallel diagnostics don't
  interleave. Keep MSVC's `file(line): error Cxxxx:` format, which lockstep's check script parses.
  Since each object is compiled once, each error appears once, and the check script's counting of
  duplicates across projects becomes unnecessary.
- **Being killed.** The check script kills the process tree at its deadline. Write objects and
  records so that a killed build leaves nothing that a later run treats as up to date (for
  instance, write the dependency record only after `cl` succeeds).
- **The post-build step.** The templates run `vcpkg z-applocal` to copy DLLs next to the
  executable. With the static triplet lockstep uses there is nothing to copy, so it can be skipped,
  but say so in the code rather than dropping it silently.
- **Object naming.** Name objects by their path under `src/` plus the command-set identity, not by
  base name, since two directories could hold files of the same name.

## Where the output goes

**Lean:** a separate directory (for instance `build_tybuild/Debug/`), with objects under it by
command set and executables in its own output directory — not `build_template/Debug/`, where
Visual Studio's builds put the executables. Sharing that directory would have the two builds
overwriting each other's executables, PDBs and incremental link state.

That means lockstep's `scripts/check.py` and `scripts/run_integration_tests.py`, which look for
executables in `build_template/Debug/`, need to be told where to look. That is a follow-up in the
lockstep repo, not part of this task; the task here should make the output location easy to name
from outside (an option, or a fixed documented path).

The same follow-up covers the check script's handling of the differing compile commands failure:
it doesn't pass the option by default, and its build stage extract should show the differing
switches.

## Suggested shape of the work

1. Extraction only: a command (or a `--dry-run` of `build`) that builds the dummy templates, reads
   the tlogs, and prints the compile and link command templates per type and the number of distinct
   objects the lockstep project set needs, with the comparison of compile commands across types.
   This is where the assumptions above are confirmed or not, before anything is built.
2. A clean build of everything, no incremental logic: compile each distinct object once, link each
   project. Compare against the MSBuild build: every project links, `Tests.exe` passes, time from
   clean.
3. Incremental rebuilds, as above.
4. Documentation in `CLAUDE.md`, and the lockstep-side follow-up noted for that repo.

The user builds and tests; hand over commands to run rather than assuming they have been run.

## Open questions for the user

- Whether, longer term, Visual Studio builds should also benefit (for example through generated
  static libraries per type). Out of scope here, where the check script is the user.
