"""Deterministic synthetic mail corpus for the Gmail stand-in.

No existing generator covered the required mix (threads with References, ~15% attachments,
non-Latin RFC 2047/2231 headers, malformed messages, Gmail labels), so this script builds raw
RFC 5322 bytes directly. Every message is a pure function of (SEED, account, folder, index)
plus the per-folder thread ring, so the corpus is reproducible byte for byte.

Size model [INFERENCE, not sourced]: text length log-normal (median ~1.2 KB), 40% carry an
HTML alternative, 15% carry 1-3 attachments with log-normal size (median 30 KB, cap 2 MB).
"""
import base64
import hashlib
import math
import quopri
import random
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.utils import format_datetime, formataddr
from urllib.parse import quote as pct

SEED = 20261112
START = datetime(2020, 11, 1, tzinfo=timezone.utc)
END = datetime(2026, 11, 1, tzinfo=timezone.utc)

GMAIL_ACCOUNT = ("gmail", "owner@gmail.example", "Mailbox Owner", "mixed", 5000)
GMAIL_ALL = "[Gmail]/All Mail"
# label -> folder that Gmail exposes for it
GMAIL_LABEL_FOLDERS = {
    "\\Inbox": "INBOX",
    "\\Sent": "[Gmail]/Sent Mail",
    "\\Important": "[Gmail]/Important",
    "Work": "Work",
    "Receipts": "Receipts",
    "Travel": "Travel",
    "Family": "Family",
    "Projects/Gemini": "Projects/Gemini",
}

NAMES = {
    # Synthetic: invented surnames, so that no name belongs to a real or fictional public figure.
    "en": ["Alice Testbridge", "Alice Fixturen", "Bob Sampleford", "Charlie Mockley", "Dana Stubbard",
           "Evan Dummett", "Fiona Placeholt", "George Synthwick", "Hannah Fakeley", "Ian Testerson",
           "Julia Corpusova", "Kevin Seedling", "Laura Mockridge", "Martin Fixtureman", "Nina Samplewood",
           "Oscar Stubbington", "Priya Dummond", "Quinn Testfield"],
    "ru": ["Иван Петров", "Ольга Иванова", "Сергей Кузнецов", "Анна Смирнова", "Дмитрий Орлов",
           "Мария Соколова", "Алексей Волков"],
    "ja": ["山田 太郎", "佐藤 花子", "鈴木 一郎", "高橋 美咲", "田中 健", "伊藤 さくら"],
    "el": ["Γιώργος Παπαδόπουλος", "Μαρία Κωνσταντίνου", "Νίκος Γεωργίου"],
    "ar": ["أحمد علي", "فاطمة حسن"],
}
DOMAINS = ["example.com", "corp.example", "mail.example.org", "example.net", "lists.example.org",
           "shop.example", "example.ru", "example.jp", "example.gr"]

WORDS = {
    "en": ("the a to of and in for on with project projects projection budget review quarterly "
           "deployment release meeting notes invoice payment schedule customer team update plan "
           "design draft feedback launch report metrics server cluster database migration backup "
           "storage network latency kubernetes docker pipeline roadmap contract vendor travel "
           "hotel flight agenda summary action items deadline approval estimate proposal").split(),
    "ru": ("отчёт проект встреча бюджет счёт оплата договор команда план задача срок обновление "
           "сервер база данных резервная копия клиент поставщик совещание итоги").split(),
    "ja": ["会議", "議事録", "見積書", "請求書", "プロジェクト", "進捗", "確認", "お願いします",
           "明日", "資料", "予定", "報告", "担当", "サーバー", "移行"],
    "el": "συνάντηση ομάδα έργο αναφορά προϋπολογισμός πρόοδος τιμολόγιο".split(),
    "ar": "تقرير المشروع اجتماع الميزانية فاتورة".split(),
}
SUBJECTS = {
    "en": ["Quarterly budget review", "Invoice {n} for {month}", "Project {proj} status update",
           "Meeting notes: {topic}", "Deployment of {svc}", "Release {ver} planning", "Lunch on {day}?",
           "Your order #{n} has shipped", "Weekly digest: {topic}", "Projection for Q{q}",
           "Kubernetes cluster upgrade", "Contract renewal {n}", "Travel itinerary {month}",
           "Backup failure on {svc}", "Design feedback for {proj}"],
    "ru": ["Отчёт за {month_ru}", "Встреча по проекту {proj}", "Счёт №{n}", "Совещание: итоги {n}",
           "Обновление сервера {svc}"],
    "ja": ["会議の議事録 {n}", "明日の会議について", "見積書の送付 ({n})", "プロジェクト{proj}の進捗",
           "請求書のご確認"],
    "el": ["Συνάντηση ομάδας {n}", "Αναφορά προόδου {proj}"],
    "ar": ["تقرير المشروع {n}"],
}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]
MONTHS_RU = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь",
             "октябрь", "ноябрь", "декабрь"]
PROJECTS = ["Atlas", "Borealis", "Cygnus", "Delta", "Gemini", "Helios", "Orion"]
SERVICES = ["api-gateway", "billing", "search", "mail-relay", "auth", "cdn"]
TOPICS = ["roadmap", "hiring", "security", "observability", "pricing", "onboarding"]
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
ATTACH = [
    ("application/pdf", ["report-{n}.pdf", "invoice-{n}.pdf", "contract-{n}.pdf", "見積書-{n}.pdf",
                         "отчёт-{n}.pdf", "Αναφορά-{n}.pdf"]),
    ("image/jpeg", ["photo-{n}.jpg", "фото-{n}.jpg", "写真-{n}.jpg", "scan {n}.jpg"]),
    ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ["budget-{n}.xlsx", "projection-q{q}.xlsx"]),
    ("text/calendar", ["invite-{n}.ics"]),
    ("application/zip", ["logs-{n}.zip"]),
]
REPLY_PREFIX = {"en": "Re: ", "ru": "Re: ", "ja": "Re: ", "el": "Re: ", "ar": "Re: "}
MALFORMED_KINDS = ["no_message_id", "no_date", "bad_date", "broken_encoded_word", "unknown_charset",
                   "raw_8bit_subject", "unterminated_multipart", "no_from", "long_subject"]


def pick_locale(rng, account_locale):
    if account_locale == "mixed":
        return rng.choices(["en", "ru", "ja", "el", "ar"], [60, 12, 12, 8, 8])[0]
    if rng.random() < 0.15:
        return rng.choice(["en", "ru", "ja", "el", "ar"])
    return account_locale


def enc_header(value):
    try:
        value.encode("ascii")
        return value
    except UnicodeEncodeError:
        return Header(value, "utf-8", maxlinelen=76).encode()


def addr(name, address):
    return formataddr((name, address), charset="utf-8")


def person(rng, locale):
    name = rng.choice(NAMES[locale])
    local = "user%d" % rng.randrange(1, 400) if locale != "en" else name.split()[0].lower() + "." + name.split()[1].lower()
    return name, f"{local}@{rng.choice(DOMAINS)}"


def fill(rng, template):
    return template.format(n=rng.randrange(100, 99999), month=rng.choice(MONTHS),
                           month_ru=rng.choice(MONTHS_RU), proj=rng.choice(PROJECTS),
                           svc=rng.choice(SERVICES), topic=rng.choice(TOPICS), ver="%d.%d" % (rng.randrange(1, 9), rng.randrange(0, 20)),
                           day=rng.choice(DAYS), q=rng.randrange(1, 5))


def body_text(rng, locale, scale):
    sentences = max(1, min(400, int(rng.lognormvariate(math.log(12 * scale), 0.9))))
    words = WORDS[locale]
    sep = "" if locale == "ja" else " "
    out = []
    for _ in range(sentences):
        n = rng.randrange(6, 15)
        out.append(sep.join(rng.choice(words) for _ in range(n)) + ("。" if locale == "ja" else "."))
        if rng.random() < 0.01:
            out.append("We need the quarterly budget review before Friday.")
        if rng.random() < 0.15:
            out.append("\n")
    return sep.join(out).replace(" \n ", "\n\n")


def encode_text_part(rng, text, subtype):
    data = text.encode("utf-8")
    try:
        text.encode("ascii")
        return f"Content-Type: text/{subtype}; charset=us-ascii\r\nContent-Transfer-Encoding: 7bit\r\n\r\n".encode() + wrap_crlf(data)
    except UnicodeEncodeError:
        pass
    if rng.random() < 0.5:
        return (f"Content-Type: text/{subtype}; charset=utf-8\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\n".encode()
                + quopri.encodestring(data).replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    return (f"Content-Type: text/{subtype}; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n\r\n".encode()
            + base64.encodebytes(data).replace(b"\n", b"\r\n"))


def wrap_crlf(data):
    lines = []
    for line in data.split(b"\n"):
        while len(line) > 900:
            lines.append(line[:900])
            line = line[900:]
        lines.append(line)
    return b"\r\n".join(lines) + b"\r\n"


def attachment_part(rng):
    ctype, names = rng.choice(ATTACH)
    filename = fill(rng, rng.choice(names))
    size = min(2_000_000, max(200, int(rng.lognormvariate(math.log(30_000), 1.0))))
    payload = base64.encodebytes(rng.randbytes(size)).replace(b"\n", b"\r\n")
    try:
        filename.encode("ascii")
        disp = f'attachment; filename="{filename}"'
        ct_name = f'; name="{filename}"'
    except UnicodeEncodeError:
        disp = "attachment; filename*=utf-8''" + pct(filename.encode("utf-8"), safe="")
        ct_name = '; name="' + Header(filename, "utf-8").encode() + '"' if rng.random() < 0.5 else ""
    inline = ctype == "image/jpeg" and rng.random() < 0.3
    if inline:
        disp = disp.replace("attachment", "inline", 1)
    head = f"Content-Type: {ctype}{ct_name}\r\nContent-Disposition: {disp}\r\nContent-Transfer-Encoding: base64\r\n\r\n"
    return head.encode() + payload, filename


def make_message(rng, account, locale_pref, n, ring, when, scale=1.0, force_attach=None, sent_from_owner=False):
    """Build one raw message. Returns (bytes, meta)."""
    acct_name, owner_addr, owner_name, _, _ = account
    locale = pick_locale(rng, locale_pref)
    malformed = rng.choice(MALFORMED_KINDS) if rng.random() < 0.01 else None
    dup_of = None
    parent = None
    if ring and rng.random() < 0.35:
        parent = rng.choice(ring[-200:])
    if parent is not None:
        subject = parent["subject"] if parent["subject"].startswith("Re: ") else REPLY_PREFIX[locale] + parent["subject"]
        refs = (parent["refs"] + [parent["msgid"]])[-20:]
        thread_root = parent["thread_root"]
    else:
        subject = fill(rng, rng.choice(SUBJECTS[locale]))
        refs = []
        thread_root = None
    msgid = f"{acct_name}.{n}.{rng.getrandbits(48):012x}@{rng.choice(DOMAINS)}"
    if ring and rng.random() < 0.001:
        dup_of = rng.choice(ring[-50:])["msgid"]
        msgid = dup_of
    if sent_from_owner:
        from_name, from_addr = owner_name, owner_addr
        to = [person(rng, locale) for _ in range(rng.randrange(1, 4))]
    else:
        from_name, from_addr = person(rng, locale)
        to = [(owner_name, owner_addr)] + [person(rng, locale) for _ in range(rng.randrange(0, 3))]
    cc = [person(rng, locale) for _ in range(rng.randrange(0, 3))] if rng.random() < 0.3 else []
    tz = timezone(timedelta(minutes=rng.choice([0, 60, 120, 180, -300, -420, 330, 540])))
    date_hdr = format_datetime(when.astimezone(tz))

    h = []
    if malformed != "no_date":
        h.append("Date: " + ("Someday, 99 Foo 20XX 25:61:00 +9999" if malformed == "bad_date" else date_hdr))
    if malformed != "no_from":
        h.append("From: " + addr(from_name, from_addr))
    h.append("To: " + ", ".join(addr(a, b) for a, b in to))
    if cc:
        h.append("Cc: " + ", ".join(addr(a, b) for a, b in cc))
    if malformed == "long_subject":
        subject = (subject + " ") * 120
    if malformed == "broken_encoded_word":
        h.append("Subject: =?utf-8?B?!!!not-base64!!!?= " + enc_header(subject))
    elif malformed == "unknown_charset":
        h.append("Subject: =?x-unknown-charset?Q?" + "abc_def" + "?= " + enc_header(subject))
    elif malformed == "raw_8bit_subject":
        h.append("Subject: " + subject + " — ünïcödé raw")
    else:
        h.append("Subject: " + enc_header(subject))
    if malformed != "no_message_id":
        h.append(f"Message-ID: <{msgid}>")
    if refs:
        h.append(f"In-Reply-To: <{refs[-1]}>")
        h.append("References: " + "\r\n ".join(f"<{r}>" for r in refs))
    h.append("MIME-Version: 1.0")

    text = body_text(rng, locale, scale)
    with_html = rng.random() < 0.4
    attach = force_attach if force_attach is not None else rng.random() < 0.15
    att_names = []
    if not with_html and not attach:
        body_part = encode_text_part(rng, text, "plain")
        head_txt, _, body_bytes = body_part.partition(b"\r\n\r\n")
        raw = ("\r\n".join(h) + "\r\n").encode("utf-8") + head_txt + b"\r\n\r\n" + body_bytes
    else:
        if with_html:
            html = "<html><body><p>" + text.replace("\n\n", "</p><p>") + "</p></body></html>"
            b_alt = f"alt-{rng.getrandbits(40):010x}"
            alt = (f"Content-Type: multipart/alternative; boundary=\"{b_alt}\"\r\n\r\n"
                   f"--{b_alt}\r\n").encode() + encode_text_part(rng, text, "plain") + f"--{b_alt}\r\n".encode() \
                + encode_text_part(rng, html, "html") + f"--{b_alt}--\r\n".encode()
        else:
            alt = encode_text_part(rng, text, "plain")
        if attach:
            b_mix = f"mix-{rng.getrandbits(40):010x}"
            parts = [alt]
            for _ in range(rng.choices([1, 2, 3], [70, 20, 10])[0]):
                part, fname = attachment_part(rng)
                parts.append(part)
                att_names.append(fname)
            body = b"".join(f"--{b_mix}\r\n".encode() + p for p in parts)
            if malformed != "unterminated_multipart":
                body += f"--{b_mix}--\r\n".encode()
            raw = ("\r\n".join(h) + f"\r\nContent-Type: multipart/mixed; boundary=\"{b_mix}\"\r\n\r\n").encode("utf-8") \
                + b"This is a multi-part message in MIME format.\r\n" + body
        else:
            head_txt, _, body_bytes = alt.partition(b"\r\n\r\n")
            raw = ("\r\n".join(h) + "\r\n").encode("utf-8") + head_txt + b"\r\n\r\n" + body_bytes
    meta = {"msgid": msgid if malformed != "no_message_id" else None, "subject": subject, "refs": refs,
            "thread_root": thread_root or msgid, "malformed": malformed, "dup_of": dup_of,
            "attachments": att_names, "locale": locale}
    ring.append({"msgid": msgid, "subject": subject, "refs": refs, "thread_root": meta["thread_root"]})
    if len(ring) > 400:
        del ring[:200]
    return raw, meta


def folder_times(rng, count, start=START, end=END):
    span = (end - start).total_seconds()
    times = sorted(start + timedelta(seconds=rng.random() * span) for _ in range(count))
    for i in range(1, count):
        if rng.random() < 0.03:
            times[i - 1], times[i] = times[i], times[i - 1]
    return times


def initial_flags(rng, folder, index, count):
    if folder in ("Sent", "Drafts"):
        return ["\\Seen"] + (["\\Draft"] if folder == "Drafts" else [])
    recent = index >= count * 0.97
    flags = []
    if rng.random() > (0.4 if recent else 0.02):
        flags.append("\\Seen")
    if rng.random() < 0.03:
        flags.append("\\Flagged")
    if folder == "INBOX" and rng.random() < 0.10:
        flags.append("\\Answered")
    return flags



def generate_gmail(seed=SEED):
    """Yield (internaldate, flags, raw, meta, labels, gm_msgid, gm_thrid) for the Gmail stand-in."""
    account = (GMAIL_ACCOUNT[0], GMAIL_ACCOUNT[1], GMAIL_ACCOUNT[2], GMAIL_ACCOUNT[3], [])
    count = GMAIL_ACCOUNT[4]
    rng = random.Random(f"{seed}:gmail")
    times = folder_times(rng, count)
    ring = []
    thread_labels = {}
    thread_ids = {}
    for i, when in enumerate(times):
        mrng = random.Random(f"{seed}:gmail:{i}")
        sent = mrng.random() < 0.10
        raw, meta = make_message(mrng, account, account[3], i, ring, when, sent_from_owner=sent)
        gm_msgid = 1_700_000_000_000_000_000 + i * 7919
        root = meta["thread_root"]
        gm_thrid = thread_ids.setdefault(root, gm_msgid)
        labels = []
        if sent:
            labels.append("\\Sent")
        elif mrng.random() < 0.55:
            labels.append("\\Inbox")
        if mrng.random() < 0.25:
            labels.append("\\Important")
        inherited = thread_labels.get(root)
        if inherited is None:
            inherited = [l for l, p in (("Work", 0.30), ("Receipts", 0.10), ("Travel", 0.08), ("Family", 0.10),
                                        ("Projects/Gemini", 0.08)) if mrng.random() < p]
            thread_labels[root] = inherited
        labels.extend(inherited)
        flags = initial_flags(mrng, "INBOX", i, count)
        internal = when + timedelta(seconds=mrng.randrange(5, 300))
        yield internal, flags, raw, meta, labels, gm_msgid, gm_thrid



GMAIL_SPAM = "[Gmail]/Spam"
GMAIL_TRASH = "[Gmail]/Trash"
# Spam and Trash hold messages that are NOT in All Mail [INFERENCE: third-party documentation, no primary
# Google page]; X-GM-MSGID ranges are disjoint from the All Mail corpus.
GMAIL_OUTSIDE = {GMAIL_SPAM: (120, 1_710_000_000_000_000_000), GMAIL_TRASH: (80, 1_720_000_000_000_000_000)}


def generate_gmail_outside(folder, seed=SEED):
    """Yield (internaldate, flags, raw, preserved_labels, gm_msgid, gm_thrid) for [Gmail]/Spam or Trash.

    Trash messages keep the user labels they had before deletion (restored by "Move to Inbox" in the web UI);
    the stand-in reports them in X-GM-LABELS inside Trash, the worst case for a client [INFERENCE]."""
    count, base = GMAIL_OUTSIDE[folder]
    account = (GMAIL_ACCOUNT[0], GMAIL_ACCOUNT[1], GMAIL_ACCOUNT[2], GMAIL_ACCOUNT[3], [])
    rng = random.Random(f"{seed}:gmail:{folder}")
    times = folder_times(rng, count, start=END - timedelta(days=29), end=END)
    ring = []
    for i, when in enumerate(times):
        mrng = random.Random(f"{seed}:gmail:{folder}:{i}")
        raw, meta = make_message(mrng, account, account[3], 900_000 + i, ring, when)
        gm = base + i * 13
        labels = []
        if folder == GMAIL_TRASH and mrng.random() < 0.4:
            labels = [mrng.choice(["Work", "Receipts", "Travel", "Family"])]
        flags = ["\\Seen"] if folder == GMAIL_TRASH or mrng.random() < 0.3 else []
        yield when + timedelta(seconds=mrng.randrange(5, 300)), flags, raw, labels, gm, gm


