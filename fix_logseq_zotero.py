#!/usr/bin/env python3
"""Fix bad ``file::`` references in Logseq pages templated from a BibTeX export.

The logseq-citation-manager plugin creates one page per BibTeX entry containing a
line of the shape::

    file:: ![Some Title](file:///path/to/file.pdf)

When attachments move, that path goes stale while the ``file`` field in the
BibTeX library stays correct.  This tool compares the two and rewrites the page.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote

import bibtexparser

DEFAULT_LIB = Path("assets/storages/logseq-citation-manager/My Library.bib")

FILE_LINE_RE = re.compile(
    r"^(?P<head>\s*file::\s*!\[(?P<title>[^\]]*)\]\()(?P<url>[^)]*)(?P<tail>\).*)$"
)
URL_RE = re.compile(r"^(?P<scheme>file:+)(?P<slashes>/*)(?P<rest>.*)$", re.IGNORECASE)


@dataclass
class Attachment:
    description: str
    path: str
    mime: str


@dataclass
class Candidate:
    cite_key: str
    page: Path
    lineno: int          # 1-based
    line: str            # without line terminator
    current_path: str    # decoded, normalised
    new_line: str
    new_path: str


def _split(text: str, sep: str) -> list[str]:
    """Split on `sep`, honouring backslash escapes but keeping them in place."""
    parts: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            buf.append(text[i : i + 2])
            i += 2
        elif text[i] == sep:
            parts.append("".join(buf))
            buf = []
            i += 1
        else:
            buf.append(text[i])
            i += 1
    parts.append("".join(buf))
    return parts


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text)


def parse_attachments(field: str) -> list[Attachment]:
    """Parse a Better BibTeX ``file`` field: ``desc:path:mime;desc:path:mime``."""
    attachments = []
    for chunk in _split(field, ";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = _split(chunk, ":")
        if len(parts) == 1:
            desc, path, mime = "", parts[0], ""
        elif len(parts) == 2:
            # Either "desc:path" or "path:mime"; only a path starts with a separator.
            if parts[0].startswith(("/", "~")):
                desc, path, mime = "", parts[0], parts[1]
            else:
                desc, path, mime = parts[0], parts[1], ""
        else:
            desc, path, mime = parts[0], ":".join(parts[1:-1]), parts[-1]
        path = _unescape(path).strip()
        if path:
            attachments.append(
                Attachment(_unescape(desc).strip(), path, _unescape(mime).strip())
            )
    return attachments


def pick_attachment(attachments: list[Attachment]) -> Attachment | None:
    """Prefer the PDF; Zotero often exports a web snapshot alongside it."""
    for att in attachments:
        if att.mime.lower() == "application/pdf":
            return att
    for att in attachments:
        if att.path.lower().endswith(".pdf"):
            return att
    return attachments[0] if attachments else None


def normalise(path: str) -> str:
    return os.path.normpath(os.path.expanduser(path))


def find_file_line(lines: list[str]) -> tuple[int, re.Match[str]] | None:
    """Return the first ``file::`` reference as (index, match)."""
    for idx, raw in enumerate(lines):
        match = FILE_LINE_RE.match(raw.rstrip("\r\n"))
        if match:
            return idx, match
    return None


def build_candidate(
    cite_key: str, page: Path, idx: int, match: re.Match[str], bib_path: str
) -> Candidate | None:
    """Compare the page's URL with the BibTeX path; return a fix if they differ."""
    url_match = URL_RE.match(match.group("url").strip())
    if url_match is None:
        return None

    scheme = url_match.group("scheme")
    slashes = url_match.group("slashes")
    rest = url_match.group("rest")

    current = unquote(rest)
    was_encoded = current != rest
    if slashes:
        current = "/" + current

    target = normalise(bib_path)
    if normalise(current) == target:
        return None

    encoded = quote(target, safe="/") if was_encoded else target
    new_url = f"{scheme}{slashes}{encoded.lstrip('/')}" if slashes else f"{scheme}{encoded}"
    line = match.group(0)
    new_line = f"{match.group('head')}{new_url}{match.group('tail')}"
    return Candidate(cite_key, page, idx + 1, line, normalise(current), new_line, target)


def apply_fix(candidate: Candidate) -> None:
    lines = candidate.page.read_text(encoding="utf-8").splitlines(keepends=True)
    idx = candidate.lineno - 1
    old = lines[idx]
    terminator = old[len(old.rstrip("\r\n")) :]
    lines[idx] = candidate.new_line + terminator
    candidate.page.write_text("".join(lines), encoding="utf-8")


class Style:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, text: str) -> str:
        return self(text, "1")

    def red(self, text: str) -> str:
        return self(text, "31")

    def green(self, text: str) -> str:
        return self(text, "32")

    def dim(self, text: str) -> str:
        return self(text, "2")


def show(candidate: Candidate, style: Style) -> None:
    print(f"{style.bold(candidate.cite_key)}  {candidate.page}:{candidate.lineno}")
    print(f"  {style.red('-')} {candidate.line.strip()}")
    print(f"  {style.green('+')} {candidate.new_line.strip()}")
    if not Path(candidate.new_path).exists():
        print(f"  {style.dim('note: target does not exist on disk')}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fix-logseq-zotero",
        description="Realign file:: references in Logseq pages with a BibTeX library.",
    )
    parser.add_argument("graph", type=Path, help="path to the Logseq graph directory")
    parser.add_argument(
        "--lib",
        type=Path,
        default=None,
        metavar="BIB",
        help=f"BibTeX library (default: <graph>/{DEFAULT_LIB})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="only list the proposed changes, then exit",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="apply_all",
        help="apply every change without asking for confirmation",
    )
    args = parser.parse_args(argv)

    style = Style(sys.stdout.isatty() and os.environ.get("NO_COLOR") is None)

    graph: Path = args.graph
    if not graph.is_dir():
        parser.error(f"graph directory not found: {graph}")
    pages = graph / "pages"
    if not pages.is_dir():
        parser.error(f"no pages directory in graph: {pages}")
    library: Path = args.lib if args.lib is not None else graph / DEFAULT_LIB
    if not library.is_file():
        parser.error(f"library not found: {library}")

    bib_parser = bibtexparser.bparser.BibTexParser(common_strings=True)
    bib_parser.ignore_nonstandard_types = False
    with library.open(encoding="utf-8") as handle:
        database = bibtexparser.load(handle, bib_parser)

    counts = {"entries": 0, "no_page": 0, "no_bib_file": 0, "no_file_line": 0, "aligned": 0}
    candidates: list[Candidate] = []

    for entry in database.entries:
        cite_key = entry.get("ID")
        if not cite_key:
            continue
        counts["entries"] += 1

        page = pages / f"{cite_key}.md"
        if not page.is_file():
            counts["no_page"] += 1
            continue

        attachment = pick_attachment(parse_attachments(entry.get("file", "")))
        if attachment is None:
            counts["no_bib_file"] += 1
            continue

        lines = page.read_text(encoding="utf-8").splitlines(keepends=True)
        found = find_file_line(lines)
        if found is None:
            counts["no_file_line"] += 1
            continue

        idx, match = found
        candidate = build_candidate(cite_key, page, idx, match, attachment.path)
        if candidate is None:
            counts["aligned"] += 1
        else:
            candidates.append(candidate)

    print(
        f"{counts['entries']} entries, {counts['entries'] - counts['no_page']} with pages: "
        f"{counts['aligned']} aligned, {len(candidates)} mismatched, "
        f"{counts['no_file_line']} without a file:: line, "
        f"{counts['no_bib_file']} without a file field in BibTeX\n"
    )

    if not candidates:
        print("Nothing to fix.")
        return 0

    if args.dry_run:
        for candidate in candidates:
            show(candidate, style)
        print(f"{len(candidates)} change(s) proposed (dry run, nothing written).")
        return 0

    if not args.apply_all and not sys.stdin.isatty():
        print(
            "stdin is not a terminal; re-run with --all to apply or --dry-run to list.",
            file=sys.stderr,
        )
        return 1

    applied = 0
    skipped = 0
    for index, candidate in enumerate(candidates):
        show(candidate, style)
        if not args.apply_all:
            try:
                answer = input("  apply? [y]es/[n]o/[a]ll/[q]uit: ").strip().lower()
            except EOFError:
                answer = "q"
            print()
            if answer in {"q", "quit"}:
                skipped += len(candidates) - index
                break
            if answer in {"a", "all"}:
                args.apply_all = True
            elif answer not in {"y", "yes"}:
                skipped += 1
                continue
        apply_fix(candidate)
        applied += 1

    print(f"{applied} change(s) applied, {skipped} skipped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
