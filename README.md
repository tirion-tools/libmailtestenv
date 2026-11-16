# libmailtestenv

Local IMAP and SMTP servers for testing mail clients, in containers on host loopback: a TLS and OAuth server with
every certificate and token failure a client must handle, a Gmail IMAP stand-in, and two public real-world corpora
served over IMAP. One command starts, stops, resets or describes them. Clients find everything they need (hosts,
ports, TLS modes, the CA, accounts, credential files) in one versioned JSON document.

## Builds on

This is glue around existing servers; it does not replace them, and fixes found in them go upstream:

- **[Dovecot](https://www.dovecot.org/)** 2.4 serves every IMAP endpoint: Maildir and mdbox stores, CONDSTORE, QRESYNC,
  per-SNI certificates (`local_name`), and XOAUTH2/OAUTHBEARER with JWTs validated locally (`oauth2 { introspection_mode
  = local }`).
- **[Postfix](https://www.postfix.org/)** serves submission on 587 (STARTTLS) and 465 (implicit TLS), authenticates through
  Dovecot and delivers through Dovecot LMTP, with per-SNI certificates (`tls_server_sni_maps`).
- **Python 3**, standard library only, for the Gmail stand-in, the corpus generator, the dataset converters and the
  tooling. **OpenSSL** issues the throwaway PKI and signs the tokens. **socat** and **rsync** move bytes and files.

All of them run unmodified from Arch Linux packages in one container image, pinned by base-image digest and by the Arch
Linux Archive snapshot of 2026-11-12, with every package version asserted at build time (`Dockerfile`).

What it adds:
- **A Gmail IMAP stand-in** (`gmail/`): a proxy in front of Dovecot that reproduces Gmail's advertised capabilities (no
  QRESYNC), XLIST, `X-GM-MSGID`/`X-GM-THRID`/`X-GM-LABELS`, labels-as-folders semantics for MOVE, COPY, APPEND,
  EXPUNGE, `UID STORE ±X-GM-LABELS` and CREATE/RENAME/DELETE of user labels, hidden All Mail and Folder Size Limits.
  Combinations it does not model are refused with `NO [CANNOT]`. Behaviour not yet confirmed against Gmail is marked
  as inferred in `gmail/gmail_proxy.py`. No existing test server emulates Gmail's IMAP extensions.
- **Hostile fixtures:**
  - the stand-in's corpus (`gmail/corpus.py`): 5,000 deterministic synthetic messages with threads, RFC 2047/2231
    headers in five scripts, attachments and nine kinds of malformed message (no Message-ID, no Date, a bad Date, broken
    encoded words, an unknown charset, a raw 8-bit Subject, an unterminated multipart, no From, a very long Subject);
  - a TLS and OAuth negative-path matrix (`mail` role): a throwaway CA; valid, wrong-name and untrusted-issuer
    certificates selected by SNI; valid, bad-signature and wrong-subject tokens;
  - Thunderbird's OpenPGP and S/MIME test messages (`fixtures/thunderbird-mpl/`, MPL-2.0): tampered and stripped
    integrity protection, signature wrapping, partial encryption, spoofed signers, with expected outcomes.
- **Dataset roles** that serve public corpora over IMAP with a read-only pristine copy, a writable working copy and a
  one-command reset: the CMU Enron corpus (`enron/`) and 28 Apache Software Foundation lists (`datasets/apache/`,
  `apache/`). Corpora are downloaded and hash-verified, never committed.
- **The env JSON contract** (`schema/env.schema.json`) and a second-instance mode (container prefix, port offset) so a
  gate or a CI job can run beside a developer's instance.

Related projects, and why this does not use them:
- [GreenMail](https://greenmail-mail-test.github.io/greenmail/) (embeddable Java SMTP/POP3/IMAP servers): no
  CONDSTORE/QRESYNC/ESEARCH, one keystore, OAuth tokens compared as passwords.
- [Mailpit](https://mailpit.axllent.org/) and [smtp4dev](https://github.com/rnwood/smtp4dev): SMTP capture with a web
  UI (Mailpit's Chaos injects SMTP errors); no IMAP (Mailpit) or a basic one (smtp4dev).
  [MailHog](https://github.com/mailhog/MailHog) is their unmaintained predecessor.
- [docker-mailserver](https://github.com/docker-mailserver/docker-mailserver): a production Dovecot/Postfix stack. Every
  behaviour needed here (an SNI certificate matrix with a rogue CA, local JWT validation, a delivery blackhole) would be
  an override of its defaults.
- [Toxiproxy](https://github.com/Shopify/toxiproxy) (TCP fault injection), [Dovecot ImapTest](https://github.com/dovecot/imaptest)
  (IMAP compliance and stress testing of servers), [Gluon](https://github.com/ProtonMail/gluon) (a Go IMAP server
  library; Gmail labels proposed in gluon#450).

## Scope

In scope: servers and fixtures that a mail client's tests connect to, on one machine. Out of scope: anything a
production server needs (spam filtering, real DNS, delivery to the internet, durable storage), and anything about a
particular client.

## Security

- **Loopback only.** Every endpoint is published on `127.0.0.1` and nowhere else; the document says so (`host`).
- **Never ports 143 or 5232 on the host.** They are often taken by a real server on the same machine (an IMAP server, a
  CalDAV server). `testenv.sh` refuses any setting that would publish either, whatever the port offset, and
  `MAILTESTENV_DENY_PORTS` adds more. Container names always start with `mailtestenv`, so no other container is ever
  stopped or replaced.
- **Throwaway secrets.** Every `mail` start issues a new CA (valid 7 days), server certificates, a JWT signing key and
  random passwords. Private keys never leave the container; the host sees certificates, `secrets.tsv` and the tokens.
  The `gmail`, `enron` and `apache` roles use the fixed password `pass`. None of this is fit for anything but tests.
- **No mail data in the repository** (see "Data rules").

## Roles

| Role | Container | Endpoints (127.0.0.1) | What it serves |
|---|---|---|---|
| `mail` | `mailtestenv-mail` | IMAP 11143 (STARTTLS), IMAPS 11993, submissions 11465, submission 11587 | Dovecot + Postfix, TLS and OAuth matrix, users alice, bob, bulk (200 synthetic messages) |
| `gmail` | `mailtestenv-gmail` | IMAP 12143 (plain) | the Gmail stand-in, 4 users |
| `enron` | `mailtestenv-enron` | IMAP 13143 (plain or STARTTLS), IMAPS 13993, submissions 13465, submission 13587 | the CMU Enron corpus, one user per mailbox owner |
| `apache` | `mailtestenv-apache` | IMAP 14143 (plain) | 28 ASF lists 2020–2025, one user, one folder per list |

```sh
./testenv.sh up                 # mail + gmail (default)
./testenv.sh up enron           # Enron (first start downloads, verifies and converts the corpus)
./testenv.sh env                # endpoints and credential files
./testenv.sh env --json         # the same as the env document
./testenv.sh status             # containers, disk, token and certificate lifetimes
./testenv.sh reset gmail        # discard all changes, back to the pristine snapshot
./testenv.sh up apache          # ASF lists (needs datasets/apache built)
./testenv.sh down               # stop everything
./testenv.sh purge              # stop everything and delete the state directory
./testenv.sh selftest           # regression tests of the Gmail stand-in (throwaway container)
```

Settings (environment variables):

| Variable | Default | Meaning |
|---|---|---|
| `MAILTESTENV_STATE` | `./.state` | State directory: certificates, secrets, corpora, `env.json`. |
| `MAILTESTENV_PREFIX` | `mailtestenv` | Container name prefix; must start with `mailtestenv`. |
| `MAILTESTENV_PORT_OFFSET` | `0` | Added to every host port (a second instance). |
| `MAILTESTENV_DENY_PORTS` | empty | More host ports that must never be published (143 and 5232 always are). |
| `MAILTESTENV_APACHE_DIR` | `datasets/apache/.cache` | Where `datasets/apache` downloads and builds the ASF dataset. |
| `MAILTESTENV_DATASET_CONTACT` | `CONTACT-PLACEHOLDER` | Contact in the ASF fetcher's User-Agent; set it before downloading. |

Each role keeps its state in `STATE/<role>/`. Files written by the containers belong to container users: delete them
with `reset` or `purge`, which go through the image.

## Env JSON

`testenv.sh env --json` prints the env document, and `testenv.sh up` writes the same document to `STATE/env.json`
(replaced by rename). The schema is `schema/env.schema.json`; `tests/test_env_contract.py` checks the document
against it and checks that this example is exactly what `env --json` prints for that state directory:

- `schema` is `mailtestenv-env` and `version` is `1`. Version 1 may gain optional members. A change that breaks a
  version-1 reader increments `version`, so a reader checks both.
- File paths (`ca`, `secrets`, `accounts_file`, `maildir`, `tls_matrix.rogue_ca`) are relative to the state directory, that is
  the directory holding `env.json`. A reader that sees the state somewhere else (a bind mount in a container) resolves
  them against the directory it read `env.json` from. `state_dir` only records where the producer saw it.
- `server_names[0]` is the name to send as SNI and to validate the certificate against; connect to `host`.
- `endpoints.<name>.tls`: `none` (plain only), `starttls` (required before authentication), `starttls-optional`
  (offered, plain authentication allowed), `implicit` (TLS from the first byte).
- Credentials: `password` is the one password of every account of the role. When it is null, `secrets` is a TSV file
  with the header `account kind host secret`. Its kinds are `password` and the `oauth.token_kinds`: a valid access
  token (24 h lifetime, re-minted hourly), one with a bad signature, one for the wrong subject. Read it when a test
  run starts.
- `accounts` lists the users. A role with many users (`enron`) names an `accounts_file` instead: a TSV with a header
  line and the user in the first column.
- `maildir` is the read-only pristine Maildir tree of a corpus role (`enron`: one directory per mailbox owner), for
  readers that use the messages without IMAP; null for the other roles.

<!-- env-json-example -->
```json
{
  "schema": "mailtestenv-env",
  "version": 1,
  "state_dir": "/srv/mailtestenv/state",
  "prefix": "mailtestenv",
  "port_offset": 0,
  "roles": {
    "mail": {
      "container": "mailtestenv-mail",
      "host": "127.0.0.1",
      "server_names": [
        "mail.mailtestenv.test",
        "dns-a.mailtestenv.test"
      ],
      "endpoints": {
        "imap": {
          "port": 11143,
          "tls": "starttls"
        },
        "imaps": {
          "port": 11993,
          "tls": "implicit"
        },
        "submissions": {
          "port": 11465,
          "tls": "implicit"
        },
        "submission": {
          "port": 11587,
          "tls": "starttls"
        }
      },
      "ca": "mail/pki/ca.crt",
      "secrets": "mail/secrets.tsv",
      "accounts": [
        "alice@mailtestenv.test",
        "bob@mailtestenv.test",
        "bulk@mailtestenv.test"
      ],
      "accounts_file": null,
      "password": null,
      "maildir": null,
      "tls_matrix": {
        "valid": [
          "mail.mailtestenv.test",
          "dns-a.mailtestenv.test"
        ],
        "wrong_name": "wrong.mailtestenv.test",
        "untrusted_issuer": "rogue.mailtestenv.test",
        "rogue_ca": "mail/pki/rogue-ca.crt"
      },
      "oauth": {
        "issuer": "https://issuer.mailtestenv.test",
        "mechanisms": [
          "XOAUTH2",
          "OAUTHBEARER"
        ],
        "token_kinds": [
          "oauth2-access-token",
          "oauth2-access-token-bad-signature",
          "oauth2-access-token-wrong-subject"
        ]
      }
    },
    "gmail": {
      "container": "mailtestenv-gmail",
      "host": "127.0.0.1",
      "server_names": [
        "gmail.mailtestenv.test"
      ],
      "endpoints": {
        "imap": {
          "port": 12143,
          "tls": "none"
        }
      },
      "ca": null,
      "secrets": null,
      "accounts": [
        "gmail",
        "gmailhidden",
        "gmailcapped",
        "gmailcap5k"
      ],
      "accounts_file": null,
      "password": "pass",
      "maildir": null
    },
    "enron": {
      "container": "mailtestenv-enron",
      "host": "127.0.0.1",
      "server_names": [
        "enron.mailtestenv.test",
        "mail.mailtestenv.test"
      ],
      "endpoints": {
        "imap": {
          "port": 13143,
          "tls": "starttls-optional"
        },
        "imaps": {
          "port": 13993,
          "tls": "implicit"
        },
        "submissions": {
          "port": 13465,
          "tls": "implicit"
        },
        "submission": {
          "port": 13587,
          "tls": "starttls"
        }
      },
      "ca": "mail/pki/ca.crt",
      "secrets": null,
      "accounts": [],
      "accounts_file": "enron/pristine/MANIFEST.tsv",
      "password": "pass",
      "maildir": "enron/pristine/mail"
    },
    "apache": {
      "container": "mailtestenv-apache",
      "host": "127.0.0.1",
      "server_names": [
        "apache.mailtestenv.test"
      ],
      "endpoints": {
        "imap": {
          "port": 14143,
          "tls": "none"
        }
      },
      "ca": null,
      "secrets": null,
      "accounts": [
        "apache"
      ],
      "accounts_file": null,
      "password": "pass",
      "maildir": null
    }
  }
}
```

## `mail`: Dovecot and Postfix with a TLS and OAuth matrix

Every start generates a throwaway CA, a rogue CA, server certificates, an RS256 key for OAuth2 tokens, random passwords
and a 200-message synthetic corpus for `bulk`, then writes for clients `STATE/mail/pki/ca.crt` (the CA to trust),
`rogue-ca.crt` and `secrets.tsv`. The tokens live 24 h and the container re-mints all three every hour for as long as
it runs, so a token read at any time has at least 23 h left. `testenv.sh status` warns when the token has under 12 h
left (the refresher stopped) and when the server certificate (7 days) expires within 24 h; `testenv.sh reset mail`
regenerates everything.

Users `alice@mailtestenv.test`, `bob@mailtestenv.test`, `bulk@mailtestenv.test`. Each has INBOX and, from the first
login, the subscribed special-use mailboxes `Sent` (`\Sent`), `Drafts` (`\Drafts`), `Trash` (`\Trash`) and `Junk`
(`\Junk`), as a real provider has (RFC 6154; Dovecot `auto = subscribe`), so no client or test needs to create them.
TLS is required before authentication. Certificates depend on SNI: `mail.mailtestenv.test` and
`dns-a.mailtestenv.test` get the valid chain, any other name (or none) the `wrong.mailtestenv.test` certificate, and
`rogue.mailtestenv.test` a certificate with the right name from an untrusted CA. Postfix delivers only to `mailtestenv.test` (`default_transport = error`).
XOAUTH2/OAUTHBEARER tokens are validated locally by Dovecot (issuer `https://issuer.mailtestenv.test`).

## `gmail`: Gmail IMAP stand-in

Dovecot (mdbox, loopback inside the container) behind `gmail_proxy.py`, published through socat. Users (password
`pass`): `gmail` (normal), `gmailhidden` (All Mail hidden), `gmailcapped` (Folder Size Limit 2,000), `gmailcap5k`
(5,000). The first start seeds the deterministic corpus into `STATE/gmail/pristine/`. Every start then serves
`STATE/gmail/work/`, a fresh copy, so tests may move, flag and expunge freely, and `reset gmail` (or any restart)
returns to the seeded state: the proxy's label model lives in memory and starts from the seeded one, so a used `work/`
must not outlive the proxy. The proxy writes per-session byte counts to `STATE/gmail/work/proxy.jsonl` and errors to
`proxy.out`.

Bounds, so that no single command can exhaust the proxy: a client's UID set is never expanded into a list; a
server-generated set (COPYUID) expands to at most 1,000,000 UIDs; a client literal over 64 MiB gets `* BYE`; a
side-connection reply over 256 MiB fails; a model update that fails or runs past 120 s answers `NO [SERVERBUG]`
(Dovecot has applied it; the model may be stale, `reset gmail`). The internal side connection reconnects when Dovecot
has closed it. Process bounds catch a runaway the rest miss: 2 GiB of address space and 1 hour of CPU time, and the
container stops when the proxy exits.

Known gaps: no TLS and no XOAUTH2 on this role (Dovecot here offers plain/login), no `X-GM-RAW` and no `SEARCH X-GM-*`.

`testenv.sh selftest` runs `tests/gmail_proxy_test.py` in a throwaway container (no network, state on tmpfs, nothing
published): its own Dovecot with the gmail configuration, a three-message account and the proxy, driven with imaplib.
Run it after any change to `gmail/`.

## `enron`: the CMU Enron corpus

`enron_mail_20150507.tar.gz` from CMU (https://www.cs.cmu.edu/~enron/), sha256 pinned in `enron/role.env`. The first
`up enron` downloads it into `STATE/enron/dl/`, verifies it and mounts it read-only; there is no other source. The
first start converts it (`enron/convert.py`, about 1.5 min) into `STATE/enron/pristine/`, which is then read-only:

- one Dovecot user per mailbox owner (150 users, password `pass`, e.g. `allen-p`, `kaminski-v`);
- a Maildir per owner: `inbox` → INBOX, every other folder → a folder with its original name (`.` becomes `_`);
  messages are seen, with the file mtime (INTERNALDATE) from the `Date` header;
- `MANIFEST.tsv` (owner, folders, messages, bytes: 150 owners, 3,305 folders, 517,394 messages) and
  `MANIFEST.sha256`, the hash of the sorted `path<TAB>sha256(message)` list.

Dovecot serves `STATE/enron/work/`, an rsync copy; `testenv.sh reset enron` (about 45 s) restores the pristine state.
Disk: 0.4 GB tarball + 2.7 GB pristine + 2.7 GB working copy. TLS: IMAPS on 13993 and STARTTLS on 13143 (plain IMAP
on 13143 also works), with a certificate for `enron.mailtestenv.test` and `mail.mailtestenv.test` from the `mail`
role's CA, issued at every `mail` start into `STATE/mail/private/` (root-only, mounted read-only into the enron
container). Start `mail` before `enron`; a later `mail` start reloads a running enron.

SMTP, so an Enron account can be added through a client's setup: Postfix submission on 13587 (STARTTLS required
before AUTH) and submissions on 13465 (implicit TLS), same certificate and CA, signing in the same users as IMAP
(SASL through the role's Dovecot, password `pass`). Unauthenticated clients are refused. Accepted mail is discarded and
logged in the container's `/var/log/postfix.log`; nothing is relayed or delivered.

## `apache`: ASF mailing lists

The dataset of `datasets/apache/` (its README has the selection, source politeness, pins and builder policy): 28
Apache Software Foundation lists, 2020-01 to 2025-12, built by `build.py` from 2,182 sha256-pinned monthly mbox
exports (`MANIFEST.tsv`), 574,415 messages. It adds what Enron lacks: DKIM signatures, ARC sets, list headers,
Authentication-Results, HTML parts, bot traffic and non-ASCII text. One IMAP user `apache` (password `pass`), one
folder per list with `/` hierarchy.

`testenv.sh up apache` refuses to start until the dataset is built in `MAILTESTENV_APACHE_DIR`. It then serves
`STATE/apache/work/mail`, a hard-link tree of the dataset (`cp -al`, no extra disk; Dovecot never rewrites a message
file in place), with indexes outside the tree. `testenv.sh reset apache` deletes `work/` and the next start links it
again (about 25 s).

## Data rules

- **Enron:** download and verify only. The tarball comes from CMU's URL, pinned by sha256; no copy is committed and
  there is no fallback to any other copy. CMU distributes it as a research resource and asks users to respect the
  privacy of the people in it.
- **ASF lists:** scripts and a manifest only. `datasets/apache/` commits the fetcher, the builder, `lists.tsv`,
  `MANIFEST.tsv` (URLs, hashes, counts) and `STATS.tsv` (aggregate statistics). No message is committed: the archives
  hold real names and addresses.
- **Thunderbird fixtures:** `fixtures/thunderbird-mpl/` holds unmodified copies of Thunderbird's OpenPGP and S/MIME
  test data under the Mozilla Public License 2.0 (`fixtures/thunderbird-mpl/LICENSE`), with each file's source path and
  sha256 in `PROVENANCE`. Nothing outside that directory is MPL-licensed.
- **Everything else is synthetic:** generated at start (the `mail` corpus) or by a seeded generator (the `gmail`
  corpus). Names in the generators are invented.

`tools/licence_check.py` enforces these rules in the gate.

## Gate

`tools/gate.sh` runs on the host (Docker and Python 3 needed):
- `static`, in the pinned gate container (`tools/container/Dockerfile`: Debian trixie by digest, packages from the
  2026-11-12 snapshot at exact versions): ShellCheck, shfmt (`-i 2 -ci`), a Python syntax check, the unit tests
  (`tests/test_*.py`: the env contract, the corpus pin) and the licence check;
- `selftest`: `testenv.sh selftest`;
- `smoke`: `tools/smoke.py` starts the `mail` role as a private instance (prefix `mailtestenv-gate`, host ports +
  30000, a temporary state directory) and works through the env document alone: the document is valid and equals
  `STATE/env.json`; IMAPS and IMAP STARTTLS logins validate against the CA and the primary name; alice has Sent,
  Drafts, Trash and Junk with their special-use flags, subscribed; the wrong-name and untrusted-issuer certificates
  fail validation; XOAUTH2 accepts the valid token and refuses the bad-signature token;
  submission and submissions authenticate, and a message reaches bob's INBOX. Then `down` and `purge`.

## Licence

MIT (`LICENSE`), except `fixtures/thunderbird-mpl/` (MPL-2.0, see Data rules).
