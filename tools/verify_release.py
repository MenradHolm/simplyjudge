"""Production build gate. Run in the exact server environment before deploying."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    os.chdir(ROOT)
    for command in (
        ['tools/security_scan.py', '--source'],
        ['-m', 'pip_audit', '-r', 'requirements.lock'],
        ['manage.py', 'check', '--deploy', '--fail-level', 'WARNING'],
        ['manage.py', 'collectstatic', '--noinput', '--clear', '--ignore', '*.map'],
        ['tools/security_scan.py', '--artifacts', 'staticfiles'],
    ):
        subprocess.run([sys.executable, *command], check=True)
    print('Production static build passed all release checks.')


if __name__ == '__main__':
    main()
