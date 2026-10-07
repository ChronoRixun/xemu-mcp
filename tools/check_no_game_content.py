"""V-TBD: inexpensive source-only Git-index guard (number assigned at merge)."""

import argparse
import re
import subprocess
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
ALLOWED_ROOTS = {'README.md', 'SPEC.md', '.gitignore', 'pyproject.toml'}
SOURCE_DIRS = {'xemu_mcp', 'tests', 'scripts', 'tools'}


def violations(name, data):
    path = PurePosixPath(name)
    errors = []
    allowed = name in ALLOWED_ROOTS or (path.parts[0] in SOURCE_DIRS and path.suffix == '.py')
    if not allowed:
        errors.append('not an allowed source/documentation path')
    if len(data) > 256 * 1024 or b'\0' in data:
        errors.append('binary or oversized file')
    try:
        text = data.decode('utf-8-sig')
    except UnicodeError:
        errors.append('not UTF-8 source text')
        return errors
    private_path = (r'(?i)(?:[c-z]:[/\\](?:users|projects|tools|games|safe)[/\\]|'
                    + '/' + r'home/[^/\s]+/)')
    if re.search(private_path, text):
        errors.append('private machine path')
    return errors


def self_test():
    assert not violations('xemu_mcp/example.py', b'print("invented")\n')
    assert violations('console.iso', b'not even a real disc')
    assert violations('tests/example.py', b'\x00')
    assert violations('tests/example.py', b'\xff')
    private = 'C:' + '/' + 'Users/' + 'Invented/'
    assert violations('README.md', private.encode())
    assert violations('_local/evidence.json', b'{}')
    print('Content guard self-test: PASS')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--all', action='store_true', help='Check every file in the Git index')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        self_test()
    if args.all or not args.self_test:
        names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
        count = 0
        errors = []
        for name in filter(None, names):
            count += 1
            # Inspect the exact staged blob, not a different working tree file.
            data = subprocess.check_output(['git', 'show', ':' + name], cwd=ROOT)
            errors.extend(f'{name}: {error}' for error in violations(name, data))
        if not count:
            raise SystemExit('No indexed files; stage the intended source files before checking')
        if errors:
            raise SystemExit('\n'.join(errors))
        print(f'Content guard: PASS ({count} indexed source files)')


if __name__ == '__main__':
    main()
