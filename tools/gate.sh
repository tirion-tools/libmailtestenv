#!/usr/bin/env bash
# shellcheck disable=SC2317 # the jobs are called by name
# The local gate, on the host (Docker and Python 3 needed): tools/gate.sh [JOB...]
#   static    shellcheck, shfmt + Python syntax, unit tests and the licence check, in the pinned gate container
#             (tools/container.sh tools/static.sh)
#   selftest  regression tests of the Gmail stand-in in a throwaway container (testenv.sh selftest)
#   smoke     up / env --json / client checks / down of the mail role as a private instance on host ports + 30000
#             (tools/smoke.py)
# Exit status 1 when any job fails; a summary of every job is printed at the end.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
JOBS=("$@")
[[ ${#JOBS[@]} -gt 0 ]] || JOBS=(static selftest smoke)

static() { tools/container.sh tools/static.sh; }
selftest() { ./testenv.sh selftest; }
smoke() { python3 tools/smoke.py; }

declare -A RESULT
fail=0
echo "gate: $(git rev-parse HEAD 2>/dev/null || echo 'no git') $(date -u +%Y-%m-%dT%H:%M:%SZ)"
for j in "${JOBS[@]}"; do
  echo "=== $j"
  start=$SECONDS
  if "$j"; then RESULT[$j]="PASS $((SECONDS - start))s"; else
    RESULT[$j]="FAIL $((SECONDS - start))s"
    fail=1
  fi
done
echo "=== summary"
for j in "${JOBS[@]}"; do echo "$j: ${RESULT[$j]}"; done
exit $fail
