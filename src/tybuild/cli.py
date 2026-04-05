import argparse
import os
import subprocess
import sys
from pathlib import Path
from tybuild.dependencies import get_cpp_dependencies, fix_includes, scan, build_dependency_graph, transitive_reachable, find_include_chain, CACHE_FILENAME
from tybuild.source_moves import run_source_files_moved
from tybuild.projects import discover_projects
from tybuild.vs_templates import generate_project_guid, generate_solution, generate_project_from_template
from tybuild.build import generate_build_files
from tybuild.cmake_export import generate_cmake_file

# from tybuild.clean import Clean


def cmd_deps(args):
    """List .cpp file dependencies for a given source file."""
    try:
        repo_root = Path.cwd()
        start_file = Path(args.start).resolve()

        deps = get_cpp_dependencies(repo_root, start_file, refresh=args.refresh)

        for dep in deps:
            print(dep)

    except (ValueError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(2)


def cmd_build(args):
    """Build the specified target."""
    print("not implemented yet")
    # if args.clean:
    #     Clean()
    # RunBuild(args.target)


def cmd_list(args):
    """List all discovered projects."""
    try:
        base_path = Path.cwd()
        projects = discover_projects(base_path)

        if not projects:
            print("No projects found in ./src/project", file=sys.stderr)
            return

        print(f"Found {len(projects)} project(s):")
        print()
        for project in projects:
            print(f"  {project.type:15} {project.name}")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_generate(args):
    """Generate Visual Studio project and solution files."""
    try:
        base_path = Path.cwd()
        generate_build_files(base_path, force=args.force)

    except (RuntimeError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(2)

def cmd_test_prj(args):
    """Test project generation from template."""
    try:

        # Paths relative to current working directory
        repo_root = Path.cwd()
        template_path = Path('./build_template/').resolve()
        output_path = Path('./build_template/').resolve()

        src_root = Path('./src/').resolve()
        start_file = Path('./src/project/sdl3/Client.cpp').resolve()

        deps = get_cpp_dependencies(repo_root, start_file)

        generate_project_from_template(
            template_path,
            'ZZZZZZZZ_sdl3',
            'Client2',
            '19EF89DE-8F64-33EA-8F28-40499A66EA07',
            src_root,
            deps,
            output_path
        )

        print(f"Project files generated successfully")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


def cmd_generate_cmake(args):
    """Generate CMake file with project information."""
    try:
        repo_root = Path.cwd()
        output_path = repo_root / 'generated_projects.cmake'

        generate_cmake_file(repo_root, output_path)

        print(f"Generated {output_path}")

    except (RuntimeError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(2)


def cmd_source_files_moved(args):
    """Detect moved/renamed source files and update includes."""
    try:
        repo_root = Path.cwd()
        run_source_files_moved(repo_root, commit=args.commit, push=args.push)
    except subprocess.CalledProcessError as e:
        print(f"Git error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_orphaned(args):
    """Find source files not referenced by any project."""
    try:
        repo_root = Path.cwd()
        src_root = repo_root / 'src'

        if not src_root.is_dir():
            print("Error: No ./src directory found", file=sys.stderr)
            sys.exit(1)

        projects = discover_projects(repo_root)
        if not projects:
            print("No projects found in ./src/project", file=sys.stderr)
            return

        # Scan and build dependency graph (shared cache)
        cache_path = repo_root / CACHE_FILENAME
        cache = scan(src_root, cache_path, refresh=args.refresh)
        dep_graph = build_dependency_graph(cache)

        # Collect all files reachable from any project root
        reachable = set()
        for project in projects:
            start_rel = project.cpp_file.relative_to(src_root).as_posix()
            reachable.add(start_rel)
            reachable.update(transitive_reachable(dep_graph, start_rel))

        # Find all source files on disk
        all_source_files = set()
        source_exts = {'.cpp', '.h', '.hpp'}
        for dirpath, _dirnames, filenames in os.walk(src_root):
            for name in filenames:
                p = Path(dirpath) / name
                if p.suffix in source_exts:
                    all_source_files.add(p.relative_to(src_root).as_posix())

        # Orphaned = on disk but not reachable from any project
        orphaned = sorted(all_source_files - reachable)

        if orphaned:
            print(f"Found {len(orphaned)} orphaned source file(s):")
            print()
            for f in orphaned:
                print(f"  {f}")
        else:
            print("No orphaned source files found.")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_show_include_chain(args):
    """Show the chain of includes between two source files."""
    try:
        repo_root = Path.cwd()
        src_root = repo_root / 'src'

        if not src_root.is_dir():
            print("Error: No ./src directory found", file=sys.stderr)
            sys.exit(1)

        from_file = (src_root / args.from_file).resolve()
        to_file = (src_root / args.to_file).resolve()

        if not from_file.is_file():
            print(f"Error: File not found: {args.from_file}", file=sys.stderr)
            sys.exit(1)
        if not to_file.is_file():
            print(f"Error: File not found: {args.to_file}", file=sys.stderr)
            sys.exit(1)

        cache_path = repo_root / CACHE_FILENAME
        cache = scan(src_root, cache_path, refresh=args.refresh)
        dep_graph = build_dependency_graph(cache)

        from_rel = from_file.relative_to(src_root).as_posix()
        to_rel = to_file.relative_to(src_root).as_posix()

        chain = find_include_chain(dep_graph, from_rel, to_rel)

        if chain is None:
            print(f"No include chain found from {args.from_file} to {args.to_file}")
            sys.exit(1)
        else:
            for i, step in enumerate(chain):
                indent = "  " * i
                print(f"{indent}{step}")

    except (ValueError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_fix_includes(args):
    """Fix includes to use source-root-relative paths."""
    try:
        src_root = Path.cwd() / 'src'
        if not src_root.is_dir():
            print("Error: No ./src directory found", file=sys.stderr)
            sys.exit(1)

        count = fix_includes(src_root)
        if count:
            print(f"Fixed includes in {count} file(s)")
        else:
            print("No includes needed fixing")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        prog='tybuild',
        description='Custom build system for C++ projects'
    )
    subparsers = parser.add_subparsers(dest='command', help='Commands')

    # Build command
    parser_build = subparsers.add_parser('build', help='Build a target')
    parser_build.add_argument('target', help='What to build')
    parser_build.add_argument('--clean', action='store_true', help='Clean before building')
    parser_build.set_defaults(func=cmd_build)

    # Dependencies command
    parser_deps = subparsers.add_parser('deps', help='List .cpp file dependencies')
    parser_deps.add_argument('start', help='Starting .cpp or .h file (relative or absolute path)')
    parser_deps.add_argument('--refresh', action='store_true',
                            help='Rebuild dependency cache from scratch')
    parser_deps.set_defaults(func=cmd_deps)

    # List command
    parser_list = subparsers.add_parser('list', help='List all discovered projects')
    parser_list.set_defaults(func=cmd_list)

    # Generate command
    parser_generate = subparsers.add_parser('generate', help='Generate Visual Studio project and solution files')
    parser_generate.add_argument('--force', action='store_true',
                                help='Force regeneration of all files, ignoring cache')
    parser_generate.set_defaults(func=cmd_generate)

    # Test project generation command
    parser_test_prj = subparsers.add_parser('test-prj', help='Test project generation from template')
    parser_test_prj.set_defaults(func=cmd_test_prj)

    # Generate CMake command
    parser_generate_cmake = subparsers.add_parser('generate-cmake', help='Generate CMake file with project information')
    parser_generate_cmake.set_defaults(func=cmd_generate_cmake)

    # Orphaned source files command
    parser_orphaned = subparsers.add_parser('orphaned', help='Find source files not referenced by any project')
    parser_orphaned.add_argument('--refresh', action='store_true',
                                help='Rebuild dependency cache from scratch')
    parser_orphaned.set_defaults(func=cmd_orphaned)

    # Show include chain command
    parser_chain = subparsers.add_parser('show-include-chain', help='Show the include chain between two source files')
    parser_chain.add_argument('from_file', help='Starting source file (relative to src/)')
    parser_chain.add_argument('to_file', help='Target source file (relative to src/)')
    parser_chain.add_argument('--refresh', action='store_true',
                              help='Rebuild dependency cache from scratch')
    parser_chain.set_defaults(func=cmd_show_include_chain)

    # Fix includes command
    parser_fix_includes = subparsers.add_parser('fix-includes', help='Fix includes to use source-root-relative paths')
    parser_fix_includes.set_defaults(func=cmd_fix_includes)

    # Source files moved command
    parser_moved = subparsers.add_parser('source-files-moved',
                                         help='Detect moved/renamed files and update includes')
    parser_moved.add_argument('--commit', action='store_true',
                              help='Commit the changes to git')
    parser_moved.add_argument('--push', action='store_true',
                              help='Commit and push (implies --commit)')
    parser_moved.set_defaults(func=cmd_source_files_moved)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    args.func(args)
