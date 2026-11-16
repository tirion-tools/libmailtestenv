#!/usr/bin/env bash
# Mail test servers: one command to start, stop and reset them (see README.md).
#
#   testenv.sh up [ROLE...]      build the image if needed, start roles (default: mail gmail); writes STATE/env.json
#   testenv.sh down [ROLE...]    stop and remove role containers (default: all)
#   testenv.sh reset ROLE        discard the role's working copy, restore its pristine snapshot
#   testenv.sh purge             stop every role and delete the whole state directory (through the image)
#   testenv.sh status            containers, ports, state directories, token and certificate lifetimes
#   testenv.sh env [--json]      endpoints and credential files for clients (--json: the env document)
#   testenv.sh selftest          regression tests of the Gmail stand-in (throwaway container, no shared state)
#
# Roles, containers and loopback ports (MAILTESTENV_PORT_OFFSET is added to every host port; 143 and 5232 are refused):
#   mail   <prefix>-mail    IMAP 127.0.0.1:11143 (STARTTLS), IMAPS :11993, submissions :11465, submission :11587
#   gmail  <prefix>-gmail   Gmail stand-in IMAP (plain) 127.0.0.1:12143
#   enron  <prefix>-enron   Enron corpus IMAP 127.0.0.1:13143 (plain or STARTTLS), IMAPS :13993, submission :13587,
#                            submissions :13465 (mail role's CA; submitted mail is discarded)
#   apache <prefix>-apache  ASF list dataset IMAP (plain) 127.0.0.1:14143
# Settings (environment):
#   MAILTESTENV_STATE        state directory, default <this directory>/.state
#   MAILTESTENV_PREFIX       container name prefix, default mailtestenv (must start with "mailtestenv")
#   MAILTESTENV_PORT_OFFSET  added to every host port, default 0
#   MAILTESTENV_DENY_PORTS   further host ports that must never be published
#   MAILTESTENV_APACHE_DIR   the built ASF dataset (datasets/apache), default <this directory>/datasets/apache/.cache
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
export MAILTESTENV_STATE=${MAILTESTENV_STATE:-$HERE/.state}
export MAILTESTENV_PREFIX=${MAILTESTENV_PREFIX:-mailtestenv}
export MAILTESTENV_PORT_OFFSET=${MAILTESTENV_PORT_OFFSET:-0}
export MAILTESTENV_DENY_PORTS=${MAILTESTENV_DENY_PORTS:-}
export MAILTESTENV_APACHE_DIR=${MAILTESTENV_APACHE_DIR:-$HERE/datasets/apache/.cache}
STATE=$MAILTESTENV_STATE
PREFIX=$MAILTESTENV_PREFIX
INPUTS_HASH=$(cd "$HERE" && find Dockerfile mail gmail enron apache -type f -not -path '*/__pycache__/*' -print0 | sort -z | xargs -0 cat | sha256sum | cut -c1-16)
IMAGE=mailtestenv:$INPUTS_HASH
LABEL=mailtestenv.prefix=$PREFIX
DEFAULT_ROLES=(mail gmail)

die() {
  echo "testenv: $*" >&2
  exit 1
}
roles() { python3 "$HERE/roles.py" "$@"; }
mapfile -t ALL_ROLES < <(roles names) # also validates every setting (prefix, ports): exits 2 on a bad one
((${#ALL_ROLES[@]})) || exit 2
container() { roles container "$1"; }
running() { [[ -n $(docker ps -q --filter "name=^$(container "$1")$") ]]; }
known() {
  local r
  for r in "${ALL_ROLES[@]}"; do [[ $r == "$1" ]] && return 0; done
  die "unknown role $1 (roles: ${ALL_ROLES[*]})"
}

# A second line of defence behind roles.py: never publish host port 143 or 5232.
guard() {
  local r p
  for r in "${ALL_ROLES[@]}"; do
    p=$(roles publish "$r")
    [[ " $p" != *":143:"* && " $p" != *":5232:"* ]] || die "role $r publishes a forbidden host port"
  done
}

build() {
  docker image inspect "$IMAGE" >/dev/null 2>&1 && return
  echo "testenv: building $IMAGE" >&2
  docker build -t "$IMAGE" "$HERE" >&2
}

role_extra_args() { # role -> extra docker run args (dataset mounts)
  case $1 in
    enron)
      # shellcheck source=enron/role.env
      source "$HERE/enron/role.env"
      enron_docker_args
      ;;
    apache)
      # shellcheck source=apache/role.env
      source "$HERE/apache/role.env"
      apache_docker_args
      ;;
  esac
}

write_env() {
  mkdir -p "$STATE"
  roles env-json >"$STATE/env.json.new" && mv -f "$STATE/env.json.new" "$STATE/env.json"
}

up() {
  guard
  build
  write_env
  local r c
  for r in "$@"; do
    known "$r"
    c=$(container "$r")
    if running "$r"; then
      echo "$r: already running ($c)"
      continue
    fi
    docker rm -f "$c" >/dev/null 2>&1 || true
    mkdir -p "$STATE/$r"
    # shellcheck disable=SC2046 # word splitting of the generated docker arguments is intended
    docker run -d --name "$c" --label "$LABEL" --hostname "$r.mailtestenv.test" \
      $(roles publish "$r") -v "$STATE/$r:/state" $(role_extra_args "$r") "$IMAGE" "/opt/testenv/$r/start.sh" >/dev/null
    echo -n "$r: starting $c "
    for _ in $(seq 1800); do
      if docker logs "$c" 2>&1 | grep -q "mailtestenv-$r ready"; then
        echo "ready"
        [[ $r != mail ]] || reload_enron_tls
        continue 2
      fi
      running "$r" || {
        echo
        docker logs "$c" | tail -n 40
        die "$r exited"
      }
      sleep 1
    done
    echo
    die "$r did not become ready within 30 min"
  done
}

# A mail start issues a new CA and enron certificate (in the directory enron mounts at /tls): a running enron reloads.
reload_enron_tls() {
  running enron || return 0
  if docker exec "$(container enron)" doveadm -c /opt/testenv/enron/dovecot.conf reload >/dev/null 2>&1 &&
    docker exec "$(container enron)" /opt/testenv/enron/start.sh --reload-tls >/dev/null 2>&1; then
    echo "enron: reloaded with the new mail CA"
  else
    echo "enron: reload failed; run testenv.sh down enron && testenv.sh up enron"
  fi
}

down() {
  local r
  for r in "$@"; do
    known "$r"
    if docker rm -f "$(container "$r")" >/dev/null 2>&1; then echo "$r: stopped"; fi
  done
}

reset() {
  local r=${1:?usage: testenv.sh reset ROLE}
  known "$r"
  if [[ $r == mail ]]; then # mail regenerates everything at start
    down mail
    up mail
    return
  fi
  local was_running=0
  if running "$r"; then was_running=1; fi
  down "$r"
  build
  # Files belong to container users: delete through the image, never from the host.
  docker run --rm -v "$STATE/$r:/state" "$IMAGE" rm -rf /state/work
  echo "$r: working copy discarded; the next start restores the pristine state"
  if ((was_running)); then up "$r"; fi
}

purge() {
  down "${ALL_ROLES[@]}"
  [[ -d $STATE ]] || return 0
  build
  docker run --rm -v "$STATE:/state" "$IMAGE" find /state -mindepth 1 -delete
  rmdir "$STATE"
  echo "state $STATE deleted"
}

status() {
  docker ps -a --filter "label=$LABEL" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
  du -sh "$STATE"/* 2>/dev/null || true
  mail_expiry
}

# The mail container re-mints its OAuth tokens hourly (24 h lifetime), so under 12 h left means the refresher stopped
# (or the container predates it): restart with `testenv.sh reset mail`. Its certificates last 7 days from the start.
mail_expiry() {
  local secrets=$STATE/mail/secrets.tsv crt=$STATE/mail/pki/mail.crt left
  running mail && [[ -r $secrets ]] || return 0
  left=$(awk -F'\t' '$2 == "oauth2-access-token" {print $4}' "$secrets" | python3 -c '
import base64, json, sys, time
body = sys.stdin.read().split(".")[1]
print(json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))["exp"] - int(time.time()))') ||
    {
      echo "mail: WARNING: no readable OAuth token in $secrets"
      return 0
    }
  if ((left < 12 * 3600)); then
    echo "mail: WARNING: OAuth token $( ((left > 0)) && echo "expires in $((left / 60)) min" || echo expired); run testenv.sh reset mail"
  else
    echo "mail: OAuth token valid for $((left / 3600)) h"
  fi
  [[ ! -r $crt ]] || openssl x509 -checkend 86400 -noout -in "$crt" >/dev/null ||
    echo "mail: WARNING: server certificate expires within 24 h ($(openssl x509 -enddate -noout -in "$crt")); run testenv.sh reset mail"
}

env_info() {
  roles env-text
  echo "The same as JSON: testenv.sh env --json (up also writes it to $STATE/env.json)."
}

selftest() {
  build
  # Own Dovecot and proxy in a throwaway container: no network, state on tmpfs, nothing shared touched.
  docker run --rm --network none --tmpfs /state --tmpfs /mailstore -v "$HERE/tests:/opt/testenv-tests:ro" "$IMAGE" \
    timeout 300 python3 /opt/testenv-tests/gmail_proxy_test.py
}

cmd=${1:-help}
shift || true
case $cmd in
  up | down | reset | purge | status | env) echo "testenv: state $STATE" >&2 ;;
esac
case $cmd in
  up)
    (($#)) || set -- "${DEFAULT_ROLES[@]}"
    up "$@"
    ;;
  down)
    (($#)) || set -- "${ALL_ROLES[@]}"
    down "$@"
    ;;
  reset) reset "$@" ;;
  purge) purge ;;
  status) status ;;
  env)
    case ${1:-} in
      --json) roles env-json ;;
      "") env_info ;;
      *) die "usage: testenv.sh env [--json]" ;;
    esac
    ;;
  selftest) selftest ;;
  *) sed -n '2,23p' "$0" ;;
esac
