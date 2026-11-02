#!/usr/bin/env bash
# Read-only health check: prints a short report to copy and paste when asking for help.
#   ./doctor.sh          (sudo ./doctor.sh also reads the system journal if your user can't)
# It never changes anything. See doctor.py for the checks.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
exec python3 -B "$HERE/doctor.py" "$@"   # -B: don't even write .pyc files
