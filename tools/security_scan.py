"""Fail a build on recognizable secrets or private files; never print matches.

Run with --source for repository files, or --artifacts PATH for the exact public
directory. Environment secret values are checked byte-for-byte in artifacts.
Heuristics complement review and credential rotation; they cannot prove absence.
"""
import argparse
import base64
import gzip
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    'insecure Django signing key': rb'django-insecure-[^\s"\x27]{20,}',
    'private key': rb'-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----',
    'provider credential': rb'\b(?:[sr]k_(?:live|test)_[A-Za-z0-9]{16,}|whsec_[A-Za-z0-9]{16,}|AKIA[A-Z0-9]{16}|ASIA[A-Z0-9]{16}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AIza[A-Za-z0-9_-]{30,}|xox[baprs]-[A-Za-z0-9-]{20,})\b',
    'credential URL': rb'(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqps?|cloudinary|https?)://[^\s/<>"\x27]+:[^\s/<>"\x27]+@',
    'JWT': rb'\beyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b',
    'literal secret': rb'(?i)(?:secret_key|api_secret|api_key|access_token|auth_token|client_secret|password)\s*[=:]\s*["\x27][^"\x27\r\n]{20,}["\x27]',
}
COMPILED = {name: re.compile(pattern) for name, pattern in PATTERNS.items()}
SECRET_NAME = re.compile(r'(?:SECRET|PASSWORD|TOKEN|PRIVATE_KEY|API_KEY|DATABASE_URL|CLOUDINARY_URL)', re.I)


def findings(path, data, artifacts=False, secret_values=()):
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        data = data.decode('utf-16').encode()
    issues = [name for name, pattern in COMPILED.items() if pattern.search(data)]
    if artifacts:
        lower = path.name.lower().removesuffix('.gz')
        if (lower.endswith(('.map', '.py', '.pyc', '.pem', '.key', '.p12', '.pfx', '.sqlite3', '.db', '.bak', '.zip', '.tar', '.tgz', '.7z', '.br'))
                or lower.startswith('.env') or '.sqlite3' in lower
                or any(part in {'.git', 'venv', 'core_config', 'outputs', 'tmp'} for part in path.parts)):
            issues.append('private or uninspectable public artifact')
        if re.search(rb'sourceMappingURL\s*=\s*data:', data):
            issues.append('inline source map')
        if any(variant in data for value in secret_values for variant in (
            value, base64.b64encode(value), quote(value.decode(), safe='').encode(),
        )):
            issues.append('server environment secret')
    return issues


def scan(paths, artifacts=False):
    values = [value.encode() for name, value in os.environ.items()
              if SECRET_NAME.search(name) and len(value) >= 8]
    count = 0
    failures = []
    for path in paths:
        count += 1
        if path.is_symlink():
            failures.append((path, ['symbolic link']))
            continue
        try:
            data = path.read_bytes()
            if path.suffix == '.gz':
                data = gzip.decompress(data)
            issues = findings(path, data, artifacts, values)
        except (OSError, EOFError, ValueError):
            issues = ['cannot inspect file']
        if issues:
            failures.append((path, issues))
    for path, issues in failures:
        print(f'{path}: {", ".join(issues)}')
    print(f'Inspected {count} files; {len(failures)} failed.')
    return bool(failures)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--source', action='store_true')
    mode.add_argument('--history', action='store_true')
    mode.add_argument('--artifacts', type=Path)
    args = parser.parse_args()
    if args.history:
        history = subprocess.check_output(['git', 'log', '--all', '--format=commit:%h', '--patch'], cwd=ROOT)
        commit = filename = ''
        hits = set()
        for line in history.splitlines():
            if line.startswith(b'commit:'):
                commit = line.decode()
            if line.startswith(b'+++ b/'):
                filename = line[6:].decode(errors='replace')
            if line.startswith(b'+') and any(pattern.search(line[1:]) for pattern in COMPILED.values()):
                hits.add((commit, filename))
        for commit, filename in sorted(hits):
            print(f'{commit} {filename}: historical credential pattern (review/rotate)')
        print(f'{len(hits)} historical file revisions flagged; secret values suppressed.')
        return bool(hits)
    if args.source:
        names = subprocess.check_output(
            ['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=ROOT,
        ).decode().split('\0')
        paths = [ROOT / name for name in names if name and (ROOT / name).is_file()]
    else:
        if not args.artifacts.is_dir():
            parser.error('Artifact directory must exist.')
        paths = list(args.artifacts.rglob('*'))
        paths = [path for path in paths if path.is_file() or path.is_symlink()]
        if not paths:
            parser.error('Artifact directory is empty.')
    return scan(paths, artifacts=not args.source)


if __name__ == '__main__':
    sys.exit(main())
