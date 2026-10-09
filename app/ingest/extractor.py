"""HTML -> clean, structured text.

Strategy:
1. Drop site chrome (header/footer/nav, search bar, chatbot widget, "related" sliders, stats strips).
2. Serialise the remaining DOM in reading order into lines, keeping inline text together
   (so "<p>Costs <b>$18,995</b> USD</p>" stays one sentence) and marking headings.
3. Flatten tables into self-describing rows ("Procedure: Ozone Therapy | Low: $500 | High: $1,500")
   so a retrieved row still makes sense out of context.
4. Split the lines into sections keyed by their heading path.
5. Separately read schema.org Product/Offer JSON-LD: these are the site's own structured
   prices and become explicit, citable "price facts".
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

from bs4 import BeautifulSoup, NavigableString, Tag

STRIP_TAGS = ["script", "style", "noscript", "svg", "header", "footer", "nav", "template", "form",
              "iframe", "button", "img", "picture", "video", "select", "input", "textarea"]
STRIP_CLASS = re.compile(
    r"related|logos-widget|accreditation|splide|breadcrumb|share|modal|popup|cookie|newsletter|"
    r"pw-footer|header-container|sidebar|search-bar|chatbot|pw-stats|review-form|rating-form",
    re.I,
)
STRIP_IDS = {"menu", "chatbot-widget"}
BLOCK_TAGS = {"p", "div", "section", "article", "main", "ul", "ol", "li", "dl", "dt", "dd", "blockquote",
              "tr", "td", "th", "tbody", "thead", "aside", "figure", "figcaption", "pre"}
HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
# UI labels that carry no knowledge.
NOISE_LINES = {
    "chat with center", "quote", "read more", "load more", "view center reviews", "write a review",
    "get your quote now!", "messenger", "whatsapp", "email", "free call", "need help?", "actions", ",",
    "contact itc for consultation", "view more", "show more", "share",
}
NOISE_PATTERNS = [re.compile(p, re.I) for p in (r"=\$\w+\?>",)]  # unrendered PHP template artefacts


@dataclass
class Section:
    heading_path: list[str]
    text: str


@dataclass
class PriceFact:
    item: str
    price: str
    currency: str
    seller: str


@dataclass
class Page:
    url: str
    page_type: str
    title: str
    sections: list[Section] = field(default_factory=list)
    price_facts: list[PriceFact] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _clean(text: str) -> str:
    for pat in NOISE_PATTERNS:
        text = pat.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _table_rows(table: Tag) -> list[str]:
    rows = table.find_all("tr")
    if not rows:
        return []
    header_cells = rows[0].find_all(["th", "td"])
    headers = [_clean(c.get_text(" ")) for c in header_cells]
    has_header = bool(rows[0].find("th")) or table.find("thead") is not None
    out = []
    for row in rows[1:] if has_header else rows:
        cells = [_clean(c.get_text(" ")) for c in row.find_all(["td", "th"])]
        if not any(cells):
            continue
        if has_header and len(headers) == len(cells):
            pairs = [f"{h}: {v}" for h, v in zip(headers, cells) if v and h.lower() not in {"action", "actions"}]
        else:
            pairs = [v for v in cells if v]
        out.append(" | ".join(pairs))
    return out


class _Serializer:
    """Walks the DOM and produces ('h', level, text) / ('t', text) items in reading order."""

    def __init__(self) -> None:
        self.items: list[tuple] = []
        self._buf: list[str] = []
        self._bullet = False  # next emitted line starts a list item

    def flush(self) -> None:
        line = _clean(" ".join(self._buf))
        self._buf = []
        if line and line.lower() not in NOISE_LINES:
            if self._bullet:
                line, self._bullet = f"- {line}", False
            self.items.append(("t", line))

    def walk(self, el: Tag) -> None:
        for child in el.children:
            if isinstance(child, NavigableString):
                if child.__class__.__name__ in ("Comment", "Doctype", "CData", "ProcessingInstruction"):
                    continue
                self._buf.append(str(child))
            elif isinstance(child, Tag):
                name = child.name
                if name in HEADINGS:
                    self.flush()
                    text = _clean(child.get_text(" "))
                    if text:
                        self.items.append(("h", HEADINGS[name], text))
                elif name == "table":
                    self.flush()
                    for row in _table_rows(child):
                        self.items.append(("t", row))
                elif name == "br":
                    self.flush()
                elif name == "li":
                    self.flush()
                    self._bullet = True
                    self.walk(child)
                    self.flush()
                    self._bullet = False
                elif name in BLOCK_TAGS:
                    self.flush()
                    self.walk(child)
                    self.flush()
                else:  # inline: keep flowing into the current line
                    self.walk(child)


def _strip_chrome(soup: BeautifulSoup) -> None:
    for tag in soup(STRIP_TAGS):
        tag.decompose()
    for tag in soup.find_all(id=lambda i: i in STRIP_IDS):
        tag.decompose()
    for tag in soup.find_all(class_=STRIP_CLASS):
        tag.decompose()


def _format_price(raw: str) -> str:
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return raw
    return f"{value:,.0f}" if value.is_integer() else f"{value:,.2f}"


def _price_facts(soup: BeautifulSoup) -> list[PriceFact]:
    facts: list[PriceFact] = []
    seen = set()
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in data if isinstance(data, list) else [data]:
            if not isinstance(node, dict) or node.get("@type") != "Product":
                continue
            offers = node.get("offers") or {}
            offers = offers[0] if isinstance(offers, list) and offers else offers
            name = _clean(str(node.get("name") or ""))
            price = str(offers.get("price") or offers.get("lowPrice") or "").strip()
            currency = str(offers.get("priceCurrency") or "").strip()
            if not name or not price:
                continue
            seller = _clean(str((node.get("brand") or {}).get("name") or ""))
            key = (name, price, currency)
            if key not in seen:
                seen.add(key)
                facts.append(PriceFact(item=name, price=_format_price(price), currency=currency, seller=seller))
    return facts


def _sections(items: list[tuple], title: str) -> list[Section]:
    sections: list[Section] = []
    path: list[tuple[int, str]] = []
    lines: list[str] = []

    def emit() -> None:
        text = "\n".join(lines).strip()
        if text:
            sections.append(Section([h for _, h in path] or [title], text))

    for item in items:
        if item[0] == "h":
            emit()
            lines = []
            level, text = item[1], item[2]
            while path and path[-1][0] >= level:
                path.pop()
            path.append((level, text))
        else:
            lines.append(item[1])
    emit()
    return sections


def extract(html: str, url: str, page_type: str) -> Page:
    soup = BeautifulSoup(html, "lxml")
    facts = _price_facts(soup)  # before stripping: JSON-LD lives in <script>
    h1 = soup.find("h1")
    title = _clean(h1.get_text(" ")) if h1 else _clean(soup.title.get_text() if soup.title else url)
    _strip_chrome(soup)
    ser = _Serializer()
    ser.walk(soup.body or soup)
    ser.flush()
    return Page(url=url, page_type=page_type, title=title, sections=_sections(ser.items, title), price_facts=facts)
