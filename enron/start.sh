#!/usr/bin/env bash
# mailtestenv-enron. /state (bind mount testenv/.state/enron) holds:
#   pristine/  converted once from the pinned tarball (/dl/enron.tar.gz, read-only), then read-only:
#              mail/<owner>/Maildir, users, MANIFEST.tsv, MANIFEST.sha256, STAMP
#   work/      the copy Dovecot serves (moves, flags, expunges, Junk allowed); `testenv.sh reset enron`
#              deletes it and the next start copies pristine/ again, mtimes (INTERNALDATE) preserved.
# `start.sh --reload-tls` (testenv.sh, after a mail start) rebuilds Postfix's chain file from /tls and reloads Postfix.
set -euo pipefail
SRC=/opt/testenv/enron
enron_pem() { (umask 077 && cat /tls/enron.key /tls/enron.crt > /etc/postfix/pki/enron.pem.new) &&
  mv -f /etc/postfix/pki/enron.pem.new /etc/postfix/pki/enron.pem; }
[[ ${1:-} == --reload-tls ]] && { enron_pem && postfix reload 2>/dev/null; exit; }
STAMP="$ENRON_SHA256 $(sha256sum "$SRC/convert.py" | cut -c1-16)"
mkdir -p /tmp/dovecot && chown testenv:testenv /tmp/dovecot

if [[ ! -f /state/pristine/STAMP || $(cat /state/pristine/STAMP) != "$STAMP" ]]; then
  echo "verifying and converting the Enron corpus (once)"
  echo "$ENRON_SHA256  /dl/enron.tar.gz" | sha256sum -c --quiet
  rm -rf /state/pristine /state/pristine.tmp /state/work
  mkdir -p /state/pristine.tmp
  python3 "$SRC/convert.py" /dl/enron.tar.gz /state/pristine.tmp
  echo "$STAMP" > /state/pristine.tmp/STAMP
  chown -R testenv:testenv /state/pristine.tmp
  chmod -R a-w /state/pristine.tmp/mail
  mv /state/pristine.tmp /state/pristine
fi
if [[ ! -d /state/work/mail ]]; then
  echo "copying the pristine snapshot to the working copy"
  rm -rf /state/work && mkdir -p /state/work
  rsync -a --chmod=Du+w,Fu+w /state/pristine/mail/ /state/work/mail/
  chown -R testenv:testenv /state/work
fi
[[ -r /tls/enron.crt && -r /tls/enron.key ]] ||
  { echo "no TLS certificate in /tls: start the mail role first (it issues enron's certificate)" >&2; exit 1; }
install -d -m 0755 /etc/postfix/pki
enron_pem  # also run by testenv.sh after a mail start re-issues the certificate
install -m 0644 "$SRC/main.cf" /etc/postfix/main.cf
install -m 0644 "$SRC/master.cf" /etc/postfix/master.cf
touch /var/log/postfix.log
dovecot -c "$SRC/dovecot.conf"
postfix check
postfix start 2>/dev/null
up() { local p; for p in 13143 13993 587 465; do (exec 3<>/dev/tcp/127.0.0.1/$p) 2>/dev/null || return 1; done; }
for _ in $(seq 100); do up && break; sleep 0.1; done
up || { cat /tmp/dovecot/dovecot.log /var/log/postfix.log; exit 1; }
echo "manifest $(cat /state/pristine/MANIFEST.sha256): $(tail -n 1 /state/pristine/MANIFEST.tsv)"
echo "mailtestenv-enron ready"
exec tail -F /tmp/dovecot/dovecot.log /var/log/postfix.log
