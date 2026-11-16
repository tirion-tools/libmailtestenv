#!/usr/bin/env bash
# Open a copy of the built Maildir in a throwaway Dovecot 2.4 container (no network, no ports, no
# protocols) and print per-folder status plus the first/last INTERNALDATE of every folder.
# The copy is a hard-link tree in .cache/dovecot-check/ (no extra disk): Dovecot only reads message
# files (no flag changes, so no renames) and replaces dovecot-uidlist by rename, so the build is untouched.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
image="${DOVECOT_IMAGE:-dovecot/dovecot:latest}"
work="$here/.cache/dovecot-check"
rm -rf "$work"
mkdir -p "$work"/{run,state,home,idx}
cp -al "$here/.cache/maildir" "$work/m"
cat > "$work/dovecot.conf" <<'EOF'
dovecot_config_version = 2.4.0
dovecot_storage_version = 2.4.0
protocols =
base_dir = /w/run
state_dir = /w/state
log_path = /dev/stderr
default_internal_user = vmail
default_internal_group = vmail
default_login_user = vmail
mail_driver = maildir
mail_path = /w/m
mailbox_list_layout = fs
mail_index_path = /w/idx
userdb static {
  fields {
    uid = vmail
    gid = vmail
    home = /w/home
  }
}
passdb static {
  password = unused
}
EOF
name="mailtestenv-apache-verify-$$"
docker run -d --name "$name" --network none -v "$work:/w" "$image" /dovecot/sbin/dovecot -F -c /w/dovecot.conf >/dev/null
trap 'docker rm -f "$name" >/dev/null' EXIT
sleep 2
dv() { docker exec "$name" /dovecot/bin/doveadm -c /w/dovecot.conf "$@" </dev/null; }
dv mailbox status -u apache "messages unseen recent uidvalidity uidnext" '*'
total=0
while IFS= read -r box; do
  n=$(dv mailbox status -u apache messages "$box" 2>/dev/null | sed -n 's/.*messages=\([0-9]*\).*/\1/p' | head -1 || true)
  n=${n:-0}
  [ "$n" -gt 0 ] || continue
  total=$((total + n))
  first=$(dv fetch -u apache date.received mailbox "$box" uid 1 | sed -n 's/^date.received: //p')
  last=$(dv fetch -u apache date.received mailbox "$box" uid "$n" | sed -n 's/^date.received: //p')
  printf '%s\t%s\t%s .. %s\n' "$box" "$n" "$first" "$last"
done < <(dv mailbox list -u apache)
echo "total messages: $total"
