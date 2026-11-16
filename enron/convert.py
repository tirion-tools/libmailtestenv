#!/usr/bin/env python3
"""Converts the CMU Enron tarball (maildir/<owner>/<folder...>/<n>.) into one real Maildir per owner.

  convert.py TARBALL OUTDIR

OUTDIR/mail/<owner>/Maildir/      inbox -> INBOX (cur/), other folders -> Maildir++ ".a.b" subfolders
OUTDIR/users                      Dovecot passwd-file: one user per owner, password "pass"
OUTDIR/MANIFEST.tsv               owner, folders, messages, bytes
OUTDIR/MANIFEST.sha256            sha256 over the sorted "path<TAB>sha256(message)" lines (the snapshot's identity)
Every message is stored as seen (":2,S") with its file mtime set from its Date header, which Dovecot's
maildir driver reports as INTERNALDATE; an unparsable Date falls back to the tarball entry's mtime.
"""
import email.utils
import hashlib
import os
import sys
import tarfile


def sanitize(component):
    return component.replace(".", "_").replace("/", "_") or "_"


def folder_dir(parts):
    if parts and parts[0].lower() == "inbox":
        parts = ["INBOX"] + parts[1:]
        if len(parts) == 1:
            return ""
    return "." + ".".join(sanitize(p) for p in parts)


def date_of(data, fallback):
    head = data.split(b"\n\n", 1)[0].split(b"\r\n\r\n", 1)[0]
    for line in head.split(b"\n"):
        if line[:5].lower() == b"date:":
            try:
                return int(email.utils.parsedate_to_datetime(line[5:].strip().decode("ascii", "replace")).timestamp())
            except (TypeError, ValueError, OverflowError):
                break
    return int(fallback)


def main():
    tarball, out = sys.argv[1], sys.argv[2]
    stats, lines, seq = {}, [], 0
    made = set()
    with tarfile.open(tarball, "r:gz") as tar:
        for m in tar:
            if not m.isfile():
                continue
            parts = m.name.split("/")
            if len(parts) < 4 or parts[0] != "maildir":
                continue
            owner, folders = sanitize(parts[1]), parts[2:-1]
            data = tar.extractfile(m).read()
            seq += 1
            ts = date_of(data, m.mtime)
            fdir = folder_dir(folders)
            base = os.path.join(out, "mail", owner, "Maildir", fdir)
            if base not in made:
                for sub in ("cur", "new", "tmp"):
                    os.makedirs(os.path.join(base, sub), exist_ok=True)
                if fdir:
                    open(os.path.join(base, "maildirfolder"), "w").close()
                made.add(base)
            name = f"{max(ts, 0)}.M{seq}P0.enron:2,S"
            path = os.path.join(base, "cur", name)
            with open(path, "wb") as f:
                f.write(data)
            os.utime(path, (max(ts, 0), max(ts, 0)))
            st = stats.setdefault(owner, {"folders": set(), "messages": 0, "bytes": 0})
            st["folders"].add(fdir or "INBOX")
            st["messages"] += 1
            st["bytes"] += len(data)
            lines.append(f"{owner}/{fdir or 'INBOX'}/{parts[-1]}\t{hashlib.sha256(data).hexdigest()}")
    with open(os.path.join(out, "users"), "w") as f:
        for owner in sorted(stats):
            f.write(f"{owner}:{{PLAIN}}pass::::::\n")
    with open(os.path.join(out, "MANIFEST.tsv"), "w") as f:
        f.write("owner\tfolders\tmessages\tbytes\n")
        for owner in sorted(stats):
            s = stats[owner]
            f.write(f"{owner}\t{len(s['folders'])}\t{s['messages']}\t{s['bytes']}\n")
        f.write(f"TOTAL\t{sum(len(s['folders']) for s in stats.values())}\t{seq}\t{sum(s['bytes'] for s in stats.values())}\n")
    digest = hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()
    with open(os.path.join(out, "MANIFEST.sha256"), "w") as f:
        f.write(digest + "\n")
    print(f"converted {seq} messages for {len(stats)} owners; manifest {digest}")


if __name__ == "__main__":
    main()
