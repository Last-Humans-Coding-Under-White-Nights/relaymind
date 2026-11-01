"""The pinned OpenClaw plugin's ``baseUrl`` rules, mirrored for the trial kit.

``parseBaseUrl`` in ``integrations/openclaw/src/config.ts`` parses the value
with the WHATWG URL parser (Node's ``new URL``), then requires http or https,
no username or password, a loopback ``hostname`` (``isLoopbackHostname``),
an empty ``search`` and ``hash``, and a ``pathname`` that is ``/v1`` once its
trailing slashes are removed. It returns ``origin + "/v1"``.

This module follows the WHATWG URL Standard's basic URL parser for the two
special schemes the plugin allows, so the kit accepts, refuses and normalizes
exactly as the plugin does (#271 review). Any scheme other than http or https
is refused: the plugin refuses it either as an invalid URL or by protocol.
Non-ASCII hosts use the UTS #46 mapping of the ``idna`` package, a required
dependency of httpx (adding it to pyproject.toml would change a reviewed
evidence anchor); only a mapping that yields an ASCII host can be a loopback
host.
"""

import unicodedata

INVALID = "invalid_base_url"
_DEFAULT_PORTS = {"http": 80, "https": 443}
_C0_OR_SPACE = "".join(chr(c) for c in range(0x21))
_HEX = "0123456789abcdefABCDEF"
_FORBIDDEN_HOST = frozenset("\x00\t\n\r #/:<>?@[\\]^|%\x7f") | {chr(c) for c in range(0x20)}


class _Failure(Exception):
    pass


def _ipv4_number(text: str) -> int:
    if not text:
        raise _Failure
    radix = 10
    if len(text) >= 2 and text[:2] in ("0x", "0X"):
        text, radix = text[2:], 16
    elif len(text) >= 2 and text[0] == "0":
        text, radix = text[1:], 8
    if not text:
        return 0
    digits = {8: "01234567", 10: "0123456789", 16: _HEX}[radix]
    if any(c not in digits for c in text):
        raise _Failure
    return int(text, radix)


def _ends_in_a_number(text: str) -> bool:
    parts = text.split(".")
    if parts[-1] == "":
        if len(parts) == 1:
            return False
        parts.pop()
    last = parts[-1]
    if last and all("0" <= c <= "9" for c in last):
        return True
    try:
        _ipv4_number(last)
    except _Failure:
        return False
    return True


def _ipv4(text: str) -> str:
    parts = text.split(".")
    if parts[-1] == "" and len(parts) > 1:
        parts.pop()
    if len(parts) > 4:
        raise _Failure
    numbers = [_ipv4_number(part) for part in parts]
    if any(n > 255 for n in numbers[:-1]) or numbers[-1] >= 256 ** (5 - len(numbers)):
        raise _Failure
    value = numbers[-1]
    for index, number in enumerate(numbers[:-1]):
        value += number * 256 ** (3 - index)
    return ".".join(str((value >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def _ipv6(text: str) -> tuple[int, ...]:
    """The WHATWG IPv6 parser; returns the eight pieces."""
    address = [0] * 8
    piece, compress, pointer = 0, None, 0

    def at(i: int) -> str:
        return text[i] if i < len(text) else ""

    if at(0) == ":":
        if at(1) != ":":
            raise _Failure
        pointer, piece = 2, 1
        compress = piece
    while at(pointer):
        if piece == 8:
            raise _Failure
        if at(pointer) == ":":
            if compress is not None:
                raise _Failure
            pointer += 1
            piece += 1
            compress = piece
            continue
        value = length = 0
        while length < 4 and at(pointer) and at(pointer) in _HEX:
            value = value * 16 + int(at(pointer), 16)
            pointer += 1
            length += 1
        if at(pointer) == ".":
            if length == 0:
                raise _Failure
            pointer -= length
            if piece > 6:
                raise _Failure
            seen = 0
            while at(pointer):
                part = None
                if seen > 0:
                    if at(pointer) == "." and seen < 4:
                        pointer += 1
                    else:
                        raise _Failure
                if not ("0" <= at(pointer) <= "9" and at(pointer)):
                    raise _Failure
                while at(pointer) and "0" <= at(pointer) <= "9":
                    number = int(at(pointer))
                    if part is None:
                        part = number
                    elif part == 0:
                        raise _Failure
                    else:
                        part = part * 10 + number
                    if part > 255:
                        raise _Failure
                    pointer += 1
                address[piece] = address[piece] * 0x100 + part
                seen += 1
                if seen in (2, 4):
                    piece += 1
            if seen != 4:
                raise _Failure
            break
        if at(pointer) == ":":
            pointer += 1
            if not at(pointer):
                raise _Failure
        elif at(pointer):
            raise _Failure
        address[piece] = value
        piece += 1
    if compress is not None:
        swaps, piece = piece - compress, 7
        while piece != 0 and swaps > 0:
            other = compress + swaps - 1
            address[piece], address[other] = address[other], address[piece]
            piece -= 1
            swaps -= 1
    elif piece != 8:
        raise _Failure
    return tuple(address)


def _percent_decode(text: str) -> bytes:
    data = text.encode("utf-8")
    out = bytearray()
    i = 0
    while i < len(data):
        if (
            data[i] == 0x25
            and i + 2 < len(data)
            and all(chr(b) in _HEX for b in data[i + 1 : i + 3])
        ):
            out.append(int(data[i + 1 : i + 3], 16))
            i += 3
        else:
            out.append(data[i])
            i += 1
    return bytes(out)


def _domain_to_ascii(domain: str) -> str:
    if not domain.isascii():
        import idna

        try:
            domain = idna.uts46_remap(domain, std3_rules=False, transitional=False)
        except (idna.IDNAError, ValueError):
            raise _Failure from None
        domain = unicodedata.normalize("NFC", domain)
        if not domain.isascii():
            # The host keeps a non-ASCII label: it is never a loopback name.
            raise _Failure
    domain = domain.lower()
    if any(label.startswith("xn--") for label in domain.split(".")):
        # A Punycode label never yields localhost or an IPv4 address.
        raise _Failure
    if not domain:
        raise _Failure
    return domain


def _host(text: str) -> str:
    """The WHATWG host parser for a special URL; returns the serialized host."""
    if text.startswith("["):
        if not text.endswith("]"):
            raise _Failure
        # Only the loopback address ::1 serializes as "[::1]"; any other
        # address is a non-loopback host, whatever its serialized form.
        pieces = _ipv6(text[1:-1])
        return "[::1]" if pieces == (0, 0, 0, 0, 0, 0, 0, 1) else "[]"
    domain = _percent_decode(text).decode("utf-8", errors="replace")
    ascii_domain = _domain_to_ascii(domain)
    if any(c in _FORBIDDEN_HOST for c in ascii_domain):
        raise _Failure
    if _ends_in_a_number(ascii_domain):
        return _ipv4(ascii_domain)
    return ascii_domain


def _loopback_hostname(hostname: str) -> bool:
    # isLoopbackHostname in integrations/openclaw/src/config.ts: no DNS.
    if hostname in ("localhost", "[::1]"):
        return True
    octets = hostname.split(".")
    return (
        len(octets) == 4
        and octets[0] == "127"
        and all(o.isascii() and o.isdigit() and len(o) <= 3 and int(o) <= 255 for o in octets)
    )


def _path_is_v1(text: str) -> bool:
    """The pathname of a special URL with its trailing slashes removed is ``/v1``."""
    segments: list[str] = []
    buffer = ""
    text = text[1:] if text[:1] in ("/", "\\") else text
    for c in [*text, ""]:
        if c in ("/", "\\", ""):
            lowered = buffer.lower()
            if lowered in ("..", ".%2e", "%2e.", "%2e%2e"):
                if segments:
                    segments.pop()
                if c == "":
                    segments.append("")
            elif lowered in (".", "%2e"):
                if c == "":
                    segments.append("")
            else:
                segments.append(buffer)
            buffer = ""
        else:
            buffer += c
    return ("/" + "/".join(segments)).rstrip("/") == "/v1"


def check_base_url(value: object) -> str:
    """The plugin's ``baseUrl`` check: its normal form, or ``invalid_base_url``.

    The refused value is never echoed: it can carry a credential (#269 review).
    """
    if not isinstance(value, str) or not value:
        raise ValueError(INVALID)
    try:
        return _check(value)
    except (_Failure, ValueError, UnicodeError):
        raise ValueError(INVALID) from None


def _check(value: str) -> str:
    text = value.strip(_C0_OR_SPACE)
    text = text.replace("\t", "").replace("\n", "").replace("\r", "")
    scheme, colon, rest = text.partition(":")
    if (
        not colon
        or not scheme
        or not scheme[0].isascii()
        or not scheme[0].isalpha()
        or any(not (c.isascii() and (c.isalnum() or c in "+-.")) for c in scheme)
    ):
        raise _Failure
    scheme = scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise _Failure
    rest = rest.lstrip("/\\")
    end = next((i for i, c in enumerate(rest) if c in "/\\?#"), len(rest))
    authority, rest = rest[:end], rest[end:]
    userinfo, at, host_port = authority.rpartition("@")
    if at and not host_port:
        raise _Failure
    # The username and password are both empty only for "" or ":" before the
    # last "@"; an earlier "@" is kept as %40 in a credential.
    if userinfo not in ("", ":"):
        raise _Failure
    inside, split = False, None
    for i, c in enumerate(host_port):
        if c == "[":
            inside = True
        elif c == "]":
            inside = False
        elif c == ":" and not inside:
            split = i
            break
    host_text = host_port if split is None else host_port[:split]
    port_text = "" if split is None else host_port[split + 1 :]
    if not host_text:
        raise _Failure
    host = _host(host_text)
    if any(not ("0" <= c <= "9") for c in port_text):
        raise _Failure
    port = int(port_text) if port_text else None
    if port is not None and port > 65535:
        raise _Failure
    if port == _DEFAULT_PORTS[scheme]:
        port = None
    # The path ends at the first "?" or "#"; the query ends at "#". The
    # plugin's search and hash are empty for an empty query and fragment.
    end = next((i for i, c in enumerate(rest) if c in "?#"), len(rest))
    path, tail = rest[:end], rest[end:]
    query, fragment = "", tail[1:]
    if tail.startswith("?"):
        query, _, fragment = tail[1:].partition("#")
    if query or fragment:
        raise _Failure
    if not _loopback_hostname(host) or not _path_is_v1(path):
        raise _Failure
    origin = f"{scheme}://{host}" + ("" if port is None else f":{port}")
    return origin + "/v1"
