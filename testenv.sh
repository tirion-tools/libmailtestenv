#!/usr/bin/env bash
# mailtestenv servers: one command to start, stop and reset them (see README.md).
#
#   testenv/testenv.sh up [ROLE...]      build the image if needed, start roles (default: mail gmail)
#   testenv/testenv.sh down [ROLE...]    stop and remove role containers (default: all running)
#   testenv/testenv.sh reset ROLE        discard the role's working copy, restore its pristine snapshot
#   testenv/testenv.sh status            containers, ports, state directories
#   testenv/testenv.sh env               endpoints and credential files for clients
#   testenv/testenv.sh selftest          regression tests of the stand-ins (throwaway container, no shared state)
#
# Roles, containers and loopback ports (never 143 / 5232):
#   mail   mailtestenv-mail    IMAP 127.0.0.1:11143, IMAPS :11993, SMTPS :11465, submission :11587
#   gmail  mailtestenv-gmail   Gmail stand-in IMAP (plain, loopback) 127.0.0.1:12143
#   enron  mailtestenv-enron   Enron corpus IMAP 127.0.0.1:13143 (plain or STARTTLS), IMAPS :13993, submission :13587,
#                                   SMTPS :13465 (mail role's CA; submitted mail is discarded)
#   apache mailtestenv-apache  ASF list dataset IMAP (plain, loopback) 127.0.0.1:14143
# State lives in MAILTESTENV_STATE/<role>/, default the main tree's testenv/.state (git-ignored); containers of
# tools/container/run.sh see it read-only at <source>/testenv/.state. The containers are workstation-wide, so up, down
# and reset run from the main tree only, whatever the state directory.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
common=$(git -C "$HERE" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || common=$(dirname "$HERE")/.git
MAIN=$(cd "$(dirname "$common")" && pwd -P)
STATE=${MAILTESTENV_STATE:-$MAIN/testenv/.state}
INPUTS_HASH=$(cd "$HERE" && find Dockerfile mail gmail enron apache -type f -not -path '*/__pycache__/*' -print0 | sort -z | xargs -0 cat | sha256sum | cut -c1-16)
IMAGE=mailtestenv:$INPUTS_HASH

# role -> "container|docker publish args|start command"
declare -A ROLE=(
  [mail]="mailtestenv-mail|-p 127.0.0.1:11143:143 -p 127.0.0.1:11993:993 -p 127.0.0.1:11465:465 -p 127.0.0.1:11587:587|/opt/testenv/mail/start.sh"
  [gmail]="mailtestenv-gmail|-p 127.0.0.1:12143:12143|/opt/testenv/gmail/start.sh"
  [enron]="mailtestenv-enron|-p 127.0.0.1:13143:13143 -p 127.0.0.1:13993:13993 -p 127.0.0.1:13587:587 -p 127.0.0.1:13465:465|/opt/testenv/enron/start.sh"
  [apache]="mailtestenv-apache|-p 127.0.0.1:14143:14143|/opt/testenv/apache/start.sh"
)
DEFAULT_ROLES=(mail gmail)

die() { echo "testenv: $*" >&2; exit 1; }
field() { local IFS='|'; read -r -a f <<<"${ROLE[$1]:?unknown role $1}"; echo "${f[$2]}"; }

guard_names() {
  # Hard rule: host ports 143 and 5232 are never used.
  local r c
  for r in "${!ROLE[@]}"; do
    c=$(field "$r" 0)
    [[ $c == mailtestenv-* ]] || die "role $r: container name $c must start with mailtestenv-"
    [[ $(field "$r" 1) != *":143:"* && $(field "$r" 1) != *":5232:"* && $(field "$r" 1) != *"127.0.0.1:143:"* ]] ||
      die "role $r publishes a forbidden host port"
  done
}

build() {
  docker image inspect "$IMAGE" >/dev/null 2>&1 && return
  echo "testenv: building $IMAGE" >&2
  docker build -t "$IMAGE" "$HERE" >&2
}

role_extra_args() { # role -> extra docker run args (dataset mounts)
  case $1 in
    enron) source "$HERE/enron/role.env"; enron_docker_args ;;
    apache) source "$HERE/apache/role.env"; apache_docker_args ;;
  esac
}

up() {
  guard_names
  build
  local r c
  for r in "$@"; do
    c=$(field "$r" 0)
    if [[ -n $(docker ps -q --filter "name=^${c}$") ]]; then echo "$r: already running ($c)"; continue; fi
    docker rm -f "$c" >/dev/null 2>&1 || true
    mkdir -p "$STATE/$r"
    # shellcheck disable=SC2046
    docker run -d --name "$c" --label mailtestenv=1 --hostname "$r.mailtestenv.test" \
      $(field "$r" 1) -v "$STATE/$r:/state" $(role_extra_args "$r") "$IMAGE" $(field "$r" 2) >/dev/null
    echo -n "$r: starting $c "
    for _ in $(seq 1800); do
      if docker logs "$c" 2>&1 | grep -q "mailtestenv-$r ready"; then echo "ready"; [[ $r == mail ]] && reload_enron_tls; continue 2; fi
      [[ -n $(docker ps -q --filter "name=^${c}$") ]] || { echo; docker logs "$c" | tail -n 40; die "$r exited"; }
      sleep 1
    done
    echo; die "$r did not become ready within 30 min"
  done
}

# A mail start issues a new CA and enron certificate (in the directory enron mounts at /tls): a running enron reloads.
reload_enron_tls() {
  local c
  c=$(field enron 0)
  [[ -n $(docker ps -q --filter "name=^${c}$") ]] || return 0
  docker exec "$c" doveadm -c /opt/testenv/enron/dovecot.conf reload >/dev/null 2>&1 &&
    docker exec "$c" /opt/testenv/enron/start.sh --reload-tls >/dev/null 2>&1 &&
    echo "enron: reloaded with the new mail CA" || echo "enron: reload failed; run testenv.sh down enron && testenv.sh up enron"
}

down() {
  local r c
  for r in "$@"; do
    c=$(field "$r" 0)
    docker rm -f "$c" >/dev/null 2>&1 && echo "$r: stopped" || true
  done
}

reset() {
  local r=${1:?usage: testenv.sh reset ROLE}
  [[ $r == mail ]] && { down mail; up mail; return; }  # mail regenerates everything at start
  local was_running=0
  [[ -n $(docker ps -q --filter "name=^$(field "$r" 0)$") ]] && was_running=1
  down "$r"
  build
  # Files belong to container users: delete through the image, never from the host.
  docker run --rm -v "$STATE/$r:/state" "$IMAGE" rm -rf /state/work
  echo "$r: working copy discarded; the next start restores the pristine state"
  ((was_running)) && up "$r" || true
}

status() {
  docker ps -a --filter label=mailtestenv=1 --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
  du -sh "$STATE"/* 2>/dev/null || true
  mail_expiry
}

# The mail container re-mints its OAuth tokens hourly (24 h lifetime), so under 12 h left means the refresher stopped
# (or the container predates it): restart with `testenv.sh reset mail`. Its certificates last 7 days from the start.
mail_expiry() {
  local secrets=$STATE/mail/secrets.tsv crt=$STATE/mail/pki/mail.crt left
  [[ -n $(docker ps -q --filter "name=^$(field mail 0)$") && -r $secrets ]] || return 0
  left=$(awk -F'\t' '$2 == "oauth2-access-token" {print $4}' "$secrets" | python3 -c '
import base64, json, sys, time
body = sys.stdin.read().split(".")[1]
print(json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))["exp"] - int(time.time()))') ||
    { echo "mail: WARNING: no readable OAuth token in $secrets"; return 0; }
  if ((left < 12 * 3600)); then
    echo "mail: WARNING: OAuth token $( ((left > 0)) && echo "expires in $((left / 60)) min" || echo expired); run testenv.sh reset mail"
  else
    echo "mail: OAuth token valid for $((left / 3600)) h"
  fi
  [[ ! -r $crt ]] || openssl x509 -checkend 86400 -noout -in "$crt" >/dev/null ||
    echo "mail: WARNING: server certificate expires within 24 h ($(openssl x509 -enddate -noout -in "$crt")); run testenv.sh reset mail"
}

env_info() {
  cat <<EOF
mail  : IMAP 127.0.0.1:11143 (STARTTLS) / IMAPS 127.0.0.1:11993, SMTPS 127.0.0.1:11465, submission 127.0.0.1:11587
        SNI mail.mailtestenv.test; trust $STATE/mail/pki/ca.crt; credentials $STATE/mail/secrets.tsv
gmail : IMAP 127.0.0.1:12143 (plain), users gmail gmailhidden gmailcapped gmailcap5k, password "pass"
enron : IMAP 127.0.0.1:13143 (plain or STARTTLS) / IMAPS 127.0.0.1:13993, submission 127.0.0.1:13587 (STARTTLS) /
        SMTPS 127.0.0.1:13465 (accepted mail is discarded), SNI enron.mailtestenv.test or mail.mailtestenv.test,
        trust $STATE/mail/pki/ca.crt; one user per Enron mailbox owner, password "pass";
        users and counts in $STATE/enron/pristine/MANIFEST.tsv
apache: IMAP 127.0.0.1:14143 (plain), user "apache", password "pass"; one folder per ASF list
        (574,415 messages, 28 folders; counts in datasets/apache/STATS.tsv)
EOF
}

selftest() {
  build
  # Own Dovecot and proxy in a throwaway container: no network, state on tmpfs, nothing shared touched.
  docker run --rm --network none --tmpfs /state --tmpfs /mailstore -v "$HERE/tests:/opt/testenv-tests:ro" "$IMAGE" \
    timeout 300 python3 /opt/testenv-tests/gmail_proxy_test.py
}

cmd=${1:-help}; shift || true
case $cmd in
  up | down | reset)
    [[ $HERE == "$MAIN/testenv" ]] ||
      die "$cmd: the servers (workstation-wide containers) belong to the main tree; run $MAIN/testenv/testenv.sh $cmd" ;;
esac
case $cmd in
  up | down | reset | status | env) echo "testenv: state $STATE" >&2 ;;
esac
case $cmd in
  up) (($#)) || set -- "${DEFAULT_ROLES[@]}"; up "$@" ;;
  down) (($#)) || set -- "${!ROLE[@]}"; down "$@" ;;
  reset) reset "$@" ;;
  status) status ;;
  env) env_info ;;
  selftest) selftest ;;
  *) sed -n '2,18p' "$0" ;;
esac
