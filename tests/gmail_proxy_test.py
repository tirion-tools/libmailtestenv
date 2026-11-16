"""Regression tests of the Gmail stand-in proxy (testenv/gmail/gmail_proxy.py), run by `testenv/testenv.sh selftest`.

Runs as root inside a throwaway container of the testenv image (no network, state on tmpfs): starts Dovecot with the
gmail role's configuration, seeds a three-message account, starts the proxy and drives it with imaplib. Every client
socket has a 20 s timeout, so a proxy that hangs fails the test instead of stalling it.
"""
import imaplib
import json
import os
import socket
import subprocess
import sys
import time

GMAIL = "/opt/testenv/gmail"
CONF = f"{GMAIL}/dovecot.conf"
ALL, SPAM, TRASH = "[Gmail]/All Mail", "[Gmail]/Spam", "[Gmail]/Trash"
PROXY, DOVECOT = 1993, 1143
TIMEOUT = 20
MAX_RSS_KB = 300 * 1024


def client():
    c = imaplib.IMAP4("127.0.0.1", PROXY, timeout=TIMEOUT)
    c.login("gmail", "pass")
    return c


def direct():
    c = imaplib.IMAP4("127.0.0.1", DOVECOT, timeout=TIMEOUT)
    c.login("gmail", "pass")
    return c


def ok(result, what):
    typ, data = result
    assert typ == "OK", f"{what}: {typ} {data}"
    return data


def search(folder, *criteria):
    """UIDs matching `criteria` in `folder`, read from Dovecot directly (the ground truth behind the proxy)."""
    c = direct()
    try:
        ok(c.select(f'"{folder}"', readonly=True), f"EXAMINE {folder}")
        return [int(x) for x in ok(c.uid("SEARCH", None, *criteria), "SEARCH")[0].split()]
    finally:
        c.logout()


def timed(fn, what):
    t = time.monotonic()
    result = fn()
    took = time.monotonic() - t
    assert took < 10, f"{what} took {took:.1f} s"
    return result


def setup():
    os.makedirs("/tmp/dovecot", exist_ok=True)
    os.makedirs("/mailstore", exist_ok=True)
    subprocess.run(["chown", "testenv:testenv", "/tmp/dovecot", "/mailstore"], check=True)
    subprocess.run(["dovecot", "-c", CONF], check=True)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", DOVECOT), 1).close()
            break
        except OSError:
            time.sleep(0.1)
    c = direct()
    for folder in (ALL, SPAM, TRASH, "Work"):
        ok(c.create(f'"{folder}"'), f"CREATE {folder}")
    gms = [1_700_000_000_000_000_001, 1_700_000_000_000_000_002, 1_700_000_000_000_000_003]
    for i, _ in enumerate(gms):
        msg = (f"Message-ID: <m{i}@test.example>\r\nFrom: a@test.example\r\nTo: owner@gmail.example\r\n"
               f"Subject: message {i}\r\nDate: Mon, 02 Nov 2026 10:00:00 +0000\r\n\r\nbody {i}\r\n").encode()
        for folder in ("INBOX", ALL):
            ok(c.append(f'"{folder}"', "", None, msg), f"APPEND {folder}")
    c.logout()
    state = {"gmail": {"folders": {"INBOX": gms, ALL: gms, SPAM: [], TRASH: [], "Work": []},
                       "msgs": {str(g): [g, ["\\Inbox"]] for g in gms}, "hidden": [], "cap": None}}
    os.makedirs("/state", exist_ok=True)
    with open("/state/gmail-state.json", "w") as f:
        json.dump(state, f)
    out = open("/state/proxy.out", "w")
    proxy = subprocess.Popen([sys.executable, f"{GMAIL}/gmail_proxy.py", str(PROXY), str(DOVECOT), "/state/gmail-state.json",
                              "/state/proxy.jsonl", "/state/caps.json"], stdout=out, stderr=subprocess.STDOUT)
    for _ in range(100):
        if "ready" in open("/state/proxy.out").read():
            return proxy
        time.sleep(0.1)
    raise RuntimeError("proxy did not start: " + open("/state/proxy.out").read())


def test_store_with_huge_and_star_uid_sets(proxy):
    """A client set is never expanded: `1:4294967295` once meant a 4-billion-element list."""
    c = client()
    ok(c.select("INBOX"), "SELECT INBOX")
    timed(lambda: ok(c.uid("STORE", "1:4294967295", "+FLAGS.SILENT", r"(\Seen)"), "STORE 1:4294967295"), "STORE")
    assert search(ALL, "SEEN") == [1, 2, 3], "\\Seen not propagated to All Mail"
    timed(lambda: ok(c.uid("STORE", "1:*", "-FLAGS.SILENT", r"(\Seen)"), "STORE 1:*"), "STORE 1:*")
    assert search(ALL, "SEEN") == [], "-\\Seen not propagated to All Mail"
    c.logout()


def test_label_store_with_huge_uid_set(proxy):
    c = client()
    ok(c.select(f'"{ALL}"'), "SELECT All Mail")
    timed(lambda: ok(c.uid("STORE", "1:4294967295", "+X-GM-LABELS", "(Work)"), "+X-GM-LABELS"), "+X-GM-LABELS")
    assert len(search("Work", "ALL")) == 3, "Work did not gain the three messages"
    timed(lambda: ok(c.uid("STORE", "2:4294967295", "-X-GM-LABELS", "(Work)"), "-X-GM-LABELS"), "-X-GM-LABELS")
    assert len(search("Work", "ALL")) == 1, "Work did not lose two messages"
    typ, data = c.uid("STORE", "1:*", "+X-GM-LABELS", "(Work)")
    assert typ == "NO" and b"CANNOT" in data[0], f"`*` label store: {typ} {data}"
    c.logout()


def test_side_connection_reconnects_after_dovecot_closed_it(proxy):
    """Dovecot logs out an idle side connection (30 min) with BYE; reading at EOF once spun forever (100 % CPU, 6 GB)."""
    c = client()
    ok(c.select("INBOX"), "SELECT INBOX")
    ok(c.uid("STORE", "1", "+FLAGS.SILENT", r"(\Answered)"), "STORE")  # the side connection is open now
    kicked = subprocess.run(["doveadm", "-c", CONF, "kick", "gmail"], capture_output=True, text=True, check=True)
    assert "kicked" in kicked.stdout, kicked.stdout
    time.sleep(0.5)
    c = client()
    ok(c.select("INBOX"), "SELECT INBOX")
    timed(lambda: ok(c.uid("STORE", "1", "+FLAGS.SILENT", r"(\Flagged)"), "STORE after kick"), "STORE after kick")
    assert search(ALL, "FLAGGED") == [1], "\\Flagged not propagated after the side connection was closed"
    c.logout()


def labels_of(c, uid):
    data = ok(c.uid("FETCH", str(uid), "(FLAGS X-GM-LABELS)"), "FETCH X-GM-LABELS")
    return data[0].decode()


def test_label_folder_create_rename_delete(proxy):
    """Folder operations on user labels keep the model in step (they once left it stale: later commands hung)."""
    c = client()
    ok(c.create("Trips"), "CREATE Trips")
    ok(c.select("INBOX"), "SELECT INBOX")
    ok(c.uid("MOVE", "3", "Trips"), "MOVE INBOX -> Trips")
    ok(c.select(f'"{ALL}"'), "SELECT All Mail")
    assert "Trips" in labels_of(c, 3) and "Inbox" not in labels_of(c, 3), labels_of(c, 3)
    ok(c.rename("Trips", "Journeys"), "RENAME Trips Journeys")
    ok(c.select(f'"{ALL}"'), "SELECT All Mail")
    assert "Journeys" in labels_of(c, 3) and "Trips" not in labels_of(c, 3), labels_of(c, 3)
    timed(lambda: ok(c.uid("STORE", "3", "+FLAGS.SILENT", r"(\Flagged)"), "STORE"), "STORE after RENAME")
    assert len(search("Journeys", "FLAGGED")) == 1, "\\Flagged not propagated to the renamed label folder"
    ok(c.delete("Journeys"), "DELETE Journeys")
    ok(c.select(f'"{ALL}"'), "SELECT All Mail")
    assert "Journeys" not in labels_of(c, 3), labels_of(c, 3)
    timed(lambda: ok(c.uid("STORE", "3", "-FLAGS.SILENT", r"(\Flagged)"), "STORE"), "STORE after DELETE")
    typ, data = c.rename('"[Gmail]/Trash"', "Bin")
    assert typ == "NO" and b"CANNOT" in data[0], f"RENAME [Gmail]/Trash: {typ} {data}"
    c.logout()


def test_model_failure_answers_no_instead_of_hanging(proxy):
    """A failed model update (here: a label folder deleted behind the proxy's back) answers NO, the session goes on."""
    c = client()
    ok(c.create("Scratch"), "CREATE Scratch")
    d = direct()
    ok(d.delete("Scratch"), "DELETE Scratch directly in Dovecot")
    d.logout()
    ok(c.select(f'"{ALL}"'), "SELECT All Mail")
    typ, data = timed(lambda: c.uid("STORE", "2", "+X-GM-LABELS", "(Scratch)"), "+X-GM-LABELS into a deleted folder")
    assert typ == "NO" and b"SERVERBUG" in data[0], f"{typ} {data}"
    ok(c.noop(), "NOOP after the failure")
    ok(c.select("INBOX"), "SELECT INBOX")
    ok(c.uid("STORE", "1", "-FLAGS.SILENT", r"(\Flagged)"), "STORE after the failure")
    assert search(ALL, "FLAGGED") == [], "-\\Flagged not propagated after a model failure"
    c.logout()


def test_oversized_literal_closes_the_connection(proxy):
    s = socket.create_connection(("127.0.0.1", PROXY), TIMEOUT)
    f = s.makefile("rb")
    f.readline()
    s.sendall(b"a LOGIN gmail pass\r\n")
    f.readline()
    s.sendall(b"b APPEND INBOX {999999999}\r\n")
    lines = f.readlines()  # until the proxy closes the connection
    assert any(b"BYE" in x for x in lines), lines
    s.close()


def test_proxy_still_answers(proxy):
    s = socket.create_connection(("127.0.0.1", PROXY), TIMEOUT)
    assert s.makefile("rb").readline().startswith(b"* OK"), "no greeting"
    s.close()
    with open(f"/proc/{proxy.pid}/status") as f:
        rss = next(int(x.split()[1]) for x in f if x.startswith("VmRSS:"))
    assert rss < MAX_RSS_KB, f"proxy RSS {rss} kB"


def test_process_bounds_are_set(proxy):
    sys.path.insert(0, GMAIL)
    import gmail_proxy  # noqa: PLC0415 - the constants of the code under test
    with open(f"/proc/{proxy.pid}/limits") as f:
        limits = {line[:26].strip(): line[26:].split()[:2] for line in f}
    assert limits["Max address space"] == [str(gmail_proxy.MAX_ADDRESS_SPACE)] * 2, limits["Max address space"]
    assert limits["Max cpu time"] == [str(gmail_proxy.MAX_CPU_SECONDS), str(gmail_proxy.MAX_CPU_SECONDS + 10)], \
        limits["Max cpu time"]


def main():
    proxy = setup()
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            t = time.monotonic()
            try:
                fn(proxy)
                print(f"PASS {name} ({time.monotonic() - t:.1f} s)", flush=True)
            except Exception as e:  # noqa: BLE001 - report every test
                failed += 1
                print(f"FAIL {name} ({time.monotonic() - t:.1f} s): {e!r}", flush=True)
    if failed:
        print(open("/state/proxy.out").read()[-4000:])
    print(f"{failed} failed" if failed else "all passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
