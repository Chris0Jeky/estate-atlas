"""Stage the GitHub Pages docs site for estate-atlas.

Standard library only, deterministic, read-only against everything except ``--out``.

    python scripts/build_docs_site.py [--out _site_src]

Copies an explicit allowlist of existing docs into a folder that GitHub's own Jekyll
(``actions/jekyll-build-pages``) turns into a site: ``README.md`` becomes ``index.md``, three design
docs keep their ``docs/`` paths, ``LICENSE`` becomes ``LICENSE.txt``. The look comes from a small theme
in ``scripts/docs_site`` (a layout and one stylesheet whose brand tokens sit in one block); the script writes
the sidebar and top navigation as ``_data/docs_nav.yml`` and gives every page a title, and does all link conversion itself (the Jekyll relative-links plugin is switched
off): a link to a published page points at the page's rendered ``.html`` file, relative to the linking
page, and a link to a file that is not published points at the file on GitHub. Links inside code (fenced
blocks of any fence length, indented blocks, code spans of any backtick length) are never touched. It
renders the fictional ``examples/shop`` atlas with the package's own HTML renderer and tour, so the demo
is the real output. A missing allowlisted file is an error (exit 1), never a silently thinner site.

``--out`` must lie strictly inside the repository root and is replaced wholesale, so it must be absent,
empty or a folder this script made: one holding a ``.docs-site-stage`` marker that is a regular file
with the exact text this script writes. Anything else is refused.
"""
from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REPO_BLOB = "https://github.com/Chris0Jeky/estate-atlas/blob/main/"
MARKER = ".docs-site-stage"
MARKER_TEXT = "staged by scripts/build_docs_site.py\n"
# Windows marks a junction (and a symlink, and a cloud placeholder) with this file attribute. ``Path.is_junction``
# only exists from Python 3.12, so the attribute is read from ``os.lstat`` directly.
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
EXAMPLE_ATLAS = "examples/shop/atlas.json"
EXAMPLE_HTML = "example/shop-atlas.html"
EXAMPLE_PAGE = "example.md"

# (repository path, staged path), in navigation order. Everything else is not published.
PAGES = (
    ("README.md", "index.md"),
    ("docs/DESIGN.md", "docs/DESIGN.md"),
    ("docs/QUALIFICATION.md", "docs/QUALIFICATION.md"),
    ("docs/ORIGIN_IDENTITY.md", "docs/ORIGIN_IDENTITY.md"),
)
LICENSE = ("LICENSE", "LICENSE.txt")
NAV = (
    ("Home", "index.md"),
    ("Design", "docs/DESIGN.md"),
    ("Qualification", "docs/QUALIFICATION.md"),
    ("Origin identity", "docs/ORIGIN_IDENTITY.md"),
    ("Example atlas", EXAMPLE_PAGE),
)
PLUGINS = ("jekyll-optional-front-matter", "jekyll-titles-from-headings", "jekyll-default-layout")
# The theme: copied as-is from scripts/docs_site into the staged site (checked like every other input).
THEME_DIR = "scripts/docs_site"
THEME_FILES = ("_layouts/default.html", "assets/css/docs.css")
NAV_DATA = "_data/docs_nav.yml"
REPO_URL = "https://github.com/Chris0Jeky/estate-atlas"
SITE_LICENSE = "GPL-3.0-only"
# Repository path each staged page comes from, for the "Edit this page" link.
SOURCE_OF = {dest: src for src, dest in PAGES}

LINK = re.compile(r'\[([^\]]*)\]\(([^)\s]+)((?:\s+"[^"]*")?)\)')
REF_DEF = re.compile(r"^( {0,3}\[[^\]\n]+\]:[ \t]*)(<[^>\n]*>|\S+)(.*)$")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
QUOTE = re.compile(r"^ {0,3}> ?")
FENCE_OPEN = re.compile(r"^( *)(`{3,}|~{3,})(.*)$")
ATX = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)")
THEMATIC = re.compile(r"^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
LIST_MARKER = re.compile(r"^( *)([-+*]|\d{1,9}[.)])(?:( +)(?=\S)| *$)")
LIQUID_OPENER = re.compile(r"\{(?=[{%])")
MASK = "\x00"
PLAIN_YAML = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ,.()-]*$")


class SiteError(Exception):
    """A reason the site cannot be staged; reported once, exit 1."""


def _text(path: Path) -> str:
    """A repository file as LF-only UTF-8 text, so output does not depend on the checkout's line endings."""
    try:
        raw = path.read_bytes()
    except OSError as err:
        raise SiteError("allowlisted file missing or unreadable: %s (%s)" % (path.name, err.strerror)) from err
    return raw.decode("utf-8").lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")


def _is_link(path: Path) -> bool:
    """A symlink, or on Windows any reparse point: a junction is one, and Python 3.11 cannot name it otherwise."""
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        return True
    try:
        return bool(getattr(os.lstat(path), "st_file_attributes", 0) & REPARSE_POINT)
    except OSError:
        return False


def _check_input(root: Path, rel: str) -> None:
    """Refuse an input that is missing, not a regular file, or reaches outside the allowlist by a link.

    Neither the file nor any folder between the repository root and it may be a symlink or a junction (any
    Windows reparse point), and the resolved path must stay inside the root and out of ``.git``.
    """
    if not os.path.lexists(root / rel):
        raise SiteError("allowlisted file missing: %s" % rel)
    current = root
    for part in Path(rel).parts:
        current = current / part
        if _is_link(current):
            raise SiteError("allowlisted input is, or sits under, a symlink or junction: %s" % rel)
    resolved = (root / rel).resolve()
    try:
        inside = resolved.relative_to(root.resolve())
    except ValueError:
        raise SiteError("allowlisted input resolves outside the repository: %s" % rel) from None
    if ".git" in inside.parts:
        raise SiteError("allowlisted input resolves inside .git: %s" % rel)
    if not resolved.is_file():
        raise SiteError("allowlisted input is not a regular file: %s" % rel)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _scalar(value: str) -> str:
    return value if PLAIN_YAML.match(value) else json.dumps(value, ensure_ascii=False)


def _unquote(line: str) -> tuple[int, str]:
    """The blockquote depth of ``line`` and what is left of it after the ``>`` markers."""
    depth = 0
    while True:
        match = QUOTE.match(line)
        if not match:
            return depth, line
        depth += 1
        line = line[match.end():]


def _classify(text: str) -> list[tuple[str, bool]]:
    """``(line, is_code)`` for each line of ``text``: fenced code (the delimiter lines included) and indented code.

    A fence closes on a line of the same character, at least as long as the opener, with nothing after it;
    one that never closes runs to the end, as in CommonMark. Indented code is four columns past the enclosing
    list item (or the margin), after a blank line or a non-paragraph block; it cannot interrupt a paragraph, and a
    list item's own continuation paragraph is not code. Blockquote markers are looked through.
    """
    result: list[tuple[str, bool]] = []
    fence = None  # (character, length, base column, blockquote depth)
    containers: list[int] = []  # content column of each open list item
    open_depth = 0
    in_paragraph = False
    for raw in text.split("\n"):
        depth, body = _unquote(raw.expandtabs(4))
        blank = not body.strip()
        indent = len(body) - len(body.lstrip(" "))
        if fence is not None:
            char, length, base, fence_depth = fence
            if depth >= fence_depth and (blank or indent >= base):
                if indent - base <= 3 and re.fullmatch(re.escape(char) + "{%d,}" % length, body.strip()):
                    fence = None
                    in_paragraph = False
                result.append((raw, True))
                continue
            fence = None  # its blockquote or list item ended without a closing line
        if depth != open_depth:
            containers, in_paragraph, open_depth = [], False, depth
        if blank:
            in_paragraph = False
            result.append((raw, False))
            continue
        thematic = bool(THEMATIC.match(body))
        marker = None if thematic else LIST_MARKER.match(body)
        atx = bool(ATX.match(body))
        opener = FENCE_OPEN.match(body)
        starts_block = bool(marker or atx or thematic or opener)
        while containers and indent < containers[-1] and not (in_paragraph and not starts_block):
            containers.pop()
        base = containers[-1] if containers else 0
        relative = indent - base
        if not in_paragraph and relative >= 4:
            result.append((raw, True))  # indented code
            continue
        if marker:
            spaces = len(marker.group(3) or "")
            width = indent + len(marker.group(2))
            gap = spaces if 1 <= spaces <= 4 else 1
            containers.append(width + gap)
            rest = body[width + gap:]
            opener = FENCE_OPEN.match(rest)
            rest_indent = len(rest) - len(rest.lstrip(" "))
            if rest.strip() and rest_indent >= 4:
                result.append((raw, True))  # code indented from the marker (five or more spaces after it)
                in_paragraph = False
                continue
            base, relative = containers[-1], rest_indent
            atx = bool(ATX.match(rest))
            thematic = bool(THEMATIC.match(rest))
            if not rest.strip():
                in_paragraph = False
                result.append((raw, False))
                continue
        if opener and relative <= 3 and not (in_paragraph and relative >= 4):
            char, info = opener.group(2), opener.group(3)
            if char[0] == "~" or "`" not in info:
                fence = (char[0], len(char), base, depth)
                in_paragraph = False
                result.append((raw, True))
                continue
        in_paragraph = not (atx or thematic)
        result.append((raw, False))
    return result


def _mask_code_spans(text: str) -> str:
    """``text`` with every code span replaced by ``MASK`` characters of the same length (newlines kept).

    A span opens at a run of backticks and closes at the next run of exactly the same length; a run with no
    such closer is literal text, and a backslash-escaped backtick opens nothing.
    """
    out = list(text)
    size = len(text)
    i = 0
    while i < size:
        if text[i] == "\\":
            i += 2
            continue
        if text[i] != "`":
            i += 1
            continue
        j = i
        while j < size and text[j] == "`":
            j += 1
        run, k, close = j - i, j, None
        while k < size:
            if text[k] != "`":
                k += 1
                continue
            end = k
            while end < size and text[end] == "`":
                end += 1
            if end - k == run:
                close = end
                break
            k = end
        if close is None:
            i = j
            continue
        for index in range(i, close):
            if text[index] != "\n":
                out[index] = MASK
        i = close
    return "".join(out)


def _published(staged_path: str) -> str:
    """The path Jekyll renders a staged file to: a Markdown page becomes the ``.html`` file."""
    return staged_path[:-3] + ".html" if staged_path.endswith(".md") else staged_path


def rewrite_links(text: str, source: str, staged: dict[str, str]) -> str:
    """Point every Markdown link at the rendered page or, failing that, at the file on GitHub.

    ``source`` is the repository path of the page, ``staged`` maps published repository paths to staged
    paths. External links, anchors, absolute paths and code (fenced, indented or inline) are left alone.
    Inline links and reference definitions are handled; staged Markdown pages are linked as their ``.html``.
    """
    base = posixpath.dirname(source)
    here = posixpath.dirname(staged[source])

    def retarget(target: str) -> str | None:
        split = re.split(r"([#?])", target, maxsplit=1)
        path, suffix = split[0], "".join(split[1:])
        if not path or SCHEME.match(path) or path.startswith("/"):
            return None
        resolved = posixpath.normpath(posixpath.join(base, path))
        if resolved in (".", "..") or resolved.startswith("../"):
            return None
        if resolved in staged:
            new = posixpath.relpath(_published(staged[resolved]), here or ".")
        else:
            new = REPO_BLOB + quote(resolved, safe="/") + ("/" if path.endswith("/") else "")
        return new + suffix

    def paragraph(run: str) -> str:
        lines, masked_lines = run.split("\n"), _mask_code_spans(run).split("\n")
        for index, masked_line in enumerate(masked_lines):
            match = REF_DEF.match(masked_line)
            if match and MASK not in match.group(2):
                target = match.group(2)
                new = retarget(target[1:-1] if target.startswith("<") else target)
                if new is not None:
                    line = lines[index]
                    lines[index] = line[:match.start(2)] + ("<%s>" % new if target.startswith("<") else new) \
                        + line[match.end(2):]
        run = "\n".join(lines)
        masked = _mask_code_spans(run)
        pieces, position = [], 0
        for match in LINK.finditer(masked):
            pieces.append(run[position:match.start()])
            target, title = match.group(2), run[match.start(3):match.end(3)]
            new = None if MASK in target + title else retarget(target)
            pieces.append(run[match.start():match.end()] if new is None else
                          "[%s](%s%s)" % (run[match.start(1):match.end(1)], new, title))
            position = match.end()
        pieces.append(run[position:])
        return "".join(pieces)

    out: list[str] = []
    run: list[str] = []
    for line, code in _classify(text):
        if code or not line.strip():
            if run:
                out.extend(paragraph("\n".join(run)).split("\n"))
                run = []
            out.append(line)
        else:
            run.append(line)
    if run:
        out.extend(paragraph("\n".join(run)).split("\n"))
    return "\n".join(out)


def _first_heading(text: str, fallback: str) -> str:
    for line, code in _classify(text):
        if not code and line.startswith("# "):
            return line[2:].strip()
    return fallback


def _demote_headings(text: str) -> str:
    return "\n".join(line if code or not line.startswith("#") else "#" + line for line, code in _classify(text))


def _url(staged_path: str) -> str:
    """The site-relative URL Jekyll gives a staged page (``page.url``)."""
    return "/" if staged_path == "index.md" else "/" + _published(staged_path)


def _nav_data(tagline: str) -> str:
    """``_data/docs_nav.yml`` for the layout: brand, top navigation and the sidebar groups. JSON strings are YAML."""
    item = lambda label, target: "{title: %s, url: %s}" % (json.dumps(label), json.dumps(_url(target)))
    lines = [
        "title: estate-atlas",
        "tagline: " + json.dumps(tagline, ensure_ascii=False),
        "repo: " + json.dumps(REPO_URL),
        "license: " + json.dumps(SITE_LICENSE),
        "top:",
    ]
    lines += ["  - " + item(label, target) for label, target in NAV]
    lines += ["groups:", "  - title: Documentation", "    pages:"]
    lines += ["      - " + item(label, target) for label, target in NAV]
    return "\n".join(lines) + "\n"


def _liquid_safe(body: str) -> str:
    """``body`` for the inside of a ``raw`` block. Liquid leaves ``raw`` at the first ``endraw`` tag it tokenises,
    so each ``{`` that could begin a ``{{`` or ``{%`` is output as a string literal between a closed and a
    reopened ``raw``: the block then holds no Liquid delimiter at all, whatever the document says."""
    return LIQUID_OPENER.sub('{% endraw %}{{ "{" }}{% raw %}', body)


def _page(dest: str, title: str, body: str) -> str:
    """Front matter and the body, which Liquid must not interpret. Navigation comes from the layout."""
    matter = ["title: " + json.dumps(title, ensure_ascii=False), "layout: default"]
    if dest in SOURCE_OF:
        matter.append("source_path: " + json.dumps(SOURCE_OF[dest]))
    return "---\n%s\n---\n\n{%% raw %%}\n%s\n{%% endraw %%}\n" % (
        "\n".join(matter), _liquid_safe(body.strip("\n")))


def _tagline(readme: str) -> str:
    match = re.search(r"^\*\*(.+?)\*\*", readme, re.M)
    if not match:
        raise SiteError("README.md has no bold tagline line to use as the site description")
    return match.group(1).strip()


def _config(description: str) -> str:
    lines = [
        "title: estate-atlas",
        "description: " + _scalar(description),
        "url: https://chris0jeky.github.io",
        "baseurl: /estate-atlas",
        "plugins:",
    ]
    lines += ["  - " + plugin for plugin in PLUGINS]
    # The plugin is on by default on GitHub Pages: leaving it out of ``plugins`` would not stop it.
    lines += ["relative_links:", "  enabled: false", ""]
    return "\n".join(lines)


def _run_cli(*argv: str) -> None:
    from estate_atlas import cli
    code = cli.main(list(argv))
    if code != 0:
        raise SiteError("estate-atlas %s exited %s" % (" ".join(argv[:2]), code))


def _example(root: Path, out: Path) -> str:
    """Render the fictional shop atlas into ``out`` and return the Markdown of the example page."""
    atlas = root / EXAMPLE_ATLAS
    if not atlas.is_file():
        raise SiteError("allowlisted file missing: %s" % EXAMPLE_ATLAS)
    (out / EXAMPLE_HTML).parent.mkdir(parents=True, exist_ok=True)
    _run_cli("render", "html", str(atlas), "--out", str(out / EXAMPLE_HTML))
    with tempfile.TemporaryDirectory() as tmp:
        tour = Path(tmp) / "tour.md"
        _run_cli("tour", str(atlas), "--md", "--out", str(tour))
        tour_md = _demote_headings(_text(tour))
    return "\n".join([
        "# Example atlas: a fictional shop",
        "",
        "This is the real output of estate-atlas on the fictional shop in `examples/shop`. Nothing here "
        "describes a deployment of anything.",
        "",
        "- [Open the interactive atlas](%s), an offline HTML page." % EXAMPLE_HTML,
        "- [The atlas file](%sexamples/shop/atlas.json) it is drawn from." % REPO_BLOB,
        "",
        tour_md,
    ])


def _is_stage_marker(marker: Path) -> bool:
    """True when ``marker`` is a regular file (not a link) holding exactly the text this script writes."""
    try:
        info = os.lstat(marker)
        if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & REPARSE_POINT:
            return False
        with open(marker, "rb") as handle:
            return handle.read(len(MARKER_TEXT) + 1) == MARKER_TEXT.encode("utf-8")
    except OSError:
        return False


def _prepare_out(root: Path, out: Path) -> Path:
    """Validate ``out`` and leave an empty folder; refuse anything that is not clearly ours to clear."""
    root, out = root.resolve(), out.resolve()
    if out == root or root not in out.parents:
        raise SiteError("--out must be a folder strictly inside the repository root (%s)" % out)
    if ".git" in out.relative_to(root).parts:
        raise SiteError("--out must not be inside .git (%s)" % out)
    if out.exists():
        if not out.is_dir():
            raise SiteError("--out exists and is not a folder: %s" % out)
        if any(out.iterdir()):
            if not _is_stage_marker(out / MARKER):
                raise SiteError("--out is a non-empty folder this script did not create; "
                                "refusing to clear it: %s" % out)
            shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    return out


def _normalise_modes(out: Path) -> None:
    """Make every staged folder 0755 and file 0644: the renderer writes through a 0600 temp file, and Jekyll
    copies static files with the mode they have, which the Pages artifact step then cannot read."""
    os.chmod(out, 0o755)
    for path in sorted(out.rglob("*")):
        os.chmod(path, 0o755 if path.is_dir() else 0o644)


def build(root: Path, out: Path) -> None:
    """Stage the site from the repository at ``root`` into ``out`` (replaced)."""
    root = Path(root)
    staged = dict(PAGES)
    staged[LICENSE[0]] = LICENSE[1]
    # Check and read every input before touching ``out``, so a failure leaves nothing half built.
    theme = [posixpath.join(THEME_DIR, name) for name in THEME_FILES]
    for rel in [src for src, _ in PAGES] + [LICENSE[0], EXAMPLE_ATLAS] + theme:
        _check_input(root, rel)
    theme_files = {name: _text(root / THEME_DIR / name) for name in THEME_FILES}
    sources = {src: _text(root / src) for src, _ in PAGES}
    license_text = _text(root / LICENSE[0])
    description = _tagline(sources["README.md"])
    out = _prepare_out(root, Path(out))
    try:
        _write(out / MARKER, MARKER_TEXT)
        _write(out / "_config.yml", _config(description))
        _write(out / NAV_DATA, _nav_data(description))
        for name, text in theme_files.items():
            _write(out / name, text)
        for src, dest in PAGES:
            body = rewrite_links(sources[src], src, staged)
            _write(out / dest, _page(dest, _first_heading(body, dest), body))
        _write(out / LICENSE[1], license_text)
        example = _example(root, out)
        _write(out / EXAMPLE_PAGE, _page(EXAMPLE_PAGE, _first_heading(example, "Example atlas"), example))
        _normalise_modes(out)
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage the estate-atlas GitHub Pages docs site.")
    parser.add_argument("--out", default="_site_src", help="folder to (re)create inside the repository root")
    parser.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = Path(args.root)
    out = Path(args.out)
    if not out.is_absolute():
        out = root / out
    try:
        build(root, out)
    except SiteError as err:
        sys.stderr.write("build_docs_site: %s\n" % err)
        return 1
    sys.stdout.write("staged the docs site in %s\n" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
