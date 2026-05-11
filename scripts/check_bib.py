"""
Verify _bibliography/papers.bib against arXiv (and optionally Crossref).

Run from project root:
    python scripts/check_bib.py
    python scripts/check_bib.py --fix          # rewrite papers.bib in place
    python scripts/check_bib.py --doi          # also check Crossref via DOI/html links

Stdlib-only. Targets the al-folio bib format produced for this site
(see _bibliography/papers.bib).
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

ATOM_NS = "{http://www.w3.org/2005/Atom}"
ARXIV_API = "https://export.arxiv.org/api/query?id_list={ids}"
CROSSREF_API = "https://api.crossref.org/works/{doi}"

# ANSI colors
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"


# --- Bib parsing -----------------------------------------------------------

@dataclass
class Entry:
    raw: str                       # original block including @type{key, ... }
    entry_type: str                # e.g. "article"
    key: str                       # citation key
    fields: dict[str, str] = field(default_factory=dict)

    def get(self, name: str) -> str | None:
        return self.fields.get(name.lower())


def parse_bib(text: str) -> list[Entry]:
    entries: list[Entry] = []
    i = 0
    while i < len(text):
        at = text.find("@", i)
        if at == -1:
            break
        # find first '{'
        brace = text.find("{", at)
        if brace == -1:
            break
        entry_type = text[at + 1 : brace].strip().lower()
        # balanced braces from `brace` onward
        depth = 0
        j = brace
        while j < len(text):
            c = text[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if j >= len(text):
            break
        block_end = j + 1
        block = text[at:block_end]
        # parse inside braces
        inner = text[brace + 1 : j]
        first_comma = inner.find(",")
        if first_comma == -1:
            i = block_end
            continue
        key = inner[:first_comma].strip()
        body = inner[first_comma + 1 :]
        fields = _parse_fields(body)
        entries.append(Entry(raw=block, entry_type=entry_type, key=key, fields=fields))
        i = block_end
    return entries


def _parse_fields(body: str) -> dict[str, str]:
    """Parse `name = {value}` pairs, value may contain nested braces."""
    fields: dict[str, str] = {}
    i = 0
    while i < len(body):
        # skip ws/commas/newlines
        while i < len(body) and body[i] in " \t\n\r,":
            i += 1
        if i >= len(body):
            break
        # name = ...
        eq = body.find("=", i)
        if eq == -1:
            break
        name = body[i:eq].strip().lower()
        i = eq + 1
        while i < len(body) and body[i] in " \t\n\r":
            i += 1
        if i >= len(body):
            break
        if body[i] == "{":
            depth = 1
            i += 1
            start = i
            while i < len(body) and depth > 0:
                if body[i] == "{":
                    depth += 1
                elif body[i] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            value = body[start:i]
            i += 1  # past closing }
        elif body[i] == '"':
            i += 1
            start = i
            while i < len(body) and body[i] != '"':
                i += 1
            value = body[start:i]
            i += 1
        else:
            # bare token (rare in our bib)
            start = i
            while i < len(body) and body[i] not in ",\n":
                i += 1
            value = body[start:i].strip()
        fields[name] = value.strip()
    return fields


# --- Name normalisation ----------------------------------------------------

def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def bib_authors(value: str) -> list[str]:
    """Split a bib 'author' field on ' and ', return ['First Last', ...]."""
    raw = re.split(r"\s+and\s+", value)
    names = []
    for n in raw:
        n = n.strip()
        if not n:
            continue
        if "," in n:
            last, first = [p.strip() for p in n.split(",", 1)]
            names.append(f"{first} {last}")
        else:
            names.append(n)
    return [_normalize_latex(n) for n in names]


def _normalize_latex(name: str) -> str:
    """Strip BibTeX/LaTeX wrappers: {\'a} → á, {\v c} → č, simplistic."""
    name = name.replace("{", "").replace("}", "")
    # common: \'a -> á, \"o -> ö, \v{c} -> č, \v c -> č
    for esc, rep in [
        (r"\\'a", "á"), (r"\\'e", "é"), (r"\\'i", "í"), (r"\\'o", "ó"), (r"\\'u", "ú"),
        (r"\\v\s?c", "č"), (r"\\v\s?s", "š"), (r"\\v\s?z", "ž"),
        (r"\\\"a", "ä"), (r"\\\"o", "ö"), (r"\\\"u", "ü"),
        (r"\\`a", "à"), (r"\\`e", "è"),
        (r"\\^a", "â"), (r"\\^e", "ê"), (r"\\^o", "ô"),
        (r"\\~n", "ñ"),
    ]:
        name = re.sub(esc, rep, name)
    return name.strip()


def name_key(name: str) -> str:
    """Lowercase, accent-stripped, whitespace-normalised for comparison."""
    return re.sub(r"\s+", " ", strip_accents(name).lower()).strip()


# --- arXiv lookups ---------------------------------------------------------

def fetch_arxiv_batch(ids: list[str]) -> dict[str, dict]:
    """Return {arxiv_id: {'title': ..., 'authors': [...]}} for given IDs."""
    if not ids:
        return {}
    url = ARXIV_API.format(ids=",".join(ids))
    req = urllib.request.Request(url, headers={"User-Agent": "bib-check/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        xml = resp.read()
    root = ET.fromstring(xml)
    out: dict[str, dict] = {}
    for entry in root.findall(f"{ATOM_NS}entry"):
        id_url = entry.findtext(f"{ATOM_NS}id", "")
        # http://arxiv.org/abs/2509.15147v1
        m = re.search(r"arxiv\.org/abs/([\w\.\-]+?)(v\d+)?$", id_url)
        if not m:
            continue
        aid = m.group(1)
        title = (entry.findtext(f"{ATOM_NS}title", "") or "").strip()
        title = re.sub(r"\s+", " ", title)
        authors = [
            (a.findtext(f"{ATOM_NS}name") or "").strip()
            for a in entry.findall(f"{ATOM_NS}author")
        ]
        out[aid] = {"title": title, "authors": authors}
    return out


# --- Crossref lookups ------------------------------------------------------

def fetch_crossref(doi: str) -> dict | None:
    url = CROSSREF_API.format(doi=urllib.parse.quote(doi, safe="/"))
    req = urllib.request.Request(url, headers={"User-Agent": "bib-check/1.0 (mailto:noreply@example.com)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            import json
            payload = json.load(resp)
    except Exception as e:
        return None
    msg = payload.get("message", {})
    title = " ".join(msg.get("title", []) or [])
    authors = []
    for a in msg.get("author", []) or []:
        given = a.get("given", "")
        family = a.get("family", "")
        full = f"{given} {family}".strip()
        if full:
            authors.append(full)
    return {"title": title, "authors": authors}


# --- Reporting -------------------------------------------------------------

def compare_authors(bib_list: list[str], external_list: list[str]) -> list[str]:
    """Return human-readable diffs."""
    diffs: list[str] = []
    n = max(len(bib_list), len(external_list))
    for i in range(n):
        b = bib_list[i] if i < len(bib_list) else "(missing)"
        e = external_list[i] if i < len(external_list) else "(missing)"
        if name_key(b) != name_key(e):
            diffs.append(f"  author {i + 1}: bib={b!r}  ext={e!r}")
    return diffs


def compare_titles(bib_title: str, ext_title: str) -> str | None:
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    if norm(bib_title) != norm(ext_title):
        return f"  title differs:\n    bib: {bib_title!r}\n    ext: {ext_title!r}"
    return None


# --- Main ------------------------------------------------------------------

def doi_from_entry(entry: Entry) -> str | None:
    """Pull a DOI out of `doi` or out of an html-link to doi.org / springer / wiley / mdpi."""
    if entry.get("doi"):
        return entry.get("doi").strip()
    html = entry.get("html") or ""
    m = re.search(r"10\.\d{4,9}/[^\s\"<>}]+", html)
    if not m:
        return None
    doi = m.group(0)
    # strip publisher-specific path suffixes that aren't part of the DOI
    for suffix in ("/full", "/abstract", "/pdf", "/html", "/meta"):
        if doi.endswith(suffix):
            doi = doi[: -len(suffix)]
    return doi


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bib", default="_bibliography/papers.bib", help="path to bib file")
    parser.add_argument("--doi", action="store_true", help="also check Crossref for entries with html/doi")
    parser.add_argument("--fix", action="store_true", help="rewrite the bib with arxiv corrections (titles + authors)")
    args = parser.parse_args(argv)

    bib_path = Path(args.bib)
    if not bib_path.exists():
        print(f"{RED}error: {bib_path} not found{RESET}", file=sys.stderr)
        return 2

    text = bib_path.read_text(encoding="utf-8")
    entries = parse_bib(text)

    # batch arxiv IDs
    arxiv_entries = [(e, e.get("arxiv")) for e in entries if e.get("arxiv")]
    aids = [a for _, a in arxiv_entries]
    print(f"{DIM}Fetching {len(aids)} arXiv records...{RESET}")
    arxiv_data = fetch_arxiv_batch(aids) if aids else {}

    issues = 0
    rewrites: list[tuple[Entry, str]] = []  # (entry, new_raw)

    for entry, aid in arxiv_entries:
        data = arxiv_data.get(aid)
        if not data:
            print(f"{YELLOW}[?]{entry.key} (arXiv:{aid}) — no record returned{RESET}")
            continue

        bib_title = entry.get("title") or ""
        bib_title_clean = re.sub(r"[{}]", "", bib_title)
        title_diff = compare_titles(bib_title_clean, data["title"])
        bib_auth_list = bib_authors(entry.get("author") or "")
        author_diffs = compare_authors(bib_auth_list, data["authors"])

        if not title_diff and not author_diffs:
            print(f"{GREEN}[OK]{entry.key} (arXiv:{aid}){RESET}")
            continue

        issues += 1
        print(f"{RED}[FAIL]{entry.key} (arXiv:{aid}){RESET}")
        if title_diff:
            print(title_diff)
        for d in author_diffs:
            print(d)

        if args.fix and (title_diff or author_diffs):
            new_raw = _rewrite_entry(entry.raw, new_title=data["title"], new_authors=data["authors"])
            rewrites.append((entry, new_raw))

    # Crossref pass (optional)
    if args.doi:
        print(f"\n{DIM}Crossref pass...{RESET}")
        for entry in entries:
            doi = doi_from_entry(entry)
            if not doi:
                continue
            time.sleep(0.2)  # be polite
            data = fetch_crossref(doi)
            if not data:
                print(f"{YELLOW}[?]{entry.key} (DOI:{doi}) — Crossref failed{RESET}")
                continue
            bib_title = entry.get("title") or ""
            bib_title_clean = re.sub(r"[{}]", "", bib_title)
            title_diff = compare_titles(bib_title_clean, data["title"])
            bib_auth_list = bib_authors(entry.get("author") or "")
            author_diffs = compare_authors(bib_auth_list, data["authors"])
            if not title_diff and not author_diffs:
                print(f"{GREEN}[OK]{entry.key} (DOI:{doi}){RESET}")
                continue
            issues += 1
            print(f"{RED}[FAIL]{entry.key} (DOI:{doi}){RESET}")
            if title_diff:
                print(title_diff)
            for d in author_diffs:
                print(d)

    # Apply rewrites
    if args.fix and rewrites:
        new_text = text
        for entry, new_raw in rewrites:
            new_text = new_text.replace(entry.raw, new_raw, 1)
        bib_path.write_text(new_text, encoding="utf-8")
        print(f"\n{GREEN}wrote {len(rewrites)} corrections to {bib_path}{RESET}")

    print()
    if issues == 0:
        print(f"{GREEN}All entries match.{RESET}")
        return 0
    print(f"{YELLOW}{issues} entries have issues. Re-run with --fix to apply arXiv corrections.{RESET}")
    return 1


def _rewrite_entry(raw: str, *, new_title: str, new_authors: list[str]) -> str:
    """Replace `title = {...}` and `author = {...}` inside an entry block."""
    out = raw
    # title
    out = re.sub(
        r"(title\s*=\s*\{)[^}]*?(\})",
        lambda m: f"{m.group(1)}{new_title}{m.group(2)}",
        out,
        count=1,
        flags=re.IGNORECASE,
    )
    # authors: "First Last" -> "Last, First"
    authors_bib = " and ".join(_to_bib_name(a) for a in new_authors)
    out = re.sub(
        r"(author\s*=\s*\{)[^}]*?(\})",
        lambda m: f"{m.group(1)}{authors_bib}{m.group(2)}",
        out,
        count=1,
        flags=re.IGNORECASE,
    )
    return out


def _to_bib_name(full: str) -> str:
    parts = full.strip().split()
    if len(parts) == 1:
        return parts[0]
    return f"{parts[-1]}, {' '.join(parts[:-1])}"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
