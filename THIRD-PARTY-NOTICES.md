# Third-party notices

The code in this repository is MIT-licensed (`LICENSE`, Copyright (c) 2026 Tirion) unless this file says otherwise.

## Bundled third-party code and data

### Thunderbird OpenPGP and S/MIME test data (MPL-2.0)

- **What:** everything in `fixtures/thunderbird-mpl/` except `manifest.json` and `PROVENANCE`: 34 test messages,
  OpenPGP test keys (Alice, Bob and Carol, including the two published sample secret keys) and NSS S/MIME test
  data (`tb-smime-Alice.p12`, `tb-smime-Bob.p12`, `tb-smime-TestCA.pem`). They are throwaway test keys and test
  certificates that protect nothing. Unmodified, renamed with a `tb-` prefix.
- **Source:** Thunderbird's comm-central, mirror https://github.com/mozilla/releases-comm-central at commit
  `e2ac505abeb247df2bb605d740a954887d2f4bfd`. `PROVENANCE` gives each file's source path and sha256.
- **Licence:** MPL-2.0; the text is in `fixtures/thunderbird-mpl/LICENSE`. `manifest.json` is MIT-licensed metadata.

## Data downloaded at run time (not in this repository)

- **CMU Enron corpus** (`enron/`): `enron_mail_20150507.tar.gz` from https://www.cs.cmu.edu/~enron/, pinned by
  sha256 and downloaded on first use. CMU distributes it as a research resource and asks users to respect the privacy
  of the people in it. No copy is committed.
- **Apache Software Foundation list archives** (`datasets/apache/`): monthly mbox exports of 28 public lists from
  https://lists.apache.org, downloaded by `fetch.py`. Committed are only the scripts, the list selection, the URL and
  sha256 manifest and aggregate statistics. The archives hold real names and addresses and are never committed.
- **Container packages:** Dovecot, Postfix, OpenSSL, Python and the other packages of the image come from Arch Linux
  and Debian at pinned snapshots when the image is built, under their own licences. Nothing from them is
  redistributed in this repository.

Everything else (the `mail` and `gmail` corpora, accounts, certificates, tokens) is generated at start or by a seeded
generator, with invented names.
