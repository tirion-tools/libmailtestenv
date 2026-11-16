"""Seed Dovecot with the Gmail stand-in users and write the proxy's initial Gmail model.

Usage: seed.py gmail <gmail-state.json> <manifest.json>
       seed.py bench <user> <messages> <manifest.json>

Gmail users (same 5,000-message All Mail corpus, plus Spam and Trash):
  gmail        normal account
  gmailhidden  All Mail hidden from IMAP ("Show in IMAP" off); the proxy hides it
  gmailcapped  Folder Size Limit 2,000: every folder shows only its newest 2,000 messages
  gmailcap5k   Folder Size Limit 5,000: All Mail holds exactly 5,000, so the cap only bites when new mail arrives
Bench users hold one large INBOX of small messages for the expunge-detection benchmark.
"""
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.dirname(__file__))
import corpus  # noqa: E402
from imapraw import Raw, quote, select_info  # noqa: E402

CAP = 2000
USERS = {"gmail": {"hidden": [], "cap": None},
         "gmailhidden": {"hidden": [corpus.GMAIL_ALL], "cap": None},
         "gmailcapped": {"hidden": [], "cap": CAP},
         "gmailcap5k": {"hidden": [], "cap": 5000}}


def append_many(raw, folder, items, depth=256):
    pending = []

    def drain():
        for tag in pending:
            status, line, _ = raw.collect(tag)
            if status != "OK":
                raise RuntimeError(f"APPEND {folder}: {line}")
        pending.clear()
    for flags, date, data in items:
        tag = raw.next_tag()
        raw.send(f'{tag} APPEND {quote(folder)} ({" ".join(flags)}) "{date}" {{{len(data)}+}}\r\n'.encode()
                 + data + b"\r\n")
        pending.append(tag)
        if len(pending) >= depth:
            drain()
    drain()


def multiappend(raw, folder, items, batch=5000):
    """RFC 3502 MULTIAPPEND: one command (one Dovecot transaction) per `batch` messages. Single APPENDs slowed
    to ~450 messages/s at 80k messages in one mdbox folder."""
    buf = []

    def flush():
        if not buf:
            return
        tag = raw.next_tag()
        parts = [f"{tag} APPEND {quote(folder)}".encode()]
        for flags, date, data in buf:
            parts.append(f' ({" ".join(flags)}) "{date}" {{{len(data)}+}}\r\n'.encode() + data)
        raw.send(b"".join(parts) + b"\r\n")
        status, line, _ = raw.collect(tag)
        if status != "OK":
            raise RuntimeError(f"MULTIAPPEND {folder}: {line}")
        buf.clear()
    for item in items:
        buf.append(item)
        if len(buf) >= batch:
            flush()
    flush()


def gmail_model():
    """Folder contents (in UID order) and per-message metadata for one Gmail account."""
    queues = {corpus.GMAIL_ALL: []}
    for f in corpus.GMAIL_LABEL_FOLDERS.values():
        queues[f] = []
    msgs = {}
    for internal, flags, data, _meta, labels, gm, thr in corpus.generate_gmail():
        item = (flags, corpus.imap_date(internal), data, gm)
        msgs[gm] = [thr, labels]
        queues[corpus.GMAIL_ALL].append(item)
        for label in labels:
            queues[corpus.GMAIL_LABEL_FOLDERS[label]].append(item)
    for folder in (corpus.GMAIL_SPAM, corpus.GMAIL_TRASH):
        queues[folder] = []
        for internal, flags, data, labels, gm, thr in corpus.generate_gmail_outside(folder):
            msgs[gm] = [thr, labels if folder == corpus.GMAIL_TRASH else []]
            queues[folder].append((flags, corpus.imap_date(internal), data, gm))
    return queues, msgs


def seed_gmail(state_path, manifest_path):
    t0 = time.monotonic()
    queues, msgs = gmail_model()
    state, manifest = {}, {}
    for user, opts in USERS.items():
        raw = Raw(user=user)
        folders = {}
        for folder, items in queues.items():
            if folder != "INBOX":
                raw.ok(f"CREATE {quote(folder)}")
            if opts["cap"]:
                items = items[-opts["cap"]:]
            append_many(raw, folder, ((f, d, data) for f, d, data, _ in items))
            _, untagged = raw.ok(f"EXAMINE {quote(folder)}")
            info = select_info(untagged)
            if info["uidnext"] != len(items) + 1:
                raise RuntimeError(f"{user}: unexpected UIDNEXT in {folder}: {info}")
            folders[folder] = [gm for *_, gm in items]
        raw.close()
        present = {gm for v in folders.values() for gm in v}
        state[user] = {"folders": folders, "msgs": {str(gm): msgs[gm] for gm in present},
                       "hidden": opts["hidden"], "cap": opts["cap"]}
        manifest[user] = {"folder_counts": {f: len(v) for f, v in folders.items()},
                          "messages": len(present), "hidden": opts["hidden"], "cap": opts["cap"]}
    all_mail = set(state["gmail"]["folders"][corpus.GMAIL_ALL])
    label_only = {gm for f, v in state["gmail"]["folders"].items() if f not in (corpus.GMAIL_ALL, corpus.GMAIL_SPAM,
                                                                              corpus.GMAIL_TRASH) for gm in v}
    manifest["gmail"]["archived_without_label"] = len(all_mail - label_only)
    manifest["gmail"]["label_memberships"] = sum(len(v) for f, v in state["gmail"]["folders"].items()
                                                 if f not in (corpus.GMAIL_ALL, corpus.GMAIL_SPAM, corpus.GMAIL_TRASH))
    manifest["seed_seconds"] = round(time.monotonic() - t0, 1)
    with open(state_path, "w") as f:
        json.dump(state, f)
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)


def bench_message(i):
    return (f"Message-ID: <bench-{i}@bench.example>\r\nDate: Mon, 02 Nov 2026 10:00:00 +0000\r\n"
            f"From: Sender {i % 977} <s{i % 977}@bench.example>\r\nTo: owner@gmail.example\r\n"
            f"Subject: bench message {i}\r\n\r\nbody {i}\r\n").encode()


def seed_bench(user, count, manifest_path):
    """One INBOX with `count` small messages; flag mix ~92% \\Seen, 3% \\Flagged, 8% \\Answered."""
    t0 = time.monotonic()
    rng = random.Random(f"{corpus.SEED}:bench:{user}")
    raw = Raw(user=user)

    def items():
        for i in range(count):
            flags = []
            if rng.random() < 0.92:
                flags.append("\\Seen")
            if rng.random() < 0.03:
                flags.append("\\Flagged")
            if rng.random() < 0.08:
                flags.append("\\Answered")
            yield flags, "02-Nov-2026 10:00:00 +0000", bench_message(i)
    multiappend(raw, "INBOX", items())
    _, untagged = raw.ok("EXAMINE INBOX")
    info = select_info(untagged)
    raw.close()
    if info["exists"] != count:
        raise RuntimeError(f"{user}: EXISTS {info}")
    with open(manifest_path, "w") as f:
        json.dump({"user": user, "messages": count, "seconds": round(time.monotonic() - t0, 1), "select": info}, f)


if __name__ == "__main__":
    if sys.argv[1] == "gmail":
        seed_gmail(sys.argv[2], sys.argv[3])
    else:
        seed_bench(sys.argv[2], int(sys.argv[3]), sys.argv[4])
