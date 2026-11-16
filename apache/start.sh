#!/usr/bin/env bash
# mailtestenv-apache. /state (bind mount testenv/.state/apache) holds work/, prepared on the host by
# apache/role.env: work/mail is a hard-link tree of datasets/apache/.cache/maildir (fs-layout Maildir of user
# `apache`, files uid 1000), work/STAMP the dataset stamp, work/index Dovecot's indexes (created here).
# Dovecot runs as uid/gid 1000, the owner of the tree, so moves, flags, expunges and Junk work and the host
# can delete everything; `testenv.sh reset apache` deletes work/ and the next start links it again.
set -euo pipefail
SRC=/opt/testenv/apache
[[ -d /state/work/mail && -f /state/work/STAMP ]] || { echo "apache: /state/work not prepared by testenv.sh" >&2; exit 1; }
[[ $(tr ' ' : < /state/work/STAMP) == "$APACHE_STAMP" ]] || { echo "apache: work copy stamp mismatch" >&2; exit 1; }
mkdir -p /tmp/dovecot && chown testenv:testenv /tmp/dovecot
install -d -o 1000 -g 1000 /state/work/index
dovecot -c "$SRC/dovecot.conf"
for _ in $(seq 100); do (exec 3<>/dev/tcp/127.0.0.1/14143) 2>/dev/null && break; sleep 0.1; done
(exec 3<>/dev/tcp/127.0.0.1/14143) 2>/dev/null || { cat /tmp/dovecot/dovecot.log; exit 1; }
echo "dataset stamp $APACHE_STAMP: $(find /state/work/mail -type d -name cur | wc -l) folders"
echo "mailtestenv-apache ready"
exec tail -F /tmp/dovecot/dovecot.log
