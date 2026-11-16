"""Minimal literal-aware IMAP client used by the seeding/mutation/verification tools.

Not a product component; it only needs to talk to the local Dovecot test server.
"""
import re
import socket

LITERAL_RE = re.compile(rb"\{(\d+)\+?\}\r\n$")


class ImapError(RuntimeError):
    pass


class Raw:
    def __init__(self, host="127.0.0.1", port=1143, user=None, password="pass"):
        self.sock = socket.create_connection((host, port))
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.buf = b""
        self.tagn = 0
        self.read_response()  # greeting
        if user is not None:
            self.ok(f'LOGIN "{user}" "{password}"')

    def close(self):
        try:
            self.command("LOGOUT")
        except Exception:
            pass
        self.sock.close()

    def _fill(self):
        chunk = self.sock.recv(1 << 20)
        if not chunk:
            raise ImapError("connection closed")
        self.buf += chunk

    def _line(self):
        while True:
            i = self.buf.find(b"\r\n")
            if i >= 0:
                line, self.buf = self.buf[: i + 2], self.buf[i + 2 :]
                return line
            self._fill()

    def _exact(self, n):
        while len(self.buf) < n:
            self._fill()
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def read_response(self):
        """Return one logical response as a list of byte segments (line text and literals)."""
        parts = []
        while True:
            line = self._line()
            m = LITERAL_RE.search(line)
            if not m:
                parts.append(line)
                return parts
            parts.append(line)
            parts.append(self._exact(int(m.group(1))))

    def next_tag(self):
        self.tagn += 1
        return f"T{self.tagn}"

    def send(self, data: bytes):
        self.sock.sendall(data)

    def command(self, text, literal=None):
        tag = self.next_tag()
        if literal is None:
            self.send(f"{tag} {text}\r\n".encode())
        else:
            self.send(f"{tag} {text} {{{len(literal)}+}}\r\n".encode() + literal + b"\r\n")
        return self.collect(tag)

    def collect(self, tag):
        untagged = []
        tb = tag.encode() + b" "
        while True:
            resp = self.read_response()
            if resp[0].startswith(tb):
                status = resp[0][len(tb):].split(b" ", 1)[0]
                return status.decode(), resp[0].decode(errors="replace").strip(), untagged
            untagged.append(resp)

    def ok(self, text, literal=None):
        status, line, untagged = self.command(text, literal)
        if status != "OK":
            raise ImapError(f"{text[:80]} -> {line}")
        return line, untagged


def quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'



def select_info(untagged):
    info = {}
    for resp in untagged:
        line = resp[0].decode(errors="replace")
        for key in ("UIDVALIDITY", "UIDNEXT", "HIGHESTMODSEQ"):
            m = re.search(rf"\[{key} (\d+)\]", line)
            if m:
                info[key.lower()] = int(m.group(1))
        m = re.match(r"\* (\d+) EXISTS", line)
        if m:
            info["exists"] = int(m.group(1))
    return info



FLAG_BITS = {"\\seen": 1, "\\answered": 2, "\\flagged": 4, "\\deleted": 8, "\\draft": 16}


