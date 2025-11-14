import argparse
import sys
from pathlib import Path
from tybuild.dependencies import get_cpp_dependencies
from tybuild.projects import discover_projects
from tybuild.vs_templates import generate_project_guid, generate_solution, generate_project_from_template
from tybuild.build import generate_build_files

# from tybuild.clean import Clean


def cmd_deps(args):
    """List .cpp file dependencies for a given source file."""
    try:
        root = Path(args.root).resolve()
        start_file = Path(args.start).resolve()

        deps = get_cpp_dependencies(root, root, start_file, refresh=args.refresh)

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
        base_path = Path(args.root).resolve() if args.root else None
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
        base_path = Path(args.root).resolve() if args.root else None
        generate_build_files(base_path, force=args.force)

    except (RuntimeError, FileNotFoundError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(2)


def cmd_test_sln(args):
    """Test solution generation."""
    try:

        # Paths relative to current working directory
        output_sln_path = Path('./build_template/Test.sln').resolve()

        # Discover projects
        projects = discover_projects()
        if not projects:
            print("No projects found in ./src/project", file=sys.stderr)
            sys.exit(1)

        print(f"Discovered {len(projects)} project(s):")
        for project in projects:
            print(f"  {project.type}/{project.name}")
        print()

        # Build list of projects to add
        projects_to_add = []
        for project in projects:
            # Generate deterministic GUID
            guid = generate_project_guid(project.type, project.name)

            # Project name (use as-is)
            project_name = project.name

            # Relative path to vcxproj (same directory as solution)
            vcxproj_rel_path = f"{project_name}.vcxproj"

            projects_to_add.append((project_name, guid, vcxproj_rel_path, project.type))
            print(f"Will add: {project_name} ({project.type}) - GUID: {guid}")

        print()
        print(f"Output: {output_sln_path}")

        generate_solution(
            output_sln_path,
            'C0159493-A465-32B1-8E61-5EB18BF6BD74',
            '5C330799-6FA6-33C3-B12C-755A9CA12672',
            '46BE4EB3-B0FD-3982-8000-AE0905052172',
            [
                ('Server', '954D3659-7E49-38DA-AAF3-DE9306D58F9B'),
                ('Client', '19EF89DE-8F64-33EA-8F28-40499A66EA07'),
            ]
        )
    
        print(f"Solution generated successfully: {output_sln_path}")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)

def cmd_test_prj(args):
    """Test project generation from template."""
    try:

        # Paths relative to current working directory
        template_path = Path('./build_template/').resolve()
        output_path = Path('./build_template/').resolve()

        src_root = Path('./src/').resolve()
        start_file = Path('./src/project/sdl3/Client.cpp').resolve()

        deps = get_cpp_dependencies(src_root, src_root, start_file)

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
    parser_deps.add_argument('root', help='Root directory containing the source tree')
    parser_deps.add_argument('start', help='Starting .cpp or .h file (relative to root or absolute)')
    parser_deps.add_argument('--refresh', action='store_true',
                            help='Rebuild dependency cache from scratch')
    parser_deps.set_defaults(func=cmd_deps)

    # List command
    parser_list = subparsers.add_parser('list', help='List all discovered projects')
    parser_list.add_argument('--root', default=None,
                           help='Root directory to search from (default: current directory)')
    parser_list.set_defaults(func=cmd_list)

    # Generate command
    parser_generate = subparsers.add_parser('generate', help='Generate Visual Studio project and solution files')
    parser_generate.add_argument('--root', default=None,
                                help='Root directory (default: current directory)')
    parser_generate.add_argument('--force', action='store_true',
                                help='Force regeneration of all files, ignoring cache')
    parser_generate.set_defaults(func=cmd_generate)

    # Test solution generation command
    parser_test_sln = subparsers.add_parser('test-sln', help='Test solution generation')
    parser_test_sln.set_defaults(func=cmd_test_sln)

    # Test project generation command
    parser_test_prj = subparsers.add_parser('test-prj', help='Test project generation from template')
    parser_test_prj.set_defaults(func=cmd_test_prj)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    args.func(args)
