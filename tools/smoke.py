#!/usr/bin/env python3
"""Smoke test of a real instance (tools/gate.sh job `smoke`, on the host: it needs Docker).

Starts the mail role as a second, private instance (prefix mailtestenv-gate, every host port + 30000, a fresh
state directory), so it never touches a running default instance. Then, through the env document only:
  - `testenv.sh env --json` is valid against schema/env.schema.json and equals STATE/env.json;
  - IMAPS and IMAP+STARTTLS log in as alice with the secrets.tsv password, validating the certificate against
    the document's CA and primary server name; an unlisted name (wrong-name certificate) and the untrusted-issuer name fail validation;
  - XOAUTH2 with the valid token logs in, the bad-signature token does not;
  - submission (STARTTLS) and submissions (implicit TLS) authenticate; a message to bob arrives in bob's INBOX.
`testenv.sh down` and `purge` run at the end whatever happened.
"""
import imaplib
import json
import os
import smtplib
import socket
import ssl
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from schema_check import errors, load_schema  # noqa: E402

OFFSET = int(os.environ.get("SMOKE_PORT_OFFSET", "30000"))
TIMEOUT = 30


def sh(env, *args):
    print("+ testenv.sh " + " ".join(args), flush=True)
    return subprocess.run([os.path.join(ROOT, "testenv.sh"), *args], env=env, check=True, text=True,
                          stdout=subprocess.PIPE).stdout


def secrets(path):
    rows = {}
    with open(path, encoding="utf-8") as f:
        header = f.readline().rstrip("\n").split("\t")
        assert header == ["account", "kind", "host", "secret"], header
        for line in f:
            account, kind, host, secret = line.rstrip("\n").split("\t")
            rows[(account, kind)] = (host, secret)
    return rows


def context(ca):
    ctx = ssl.create_default_context(cafile=ca)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def must_fail_validation(host, port, ca, name):
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT) as raw:
            with context(ca).wrap_socket(raw, server_hostname=name):
                pass
    except ssl.SSLCertVerificationError as e:
        print(f"  {name}: refused as expected ({e.verify_message})")
        return
    raise AssertionError(f"{name}: certificate validated, expected a failure")


def run(env, state):
    out = sh(env, "env", "--json")
    doc = json.loads(out)
    problems = errors(doc, load_schema())
    assert not problems, problems
    with open(os.path.join(state, "env.json"), encoding="utf-8") as f:
        assert json.load(f) == doc, "STATE/env.json differs from env --json"
    print("  env --json: valid, equals STATE/env.json")

    mail = doc["roles"]["mail"]
    host, name = mail["host"], mail["server_names"][0]
    ca = os.path.join(state, mail["ca"])
    sec = secrets(os.path.join(state, mail["secrets"]))
    alice, bob = mail["accounts"][0], mail["accounts"][1]
    pw_alice, pw_bob = sec[(alice, "password")][1], sec[(bob, "password")][1]
    ep = {k: v["port"] for k, v in mail["endpoints"].items()}

    with imap(host, ep["imaps"], name, ca) as c:
        c.login(alice, pw_alice)
        assert c.select("INBOX")[0] == "OK"
        # A fresh instance has the special-use mailboxes, subscribed, as a real provider does (RFC 6154).
        typ, data = c.list()
        assert typ == "OK", data
        special = ("Sent", "Drafts", "Trash", "Junk")
        name_of = lambda line: line.decode().rsplit(" ", 1)[-1].strip('"')
        boxes = {name_of(line): line.decode() for line in data}
        for box in special:
            assert box in boxes and f"\\{box}" in boxes[box], f"{box}: {boxes.get(box)}"
        typ, data = c.lsub('""', "*")
        assert typ == "OK" and set(special) <= {name_of(line) for line in data}, data
    print("  imaps: alice logged in; Sent, Drafts, Trash, Junk exist with their special-use flags and are subscribed")
    with imap(host, ep["imap"], name) as c:
        c.starttls(context(ca))
        c.login(alice, pw_alice)
    print("  imap + STARTTLS: alice logged in")
    tm = mail["tls_matrix"]
    # Any SNI name outside tls_matrix.valid gets the wrong_name certificate: it must not validate for that name.
    must_fail_validation(host, ep["imaps"], ca, "unlisted." + name.split(".", 1)[1])
    must_fail_validation(host, ep["imaps"], ca, tm["untrusted_issuer"])

    token = sec[(alice, mail["oauth"]["token_kinds"][0])][1]
    bad = sec[(alice, "oauth2-access-token-bad-signature")][1]
    with imap(host, ep["imaps"], name, ca) as c:
        c.authenticate("XOAUTH2", lambda _: f"user={alice}\x01auth=Bearer {token}\x01\x01".encode())
    print("  XOAUTH2: valid token accepted")
    with imap(host, ep["imaps"], name, ca) as c:
        try:
            c.authenticate("XOAUTH2", lambda _: f"user={alice}\x01auth=Bearer {bad}\x01\x01".encode())
        except imaplib.IMAP4.error:
            print("  XOAUTH2: bad-signature token refused")
        else:
            raise AssertionError("XOAUTH2 accepted the bad-signature token")

    subject = f"smoke {time.time_ns()}"
    msg = f"From: {alice}\r\nTo: {bob}\r\nSubject: {subject}\r\n\r\nsmoke body\r\n"
    with smtplib.SMTP(host, ep["submission"], timeout=TIMEOUT) as s:
        s._host = name  # SNI and certificate name for starttls()
        s.starttls(context=context(ca))
        s.login(alice, pw_alice)
        s.sendmail(alice, [bob], msg)
    print("  submission + STARTTLS: sent alice -> bob")
    class Smtps(smtplib.SMTP_SSL):  # connects to the address, validates against the server name
        def _get_socket(self, _host, port, timeout):
            return self.context.wrap_socket(socket.create_connection((host, port), timeout), server_hostname=name)

    with Smtps(host, ep["submissions"], context=context(ca), timeout=TIMEOUT) as s:
        s.login(bob, pw_bob)
    print("  submissions: bob authenticated")
    deadline = time.monotonic() + 60
    while True:
        with imap(host, ep["imaps"], name, ca) as c:
            c.login(bob, pw_bob)
            c.select("INBOX", readonly=True)
            _, data = c.search(None, "SUBJECT", f'"{subject}"')
            if data[0].split():
                break
        if time.monotonic() > deadline:
            raise AssertionError("the message did not reach bob's INBOX within 60 s")
        time.sleep(1)
    print("  delivery: the message is in bob's INBOX")


def imap(host, port, name, ca=None):
    """IMAP to `host` that uses `name` as SNI and certificate name (imaplib takes both from the host it connects
    to): implicit TLS when `ca` is given, else plain until starttls()."""
    if ca is None:
        class Imap(imaplib.IMAP4):
            def _create_socket(self, timeout):
                return socket.create_connection((host, self.port), timeout)

        return Imap(name, port, timeout=TIMEOUT)

    class Imaps(imaplib.IMAP4_SSL):
        def _create_socket(self, timeout):
            return self.ssl_context.wrap_socket(socket.create_connection((host, self.port), timeout),
                                                server_hostname=self.host)

    return Imaps(name, port, ssl_context=context(ca), timeout=TIMEOUT)


def main():
    state = tempfile.mkdtemp(prefix="mailtestenv-smoke-")
    env = {**os.environ, "MAILTESTENV_STATE": state, "MAILTESTENV_PREFIX": "mailtestenv-gate",
           "MAILTESTENV_PORT_OFFSET": str(OFFSET)}
    env.pop("MAILTESTENV_DENY_PORTS", None)
    ok = False
    try:
        sh(env, "up", "mail")
        run(env, state)
        print(sh(env, "status"), end="")
        ok = True
    finally:
        sh(env, "down")
        sh(env, "purge")
    print(f"smoke: {'ok' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
