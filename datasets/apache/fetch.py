#!/usr/bin/env python3
"""Download monthly mbox exports of public ASF lists from lists.apache.org, pinned in MANIFEST.tsv.

Politeness: strictly sequential, at least 2 s between the end of one request and the start of the
next, one retry after a 30 s backoff, descriptive User-Agent. Each (list, month) is fetched once;
reruns resume from MANIFEST.tsv and only fetch rows that are missing. A row whose cached file has
vanished is re-fetched and must reproduce the pinned sha256, otherwise the run stops (drift).

Usage:
  fetch.py --from 2020-01 --to 2025-12           # full range
  fetch.py --months 2019-06,2022-06,2025-06      # size sample (same cache, same manifest)
  fetch.py --verify                              # re-hash every cached file against the manifest
  fetch.py --split-days --only L@D --months M     # a month whose export stalls, one request per day
"""
import argparse
import hashlib
import http.client
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / ".cache"
MBOX_DIR = CACHE / "mbox"
MANIFEST = HERE / "MANIFEST.tsv"
LISTS = HERE / "lists.tsv"
RUNLOG = CACHE / "fetch-runs.tsv"
API = "https://lists.apache.org/api/mbox.lua"
MIN_GAP_S = 2.0
RETRY_BACKOFF_S = 30.0
CONTACT = os.environ.get("MAILTESTENV_DATASET_CONTACT", "CONTACT-PLACEHOLDER")
USER_AGENT = (f"mailtestenv-dataset-fetch/1.0 (one-off sequential download of public list archives "
              f"for a mail-client test corpus; <=1 req/2s; contact: {CONTACT})")
HEADER = ["list", "month", "url", "bytes", "sha256", "messages", "fetched_utc"]


def read_lists(path=LISTS):
    rows = []
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, domain, kind, folder = line.split("\t")
        rows.append({"list": name, "domain": domain, "kind": kind, "folder": folder,
                     "addr": f"{name}@{domain}"})
    return rows


def read_manifest():
    pins = {}
    if MANIFEST.exists():
        for line in MANIFEST.read_text().splitlines()[1:]:
            if line.strip():
                r = dict(zip(HEADER, line.split("\t")))
                pins[(r["list"], r["month"])] = r
    return pins


def append_manifest(row):
    new = not MANIFEST.exists()
    with MANIFEST.open("a") as f:
        if new:
            f.write("\t".join(HEADER) + "\n")
        f.write("\t".join(str(row[k]) for k in HEADER) + "\n")
        f.flush()
        os.fsync(f.fileno())


def months_between(a, b):
    y, m = map(int, a.split("-"))
    y2, m2 = map(int, b.split("-"))
    out = []
    while (y, m) <= (y2, m2):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def cache_path(addr, month):
    return MBOX_DIR / addr / f"{month}.mbox"


def url_for(lst, period):
    # period is YYYY-MM, or YYYY-MM-DD for a month the server cannot export whole (--split-days).
    # Pony Mail's day span "dfr=D|dto=D" is the open interval (D 00:00:00, D 23:59:59).
    d = period if len(period) == 7 else f"dfr={period}|dto={period}"
    return API + "?" + urllib.parse.urlencode({"list": lst["list"], "domain": lst["domain"], "d": d})


def days_of(month):
    y, m = map(int, month.split("-"))
    n = (datetime(y + m // 12, m % 12 + 1, 1) - datetime(y, m, 1)).days
    return [f"{month}-{d:02d}" for d in range(1, n + 1)]


def sha256_file(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def count_messages(path):
    # The Pony Mail export is mboxrd: body lines matching ^>*From are '>'-escaped, so every line
    # starting with "From " is a message boundary.
    n = 0
    with path.open("rb") as f:
        for line in f:
            if line.startswith(b"From "):
                n += 1
    return n


class Limiter:
    def __init__(self):
        self.last_end = 0.0
        self.requests = 0

    def wait(self, extra=0.0):
        delay = self.last_end + MIN_GAP_S + extra - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def done(self):
        self.last_end = time.monotonic()
        self.requests += 1


def download(url, dest, limiter):
    tmp = dest.with_suffix(".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_err = None
    for attempt in range(2):
        limiter.wait(RETRY_BACKOFF_S if attempt else 0.0)
        h = hashlib.sha256()
        size = 0
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as out:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                ctype = resp.headers.get("Content-Type", "")
                if not ctype.startswith("application/mbox"):
                    raise RuntimeError(f"unexpected Content-Type {ctype!r}")
                for chunk in iter(lambda: resp.read(1 << 20), b""):
                    out.write(chunk)
                    h.update(chunk)
                    size += len(chunk)
            limiter.done()
            os.replace(tmp, dest)
            return size, h.hexdigest()
        except (urllib.error.URLError, http.client.HTTPException, OSError, RuntimeError, TimeoutError) as e:
            # HTTPException covers IncompleteRead: the server cut the chunked stream short.
            limiter.done()
            last_err = e
            print(f"  attempt {attempt + 1} failed: {e}", file=sys.stderr, flush=True)
    tmp.unlink(missing_ok=True)
    raise RuntimeError(f"giving up on {url}: {last_err}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", default="2020-01")
    ap.add_argument("--to", dest="end", default="2025-12")
    ap.add_argument("--months", help="comma-separated explicit months (overrides --from/--to)")
    ap.add_argument("--only", help="comma-separated list addresses to restrict to")
    ap.add_argument("--max-mbox-gb", type=float, default=7.0,
                    help="stop before exceeding this many GB of cached mbox (Maildir build needs the rest)")
    ap.add_argument("--split-days", action="store_true",
                    help="fetch unpinned months one day at a time (for months whose whole export stalls)")
    ap.add_argument("--verify", action="store_true", help="re-hash cached files against MANIFEST.tsv and exit")
    a = ap.parse_args()

    lists = read_lists()
    if a.only:
        keep = set(a.only.split(","))
        lists = [l for l in lists if l["addr"] in keep]
    pins = read_manifest()

    if a.verify:
        bad = 0
        for (addr, month), r in sorted(pins.items()):
            p = cache_path(addr, month)
            got = sha256_file(p) if p.exists() else "missing"
            if got != r["sha256"]:
                bad += 1
                print(f"MISMATCH {addr} {month}: manifest {r['sha256']} cache {got}")
        print(f"verified {len(pins)} rows, {bad} bad")
        return 1 if bad else 0

    months = a.months.split(",") if a.months else months_between(a.start, a.end)
    day_split = {(a, p[:7]) for (a, p) in pins if len(p) == 10}
    todo = []
    for l in lists:
        for m in months:
            if (l["addr"], m) in day_split or (a.split_days and (l["addr"], m) not in pins):
                todo += [(l, d) for d in days_of(m)]
            else:
                todo.append((l, m))
    total_bytes = sum(int(r["bytes"]) for r in pins.values())
    limiter = Limiter()
    t0 = time.time()
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fetched = skipped = 0
    failed, consecutive = [], 0
    try:
        for i, (lst, month) in enumerate(todo, 1):
            key = (lst["addr"], month)
            dest = cache_path(*key)
            pin = pins.get(key)
            if pin and dest.exists() and dest.stat().st_size == int(pin["bytes"]):
                skipped += 1
                continue
            if not pin and dest.exists():
                # Renamed into place but the manifest append was interrupted: pin what we have.
                size, digest = dest.stat().st_size, sha256_file(dest)
            else:
                if total_bytes > a.max_mbox_gb * 1e9:
                    print(f"stopping: cached mbox total {total_bytes / 1e9:.2f} GB exceeds --max-mbox-gb")
                    break
                url = url_for(lst, month)
                try:
                    size, digest = download(url, dest, limiter)
                except RuntimeError as e:
                    # Leave the row unpinned for the next run. Repeated failures mean the server is
                    # unwell: stop instead of hammering it.
                    failed.append(key)
                    consecutive += 1
                    print(f"[{i}/{len(todo)}] FAILED {e}", flush=True)
                    if consecutive >= 3:
                        print("stopping: three consecutive failures")
                        break
                    continue
                consecutive = 0
                fetched += 1
            if pin:
                if digest != pin["sha256"]:
                    raise SystemExit(f"DRIFT {key}: pinned {pin['sha256']} received {digest}; "
                                     f"check the diff, then delete the manifest row to re-pin")
                continue
            row = {"list": lst["addr"], "month": month, "url": url_for(lst, month), "bytes": size,
                   "sha256": digest, "messages": count_messages(dest),
                   "fetched_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            append_manifest(row)
            pins[key] = row
            total_bytes += size
            print(f"[{i}/{len(todo)}] {lst['addr']} {month} {size} B {row['messages']} msgs "
                  f"(total {total_bytes / 1e9:.2f} GB, {time.time() - t0:.0f}s)", flush=True)
    finally:
        RUNLOG.parent.mkdir(parents=True, exist_ok=True)
        new = not RUNLOG.exists()
        with RUNLOG.open("a") as f:
            if new:
                f.write("started_utc\telapsed_s\trequests\tfetched\tskipped\targs\n")
            f.write(f"{started}\t{time.time() - t0:.0f}\t{limiter.requests}\t{fetched}\t{skipped}\t"
                    f"{' '.join(sys.argv[1:])}\n")
    for key in failed:
        print(f"FAILED (unpinned, re-run to retry): {key[0]} {key[1]}")
    print(f"done: {fetched} fetched, {len(failed)} failed, {skipped} already pinned, {limiter.requests} requests, "
          f"{time.time() - t0:.0f}s, manifest total {total_bytes / 1e9:.2f} GB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
