# Known Issues

Issues noticed in passing on 2026-09-12, while upgrading the lockstep repo from Visual Studio 2022
to Visual Studio 2026. None of them was the subject of that work, so each was noted rather than
fixed, apart from the minimum needed to get that build going. They are written down here to be
picked up as a piece of work of their own.

Ordered by how much they matter, which is roughly the reverse of how much work they are.

## 1. ONE_CHECK.vcxproj hardcodes an absolute path to the interpreter — FIXED 2026-09-12

`src/tybuild/templates/ONE_CHECK.vcxproj` runs the regeneration step as

```
D:\tybuild\venv\Scripts\python.exe -m tybuild generate
```

four times, once per configuration. Nothing rewrites it: the template processing in `vs_templates.py`
substitutes the template *name* for the project name and manipulates the GUID and source list, and
that is all, so the path is copied into the generated project verbatim.

So a machine where this repository is not checked out at `D:\tybuild` gets a solution whose
ONE_CHECK step fails on every build. That is a machine-specific path baked into a checked-in
template of a tool that is otherwise indifferent to where it lives.

This is the one that actually blocks something. Two further machines are planned for the lockstep
work — an agent VM, and a second host at another location — and neither will have this layout.

The obvious fix is to substitute the running interpreter (`sys.executable`) at generate time, which
needs no configuration and cannot disagree with the environment `tybuild generate` is actually run
from. Worth checking whether anything else in the templates is similarly absolute before doing it.

**Fixed.** The template now carries a quoted `@TYBUILD_PYTHON@` placeholder, substituted with
`sys.executable` by `_render_builtin_template()` in `build.py`. Quoting it also fixes interpreter
paths containing spaces, which the old unquoted absolute path would have broken on — relevant to
the planned agent VM, where a `C:\Program Files\...` interpreter is likely.

Two details that came out of doing it:

- The check for whether to rewrite a built-in template compared the *unsubstituted* template's byte
  length against a size in the build cache. That could not see an interpreter path change, and a
  size is a weak signal for one anyway, since two paths can be the same length. It now compares the
  fully substituted content against the destination file's contents.
- The package templates directory holds only `ONE_CHECK.vcxproj`, and the interpreter path was the
  only absolute path in it, so nothing else needed the same treatment.

## 2. The toolset and Windows SDK version are hardcoded in one template and derived in the others — FIXED 2026-09-12

`ALL_BUILD.vcxproj`, `ZERO_CHECK.vcxproj` and the `ZZZZZZZZ_<type>.vcxproj` templates live in the
consuming repository's `build_template/` and are cmake output, so a toolchain change reaches them
by deleting that directory and regenerating. `ONE_CHECK.vcxproj` is shipped inside this package
instead (`build.py`, `builtin_templates`), so it holds its own `ToolsVersion`, `PlatformToolset`
and `WindowsTargetPlatformVersion`, and a toolchain change does not reach it at all.

One template hardcoding what its siblings derive is invisible until the two disagree, and they had
already drifted before anyone went looking: at the point the Visual Studio upgrade started,
ONE_CHECK named Windows SDK `10.0.22621.0` while the cmake-generated templates beside it named
`10.0.26100.0`. The toolset upgrade then turned up the same problem a second time, in the same file.

Having tybuild read these values out of the cmake-generated templates it already has in
`build_template/` would make the whole set derive from one place and remove the class of problem.
The alternative is to keep bumping ONE_CHECK by hand at each upgrade, which is what has happened so
far, and which is only cheap because nobody has yet been bitten by the drift in a way that cost real
time.

Two more copies of the same values turned up on 2026-09-12 that this note did not originally list.
Both belong to whoever does this issue:

- **`generate_utility_project()` in `vs_templates.py`** hardcodes `ToolsVersion` 17.0 (line 486),
  `WindowsTargetPlatformVersion` 10.0.22621.0 (line 509), `PlatformToolset` v143 (line 525), and
  `ToolsVersion` 17.0 again for the filters file (line 610). It is dead code — nothing calls it, and
  `build.py` copies the `ONE_CHECK.vcxproj` file instead — which is exactly why it escaped the
  by-hand sweep and still holds the pre-upgrade values, the old Windows SDK included. It looks like
  an earlier attempt at generating ONE_CHECK programmatically instead of shipping it as a file.
  Probably delete it rather than add a fourth thing to remember to bump.

- **`generate_solution()` writes no version lines.** It emits the format line and
  `# Visual Studio Version 18` and stops. Real cmake output also carries
  `VisualStudioVersion = <n>.0.x.y` and `MinimumVisualStudioVersion = 10.0.40219.1`, which are what
  VSLauncher reads when a .sln is opened from the shell. On 2026-09-12 a generated solution was
  opened in Visual Studio 2022 on a machine that has 2026 installed, and its v145 projects then
  failed with MSB8020, because VS 2022's MSBuild resolves `$(VCTargetsPath)` into its own install.
  That turned out to be the wrong IDE being picked by hand rather than by the launcher, so it does
  not prove the missing lines caused it — but it is the exact failure they exist to prevent, and the
  version number in them is one more value that should be derived rather than typed.

**Fixed**, by the route this note suggested. `read_toolchain_settings()` in `vs_templates.py` reads
`ToolsVersion`, `PlatformToolset` and `WindowsTargetPlatformVersion` out of
`build_template/ZERO_CHECK.vcxproj` — cmake output, always present, and a Utility project like
ONE_CHECK rather than a compiling one. The ONE_CHECK template now names no toolchain of its own,
carrying `@TYBUILD_TOOLS_VERSION@`, `@TYBUILD_PLATFORM_TOOLSET@` and `@TYBUILD_WINDOWS_SDK@`
instead, substituted at generate time alongside `@TYBUILD_PYTHON@`. The solution header's Visual
Studio version is derived from the same `ToolsVersion`, so the hand-edit of 2026-09-12 is undone and
cannot be needed again. `generate_utility_project()` was deleted outright.

There are now no literal toolset, SDK or MSBuild version values left anywhere in the package.

One thing that had to be handled to avoid recreating the same bug one level up: the solution was
only regenerated when the project set changed or `--force` was passed, so a toolchain change would
have left the solution header naming the old Visual Studio indefinitely. The build cache now records
the toolchain, and a change to it regenerates the solution.

Verified against the lockstep repo: the settings are picked up correctly
(`Toolchain: v145, Windows SDK 10.0.26100.0, MSBuild tools 18.0 (from ZERO_CHECK.vcxproj)`), the
generated ONE_CHECK is byte-identical to the hand-edited one it replaces — which is the point, the
values did not change, only where they come from — and the solution regenerated on the first run
because the cache had no toolchain recorded yet.

### Still open: the solution's `VisualStudioVersion` lines

The second bullet above is *not* fixed. `generate_solution()` still writes no
`VisualStudioVersion` / `MinimumVisualStudioVersion` lines. The major version in the comment is now
derived, but those two lines are still absent.

They were left out deliberately. `MinimumVisualStudioVersion = 10.0.40219.1` is a constant, but
`VisualStudioVersion` wants a full four-part version (`18.0.31903.59` or similar) and there is no
source for one: `build_template/` holds no cmake-generated `.sln` to copy it from, and `ToolsVersion`
only gives the major. Writing `18.0.0.0` would be inventing a value, which is the habit this whole
issue exists to break. Options, if it turns out to matter: read the real version from the VS install
(`vswhere`, or `CMAKE_GENERATOR_INSTANCE` in `build_template/CMakeCache.txt`, which does point at
the exact instance), or have cmake keep its own `.sln` in `build_template/` as the reference.

## 3. CLAUDE.md predates ONE_CHECK — FIXED 2026-09-12

The project guide describes a two-meta-project world that no longer exists. It lists only ALL_BUILD
and ZERO_CHECK under `build_template/`, gives a `generate_solution` signature with no ONE_CHECK
GUID, and states that user projects depend on ZERO_CHECK — where in fact each user project depends
on ONE_CHECK, and ONE_CHECK depends on ZERO_CHECK. The `templates/` directory inside the package,
which is where ONE_CHECK actually lives and the whole reason issue 2 exists, is not mentioned in the
directory structure at all.

So the one document an agent reads before working here is silent about the mechanism behind both
issues above. Worth fixing as part of whichever of them is done first, rather than on its own.

**Fixed** alongside issue 1. `CLAUDE.md` now has the package `templates/` directory in its structure
diagram, a note on `build_template/` saying ONE_CHECK is explicitly *not* there, the real
`generate_solution` signature and dependency chain, a "Meta-Projects: Where Each One Lives" section
pointing at issue 2, and a "Built-In Template Placeholders" section covering `@TYBUILD_PYTHON@`.

One further staleness was found and fixed while in there: the guide listed ALL_BUILD's and
ZERO_CHECK's GUIDs under a heading of "Hardcoded GUIDs", but `build.py` does not hardcode them — it
reads each meta-project's GUID back out of the copied file with `get_project_guid()`. `source_moves.py`
was also missing from the module list.

## What was changed on 2026-09-12

So that a later session knows the baseline rather than rediscovering it. Only what the Visual Studio
2026 build needed, by hand, in the manner issue 2 describes:

- `src/tybuild/templates/ONE_CHECK.vcxproj`: `ToolsVersion` 17.0 to 18.0, `PlatformToolset` v143 to
  v145 in all four configurations, and `WindowsTargetPlatformVersion` 10.0.22621.0 to 10.0.26100.0,
  the last of these being the pre-existing drift rather than anything to do with the upgrade.
- `src/tybuild/vs_templates.py`: the generated solution header, `# Visual Studio Version 17` to 18.

The toolset those values correspond to is recorded on the lockstep side, in
`docs/pinned_dependency_versions.txt`, along with why it is pinned at patch granularity.
