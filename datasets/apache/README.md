# ASF mailing-list test dataset

Real, modern, public list mail for the mail test environment: 28 Apache Software Foundation
lists, 2020-01 through 2025-12, as one Maildir folder per list. It complements the Enron
server (2000–2002 corporate mail) with what Enron lacks: DKIM signatures, ARC sets, `List-Id`
and the other RFC 2369/2919 list headers, Authentication-Results from the ASF MX, HTML parts,
attachments, bot traffic (JIRA, GitHub, Bugzilla, commit mails) and non-ASCII/CJK text
(`user-zh@flink`, Chinese reply attributions on dev lists). It is also the corpus for running
local DKIM verification over real list mail.

Everything downloaded or built lives in `.cache/` (gitignored). Committed: the scripts,
`lists.tsv` (selection), `MANIFEST.tsv` (pins) and `STATS.tsv` (aggregate statistics only).

## Files

| File | Purpose |
|---|---|
| `lists.tsv` | The 28 lists: name, domain, kind (announce/dev/user/issues/commits), IMAP folder. |
| `fetch.py` | Rate-limited downloader; writes `.cache/mbox/<list@domain>/<YYYY-MM>.mbox` and appends `MANIFEST.tsv`. |
| `MANIFEST.tsv` | One row per list-month: list, month, URL, bytes, sha256, message count, fetch time (UTC). |
| `build.py` | mbox → Maildir builder; writes `.cache/maildir/`, `.cache/build/{malformed,duplicates}.tsv`, `STATS.tsv`. |
| `STATS.tsv` | Messages per list/year, malformed count, and % with DKIM-Signature, ARC-Seal, List-Id, HTML, attachment, non-ASCII, CJK. |
| `verify-dovecot.sh` | Opens a hard-link copy of the build in a throwaway Dovecot 2.4 container (`--network none`, no listeners) and prints every folder's count and INTERNALDATE range. |

## Reproduce

```sh
cd datasets/apache
./fetch.py                 # 2020-01..2025-12; resumes from MANIFEST.tsv; failed months stay unpinned
./fetch.py --split-days --only user@spark.apache.org --months 2024-10   # a month whose export stalls
./fetch.py --verify        # re-hash every cached mbox against MANIFEST.tsv
./build.py                 # verifies sha256 again, then builds .cache/maildir (all CPUs)
./verify-dovecot.sh        # optional: Dovecot's view of the build
```

Python 3 standard library only; `verify-dovecot.sh` needs Docker and `dovecot/dovecot:latest` (2.4).

## Source and politeness

- Source: the Pony Mail Foal API that backs lists.apache.org,
  `https://lists.apache.org/api/mbox.lua?list=L&domain=D&d=YYYY-MM`.
- ASF guidance checked before downloading (2026-11-14):
  - `lists.apache.org/robots.txt` does not exist (HTTP 404).
  - Pony Mail's API client guide (`apache/ponymail-foal` `docs/api_client_guide.md`, "Rate Limiting")
    says there is no rate limit on the API but asks clients to be respectful: cache responses and
    avoid tight polling loops.
  - The legacy archive host `mail-archives.apache.org/robots.txt` sets a global crawl-delay of
    two seconds and disallows unlisted bots. That rule is for crawlers of that host; this tool
    does not crawl, but it still keeps the 2 s delay.
- What `fetch.py` does:
  - one request at a time;
  - at least 2 s from the end of one request to the start of the next;
  - one retry after a 30 s backoff, a 120 s read timeout, and a failed list-month left unpinned
    for the next run (three failures in a row stop the run);
  - each list-month fetched exactly once and cached, so re-runs only fetch missing rows.
- User-Agent: `mailtestenv-dataset-fetch/1.0 (one-off sequential download of public list archives for a
  mail-client test corpus; <=1 req/2s; contact: …)`. The contact is `$MAILTESTENV_DATASET_CONTACT`;
  it defaults to the literal `CONTACT-PLACEHOLDER`, and the existing download used that placeholder.
- Pinning:
  - ASF publishes no hashes, so each file is pinned by sha256 after download.
  - The export is not guaranteed byte-stable (`DEPS.env` records one drift).
  - A re-fetch of a pinned row that no longer matches stops with both hashes. To re-pin, check
    the diff, then delete the row.
- Empty months and unknown list names both return HTTP 200 with an empty body. List names were
  checked against `/api/preferences.lua`. An empty month is pinned as 0 bytes (e.g.
  `solr-user@lucene` after the move to `users@solr` in 2021).

## Selection and size

| Kind | Lists |
|---|---|
| announce | announce@apache.org |
| general/dev | general@incubator, dev@{httpd, tomcat, kafka, spark, airflow, cassandra, flink, arrow, beam, logging, commons}, common-dev@hadoop |
| user | users@{httpd, tomcat, kafka, maven, solr}, user@{spark, cassandra, hadoop}, java-user@lucene, solr-user@lucene, user-zh@flink |
| issues/commits | cvs@httpd (svn commits), common-issues@hadoop (JIRA), issues@commons (JIRA/GitHub) |

How the size was chosen:
- The first plan was 2019-01..2025-12. Before the full run, every list was sampled for
  2019-06, 2022-06 and 2025-06 (84 requests, 478 s).
- The samples came to 273.6, 203.8 and 90.9 MB per month of mbox plus Maildir (Maildir counted
  at 4 KiB blocks). Interpolated over the years, that gives about 16 GB for 2019–2025, which is
  over the 15 GB budget, and about 12.8 GB for 2020–2025.
- Following "shrink years before lists", 2019 was dropped. The 2019-06 probe files were deleted
  and removed from the manifest.

## Result (built 2026-11-15)

Totals:
- **574,415 messages** in 28 folders, from 2,182 pinned downloads (`MANIFEST.tsv`).
- 0 exact duplicates.
- 915 messages flagged malformed, all kept:
  - 784 `utf8-replacement-char`
  - 96 `no-message-id`
  - 30 `InvalidMultipartContentTransferEncodingDefect`
  - 3 `StartBoundaryNotFoundDefect`
  - 3 `MissingHeaderBodySeparatorDefect`
  - 2 `no-from`
  - 2 `bad-or-missing-date`

Disk use, in 4 KiB blocks:

| Part | Size |
|---|---|
| mbox cache | 6.57 GB |
| Maildir | 7.65 GB (6.54 GB of file content) |
| **Total** | **14.2 GB (13.2 GiB)**, inside the 15 GB budget |

Pins for the pristine snapshot:
- `sha256(MANIFEST.tsv)` = `1b54962f1feaa0b720fd3a130fa8379c7c5e19ce863e54433670593399bef9dd`
- Maildir index digest = `90897f71e7f53cace792e07edd77bae6599474dbc201a60f8e7e3e3af08d44a5`
  (`.cache/build/maildir-index.sha256`, over 574,443 files: 574,415 messages and 28 `dovecot-uidlist`).

Download:
- 2,260 counted requests (plus a handful of manual probes) over 5 h 26 min of fetch time:
  - 478 s for the size probe;
  - about 4 h 37 min for the main pass, which ran in three resumed segments: an
    `IncompleteRead` crash, then a fix and a restart;
  - 1 h 30 min of Spark retries and day-by-day fetches.
- Wall clock: 2026-11-14 18:40 to 2026-11-15 00:13 UTC.
- Transient failures: one HTTP 503 and one `IncompleteRead`; each succeeded on retry.
- Cross-check: 24 Apache months (Jan–Apr 2025 of six lists) pinned by an independent download two
  days earlier have identical sha256s here.

Known gaps, all on Spark:
- `lists.apache.org` stalls partway through these whole-month exports, every time (at least two
  attempts each, across separate runs):
  - dev@spark: 2024-10
  - user@spark: 2023-03, 2024-04, 2024-10, 2024-11, 2024-12
- Those six months were fetched one day per request instead (`--split-days`; manifest rows with
  a `YYYY-MM-DD` period). The same stall then hits these whole days, which are missing:
  - both lists: 2024-10-08, 2024-10-15, 2024-10-16
  - user@spark also: 2023-03-21, 2024-04-27, 2024-11-13, 2024-11-14, 2024-11-17, 2024-12-02
- Since both lists fail on the same October dates, the cause is probably cross-posted messages
  the server cannot serve [INFERENCE].
- Pony Mail's day span is the open interval (00:00:00, 23:59:59). In those six months, a message
  stamped at exactly one of those two seconds would also be missing.

Other observations:
- 5 users@tomcat messages that Pony Mail files under 2022-01 and 2024-04 were actually delivered
  in 2001. Their `From_` and `Received` lines agree, so their INTERNALDATE is 2001.
- 1 dev@arrow message from the 2025-12 export was delivered 2026-01-01 01:53 UTC.

Per list (2020–2025; `STATS.tsv` has the per-year rows):

| List | Messages | DKIM % | ARC % | HTML % | Attach % | non-ASCII % | CJK % |
|---|---:|---:|---:|---:|---:|---:|---:|
| announce@apache.org | 5,035 | 7.0 | 0.0 | 6.7 | 0.6 | 34.8 | 2.3 |
| general@incubator.apache.org | 14,815 | 49.6 | 2.2 | 0.3 | 0.2 | 70.9 | 34.2 |
| dev@httpd.apache.org | 5,184 | 61.7 | 3.0 | 19.1 | 4.1 | 58.9 | 0.1 |
| users@httpd.apache.org | 4,757 | 54.5 | 8.6 | 50.3 | 2.2 | 62.1 | 1.6 |
| cvs@httpd.apache.org | 7,334 | 0.1 | 0.0 | 0.1 | 0.0 | 22.0 | 5.6 |
| dev@tomcat.apache.org | 49,834 | 5.4 | 0.2 | 1.3 | 0.2 | 14.2 | 1.9 |
| users@tomcat.apache.org | 12,152 | 54.2 | 13.8 | 2.3 | 1.3 | 48.0 | 1.1 |
| dev@kafka.apache.org | 50,182 | 28.0 | 1.7 | 0.4 | 0.4 | 55.2 | 5.9 |
| users@kafka.apache.org | 7,689 | 68.0 | 11.6 | 3.5 | 1.5 | 47.8 | 5.8 |
| dev@spark.apache.org | 9,810 | 72.5 | 2.5 | 87.2 | 1.6 | 86.2 | 26.1 |
| user@spark.apache.org | 9,136 | 81.9 | 7.8 | 89.7 | 1.7 | 77.8 | 6.9 |
| dev@airflow.apache.org | 16,719 | 60.8 | 2.2 | 25.2 | 0.1 | 72.6 | 1.8 |
| dev@cassandra.apache.org | 13,274 | 63.8 | 4.1 | 58.5 | 0.1 | 78.2 | 4.4 |
| user@cassandra.apache.org | 4,190 | 88.6 | 12.2 | 87.3 | 1.3 | 80.0 | 1.9 |
| java-user@lucene.apache.org | 2,077 | 80.4 | 8.6 | 2.4 | 0.6 | 51.9 | 4.6 |
| solr-user@lucene.apache.org | 4,956 | 75.5 | 12.8 | 3.0 | 0.8 | 50.0 | 1.3 |
| users@solr.apache.org | 9,495 | 69.8 | 11.7 | 4.0 | 1.1 | 56.2 | 1.1 |
| common-dev@hadoop.apache.org | 13,361 | 5.7 | 0.2 | 0.2 | 2.1 | 20.4 | 2.2 |
| user@hadoop.apache.org | 974 | 66.0 | 9.9 | 87.6 | 1.6 | 66.0 | 14.9 |
| common-issues@hadoop.apache.org | 138,053 | 2.0 | 0.0 | 0.0 | 0.0 | 18.8 | 0.1 |
| dev@flink.apache.org | 51,507 | 35.8 | 2.0 | 0.5 | 0.5 | 54.7 | 26.4 |
| user-zh@flink.apache.org | 16,477 | 80.7 | 3.6 | 4.2 | 0.4 | 98.7 | 97.6 |
| dev@arrow.apache.org | 17,662 | 72.4 | 2.2 | 0.4 | 0.3 | 55.6 | 4.2 |
| dev@beam.apache.org | 16,156 | 78.8 | 1.2 | 79.5 | 1.4 | 77.3 | 1.8 |
| users@maven.apache.org | 4,973 | 59.8 | 7.1 | 1.3 | 1.5 | 54.4 | 0.7 |
| dev@logging.apache.org | 7,177 | 64.6 | 1.6 | 0.5 | 0.2 | 71.7 | 0.9 |
| dev@commons.apache.org | 9,789 | 80.8 | 2.8 | 0.5 | 0.1 | 54.0 | 4.4 |
| issues@commons.apache.org | 71,647 | 1.0 | 0.0 | 0.0 | 0.0 | 25.4 | 0.3 |
| ALL | 574,415 | 29.4 | 2.1 | 9.2 | 0.4 | 42.1 | 8.1 |

List-Id is present on 100 % of messages. All lists, by year of INTERNALDATE:

| Year | Messages | DKIM % | ARC % | HTML % | non-ASCII % | CJK % |
|---|---:|---:|---:|---:|---:|---:|
| 2020 | 120,640 | 36.4 | 2.3 | 10.5 | 43.5 | 11.9 |
| 2021 | 109,295 | 26.9 | 1.7 | 9.4 | 40.2 | 7.4 |
| 2022 | 97,616 | 26.6 | 2.0 | 10.6 | 40.4 | 7.3 |
| 2023 | 89,049 | 27.8 | 2.2 | 8.8 | 45.0 | 7.6 |
| 2024 | 82,756 | 24.0 | 2.1 | 6.9 | 42.6 | 7.0 |
| 2025 | 75,053 | 32.9 | 2.0 | 8.3 | 41.0 | 6.0 |

How to read the DKIM and ARC shares:
- They count headers present in the archived copy. They say nothing about validity.
- Bot lists pull the overall DKIM share down: common-issues@hadoop, issues@commons, dev@tomcat
  (Bugzilla/GitHub), cvs@httpd and announce (moderated) all sit at 0–7 %. On human lists the
  share is 50–90 %.
- ARC stays at about 2 % in every year. It comes from senders' providers. ASF's MX evidently does not add ARC, otherwise the share would be near 100 %.

## Serving it (testenv)

Recommended: **one IMAP user `apache` with one folder per list.**
- It matches how a real person reads lists: one account, server-side filters into folders.
- It exercises the folder tree, many-mailbox sync, and one 138k-message folder
  (`hadoop/common-issues`).
- It needs one login instead of 28.

Dovecot 2.4 settings, the same as `verify-dovecot.sh` uses:

```
mail_driver = maildir
mail_path = <copy of .cache/maildir>
mailbox_list_layout = fs        # '/' hierarchy separator, e.g. "kafka/dev"
mail_index_path = <separate dir>   # keep indexes out of the pristine tree
```

`INBOX` is empty. The parents (`httpd`, `kafka`, …) are plain directories with no messages.

Keep `.cache/maildir` pristine and serve a working copy. `cp -al` (hard links) costs no disk,
because Dovecot never rewrites a Maildir message file in place: flag changes are renames,
expunges are unlinks, and `dovecot-uidlist` is replaced by rename. A writable server can
therefore use the hard-link copy, and a reset is `rm -rf copy && cp -al`. The files are owned
by uid 1000, which is `vmail` in the `dovecot/dovecot` image.

## Builder policy

- Parsing:
  - The export is mboxrd: every body line matching `^>*From\s+` gets one `>`.
  - `build.py` splits on `From ` lines and removes exactly that escaping, using the exporter's own
    definition of `\s`, including Unicode whitespace.
  - It drops the one blank line the exporter appends to each message.
- INTERNALDATE (the Maildir file's mtime):
  - Normally the delivery time from the mbox `From_` line, i.e. when ASF's archiver received the
    message.
  - When the exporter had to make that line up (`MAILER-DAEMON … 1970`), the `Date:` header is
    used instead; if that is missing, the first day of the month.
- Flags: none. All messages are unseen and not `\Recent`. They are stored in `cur/` with an empty
  `:2,` suffix, so Dovecot never renames them on first open.
- File names: `<epoch>.G<sha256[:24]>.mailtest,S=<size>,W=<CRLF size>:2,`. They are
  deterministic, so rebuilds are byte-identical.
- UIDs: each folder gets a `dovecot-uidlist` with UID 1..n in INTERNALDATE order and a fixed
  UIDVALIDITY (CRC-32 of the folder name). Dovecot 2.4.5 accepts both (checked with `verify-dovecot.sh`).
- De-duplication: only byte-identical messages within one list are removed. Each removal is
  listed in `.cache/build/duplicates.tsv`.
- Malformed messages are kept and listed in `.cache/build/malformed.tsv` with their reasons:
  - `no-header-body-separator`;
  - `bad-header-line`;
  - `no-from`, `no-message-id`, `bad-or-missing-date`;
  - `no-envelope-date`;
  - `defect:<Python email defect>`;
  - `utf8-replacement-char`: U+FFFD in the raw bytes.
- Statistics, counted per message:
  - `dkim`: has a `DKIM-Signature`.
  - `arc`: has an `ARC-Seal`.
  - `list_id`: has a `List-Id`.
  - `html`: has any `text/html` part.
  - `attachment`: has a leaf part with `Content-Disposition: attachment`, or a non-inline leaf
    that has a filename or a non-text type. PGP/S-MIME signature parts don't count.
  - `non_ascii` / `cjk`: the decoded Subject, From and text parts (quoted text included) contain
    a non-ASCII character, or CJK / kana / Hangul.

## Caveats for DKIM work

- Pony Mail stores each source as text and re-encodes it as UTF-8 on export. A message that
  carried non-UTF-8 8-bit bytes comes back altered, and its DKIM body hash cannot verify.
  Those messages are the ones flagged `utf8-replacement-char`.
- Everything else is the archived source as received by ASF's archiver. That includes the
  `Received` / `Authentication-Results` headers from ASF's MX, and any changes the list made
  (subject tags, footers) that break the original sender's signature.
- The export streams messages with a 5 s per-write timeout and ends the response early if a write
  fails. A cut-short month would still have HTTP 200 and complete messages, so pinning cannot
  detect it.
