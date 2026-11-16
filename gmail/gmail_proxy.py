"""Gmail stand-in: an IMAP proxy in front of Dovecot that models Gmail's IMAP behaviour.

What it changes relative to plain Dovecot (each item cites what it imitates):
  * CAPABILITY is Gmail's captured list (pre- and post-auth, emersion's capture:
    https://gist.github.com/emersion/2c769bc1ed60a7b7945910d35b606801) intersected with what Dovecot can back,
    plus X-GM-EXT-1 and an emulated XLIST. QRESYNC is never advertised and is removed from ENABLE, as on
    Gmail (https://issuetracker.google.com/issues/148814760). CONDSTORE and ESEARCH come from Dovecot.
  * X-GM-MSGID / X-GM-THRID / X-GM-LABELS are stripped from FETCH commands and injected into the responses
    from a per-user Gmail model (message -> labels, folder -> {UID: X-GM-MSGID}).
  * Label semantics on mutations (Gmail treats labels as folders,
    https://developers.google.com/workspace/gmail/imap/imap-extensions):
      - MOVE into [Gmail]/Spam or [Gmail]/Trash removes the message from All Mail and from every label folder
        (All Mail excludes Spam and Trash [INFERENCE: third-party documentation]); Spam clears labels, Trash
        keeps them and still reports them in X-GM-LABELS (worst case for a client) [INFERENCE];
      - MOVE out of Spam/Trash into a label folder restores the message to All Mail with that one label;
      - MOVE / COPY between label folders changes labels; the All Mail copy's MODSEQ is bumped so CONDSTORE
        clients see the label change [INFERENCE: Gmail bumps MODSEQ on label changes];
      - APPEND to a label folder also places the message in All Mail with a new X-GM-MSGID;
      - EXPUNGE in Spam/Trash deletes forever; EXPUNGE in a label folder removes that label.
      - UID STORE +/-X-GM-LABELS on All Mail adds/removes labels (copy into / expunge from the label folder, All Mail
        MODSEQ bumped), as on Gmail (https://developers.google.com/workspace/gmail/imap/imap-extensions#access_to_gmail_labels_x-gm-labels);
        the command is answered after Dovecot answers a NOOP sent in its place, so the client sees OK after the effect; no untagged
        FETCH (X-GM-LABELS ...) echo is sent, even without .SILENT.
      - CREATE makes a user label of the same name; RENAME of a user label (nested ones with it) renames the label on
        its messages; DELETE removes the label from its messages, which stay in All Mail. Label changes bump MODSEQ.
        RENAME and DELETE of INBOX and [Gmail]/ folders answer NO [CANNOT], as on Gmail.
    Unmodelled combinations (MOVE out of All Mail to a label, COPY into All Mail/Spam/Trash, STORE X-GM-LABELS outside
    All Mail, with a `*` UID set or naming an unknown label) are refused with NO [CANNOT] rather than silently given Dovecot semantics.
  * "Show in IMAP" off: hidden folders are dropped from LIST/XLIST/LSUB/STATUS and SELECT/EXAMINE/STATUS/
    MOVE/COPY to them answer NO [NONEXISTENT].
  * Folder Size Limit (Gmail setting maxFolderSize, legal values 1000/2000/5000/10000,
    https://developers.google.com/workspace/gmail/api/reference/rest/v1/ImapSettings): every folder exposes
    at most `cap` messages, the newest by UID [INFERENCE: which messages Gmail keeps is not documented];
    after each APPEND the oldest are removed from the IMAP view.
  * Bytes are counted per client session (JSONL log). Internal side-connection traffic is not counted.
  * Bounds (no single command may exhaust the proxy): client UID sets are never expanded into lists (STORE passes the
    client's set to Dovecot; X-GM-LABELS stores test the model's All Mail UIDs against it), server-generated sets
    expand to at most MAX_SET_EXPANSION UIDs, client literals over MAX_LITERAL close the connection, a side-connection
    reply over SIDE_REPLY_MAX fails, and a model update that fails or runs past MODEL_TIMEOUT seconds answers the
    client's command `NO [SERVERBUG]` (Dovecot already applied it; the model may be stale: `testenv.sh reset gmail`).
    The side connection reconnects when Dovecot closed it (autologout after 30 idle minutes, BYE).
  * Process bounds, for a runaway the per-command bounds miss: address space MAX_ADDRESS_SPACE (an allocation past it
    fails with MemoryError, which ends that client's connection or the proxy) and MAX_CPU_SECONDS of CPU time in all
    (SIGXCPU kills the proxy; a day of shared use costs about a minute). gmail/start.sh stops the container when the
    proxy exits, so `docker ps` shows it.

Usage: gmail_proxy.py <listen-port> <upstream-port> <gmail-state.json> <log.jsonl> <caps.json>
"""
import asyncio
import base64
import json
import re
import resource
import sys
import traceback

import corpus

ALL, SPAM, TRASH = corpus.GMAIL_ALL, corpus.GMAIL_SPAM, corpus.GMAIL_TRASH
LABEL_OF = {folder: label for label, folder in corpus.GMAIL_LABEL_FOLDERS.items()}
GMAIL_PREAUTH = ("IMAP4rev1 UNSELECT IDLE NAMESPACE QUOTA ID XLIST CHILDREN X-GM-EXT-1 XYZZY SASL-IR AUTH=XOAUTH2 "
                 "AUTH=PLAIN AUTH=PLAIN-CLIENTTOKEN AUTH=OAUTHBEARER AUTH=XOAUTH").split()
GMAIL_POSTAUTH = ("IMAP4rev1 UNSELECT IDLE NAMESPACE QUOTA ID XLIST CHILDREN X-GM-EXT-1 UIDPLUS COMPRESS=DEFLATE "
                  "ENABLE MOVE CONDSTORE ESEARCH UTF8=ACCEPT LIST-EXTENDED LIST-STATUS APPENDLIMIT=35882577").split()
EMULATED = {"X-GM-EXT-1", "XLIST"}
XLIST_ATTRS = {b"\\All": b"\\AllMail", b"\\Junk": b"\\Spam", b"\\Flagged": b"\\Starred"}

LITERAL_RE = re.compile(rb"\{(\d+)\+?\}\r\n$")
GM_ITEM_RE = re.compile(rb"\s*\bX-GM-(?:MSGID|THRID|LABELS)\b", re.I)
UID_RE = re.compile(rb"\bUID (\d+)")
CAP_RE = re.compile(rb"CAPABILITY [^\]\r\n]*")
COPYUID_RE = re.compile(rb"\[COPYUID \d+ ([0-9:,]+) ([0-9:,]+)\]")
APPENDUID_RE = re.compile(rb"\[APPENDUID \d+ (\d+)\]")
ASTRING = rb'("(?:[^"\\]|\\.)*"|\S+)'
LABEL_STORE_RE = re.compile(rb"^(\S+) ([+-])X-GM-LABELS(?:\.SILENT)? \((.*)\)$", re.I | re.S)
LABEL_FOLDER = corpus.GMAIL_LABEL_FOLDERS
CMD_RE = re.compile(rb"^(\S+) (UID )?([A-Za-z]+)(?: (.*))?\r\n$", re.S)
SELECT_RE = re.compile(r'^SELECT "(.*)"$')
MAX_SET_EXPANSION = 1_000_000
MAX_LITERAL = 64 << 20  # above Gmail's APPENDLIMIT (35,882,577)
MAX_ADDRESS_SPACE = 2 << 30  # idle proxy: about 55 MB virtual, 45 MB resident
MAX_CPU_SECONDS = 3600
SIDE_REPLY_MAX = 256 << 20
MODEL_TIMEOUT = 120


def unquote(token: bytes) -> str:
    if token.startswith(b'"'):
        token = token[1:-1].replace(b'\\"', b'"').replace(b"\\\\", b"\\")
    return token.decode()


def seq_ranges(text: bytes):
    """[(low, high)] of an IMAP sequence set without `*`; ValueError otherwise."""
    out = []
    for part in text.split(b","):
        a, _, b = part.partition(b":")
        lo, hi = int(a), int(b or a)
        out.append((min(lo, hi), max(lo, hi)))
    return out


def seq_set(text: bytes):
    """Expands a server-generated set (COPYUID): at most MAX_SET_EXPANSION UIDs, never a client's set."""
    out = []
    for lo, hi in seq_ranges(text):
        if len(out) + hi - lo + 1 > MAX_SET_EXPANSION:
            raise ValueError(f"set {text[:64]!r} expands past {MAX_SET_EXPANSION} UIDs")
        out.extend(range(lo, hi + 1))
    return out


def valid_set(text: bytes):
    try:
        seq_ranges(text)
        return True
    except ValueError:
        return False


def in_set(uids, text: bytes):
    """The UIDs among `uids` (the model's, bounded by the mailbox size) that a client's set names."""
    ranges = seq_ranges(text)
    return sorted(u for u in uids if any(lo <= u <= hi for lo, hi in ranges))


def uid_set(uids):
    uids = sorted(uids)
    parts, i = [], 0
    while i < len(uids):
        j = i
        while j + 1 < len(uids) and uids[j + 1] == uids[j] + 1:
            j += 1
        parts.append(str(uids[i]) if i == j else f"{uids[i]}:{uids[j]}")
        i = j + 1
    return ",".join(parts)


def label_astring(label):
    if label.startswith("\\") or not re.fullmatch(r"[A-Za-z0-9/_.-]+", label):
        return '"' + label.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return label


class SideClosed(ConnectionError):
    """Dovecot closed the side connection before answering (BYE or EOF, nothing else received)."""


class SideNo(RuntimeError):
    """Dovecot answered a side command NO or BAD; the connection stays usable."""


class Side:
    """Internal connection to Dovecot used by the Gmail model to apply label side effects."""

    def __init__(self, port, user):
        self.port, self.user, self.n = port, user, 0
        self.reader = self.writer = None
        self.selected = None

    async def open(self):
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port, limit=1 << 26)
        await self.reader.readline()
        await self.exchange(f'LOGIN "{self.user}" "pass"')
        folder, self.selected = self.selected, None
        if folder is not None:
            try:
                await self.exchange(f'SELECT "{folder}"')
                self.selected = folder
            except SideNo:  # deleted meanwhile: the next command that needs it fails on its own
                pass

    def close(self):
        if self.writer is not None:
            self.writer.close()
        self.reader = self.writer = None

    async def cmd(self, text):
        """Runs one command. Dovecot closes an idle connection after 30 minutes (autologout) and may BYE it; reading at
        EOF returns b"" without yielding, so this connection once spun forever, blocking every client, growing a list
        of empty lines to 6 GB. A command that got only BYE/EOF was not run: reconnect (re-selecting the mailbox) and
        send it once more. Any other failure, including cancellation, drops the connection."""
        for attempt in (1, 2):
            try:
                if self.writer is None:
                    await self.open()
                m = SELECT_RE.match(text)
                if m:
                    self.selected = None
                result = await self.exchange(text)
                if m:
                    self.selected = m.group(1)
                return result
            except SideClosed:
                self.close()
                if attempt == 2:
                    raise
            except SideNo:
                raise
            except BaseException:
                self.close()
                raise

    async def exchange(self, text):
        self.n += 1
        tag = f"G{self.n}".encode()
        try:
            self.writer.write(tag + b" " + text.encode() + b"\r\n")
            await self.writer.drain()
        except (ConnectionError, OSError) as e:
            raise SideClosed(f"side {text!r}: {e!r}") from e
        lines, size = [], 0
        while True:
            line = await self.reader.readline()
            if not line or line.startswith(b"* BYE"):
                if lines:
                    raise RuntimeError(f"side {text!r}: connection closed mid-reply ({line!r})")
                raise SideClosed(f"side {text!r}: {line!r}")
            m = LITERAL_RE.search(line)
            if m:
                try:
                    line += await self.reader.readexactly(int(m.group(1)))
                except asyncio.IncompleteReadError as e:
                    raise RuntimeError(f"side {text!r}: connection closed mid-literal") from e
            size += len(line)
            if size > SIDE_REPLY_MAX:
                raise RuntimeError(f"side {text!r}: reply over {SIDE_REPLY_MAX} bytes")
            if line.startswith(tag + b" "):
                if not line[len(tag) + 1:].startswith(b"OK"):
                    raise SideNo(f"side {text!r}: {line!r}")
                return line, lines
            lines.append(line)


class Store:
    """Per-user Gmail model shared by all sessions of that user."""

    def __init__(self, user, data, upstream):
        self.user = user
        self.folders = {f: {i + 1: gm for i, gm in enumerate(v)} for f, v in data["folders"].items()}
        self.msgs = {int(k): v for k, v in data["msgs"].items()}
        self.hidden = set(data["hidden"])
        self.label_of, self.label_folder = dict(LABEL_OF), dict(LABEL_FOLDER)
        self.cap = data["cap"]
        self.next_gm = 1_730_000_000_000_000_000
        self.lock = asyncio.Lock()
        self.side = Side(upstream, user)
        self.effects = {"label_bumps": 0, "removed_from_other_folders": 0, "restored_to_all_mail": 0,
                        "appended": 0, "cap_trimmed": 0, "deleted_forever": 0}

    def labels_for(self, folder, gm):
        thr, labels = self.msgs.get(gm, [gm, []])
        return thr, ([] if folder == SPAM else labels)

    async def remove(self, folder, uids):
        await self.side.cmd(f'SELECT "{folder}"')
        s = uid_set(uids)
        await self.side.cmd(f"UID STORE {s} +FLAGS.SILENT (\\Deleted)")
        await self.side.cmd(f"UID EXPUNGE {s}")
        for u in uids:
            self.folders[folder].pop(u, None)

    async def copy(self, src, uid, dst):
        await self.side.cmd(f'SELECT "{src}"')
        line, untagged = await self.side.cmd(f'UID COPY {uid} "{dst}"')
        m = COPYUID_RE.search(line) or next((COPYUID_RE.search(x) for x in untagged if COPYUID_RE.search(x)), None)
        return seq_set(m.group(2))[0]

    async def bump(self, gm):
        await self.bump_many({gm})

    async def bump_many(self, gms):
        """Bumps the All Mail MODSEQ of messages whose labels changed (one STORE pair for all of them)."""
        uids = [u for u, g in self.folders[ALL].items() if g in gms]
        if not uids:
            return
        s = uid_set(uids)
        await self.side.cmd(f'SELECT "{ALL}"')
        await self.side.cmd(f"UID STORE {s} +FLAGS.SILENT ($GmLabelChange)")
        await self.side.cmd(f"UID STORE {s} -FLAGS.SILENT ($GmLabelChange)")
        self.effects["label_bumps"] += len(uids)

    def created(self, folder):
        if folder not in self.folders:
            self.folders[folder] = {}
            self.label_of[folder] = folder
            self.label_folder[folder] = folder

    async def renamed(self, old, new):
        changed = set()
        for folder in [f for f in self.folders if f == old or f.startswith(old + "/")]:
            moved_to = new + folder[len(old):]
            self.folders[moved_to] = self.folders.pop(folder)
            label = self.label_of.pop(folder, None)
            if label is None:
                continue
            del self.label_folder[label]
            self.label_of[moved_to] = self.label_folder[moved_to] = moved_to
            for gm in self.folders[moved_to].values():
                labels = self.msgs.get(gm, [0, []])[1]
                if label in labels:
                    labels[labels.index(label)] = moved_to
                    changed.add(gm)
        await self.bump_many(changed)

    async def deleted(self, folder):
        label = self.label_of.pop(folder, None)
        self.label_folder.pop(label, None)
        changed = set()
        for gm in self.folders.pop(folder, {}).values():
            labels = self.msgs.get(gm, [0, []])[1]
            if label in labels:
                labels.remove(label)
                changed.add(gm)
        await self.bump_many(changed)

    async def resync_map(self, folder):
        await self.side.cmd(f'SELECT "{folder}"')
        _, untagged = await self.side.cmd("UID SEARCH ALL")
        present = set()
        for line in untagged:
            if line.startswith(b"* SEARCH"):
                present.update(int(x) for x in line[8:].split())
        removed = [self.folders[folder].pop(u) for u in list(self.folders[folder]) if u not in present]
        return removed

    async def trim_cap(self):
        if not self.cap:
            return
        for folder, m in self.folders.items():
            if len(m) > self.cap:
                oldest = sorted(m)[: len(m) - self.cap]
                await self.remove(folder, oldest)
                self.effects["cap_trimmed"] += len(oldest)

    async def after_move(self, src, pairs, dst):
        moved = []
        for su, du in pairs:
            gm = self.folders[src].pop(su, None)
            if gm is None:
                continue
            self.folders[dst][du] = gm
            moved.append((gm, du))
        if dst in (SPAM, TRASH):  # one pass over the model and one removal per folder, however many moved
            gms = {gm for gm, _ in moved}
            for f, m in self.folders.items():
                uids = [u for u, g in m.items() if g in gms] if f != dst else []
                if uids:
                    await self.remove(f, uids)
                    self.effects["removed_from_other_folders"] += len(uids)
            if dst == SPAM:
                for gm in gms:
                    self.msgs[gm][1] = []
            return
        for gm, du in moved:
            if src in (SPAM, TRASH):
                label = self.label_of.get(dst)
                self.msgs[gm][1] = [label] if label else []
                if dst != ALL:
                    self.folders[ALL][await self.copy(dst, du, ALL)] = gm
                    self.effects["restored_to_all_mail"] += 1
            else:
                labels = self.msgs[gm][1]
                if self.label_of[src] in labels:
                    labels.remove(self.label_of[src])
                if self.label_of[dst] not in labels:
                    labels.append(self.label_of[dst])
                await self.dedupe(dst, gm, du)
                await self.bump(gm)

    async def dedupe(self, dst, gm, du):
        """A label holds a message once: drop the new copy if the label folder already had it."""
        if any(g == gm and u != du for u, g in self.folders[dst].items()):
            await self.remove(dst, [du])
            self.effects["duplicate_label_copies_removed"] = self.effects.get("duplicate_label_copies_removed", 0) + 1

    async def after_copy(self, src, pairs, dst):
        for su, du in pairs:
            gm = self.folders[src].get(su)
            if gm is None:
                continue
            self.folders[dst][du] = gm
            if self.label_of[dst] not in self.msgs[gm][1]:
                self.msgs[gm][1].append(self.label_of[dst])
            await self.dedupe(dst, gm, du)
            await self.bump(gm)

    async def after_append(self, dst, uid):
        gm = self.next_gm
        self.next_gm += 1
        self.folders[dst][uid] = gm
        label = self.label_of.get(dst)
        self.msgs[gm] = [gm, [label] if label else []]
        if label:
            self.folders[ALL][await self.copy(dst, uid, ALL)] = gm
        self.effects["appended"] += 1
        await self.trim_cap()

    async def after_store(self, folder, uids):
        """Gmail flags are per message: copy \\Seen \\Answered \\Flagged \\Draft to the message's other folder copies
        (\\Deleted stays per folder: STORE \\Deleted + EXPUNGE in a label folder only removes that label). `uids` is
        the client's set (it may be `1:*` or `1:4294967295`): Dovecot resolves it, it is never expanded here."""
        model = self.folders.get(folder)
        if not model:
            return
        await self.side.cmd(f'SELECT "{folder}"')
        _, untagged = await self.side.cmd(f"UID FETCH {uids.decode()} (UID FLAGS)")
        others = {}  # X-GM-MSGID -> [(folder, uid)] of its copies in the other folders, one pass over the model
        for f, m in self.folders.items():
            if f != folder:
                for u, g in m.items():
                    others.setdefault(g, []).append((f, u))
        for line in untagged:
            mu, mf = UID_RE.search(line), re.search(rb"FLAGS \(([^)]*)\)", line)
            if not mu or not mf:
                continue
            flags = " ".join(f for f in mf.group(1).decode().split()
                             if f.lower() in ("\\seen", "\\answered", "\\flagged", "\\draft"))
            for f, u in others.get(model.get(int(mu.group(1))), ()):
                await self.side.cmd(f'SELECT "{f}"')
                await self.side.cmd(f"UID STORE {u} -FLAGS.SILENT (\\Seen \\Answered \\Flagged \\Draft)")
                if flags:
                    await self.side.cmd(f"UID STORE {u} +FLAGS.SILENT ({flags})")
                self.effects["flag_propagations"] = self.effects.get("flag_propagations", 0) + 1

    async def store_labels(self, uids, add, labels):
        """UID STORE +/-X-GM-LABELS on All Mail: the label folder gains or loses the message's copy. `uids` is the
        client's set, tested against the model's All Mail UIDs."""
        for u in in_set(list(self.folders[ALL]), uids):
            gm = self.folders[ALL].get(u)
            if gm is None:
                continue
            have = self.msgs[gm][1]
            changed = False
            for label in labels:
                folder = self.label_folder[label]
                if add and label not in have:
                    have.append(label)
                    self.folders[folder][await self.copy(ALL, u, folder)] = gm
                    changed = True
                elif not add and label in have:
                    have.remove(label)
                    copies = [x for x, g in self.folders[folder].items() if g == gm]
                    if copies:
                        await self.remove(folder, copies)
                    changed = True
            if changed:
                await self.bump(gm)
                self.effects["label_stores"] = self.effects.get("label_stores", 0) + 1

    async def after_expunge(self, folder):
        for gm in await self.resync_map(folder):
            if folder in (SPAM, TRASH):
                self.msgs.pop(gm, None)
                self.effects["deleted_forever"] += 1
            elif folder in self.label_of and self.label_of[folder] in self.msgs.get(gm, [0, []])[1]:
                self.msgs[gm][1].remove(self.label_of[folder])
                await self.bump(gm)


class Session:
    def __init__(self, stores, caps, log_path):
        self.stores, self.caps, self.log_path = stores, caps, log_path
        self.store = None
        self.authed = False
        self.selected = None
        self.pending_select = {}
        self.auth_tags = set()
        self.gm_tags = set()
        self.xlist_tags = set()
        self.ops = {}
        self.copyuid = {}
        self.s2c = self.c2s = self.injected = self.misses = self.refused = 0
        self.commands = {}

    # ------------------------------------------------------------------ client -> server
    def local_reply(self, writer, data: bytes):
        self.s2c += len(data)
        writer.write(data)

    def sasl_plain(self, b64):
        try:
            parts = base64.b64decode(b64.strip()).split(b"\0")
            self.store = self.stores.get(parts[1].decode())
        except (ValueError, IndexError):
            pass

    def classify(self, line, client_writer):
        """Returns the line to forward, or None if the proxy answered it itself."""
        if getattr(self, "await_sasl", False):
            self.await_sasl = False
            self.sasl_plain(line)
            return line
        m = CMD_RE.match(line)
        if not m:
            return line
        tag, uid, verb, rest = m.group(1), m.group(2), m.group(3).upper(), m.group(4) or b""
        key = ("UID " if uid else "") + verb.decode()
        self.commands[key] = self.commands.get(key, 0) + 1
        if verb in (b"LOGIN", b"AUTHENTICATE"):
            self.auth_tags.add(tag)
            if verb == b"LOGIN":
                self.store = self.stores.get(unquote(re.match(ASTRING, rest).group(1)))
            else:
                args = rest.split()
                if len(args) > 1:
                    self.sasl_plain(args[1])
                else:
                    self.await_sasl = True
            return line
        if verb == b"ENABLE":
            wanted = [c for c in rest.split() if c.upper().decode() in self.caps["postauth"]]
            if not wanted:
                self.local_reply(client_writer, b"* ENABLED\r\n" + tag + b" OK ENABLE completed\r\n")
                return None
            return tag + b" ENABLE " + b" ".join(wanted) + b"\r\n"
        if verb == b"XLIST":
            self.xlist_tags.add(tag)
            return tag + b" LIST " + rest + b"\r\n"
        hidden = self.store.hidden if self.store else set()
        if verb in (b"SELECT", b"EXAMINE", b"STATUS"):
            name = unquote(re.match(ASTRING, rest).group(1))
            if name in hidden:
                self.local_reply(client_writer, tag + b" NO [NONEXISTENT] Unknown Mailbox: " + name.encode() + b"\r\n")
                if verb != b"STATUS":
                    self.selected = None
                return None
            if verb != b"STATUS":
                self.pending_select[tag] = name
            return line
        if verb in (b"MOVE", b"COPY") and self.store:
            mm = re.match(rb"(\S+) " + ASTRING, rest)
            dst = unquote(mm.group(2))
            src = self.selected
            refuse = None
            if dst in hidden:
                refuse = b"NO [NONEXISTENT] Unknown Mailbox"
            elif verb == b"MOVE" and src == ALL and dst not in (SPAM, TRASH):
                refuse = b"NO [CANNOT] stand-in does not model MOVE out of All Mail except to Spam/Trash"
            elif verb == b"COPY" and (dst in (ALL, SPAM, TRASH) or src in (SPAM, TRASH)):
                refuse = b"NO [CANNOT] stand-in does not model this COPY"
            elif verb == b"MOVE" and src not in (SPAM, TRASH, ALL) and dst not in (SPAM, TRASH) and dst not in self.store.label_of:
                refuse = b"NO [CANNOT] stand-in does not model this MOVE"
            if refuse:
                self.refused += 1
                self.local_reply(client_writer, tag + b" " + refuse + b"\r\n")
                return None
            self.ops[tag] = (verb.decode(), src, dst)
            return line
        if verb == b"APPEND" and self.store:
            dst = unquote(re.match(ASTRING, rest).group(1))
            if dst in hidden:
                self.local_reply(client_writer, tag + b" NO [NONEXISTENT] Unknown Mailbox\r\n")
                return None
            self.ops[tag] = ("APPEND", None, dst)
            return line
        if verb == b"EXPUNGE" and self.store:
            self.ops[tag] = ("EXPUNGE", self.selected, None)
            return line
        names = [unquote(t) for t in re.findall(ASTRING, rest)][:2] if verb in (b"CREATE", b"RENAME", b"DELETE") else []
        if names and self.store:
            if verb != b"CREATE" and (names[0].upper() == "INBOX" or names[0].startswith("[Gmail]")):
                self.refused += 1
                self.local_reply(client_writer, tag + b" NO [CANNOT] system folder\r\n")
                return None
            self.ops[tag] = (verb.decode(), names[0], names[-1])
            return line
        if verb == b"STORE" and b"X-GM-LABELS" in rest.upper():
            mm = LABEL_STORE_RE.match(rest.rstrip(b"\r\n"))
            labels = [unquote(t) for t in re.findall(ASTRING, mm.group(3))] if mm else []
            if not (uid and self.store and self.selected == ALL and mm and valid_set(mm.group(1)) and labels
                    and all(l in self.store.label_folder for l in labels)):
                self.refused += 1
                self.local_reply(client_writer, tag + b" NO [CANNOT] stand-in models UID STORE X-GM-LABELS on All Mail"
                                 b" only, explicit UIDs and known labels only\r\n")
                return None
            self.ops[tag] = ("LABELS", ALL, (mm.group(1), mm.group(2) == b"+", labels))
            return tag + b" NOOP\r\n"

        if verb == b"STORE" and uid and self.store and self.selected:
            self.ops[tag] = ("STORE", self.selected, rest.split()[0])
            return line
        if verb == b"FETCH" and b"X-GM-" in rest.upper():
            self.gm_tags.add(tag)
            return GM_ITEM_RE.sub(b"", line).replace(b"( ", b"(")
        return line

    async def client_to_server(self, reader, writer, client_writer):
        while True:
            line = await reader.readline()
            if not line:
                break
            m = LITERAL_RE.search(line)
            self.c2s += len(line)
            out = self.classify(line, client_writer)
            if out is not None:
                writer.write(out)
            while m:  # literals: forward the data and the rest of the same command (which may hold more)
                if int(m.group(1)) > MAX_LITERAL:
                    client_writer.write(f"* BYE literal over {MAX_LITERAL} bytes\r\n".encode())
                    await client_writer.drain()
                    client_writer.close()
                    writer.close()
                    return
                data = await reader.readexactly(int(m.group(1)))
                rest = await reader.readline()
                self.c2s += len(data) + len(rest)
                if out is not None:
                    writer.write(data + rest)
                m = LITERAL_RE.search(rest)
            await writer.drain()
            await client_writer.drain()
        writer.close()

    # ------------------------------------------------------------------ server -> client
    async def read_response(self, reader):
        parts = []
        while True:
            line = await reader.readline()
            if not line:
                return None
            parts.append(line)
            m = LITERAL_RE.search(line)
            if not m:
                return parts
            parts.append(await reader.readexactly(int(m.group(1))))

    def rewrite_caps(self, line, post):
        caps = self.caps["postauth" if post else "preauth"]
        return CAP_RE.sub(b"CAPABILITY " + " ".join(caps).encode(), line, count=1)

    def inject(self, parts):
        head = b" ".join(parts[0::2])
        m = UID_RE.search(head)
        gm = self.store.folders.get(self.selected or "", {}).get(int(m.group(1))) if (m and self.store) else None
        if gm is None:
            self.misses += 1
            return parts
        thr, labels = self.store.labels_for(self.selected, gm)
        extra = f" X-GM-MSGID {gm} X-GM-THRID {thr} X-GM-LABELS ({' '.join(label_astring(l) for l in labels)})"
        last = parts[-1]
        idx = last.rstrip(b"\r\n").rfind(b")")
        parts[-1] = last[:idx] + extra.encode() + last[idx:]
        self.injected += 1
        return parts

    def hidden_line(self, first):
        if not self.store or not self.store.hidden:
            return False
        if not re.match(rb"\* (LIST|LSUB|STATUS|XLIST) ", first):
            return False
        return any(f'"{h}"'.encode() in first for h in self.store.hidden)

    async def server_to_client(self, reader, writer):
        while True:
            parts = await self.read_response(reader)
            if parts is None:
                break
            first = parts[0]
            tag = first.split(b" ", 1)[0]
            if b"CAPABILITY" in first and len(parts) == 1:
                parts[0] = first = self.rewrite_caps(first, self.authed or tag in self.auth_tags)
            if first.startswith(b"* ") and b" FETCH (" in first and self.gm_tags:
                parts = self.inject(parts)
            elif self.hidden_line(first):
                continue
            elif first.startswith(b"* LIST ") and self.xlist_tags:
                line = b"* XLIST " + first[7:]
                for k, v in XLIST_ATTRS.items():
                    line = line.replace(k + b" ", v + b" ").replace(k + b")", v + b")")
                if line.rstrip().endswith(b" INBOX") or line.rstrip().endswith(b'"INBOX"'):
                    line = line.replace(b"(", b"(\\Inbox ", 1)
                parts[0] = line
            elif first.startswith(b"* OK [COPYUID"):
                self.copyuid["last"] = first
            elif tag != b"*" and tag != b"+":
                parts[0] = await self.complete(tag, first)
            data = b"".join(parts)
            self.s2c += len(data)
            writer.write(data)
            await writer.drain()
        writer.close()

    async def complete(self, tag, first):
        """Updates the session and the model for a tagged response; returns the line to send the client."""
        ok = first.split(b" ", 2)[1:2] == [b"OK"]
        if tag in self.auth_tags:
            self.auth_tags.discard(tag)
            self.authed = ok
        if tag in self.pending_select:
            name = self.pending_select.pop(tag)
            self.selected = name if ok else None
        self.gm_tags.discard(tag)
        self.xlist_tags.discard(tag)
        op = self.ops.pop(tag, None)
        if not op or not ok:
            self.copyuid.pop("last", None)
            return first
        async with self.store.lock:
            try:
                await asyncio.wait_for(self.update_model(first, *op), MODEL_TIMEOUT)
            except Exception as e:  # incl. TimeoutError: answer instead of leaving the client waiting forever
                self.store.side.close()
                print(f"{self.store.user}: model update after {op[0]} failed: {e!r}", file=sys.stderr, flush=True)
                traceback.print_exc()
                return (tag + b" NO [SERVERBUG] Gmail stand-in: model update failed (" + type(e).__name__.encode()
                        + b"); Dovecot applied the command, the label model may be stale (testenv.sh reset gmail)\r\n")
        return first

    async def update_model(self, first, verb, src, dst):
        if verb in ("MOVE", "COPY"):
            m = COPYUID_RE.search(first) or COPYUID_RE.search(self.copyuid.pop("last", b""))
            if m:
                pairs = list(zip(seq_set(m.group(1)), seq_set(m.group(2))))
                await (self.store.after_move if verb == "MOVE" else self.store.after_copy)(src, pairs, dst)
        elif verb == "APPEND":
            m = APPENDUID_RE.search(first)
            await self.store.after_append(dst, int(m.group(1)))
        elif verb == "EXPUNGE" and src:
            await self.store.after_expunge(src)
        elif verb == "STORE":
            await self.store.after_store(src, dst)
        elif verb == "LABELS":
            uids, add, labels = dst
            await self.store.store_labels(uids, add, labels)
        elif verb == "CREATE":
            self.store.created(src)
        elif verb == "RENAME":
            await self.store.renamed(src, dst)
        elif verb == "DELETE":
            await self.store.deleted(src)


async def probe_caps(upstream):
    r, w = await asyncio.open_connection("127.0.0.1", upstream)
    greet = await r.readline()
    pre = set(CAP_RE.search(greet).group(0).split()[1:]) if CAP_RE.search(greet) else set()
    w.write(b'a LOGIN "gmail" "pass"\r\nb CAPABILITY\r\nc LOGOUT\r\n')
    await w.drain()
    post = set()
    while True:
        line = await r.readline()
        if not line:
            break
        if line.startswith(b"* CAPABILITY"):
            post = set(line.split()[2:])
    w.close()
    dec = lambda s: {x.decode() for x in s}  # noqa: E731
    pre, post = dec(pre), dec(post)
    pre_ok = [c for c in GMAIL_PREAUTH if c in pre or c in EMULATED]
    post_ok = [c for c in GMAIL_POSTAUTH if c in post or c in EMULATED]
    return {"dovecot_preauth": sorted(pre), "dovecot_postauth": sorted(post),
            "gmail_preauth": GMAIL_PREAUTH, "gmail_postauth": GMAIL_POSTAUTH,
            "preauth": pre_ok, "postauth": post_ok,
            "dropped_preauth_not_backed": [c for c in GMAIL_PREAUTH if c not in pre_ok],
            "dropped_postauth_not_backed": [c for c in GMAIL_POSTAUTH if c not in post_ok],
            "qresync_advertised": "QRESYNC" in post_ok, "condstore_advertised": "CONDSTORE" in post_ok,
            "esearch_advertised": "ESEARCH" in post_ok}


async def main():
    listen_port, upstream, state_path, log_path, caps_path = (int(sys.argv[1]), int(sys.argv[2]), sys.argv[3],
                                                              sys.argv[4], sys.argv[5])
    with open(state_path) as f:
        state = json.load(f)
    stores = {u: Store(u, d, upstream) for u, d in state.items()}
    caps = await probe_caps(upstream)
    with open(caps_path, "w") as f:
        json.dump(caps, f, indent=2)

    async def handle(client_reader, client_writer):
        up_reader, up_writer = await asyncio.open_connection("127.0.0.1", upstream, limit=1 << 26)
        s = Session(stores, caps, log_path)
        await asyncio.gather(s.client_to_server(client_reader, up_writer, client_writer),
                             s.server_to_client(up_reader, client_writer), return_exceptions=True)
        with open(log_path, "a") as f:
            f.write(json.dumps({"user": s.store.user if s.store else None, "bytes_s2c": s.s2c, "bytes_c2s": s.c2s,
                                "injected": s.injected, "misses": s.misses, "refused": s.refused,
                                "commands": s.commands}) + "\n")

    async def dump_effects():
        while True:
            await asyncio.sleep(0.5)
            with open(log_path + ".effects", "w") as f:
                json.dump({u: s.effects for u, s in stores.items()}, f)

    server = await asyncio.start_server(handle, "127.0.0.1", listen_port, limit=1 << 26)
    asyncio.get_running_loop().create_task(dump_effects())
    print("ready", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    resource.setrlimit(resource.RLIMIT_AS, (MAX_ADDRESS_SPACE, MAX_ADDRESS_SPACE))
    resource.setrlimit(resource.RLIMIT_CPU, (MAX_CPU_SECONDS, MAX_CPU_SECONDS + 10))
    asyncio.run(main())
