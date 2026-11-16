#!/usr/bin/env bash
# mailtestenv-gmail. /state (bind mount) holds:
#   pristine/   seeded once: mailstore/, gmail-state.json, manifest.json, STAMP (read-only afterwards)
#   work/       the mailstore Dovecot serves; recreated from pristine/ at every start
# Dovecot listens on 127.0.0.1:1143 inside the container; the proxy on 127.0.0.1:1993; socat
# exposes the proxy as 0.0.0.0:12143, which the host publishes on 127.0.0.1 only.
set -euo pipefail
SRC=/opt/testenv/gmail
STAMP=$(cat "$SRC"/*.py "$SRC"/dovecot.conf | sha256sum | cut -c1-16)
mkdir -p /tmp/dovecot /state
chown testenv:testenv /tmp/dovecot
start_dovecot() { dovecot -c "$SRC/dovecot.conf"; for _ in $(seq 100); do (exec 3<>/dev/tcp/127.0.0.1/1143) 2>/dev/null && return; sleep 0.1; done; echo "dovecot did not start" >&2; exit 1; }

if [[ ! -f /state/pristine/STAMP || $(cat /state/pristine/STAMP) != "$STAMP" ]]; then
  echo "seeding Gmail stand-in mailboxes (once)"
  rm -rf /state/pristine /mailstore && install -d -o testenv -g testenv /mailstore /state/pristine
  start_dovecot
  python3 "$SRC/seed.py" gmail /state/pristine/gmail-state.json /state/pristine/manifest.json
  dovecot -c "$SRC/dovecot.conf" stop; sleep 1
  cp -a /mailstore /state/pristine/mailstore
  echo "$STAMP" > /state/pristine/STAMP
  rm -rf /mailstore
fi
# The proxy keeps its label model in memory, loaded from gmail-state.json at start: serving a used work/ after a restart
# would pair the seeded model with a changed mailstore, so every start serves a fresh copy of pristine/ (about 1 GB).
rm -rf /state/work && mkdir -p /state/work
cp -a /state/pristine/mailstore /state/work/mailstore
cp /state/pristine/gmail-state.json /state/work/gmail-state.json
ln -sfn /state/work/mailstore /mailstore
start_dovecot
python3 "$SRC/gmail_proxy.py" 1993 1143 /state/work/gmail-state.json /state/work/proxy.jsonl /state/work/capabilities.json > /state/work/proxy.out 2>&1 &
proxy=$!
for _ in $(seq 100); do grep -q ready /state/work/proxy.out 2>/dev/null && break; sleep 0.1; done
grep -q ready /state/work/proxy.out || { cat /state/work/proxy.out; exit 1; }
socat TCP-LISTEN:12143,fork,reuseaddr TCP:127.0.0.1:1993 &
echo "mailtestenv-gmail ready: users gmail gmailhidden gmailcapped gmailcap5k, password 'pass'"
tail -F /tmp/dovecot/dovecot.log &
# The proxy holds the label model: when it exits (killed by its process bounds, or crashed) the container stops with it.
status=0
wait "$proxy" || status=$?
echo "gmail_proxy.py exited with status $status; last lines of /state/work/proxy.out:" >&2
tail -n 20 /state/work/proxy.out >&2
exit 1
