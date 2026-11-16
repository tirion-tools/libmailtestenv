#!/usr/bin/env python3
"""Licence and data-rule check (tools/gate.sh job `licence`).

- LICENSE is the MIT licence with the project's copyright line.
- fixtures/thunderbird-mpl/ is the only MPL-2.0 material: its LICENSE is the MPL-2.0 text, every file in it is
  listed in PROVENANCE with its sha256, and its manifest names only files that exist.
- No mail corpus is committed: no mbox or Maildir files, no archives, no message files outside the MPL fixtures,
  nothing over 1 MiB. The Enron role downloads from one pinned URL and verifies the sha256, with no other source.
"""
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MPL_DIR = os.path.join("fixtures", "thunderbird-mpl")
SKIP_DIRS = {".git", ".state", ".cache", "__pycache__"}
MAX_BYTES = 1 << 20
COPYRIGHT = "Copyright (c) 2026 libmailtestenv contributors"


def files():
    for top, dirs, names in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for n in sorted(names):
            yield os.path.relpath(os.path.join(top, n), ROOT)


def read(rel, mode="r"):
    with open(os.path.join(ROOT, rel), mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        return f.read()


def check():
    problems = []
    lic = read("LICENSE")
    if not lic.startswith(f"MIT License\n\n{COPYRIGHT}\n") or "THE SOFTWARE IS PROVIDED \"AS IS\"" not in lic:
        problems.append("LICENSE is not the MIT licence with the project copyright line")
    if not read(os.path.join(MPL_DIR, "LICENSE")).startswith("Mozilla Public License Version 2.0\n"):
        problems.append(f"{MPL_DIR}/LICENSE is not the MPL-2.0 text")

    listed = {}
    for line in read(os.path.join(MPL_DIR, "PROVENANCE")).splitlines():
        m = re.match(r"^([0-9a-f]{64})  (\S+)  <- (\S+)$", line)
        if m:
            listed[os.path.join(MPL_DIR, m.group(2))] = m.group(1)
    own = {os.path.join(MPL_DIR, n) for n in ("LICENSE", "PROVENANCE", "manifest.json")}
    present = {f for f in files() if f.startswith(MPL_DIR + os.sep)} - own
    for f in sorted(present - set(listed)):
        problems.append(f"{f}: not listed in PROVENANCE")
    for f in sorted(set(listed) - present):
        problems.append(f"{f}: listed in PROVENANCE but missing")
    for f in sorted(present & set(listed)):
        if hashlib.sha256(read(f, "rb")).hexdigest() != listed[f]:
            problems.append(f"{f}: sha256 differs from PROVENANCE")
    manifest = json.loads(read(os.path.join(MPL_DIR, "manifest.json")))
    named = [x["file"] for x in manifest["fixtures"]]
    keys = manifest["keys"]
    named += keys["secret_keys"] + keys["certs"] + keys["smime_anchors"] + [p["file"] for p in keys["pkcs12"]]
    for n in named:
        if os.path.join(MPL_DIR, n) not in listed:
            problems.append(f"manifest.json names {n}, which PROVENANCE does not list")

    for f in files():
        if "Mozilla Public License" in read(f, "rb").decode("utf-8", "replace") and not f.startswith(MPL_DIR + os.sep):
            if f != os.path.join("tools", "licence_check.py") and f != "README.md":
                problems.append(f"{f}: MPL text outside {MPL_DIR}")
        parts = f.split(os.sep)
        if re.search(r"\.(mbox|eml|msg|tar|tgz|gz|xz|zst|zip|7z)$", f) and not f.startswith(MPL_DIR + os.sep):
            problems.append(f"{f}: mail or archive file outside {MPL_DIR}")
        if {"cur", "new"} & set(parts[:-1]):
            problems.append(f"{f}: looks like a committed Maildir")
        if os.path.getsize(os.path.join(ROOT, f)) > MAX_BYTES:
            problems.append(f"{f}: larger than 1 MiB")

    role = read(os.path.join("enron", "role.env"))
    if not re.search(r"^ENRON_SHA256=[0-9a-f]{64}$", role, re.M) or "sha256sum -c" not in role:
        problems.append("enron/role.env: the tarball is not sha256-pinned and verified")
    if re.search(r"^\s*(cp|ln|rsync)\s", role, re.M) or len(re.findall(r"^ENRON_URL=https://", role, re.M)) != 1:
        problems.append("enron/role.env: a source other than the one pinned download")
    return problems


if __name__ == "__main__":
    found = check()
    for p in found:
        print(f"licence: {p}")
    print(f"licence: {'FAIL' if found else 'ok'} ({len(found)} problems)")
    sys.exit(1 if found else 0)
