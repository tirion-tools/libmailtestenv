#!/usr/bin/env python3
"""Build per-list Maildir folders (Dovecot LAYOUT=fs, '/' separator) from the pinned mbox cache.

Input:  MANIFEST.tsv + .cache/mbox/<list@domain>/<YYYY-MM>.mbox (sha256-verified before use).
Output: .cache/maildir/<folder>/{cur,new,tmp}       one Maildir per list, folder from lists.tsv
        .cache/maildir/<folder>/dovecot-uidlist      UIDs 1..n in INTERNALDATE order
        .cache/build/malformed.tsv                    every message with a defect (kept, not dropped)
        .cache/build/duplicates.tsv                   exact duplicates removed within a list
        .cache/build/maildir-index.{tsv,sha256}       path/bytes/mtime of every built file + its digest
        STATS.tsv                                     per list/year counts and header/content shares
                                                      (.cache/build/stats-partial.tsv with --only)

Policy:
- INTERNALDATE (file mtime) = the archive's delivery date from the mbox From_ line; when the
  export had to synthesize it (MAILER-DAEMON ... 1970) the Date header; else the 1st of the month.
- Flags: none. Every message is stored in cur/ with an empty ":2," info, i.e. unseen and not
  \\Recent, so Dovecot never renames files on first access.
- De-duplication: only byte-identical messages (after mboxrd unescaping) within one list.
"""
import argparse
import email
import email.errors
import email.header
import email.policy
import email.utils
import hashlib
import os
import re
import shutil
import sys
import time
import zlib
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch import CACHE, HERE, cache_path, read_lists, read_manifest, sha256_file  # noqa: E402

OUT = CACHE / "maildir"
REPORT = CACHE / "build"
STATS = HERE / "STATS.tsv"

FROM_LINE = re.compile(rb"^From (\S+) +(.+?)\s*$")
# Pony Mail escapes body lines matching the *str* regex ^>*From\s+ (mboxrd). Undo exactly that: one
# '>' off when "From" is followed by a character Python's str \s matches, written here as its UTF-8
# bytes (ASCII whitespace incl. \x1c-\x1f, U+0085, U+00A0, U+1680, U+2000-200A, U+2028/9, U+202F,
# U+205F, U+3000). '\n' is excluded: the exporter split on it, so it never followed "From".
ESCAPED_FROM = re.compile(rb"^>(>*From(?:[ \t\r\f\v\x1c-\x1f]|\xc2[\x85\xa0]|\xe1\x9a\x80"
                          rb"|\xe2\x80[\x80-\x8a\xa8\xa9\xaf]|\xe2\x81\x9f|\xe3\x80\x80))")
HEADER_LINE = re.compile(rb"^[\x21-\x39\x3b-\x7e]+[ \t]*:")
CJK = re.compile("[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]")
NON_ASCII = re.compile("[^\x00-\x7f]")
SIGNATURE_TYPES = {"application/pgp-signature", "application/pkcs7-signature",
                   "application/x-pkcs7-signature"}
SHARE_KEYS = ["dkim", "arc", "list_id", "html", "attachment", "non_ascii", "cjk"]


def split_mbox(path):
    """Yield (index, from_line, message_bytes) from a Pony Mail mboxrd export."""
    cur, from_line, idx = [], None, 0
    with path.open("rb") as f:
        for line in f:
            if line.startswith(b"From "):
                if from_line is not None:
                    yield idx, from_line, finish(cur)
                    idx += 1
                from_line, cur = line, []
            elif from_line is not None:
                cur.append(ESCAPED_FROM.sub(rb"\1", line))
    if from_line is not None:
        yield idx, from_line, finish(cur)


def finish(lines):
    raw = b"".join(lines)
    # The exporter terminates every message with an extra blank line; drop exactly that one.
    return raw[:-1] if raw.endswith(b"\n\n") else raw


def envelope_date(from_line):
    m = FROM_LINE.match(from_line)
    if not m:
        return None
    try:
        dt = datetime.strptime(" ".join(m.group(2).decode("ascii", "replace").split()), "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return None
    if dt.year <= 1970:
        return None  # exporter fallback "From MAILER-DAEMON Thu Jan  1 00:00:00 1970"
    return dt.replace(tzinfo=timezone.utc)


def header_date(value):
    if not value:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(str(value))
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def decode_header_text(value):
    if value is None:
        return ""
    try:
        return str(email.header.make_header(email.header.decode_header(str(value))))
    except (UnicodeError, LookupError, ValueError, email.errors.HeaderParseError):
        return str(value)


def part_text(part):
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "us-ascii"
    try:
        return payload.decode(charset, "replace")
    except LookupError:
        return payload.decode("utf-8", "replace")


def header_problems(raw):
    """Return (problems, header_bytes) for structural header issues the parser silently tolerates."""
    probs = []
    sep = raw.find(b"\n\n")
    sep_crlf = raw.find(b"\r\n\r\n")
    cands = [s for s in (sep, sep_crlf) if s >= 0]
    if not cands:
        probs.append("no-header-body-separator")
        head = raw
    else:
        head = raw[:min(cands)]
    for line in head.split(b"\n"):
        line = line.rstrip(b"\r")
        if not line or line[:1] in (b" ", b"\t"):
            continue
        if not HEADER_LINE.match(line):
            probs.append("bad-header-line")
            break
    return probs


def analyse(raw, from_line, month):
    """Parse one message; return (internaldate, flags dict, problem list)."""
    probs = header_problems(raw)
    if b"\xef\xbf\xbd" in raw:
        probs.append("utf8-replacement-char")  # archiver decoded non-UTF-8 bytes lossily
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    for d in msg.defects:
        probs.append("defect:" + type(d).__name__)
    if msg["From"] is None:
        probs.append("no-from")
    if msg["Message-ID"] is None:
        probs.append("no-message-id")
    hdate = header_date(msg["Date"])
    if hdate is None:
        probs.append("bad-or-missing-date")
    when = envelope_date(from_line)
    if when is None:
        probs.append("no-envelope-date")
        first_day = month if len(month) == 10 else month + "-01"  # period: YYYY-MM or YYYY-MM-DD
        when = hdate or datetime.strptime(first_day, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    f = dict.fromkeys(SHARE_KEYS, False)
    f["dkim"] = msg["DKIM-Signature"] is not None
    f["arc"] = msg["ARC-Seal"] is not None
    f["list_id"] = msg["List-Id"] is not None
    texts = [decode_header_text(msg["Subject"]), decode_header_text(msg["From"])]
    try:
        for part in msg.walk():
            for d in part.defects:
                if part is not msg:
                    probs.append("defect:" + type(d).__name__)
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            disp = part.get_content_disposition()
            if ctype == "text/html":
                f["html"] = True
            if disp == "attachment" or (disp != "inline" and ctype not in SIGNATURE_TYPES and (
                    part.get_filename() or not ctype.startswith(("text/", "message/")))):
                f["attachment"] = True
            if ctype in ("text/plain", "text/html") and disp != "attachment":
                texts.append(part_text(part))
    except Exception as e:  # a parse failure is a finding, never a reason to drop the message
        probs.append("parse-error:" + type(e).__name__)
    joined = "".join(texts)
    f["non_ascii"] = bool(NON_ASCII.search(joined))
    f["cjk"] = bool(CJK.search(joined))
    return when, f, sorted(set(probs))


def build_list(lst, rows):
    folder = OUT / lst["folder"]
    if folder.exists():
        shutil.rmtree(folder)
    for sub in ("cur", "new", "tmp"):
        (folder / sub).mkdir(parents=True)
    seen = {}
    entries = []  # (internaldate, sha, basename)
    malformed, dups = [], []
    stats = defaultdict(lambda: defaultdict(int))
    for row in sorted(rows, key=lambda r: r["month"]):
        path = cache_path(lst["addr"], row["month"])
        got = sha256_file(path)
        if got != row["sha256"]:
            raise RuntimeError(f"{path}: sha256 {got} != pinned {row['sha256']}")
        n = 0
        for idx, from_line, raw in split_mbox(path):
            n += 1
            digest = hashlib.sha256(raw).hexdigest()
            if digest in seen:
                dups.append((lst["addr"], row["month"], idx, seen[digest]))
                continue
            when, flags, probs = analyse(raw, from_line, row["month"])
            epoch = int(when.timestamp())
            vsize = len(raw) + raw.count(b"\n") - raw.count(b"\r\n")
            base = f"{epoch}.G{digest[:24]}.mailtest,S={len(raw)},W={vsize}"
            dest = folder / "cur" / (base + ":2,")
            dest.write_bytes(raw)
            os.utime(dest, (epoch, epoch))
            seen[digest] = base
            entries.append((epoch, digest, base))
            if probs:
                malformed.append((lst["addr"], row["month"], idx, base, ",".join(probs)))
            s = stats[when.year]
            s["messages"] += 1
            s["malformed"] += bool(probs)
            for k in SHARE_KEYS:
                s[k] += flags[k]
        if n != int(row["messages"]):
            raise RuntimeError(f"{path}: split {n} messages, manifest says {row['messages']}")
    entries.sort()
    uidvalidity = (zlib.crc32(lst["folder"].encode()) & 0x7FFFFFFF) or 1
    with (folder / "dovecot-uidlist").open("w") as f:
        f.write(f"3 V{uidvalidity} N{len(entries) + 1}\n")
        for uid, (_, _, base) in enumerate(entries, 1):
            f.write(f"{uid} :{base}\n")
    for d in ("cur", "new", "tmp"):
        os.utime(folder / d)
    return lst["addr"], {y: dict(v) for y, v in stats.items()}, malformed, dups, len(entries)


def write_index():
    """Pin the built tree: one line per file (path, bytes, mtime), sorted. Message file names embed
    sha256 prefixes of their content, so the index digest identifies the pristine snapshot."""
    lines = []
    for root, dirs, files in os.walk(OUT):
        for name in files:
            p = Path(root) / name
            st = p.stat()
            lines.append(f"{p.relative_to(OUT)}\t{st.st_size}\t{int(st.st_mtime)}")
    lines.sort()
    data = ("\n".join(lines) + "\n").encode()
    (REPORT / "maildir-index.tsv").write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    (REPORT / "maildir-index.sha256").write_text(f"{digest}  maildir-index.tsv\n")
    return digest, len(lines), sum(int(l.rsplit("\t", 2)[1]) for l in lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs", type=int, default=os.cpu_count())
    ap.add_argument("--only", help="comma-separated list addresses")
    a = ap.parse_args()
    lists = read_lists()
    if a.only:
        keep = set(a.only.split(","))
        lists = [l for l in lists if l["addr"] in keep]
    pins = read_manifest()
    by_list = defaultdict(list)
    for (addr, _), r in pins.items():
        by_list[addr].append(r)
    lists = [l for l in lists if by_list[l["addr"]]]
    lists.sort(key=lambda l: -sum(int(r["bytes"]) for r in by_list[l["addr"]]))
    REPORT.mkdir(parents=True, exist_ok=True)
    # verify-dovecot.sh's hard-link tree would pin the replaced files' disk blocks.
    shutil.rmtree(CACHE / "dovecot-check", ignore_errors=True)
    t0 = time.time()
    results = {}
    with ProcessPoolExecutor(a.jobs) as pool:
        futs = {pool.submit(build_list, l, by_list[l["addr"]]): l for l in lists}
        for fut in futs:
            addr, stats, malformed, dups, n = fut.result()
            results[addr] = (stats, malformed, dups, n)
            print(f"{addr}: {n} messages, {len(dups)} duplicates removed, {len(malformed)} malformed "
                  f"({time.time() - t0:.0f}s)", flush=True)

    order = [l["addr"] for l in read_lists() if l["addr"] in results]
    with (REPORT / "malformed.tsv").open("w") as f:
        f.write("list\tmonth\tindex_in_mbox\tmaildir_base\tproblems\n")
        for addr in order:
            for r in results[addr][1]:
                f.write("\t".join(map(str, r)) + "\n")
    with (REPORT / "duplicates.tsv").open("w") as f:
        f.write("list\tmonth\tindex_in_mbox\tkept_maildir_base\n")
        for addr in order:
            for r in results[addr][2]:
                f.write("\t".join(map(str, r)) + "\n")

    cols = ["messages", "malformed"] + SHARE_KEYS
    total = defaultdict(lambda: defaultdict(int))
    lines = ["list\tyear\tmessages\tmalformed\t" + "\t".join(f"{k}_pct" for k in SHARE_KEYS)]

    def fmt(addr, year, s):
        n = s.get("messages", 0)
        pct = [f"{100 * s.get(k, 0) / n:.1f}" if n else "" for k in SHARE_KEYS]
        return f"{addr}\t{year}\t{n}\t{s.get('malformed', 0)}\t" + "\t".join(pct)

    for addr in order:
        stats = results[addr][0]
        agg = defaultdict(int)
        for year in sorted(stats):
            lines.append(fmt(addr, year, stats[year]))
            for c in cols:
                agg[c] += stats[year].get(c, 0)
                total[year][c] += stats[year].get(c, 0)
                total["all"][c] += stats[year].get(c, 0)
        lines.append(fmt(addr, "all", agg))
    for year in sorted(k for k in total if k != "all"):
        lines.append(fmt("ALL", year, total[year]))
    lines.append(fmt("ALL", "all", total["all"]))
    # A partial (--only) build reports to .cache/build so the committed STATS.tsv stays complete.
    (REPORT / "stats-partial.tsv" if a.only else STATS).write_text("\n".join(lines) + "\n")
    digest, nfiles, nbytes = write_index()
    print(f"maildir index: {nfiles} files, {nbytes} bytes, sha256 {digest} (.cache/build/maildir-index.tsv)")
    n_all = sum(r[3] for r in results.values())
    n_dup = sum(len(r[2]) for r in results.values())
    n_bad = sum(len(r[1]) for r in results.values())
    print(f"built {n_all} messages in {len(results)} folders, {n_dup} duplicates removed, "
          f"{n_bad} malformed recorded, {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
