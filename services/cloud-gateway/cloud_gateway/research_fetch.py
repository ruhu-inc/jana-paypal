"""The research agent's page fetcher (SPEC §18.3 stage 3b, §18.10): a search result's page as readable text.

So that every claim in a pack can be checked against real text, the gateway fetches the pages Spark's searches
returned. A page is fetched only when

- its URL is `https` on the default port, with no credentials in it, and the source policy allows its host;
- every address its host resolves to is global (no private, loopback, link-local, multicast or reserved address,
  IPv4-mapped addresses included). The connection then goes to the address that was checked, with the host name as
  TLS server name and `Host`, so a second DNS answer cannot point it elsewhere (DNS rebinding);
- each redirect, at most `max_redirects`, passes the same checks;
- the reply is a 200 of `text/html` or `text/plain`, within `page_max_bytes` and `page_timeout_s` for the whole fetch.

HTML is reduced with the standard library's `html.parser`: script, style, nav, header, footer, aside and form (and
other non-content elements) are dropped; headings, paragraphs and list items are kept as blocks, the title is taken
from `<title>`, and the text is cut at a block boundary to `page_max_chars`. A block with an instruction-like span
(the `inert` rule of `cloud_gateway.ask`) is withheld whole: a page that tries to steer the model is not a source.

The source policy (§18.10) has no allow-list: excluded hosts (social media, forums, video, answer-selling sites, and
top-level domains that returned only spam) are never fetched, and reference and educational hosts rank first. It
judges a page by its host, never by its claims.

Nothing here logs a URL or page text."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from cloud_gateway.ask import instruction_like
from cloud_gateway.config import Research

Resolver = Callable[[str, int], Awaitable[list[str]]]
ClientFactory = Callable[[], httpx.AsyncClient]
REDIRECTS = frozenset({301, 302, 303, 307, 308})
TYPES = frozenset({"text/html", "text/plain"})
TITLE_MAX, SITE_MAX = 160, 120


class FetchRefused(Exception):
    """Why a page was not fetched: scheme, excluded, address, redirects, status, type, size, timeout, network, empty."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Fetched:
    url: str  # the page's final URL, after redirects
    title: str
    site: str
    text: str
    withheld: int  # blocks withheld as instruction-like


def _host(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return (parts.hostname or "").lower().rstrip("."), parts.path or "/"


def _matches(host: str, path: str, entry: str) -> bool:
    domain, _, prefix = entry.lower().partition("/")
    if host != domain and not host.endswith("." + domain):
        return False
    return not prefix or path == "/" + prefix or path.startswith("/" + prefix.rstrip("/") + "/")


class SourcePolicy:
    """§18.10: exclusion and ranking by host (and an optional path prefix), never by a page's own claims."""

    def __init__(self, excluded: Iterable[str], preferred: Iterable[str], suffixes: Iterable[str],
                 excluded_suffixes: Iterable[str] = ()) -> None:  # fmt: skip
        self.excluded = tuple(excluded)
        self.excluded_suffixes = tuple(s.lower() for s in excluded_suffixes)
        self.preferred_domains = tuple(preferred)
        self.suffixes = tuple(s.lower() for s in suffixes)

    @classmethod
    def of(cls, spec: Research) -> SourcePolicy:
        return cls(spec.excluded_domains, spec.preferred_domains, spec.preferred_suffixes, spec.excluded_suffixes)

    def allowed(self, url: str) -> bool:
        host, path = _host(url)
        return (
            bool(host)
            and not any(host.endswith(s) for s in self.excluded_suffixes)
            and not any(_matches(host, path, entry) for entry in self.excluded)
        )

    def preferred(self, url: str) -> bool:
        host, path = _host(url)
        return any(host.endswith(s) for s in self.suffixes) or any(
            _matches(host, path, entry) for entry in self.preferred_domains
        )

    def rank(self, urls: Sequence[str]) -> list[str]:
        """The allowed URLs, reference and educational ones first, otherwise in the order given."""
        allowed = [u for u in urls if self.allowed(u)]
        return sorted(allowed, key=lambda u: 0 if self.preferred(u) else 1)


def public(address: str) -> bool:
    """Whether an address is one the gateway may connect to: global, and none of the special ranges."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified
    )


async def system_resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _default_client() -> httpx.AsyncClient:
    # A fresh client per page: a pooled connection made for one host name must never carry another's request.
    return httpx.AsyncClient(follow_redirects=False, trust_env=False)


class _Reducer(HTMLParser):
    SKIP = frozenset({"script", "style", "nav", "header", "footer", "aside", "form", "noscript", "template", "svg",
                      "iframe", "button", "select", "textarea", "dialog", "canvas", "object", "math"})  # fmt: skip
    BLOCKS = frozenset({"p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd", "blockquote", "pre",
                        "figcaption", "caption"})  # fmt: skip
    BOUNDARIES = frozenset({"div", "section", "article", "main", "body", "br", "hr", "table", "tr", "td", "th",
                            "ul", "ol", "dl", "figure", "details", "summary"})  # fmt: skip
    VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track",
                      "wbr", "param"})  # fmt: skip

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.in_title = False
        self.title: list[str] = []
        self.block: str | None = None  # the open kept block's tag, or None for loose text
        self.buffer: list[str] = []
        self.out: list[tuple[bool, str]] = []  # (kept block?, text) in document order

    def _flush(self) -> None:
        text = " ".join("".join(self.buffer).split())
        self.buffer = []
        if text:
            tag = self.block
            if tag is not None and tag[0] == "h" and tag[1:].isdigit():
                text = "#" * int(tag[1:]) + " " + text
            elif tag == "li":
                text = "- " + text
            self.out.append((tag is not None, text))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self.skip += 1
        elif self.skip:
            return
        elif tag == "title":
            self.in_title = True
        elif tag in self.BLOCKS:
            self._flush()
            self.block = tag
        elif tag == "br" and self.block is not None:
            self.buffer.append(" ")
        elif tag in self.BOUNDARIES:
            self._flush()  # a container inside a kept block (a list in a list item) starts a new block of its kind

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            return  # a self-closed skip element holds nothing
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag == "title":
            self.in_title = False
        elif self.skip:
            return
        elif tag in self.BLOCKS and self.block is not None:
            self._flush()
            self.block = None
        elif tag in self.BOUNDARIES and tag not in self.VOID:
            self._flush()  # the end of a container ends any block left open in it (an unclosed <li> or <p>)
            self.block = None

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title.append(data)
        elif not self.skip:
            self.buffer.append(data)

    def finish(self) -> tuple[str, list[tuple[bool, str]]]:
        self.close()
        self._flush()
        return " ".join("".join(self.title).split()), self.out


def reduce_html(html: str, min_chars: int = 200) -> tuple[str, list[str]]:
    """The page's title and its readable blocks. Text outside headings, paragraphs and list items is used only when
    those hold less than `min_chars` (a page that sets its text in bare divs)."""
    parser = _Reducer()
    parser.feed(html)
    title, out = parser.finish()
    kept = [text for is_block, text in out if is_block]
    if sum(len(t) for t in kept) < min_chars:
        kept = [text for _, text in out]
    return title, kept


def reduce_plain(text: str) -> list[str]:
    return [" ".join(block.split()) for block in re.split(r"\n\s*\n", text) if block.strip()]


def assemble(blocks: Sequence[str], max_chars: int) -> tuple[str, int]:
    """The blocks as one text, instruction-like blocks withheld, cut at a block boundary to `max_chars`."""
    out: list[str] = []
    size = withheld = 0
    for block in blocks:
        if instruction_like(block):
            withheld += 1
            continue
        extra = len(block) + (2 if out else 0)
        if size + extra > max_chars:
            if not out:  # a single block longer than the cap is cut, not dropped
                out.append(block[:max_chars])
            break
        out.append(block)
        size += extra
    return "\n\n".join(out), withheld


def _media_type(value: str | None) -> tuple[str, str | None]:
    if not value:
        return "", None
    media, _, rest = value.partition(";")
    charset = None
    for param in rest.split(";"):
        name, _, setting = param.strip().partition("=")
        if name.lower() == "charset" and setting:
            charset = setting.strip().strip('"').lower()
    return media.strip().lower(), charset


class Fetcher:
    def __init__(
        self,
        spec: Research,
        policy: SourcePolicy | None = None,
        *,
        client_factory: ClientFactory = _default_client,
        resolve: Resolver = system_resolve,
    ) -> None:
        self.spec = spec
        self.policy = policy or SourcePolicy.of(spec)
        self.client_factory = client_factory
        self.resolve = resolve

    async def fetch(self, url: str) -> Fetched:
        try:
            return await asyncio.wait_for(self._fetch(url), self.spec.page_timeout_s)
        except TimeoutError:
            raise FetchRefused("timeout") from None

    def _checked(self, url: str) -> tuple[str, str]:
        """The URL's host and its pinned path, or a refusal for the scheme, credentials, port or source policy."""
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            raise FetchRefused("scheme") from None
        host = (parts.hostname or "").lower().rstrip(".")
        if parts.scheme != "https" or not host or parts.username or parts.password or port not in (None, 443):
            raise FetchRefused("scheme")
        if not self.policy.allowed(url):
            raise FetchRefused("excluded")
        return host, urlunsplit(("", "", parts.path or "/", parts.query, ""))

    async def _address(self, host: str) -> str:
        try:
            literal = ipaddress.ip_address(host.strip("[]"))
            addresses = [str(literal)]
        except ValueError:
            try:
                addresses = await self.resolve(host, 443)
            except (OSError, UnicodeError):
                raise FetchRefused("network") from None
        if not addresses or not all(public(a) for a in addresses):
            raise FetchRefused("address")
        return addresses[0]

    async def _fetch(self, url: str) -> Fetched:
        current = url
        for _hop in range(self.spec.max_redirects + 1):
            host, target = self._checked(current)
            address = await self._address(host)
            pinned = f"https://[{address}]{target}" if ":" in address else f"https://{address}{target}"
            async with self.client_factory() as client:
                try:
                    async with client.stream(
                        "GET",
                        pinned,
                        headers={"host": host, "accept": "text/html, text/plain;q=0.9", "accept-encoding": "identity",
                                 "user-agent": "JanaResearch/1.0 (study source fetcher)"},
                        extensions={"sni_hostname": host},
                    ) as response:  # fmt: skip
                        if response.status_code in REDIRECTS:
                            location = response.headers.get("location")
                            if not location:
                                raise FetchRefused("status")
                            current = urljoin(current, location)
                            continue
                        if response.status_code != 200:
                            raise FetchRefused("status")
                        media, charset = _media_type(response.headers.get("content-type"))
                        if media not in TYPES:
                            raise FetchRefused("type")
                        declared = response.headers.get("content-length")
                        if declared is not None and declared.isdigit() and int(declared) > self.spec.page_max_bytes:
                            raise FetchRefused("size")
                        body = bytearray()
                        # Decoded bytes: a compressed page is capped by what it unpacks to.
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > self.spec.page_max_bytes:
                                raise FetchRefused("size")
                except httpx.HTTPError:
                    raise FetchRefused("network") from None
            return self._page(current, host, media, charset, bytes(body))
        raise FetchRefused("redirects")

    def _page(self, url: str, host: str, media: str, charset: str | None, body: bytes) -> Fetched:
        try:
            decoded = body.decode(charset or "utf-8", errors="replace")
        except LookupError:
            decoded = body.decode("utf-8", errors="replace")
        if media == "text/html":
            title, blocks = reduce_html(decoded, self.spec.page_min_chars)
        else:
            title, blocks = "", reduce_plain(decoded)
        text, withheld = assemble(blocks, self.spec.page_max_chars)
        if len(text) < self.spec.page_min_chars:
            raise FetchRefused("empty")
        site = host.removeprefix("www.")[:SITE_MAX]
        title = title if title and not instruction_like(title) else site
        return Fetched(url, title[:TITLE_MAX], site, text, withheld)
