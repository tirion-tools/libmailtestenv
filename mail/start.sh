#!/usr/bin/env bash
# mailtestenv-mail: Dovecot 2.4.5 (IMAP 143/993) + Postfix (submission 587, submissions 465).
# Every start generates a throwaway PKI,
# an OAuth2 JWT signing key with tokens, random passwords and a synthetic corpus, and writes what a
# client needs to /state (bind mount testenv/.state/mail): ca.crt, rogue-ca.crt, secrets.tsv. The enron role's
# certificate and key, from the same CA, go to /state/private (root-only, mounted into the enron container).
# TLS everywhere; the right certificate is served only for SNI mail.mailtestenv.test (or dns-a.),
# `wrong.` otherwise; rogue.mailtestenv.test has the right name from an untrusted CA.
# Postfix delivers to mailtestenv.test only (default_transport = error).
set -euo pipefail
SRC=/opt/testenv/mail
STATE=/state
MESSAGES=${MESSAGES:-200}
mkdir -p "$STATE/pki" /etc/dovecot/pki /etc/postfix/pki /etc/dovecot/oauth2-keys/default/RS256
rm -f "$STATE"/pki/* "$STATE"/secrets.tsv
# private/ is bind-mounted into the enron container: replace its files, never the directory.
install -d -m 0700 "$STATE/private"
cd "$STATE/pki"

issue() { # name san-list ca
  openssl req -newkey rsa:2048 -nodes -subj "/CN=$1.mailtestenv.test" -keyout "$1.key" -out "$1.csr" 2>/dev/null
  printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n' "$2" > "$1.ext"
  openssl x509 -req -in "$1.csr" -CA "$3.crt" -CAkey "$3.key" -CAcreateserial -days 7 -sha256 -extfile "$1.ext" -out "$1.crt" 2>/dev/null
}
for ca in ca rogue-ca; do
  openssl req -x509 -newkey rsa:2048 -nodes -days 7 -sha256 -subj "/CN=mailtestenv ${ca}" \
    -addext 'basicConstraints=critical,CA:TRUE' -addext 'keyUsage=critical,keyCertSign,cRLSign' \
    -keyout "$ca.key" -out "$ca.crt" 2>/dev/null
done
issue mail 'DNS:mail.mailtestenv.test,DNS:dns-a.mailtestenv.test' ca
issue wrong 'DNS:wrong.mailtestenv.test' ca
issue rogue 'DNS:rogue.mailtestenv.test' rogue-ca
issue enron 'DNS:enron.mailtestenv.test,DNS:mail.mailtestenv.test' ca
install -m 0644 enron.crt "$STATE/private/enron.crt" && install -m 0600 enron.key "$STATE/private/enron.key"
rm -f enron.crt
for n in mail wrong rogue; do
  install -m 0644 "$n.crt" /etc/dovecot/pki/ && install -m 0600 "$n.key" /etc/dovecot/pki/
  cat "$n.key" "$n.crt" > "/etc/postfix/pki/$n.pem" && chmod 0600 "/etc/postfix/pki/$n.pem"
done
rm -f ./*.key ./*.csr ./*.ext ./*.srl   # the host only ever sees public certificates

openssl genrsa -out /root/jwt.key 2048 2>/dev/null
openssl rsa -in /root/jwt.key -pubout -out /etc/dovecot/oauth2-keys/default/RS256/default 2>/dev/null
chmod 0644 /etc/dovecot/oauth2-keys/default/RS256/default
# Access tokens live TOKEN_TTL seconds; write_secrets re-mints them every TOKEN_REFRESH seconds while the container
# runs, so a token read from secrets.tsv always has at least TOKEN_TTL - TOKEN_REFRESH left.
TOKEN_TTL=${TOKEN_TTL:-86400}
TOKEN_REFRESH=${TOKEN_REFRESH:-3600}
mint() { # sub [bad]
  python3 - "$1" "${2:-}" "$TOKEN_TTL" <<'PY'
import base64, json, subprocess, sys, time
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
now = int(time.time())
head = b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
body = b64(json.dumps({"iss": "https://issuer.mailtestenv.test", "sub": sys.argv[1], "iat": now, "nbf": now - 5,
                       "exp": now + int(sys.argv[3]), "scope": "mail"}, separators=(",", ":")).encode())
sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", "/root/jwt.key"], input=f"{head}.{body}".encode(),
                     capture_output=True, check=True).stdout
if sys.argv[2] == "bad":
    sig = bytes([sig[0] ^ 0xFF]) + sig[1:]
print(f"{head}.{body}.{b64(sig)}")
PY
}

: > /etc/dovecot/users
: > /etc/postfix/vmailbox
declare -A PW
for name in alice bob bulk; do
  PW[$name]=$(openssl rand -hex 16)
  echo "$name@mailtestenv.test:{PLAIN}${PW[$name]}:5000:5000::/srv/mail/$name::" >> /etc/dovecot/users
  echo "$name@mailtestenv.test $name/" >> /etc/postfix/vmailbox
  rm -rf "/srv/mail/$name"
  install -d -o vmail -g vmail "/srv/mail/$name/Maildir/"{cur,new,tmp}
done
chmod 0640 /etc/dovecot/users
chgrp dovecot /etc/dovecot/users 2>/dev/null || true
write_secrets() { # replaced by rename, so a reader never sees a half-written file or an empty token
  local good bad wrong
  good=$(mint alice@mailtestenv.test) && bad=$(mint alice@mailtestenv.test bad) && wrong=$(mint bob@mailtestenv.test) || return 1
  {
    printf 'account\tkind\thost\tsecret\n'
    for name in alice bob bulk; do printf '%s@mailtestenv.test\tpassword\tmail.mailtestenv.test\t%s\n' "$name" "${PW[$name]}"; done
    printf 'alice@mailtestenv.test\toauth2-access-token\tmail.mailtestenv.test\t%s\n' "$good"
    printf 'alice@mailtestenv.test\toauth2-access-token-bad-signature\tmail.mailtestenv.test\t%s\n' "$bad"
    printf 'alice@mailtestenv.test\toauth2-access-token-wrong-subject\tmail.mailtestenv.test\t%s\n' "$wrong"
  } > "$STATE/secrets.tsv.new" && chmod 0644 "$STATE/secrets.tsv.new" && mv -f "$STATE/secrets.tsv.new" "$STATE/secrets.tsv"
}
write_secrets

# Synthetic corpus for bulk@ (no real mail): MESSAGES plain-text messages, seen.
python3 - "$MESSAGES" <<'PY'
import sys, email.utils
for i in range(int(sys.argv[1])):
    date = email.utils.formatdate(1_790_000_000 + i * 3600, usegmt=True)
    msg = (f"From: Sender {i % 17} <sender{i % 17}@example.org>\r\nTo: bulk@mailtestenv.test\r\n"
           f"Subject: Seeded message {i}\r\nDate: {date}\r\nMessage-ID: <seed-{i}@mailtestenv.test>\r\n"
           f"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nBody of seeded message {i}.\r\n")
    with open(f"/srv/mail/bulk/Maildir/cur/{1790000000 + i}.seed{i}:2,S", "w", newline="") as f:
        f.write(msg)
PY
chown -R vmail:vmail /srv/mail

install -m 0644 "$SRC/dovecot.conf" /etc/dovecot/dovecot.conf
install -m 0644 "$SRC/main.cf" /etc/postfix/main.cf
install -m 0644 "$SRC/master.cf" /etc/postfix/master.cf
printf 'mail.mailtestenv.test /etc/postfix/pki/mail.pem\nrogue.mailtestenv.test /etc/postfix/pki/rogue.pem\n' > /etc/postfix/sni
postmap -F lmdb:/etc/postfix/sni
touch /var/log/postfix.log /var/log/dovecot.log
dovecot
postfix check
postfix start 2>/dev/null
for _ in $(seq 100); do
  ok=1
  for p in 993 143 465 587; do (exec 3<>/dev/tcp/127.0.0.1/$p) 2>/dev/null || ok=0; done
  [[ $ok == 1 ]] && break
  sleep 0.1
done
[[ $ok == 1 ]] || { echo "servers did not start" >&2; tail -n 50 /var/log/postfix.log /var/log/dovecot.log >&2; exit 1; }
cp /opt/testenv-packages.txt "$STATE/server-versions.txt"
find "$STATE" -path "$STATE/private" -prune -o -exec chmod a+rX {} +  # private/ stays root-only
# Token refresh for the container's lifetime; a failed round is logged and retried at the next one.
(
  set +e
  while sleep "$TOKEN_REFRESH"; do write_secrets || echo "testenv-mail: token refresh failed" >&2; done
) &
echo "mailtestenv-mail ready"
exec tail -F /var/log/dovecot.log /var/log/postfix.log
