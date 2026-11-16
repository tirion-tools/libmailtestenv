#!/usr/bin/env python3
"""The role table of the mail test environment and its env JSON contract (schema/env.schema.json, README "Env JSON").

testenv.sh is the only caller:
  roles.py names              role names, one per line
  roles.py container ROLE     container name of ROLE
  roles.py publish ROLE       docker publish arguments of ROLE (host side always 127.0.0.1)
  roles.py env-json           the env document
  roles.py env-text           the same, as a summary for people

Settings come from the environment (testenv.sh passes them on):
  MAILTESTENV_STATE        state directory (required)
  MAILTESTENV_PREFIX       container name prefix, default "mailtestenv"; must start with "mailtestenv"
  MAILTESTENV_PORT_OFFSET  added to every host port, default 0 (a second instance on other ports)
  MAILTESTENV_DENY_PORTS   extra host ports that must never be published (space or comma separated)
Host ports 143 and 5232 are refused whatever the settings.
"""
import json
import os
import re
import sys

SCHEMA = "mailtestenv-env"
VERSION = 1
DOMAIN = "mailtestenv.test"
HOST = "127.0.0.1"
PASSWORD = "pass"  # the fixed password of the gmail, enron and apache roles (plain-text test data, no secrets)
FORBIDDEN_PORTS = frozenset({143, 5232})
PREFIX_RE = re.compile(r"^mailtestenv[a-z0-9-]{0,40}$")

# role -> endpoints: name -> (host port before the offset, container port, TLS mode)
#   none               plain IMAP only
#   starttls           STARTTLS required before authentication
#   starttls-optional  STARTTLS offered; plain authentication allowed too
#   implicit           TLS from the first byte
ROLES = {
    "mail": {
        "endpoints": {
            "imap": (11143, 143, "starttls"),
            "imaps": (11993, 993, "implicit"),
            "submissions": (11465, 465, "implicit"),
            "submission": (11587, 587, "starttls"),
        },
        "server_names": [f"mail.{DOMAIN}", f"dns-a.{DOMAIN}"],
        "ca": "mail/pki/ca.crt",
        "secrets": "mail/secrets.tsv",
        "accounts": [f"alice@{DOMAIN}", f"bob@{DOMAIN}", f"bulk@{DOMAIN}"],
        "password": None,
        "tls_matrix": {"valid": [f"mail.{DOMAIN}", f"dns-a.{DOMAIN}"], "wrong_name": f"wrong.{DOMAIN}",
                       "untrusted_issuer": f"rogue.{DOMAIN}", "rogue_ca": "mail/pki/rogue-ca.crt"},
        "oauth": {"issuer": f"https://issuer.{DOMAIN}", "mechanisms": ["XOAUTH2", "OAUTHBEARER"],
                  "token_kinds": ["oauth2-access-token", "oauth2-access-token-bad-signature",
                                  "oauth2-access-token-wrong-subject"]},
    },
    "gmail": {
        "endpoints": {"imap": (12143, 12143, "none")},
        "server_names": [f"gmail.{DOMAIN}"],
        "accounts": ["gmail", "gmailhidden", "gmailcapped", "gmailcap5k"],
        "password": PASSWORD,
    },
    "enron": {
        "endpoints": {
            "imap": (13143, 13143, "starttls-optional"),
            "imaps": (13993, 13993, "implicit"),
            "submissions": (13465, 465, "implicit"),
            "submission": (13587, 587, "starttls"),
        },
        "server_names": [f"enron.{DOMAIN}", f"mail.{DOMAIN}"],
        "ca": "mail/pki/ca.crt",
        "accounts_file": "enron/pristine/MANIFEST.tsv",
        "password": PASSWORD,
        "maildir": "enron/pristine/mail",
    },
    "apache": {
        "endpoints": {"imap": (14143, 14143, "none")},
        "server_names": [f"apache.{DOMAIN}"],
        "accounts": ["apache"],
        "password": PASSWORD,
    },
}


class SettingsError(Exception):
    pass


def settings(env):
    state = env.get("MAILTESTENV_STATE", "")
    if not state:
        raise SettingsError("MAILTESTENV_STATE is not set")
    prefix = env.get("MAILTESTENV_PREFIX", "mailtestenv")
    if not PREFIX_RE.match(prefix):
        raise SettingsError(f"MAILTESTENV_PREFIX {prefix!r} must match {PREFIX_RE.pattern}")
    try:
        offset = int(env.get("MAILTESTENV_PORT_OFFSET", "0"))
    except ValueError:
        raise SettingsError("MAILTESTENV_PORT_OFFSET must be an integer") from None
    try:
        extra = {int(p) for p in re.split(r"[\s,]+", env.get("MAILTESTENV_DENY_PORTS", "").strip()) if p}
    except ValueError:
        raise SettingsError("MAILTESTENV_DENY_PORTS must list port numbers") from None
    denied = FORBIDDEN_PORTS | extra
    for role, spec in ROLES.items():
        for name, (host, _, _) in spec["endpoints"].items():
            port = host + offset
            if not 1024 <= port <= 65535:
                raise SettingsError(f"{role} {name}: host port {port} is outside 1024-65535")
            if port in denied:
                raise SettingsError(f"{role} {name}: host port {port} is refused")
    return os.path.abspath(state), prefix, offset


def container(prefix, role):
    return f"{prefix}-{role}"


def publish(offset, role):
    return " ".join(f"-p {HOST}:{host + offset}:{inner}" for host, inner, _ in ROLES[role]["endpoints"].values())


def env_document(state, prefix, offset):
    roles = {}
    for role, spec in ROLES.items():
        r = {
            "container": container(prefix, role),
            "host": HOST,
            "server_names": spec["server_names"],
            "endpoints": {name: {"port": host + offset, "tls": tls} for name, (host, _, tls) in spec["endpoints"].items()},
            "ca": spec.get("ca"),
            "secrets": spec.get("secrets"),
            "accounts": spec.get("accounts", []),
            "accounts_file": spec.get("accounts_file"),
            "password": spec.get("password"),
            "maildir": spec.get("maildir"),
        }
        for extra in ("tls_matrix", "oauth"):
            if extra in spec:
                r[extra] = spec[extra]
        roles[role] = r
    return {"schema": SCHEMA, "version": VERSION, "state_dir": state, "prefix": prefix, "port_offset": offset,
            "roles": roles}


def env_text(doc):
    lines = []
    for role, r in doc["roles"].items():
        eps = ", ".join(f"{name} {r['host']}:{e['port']} ({e['tls']})" for name, e in r["endpoints"].items())
        lines.append(f"{role:6} {r['container']}: {eps}")
        lines.append(f"       server names {' '.join(r['server_names'])}")
        if r["ca"]:
            lines.append(f"       trust {os.path.join(doc['state_dir'], r['ca'])}")
        users = " ".join(r["accounts"]) or f"listed in {os.path.join(doc['state_dir'], r['accounts_file'])}"
        cred = (f'password "{r["password"]}"' if r["password"] else
                f"credentials in {os.path.join(doc['state_dir'], r['secrets'])}")
        lines.append(f"       users {users}; {cred}")
    return "\n".join(lines)


def main(argv, env):
    try:
        state, prefix, offset = settings(env)
    except SettingsError as e:
        print(f"testenv: {e}", file=sys.stderr)
        return 2
    cmd, args = (argv[0] if argv else ""), argv[1:]
    if cmd == "names" and not args:
        print("\n".join(ROLES))
    elif cmd in ("container", "publish") and len(args) == 1 and args[0] in ROLES:
        print(container(prefix, args[0]) if cmd == "container" else publish(offset, args[0]))
    elif cmd == "env-json" and not args:
        print(json.dumps(env_document(state, prefix, offset), indent=2))
    elif cmd == "env-text" and not args:
        print(env_text(env_document(state, prefix, offset)))
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
