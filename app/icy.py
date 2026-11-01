"""Pure parser for Shoutcast/Icecast in-band metadata (no network code, easy to test)."""
import re

_TITLE = re.compile(r"StreamTitle='(.*?)';", re.S)


class IcyParser:
    def __init__(self, metaint: int):
        self.metaint = metaint
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[str]:
        """Feed raw stream bytes; returns any StreamTitle values found."""
        self.buf += data
        titles = []
        while len(self.buf) > self.metaint:
            mlen = self.buf[self.metaint] * 16
            if len(self.buf) < self.metaint + 1 + mlen:
                break
            meta = bytes(self.buf[self.metaint + 1: self.metaint + 1 + mlen]).rstrip(b"\0")
            del self.buf[: self.metaint + 1 + mlen]
            if meta:
                m = _TITLE.search(meta.decode("utf-8", "replace"))
                if m and m.group(1).strip():
                    titles.append(m.group(1).strip())
        return titles
