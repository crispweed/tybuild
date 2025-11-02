import argparse
import sys
from pathlib import Path
from tybuild.dependencies import get_cpp_dependencies

# from tybuild.build import RunBuild
# from tybuild.clean import Clean


def cmd_deps(args):
    """List .cpp file dependencies for a given source file."""
    try:
        root = Path(args.root).resolve()
        start_file = Path(args.start).resolve()

        deps = get_cpp_dependencies(root, start_file, refresh=args.refresh)

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

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    args.func(args)
