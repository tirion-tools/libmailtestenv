#!/usr/bin/env bash
# shellcheck disable=SC2317 # the jobs are called by name
# The gate's static jobs, run in the pinned gate container (tools/container.sh tools/static.sh [JOB...]):
#   lint        ShellCheck over every shell script, sourced role files included
#   format      shfmt (2-space indent, indented case items) and a Python syntax check
#   unit        the Python unit tests (tests/test_*.py)
#   licence     tools/licence_check.py
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
JOBS=("$@")
[[ ${#JOBS[@]} -gt 0 ]] || JOBS=(lint format unit licence)
mapfile -t SHELL_FILES < <(find . -path ./.git -prune -o -path ./.state -prune -o -path ./.cache -prune -o \
  -type f \( -name '*.sh' -o -name 'role.env' \) -print | sort)
mapfile -t PY_FILES < <(find . -path ./.git -prune -o -path ./.state -prune -o -path ./.cache -prune -o \
  -type f -name '*.py' -print | sort)

lint_job() { shellcheck -x -s bash "${SHELL_FILES[@]}"; }
format_job() {
  shfmt -d -i 2 -ci -ln bash "${SHELL_FILES[@]}" &&
    python3 -c 'import ast, sys
for f in sys.argv[1:]:
    ast.parse(open(f, encoding="utf-8").read(), f)' "${PY_FILES[@]}"
}
unit_job() { python3 -m unittest discover -s tests -v; }
licence_job() { python3 tools/licence_check.py; }

declare -A RESULT
fail=0
for j in "${JOBS[@]}"; do
  echo "=== $j"
  start=$SECONDS
  if "${j}_job"; then RESULT[$j]="PASS $((SECONDS - start))s"; else
    RESULT[$j]="FAIL $((SECONDS - start))s"
    fail=1
  fi
done
echo "=== static summary"
for j in "${JOBS[@]}"; do echo "$j: ${RESULT[$j]}"; done
exit $fail
