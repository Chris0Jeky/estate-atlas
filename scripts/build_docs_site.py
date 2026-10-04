"""Stage the GitHub Pages docs site for estate-atlas.

Standard library only, deterministic, read-only against everything except ``--out``.

    python scripts/build_docs_site.py [--out _site_src]

Copies an explicit allowlist of existing docs into a folder that GitHub's own Jekyll
(``actions/jekyll-build-pages``) turns into a site: ``README.md`` becomes ``index.md``, three design
docs keep their ``docs/`` paths, ``LICENSE`` becomes ``LICENSE.txt``. It adds a navigation line and a
title to every page, points links to files that are not published at the file on GitHub, and renders
the fictional ``examples/shop`` atlas with the package's own HTML renderer and tour, so the demo is
the real output. A missing allowlisted file is an error (exit 1), never a silently thinner site.

``--out`` must lie strictly inside the repository root and is replaced wholesale, so it must be absent,
empty or a folder this script made (it carries a ``.docs-site-stage`` marker); anything else is refused.
"""
from __future__ import annotations

import argparse
import json
import os
import posixpath
import re
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REPO_BLOB = "https://github.com/Chris0Jeky/estate-atlas/blob/main/"
MARKER = ".docs-site-stage"
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
PLUGINS = ("jekyll-relative-links", "jekyll-optional-front-matter",
           "jekyll-titles-from-headings", "jekyll-default-layout")

LINK = re.compile(r'\[([^\]]*)\]\(([^)\s]+)((?:\s+"[^"]*")?)\)')
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
CODE_SPAN = re.compile(r"(`+[^`\n]*`+)")
FENCE = re.compile(r"^\s*(```|~~~)")
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


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _scalar(value: str) -> str:
    return value if PLAIN_YAML.match(value) else json.dumps(value, ensure_ascii=False)


def _lines_with_fence_state(text: str):
    """Yield (line, in_fence) for each line; the fence delimiter lines themselves count as fenced."""
    fence = None
    for line in text.split("\n"):
        match = FENCE.match(line)
        if fence is None:
            if match:
                fence = match.group(1)
                yield line, True
                continue
            yield line, False
        else:
            if match and match.group(1) == fence:
                fence = None
            yield line, True


def rewrite_links(text: str, source: str, staged: dict[str, str]) -> str:
    """Point every Markdown link at a published page or, failing that, at the file on GitHub.

    ``source`` is the repository path of the page, ``staged`` maps published repository paths to staged
    paths. External links, anchors, absolute paths and code (fenced or inline) are left alone.
    """
    base = posixpath.dirname(source)
    here = posixpath.dirname(staged[source])

    def fix(match: re.Match) -> str:
        label, target, title = match.groups()
        split = re.split(r"([#?])", target, maxsplit=1)
        path, suffix = split[0], "".join(split[1:])
        if not path or SCHEME.match(path) or path.startswith("/"):
            return match.group(0)
        resolved = posixpath.normpath(posixpath.join(base, path))
        if resolved in (".", "..") or resolved.startswith("../"):
            return match.group(0)
        if resolved in staged:
            new = posixpath.relpath(staged[resolved], here or ".")
        else:
            new = REPO_BLOB + quote(resolved, safe="/") + ("/" if path.endswith("/") else "")
        return "[%s](%s%s%s)" % (label, new, suffix, title)

    out = []
    for line, fenced in _lines_with_fence_state(text):
        if fenced or "](" not in line:
            out.append(line)
            continue
        parts = CODE_SPAN.split(line)
        out.append("".join(p if i % 2 else LINK.sub(fix, p) for i, p in enumerate(parts)))
    return "\n".join(out)


def _first_heading(text: str, fallback: str) -> str:
    for line, fenced in _lines_with_fence_state(text):
        if not fenced and line.startswith("# "):
            return line[2:].strip()
    return fallback


def _demote_headings(text: str) -> str:
    return "\n".join(line if fenced or not line.startswith("#") else "#" + line
                     for line, fenced in _lines_with_fence_state(text))


def _nav(page: str) -> str:
    here = posixpath.dirname(page) or "."
    return " · ".join("[%s](%s)" % (label, posixpath.relpath(target, here)) for label, target in NAV)


def _page(dest: str, title: str, body: str) -> str:
    """Front matter, navigation and the body, which Liquid must not interpret."""
    return "---\ntitle: %s\n---\n\n%s\n\n{%% raw %%}\n%s\n{%% endraw %%}\n" % (
        json.dumps(title, ensure_ascii=False), _nav(dest), body.strip("\n"))


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
        "theme: jekyll-theme-primer",
        "plugins:",
    ]
    lines += ["  - " + plugin for plugin in PLUGINS]
    lines += ["relative_links:", "  enabled: true", "  collections: false", ""]
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
            if not (out / MARKER).is_file():
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
    # Read and check every input before touching ``out``, so a failure leaves nothing half built.
    sources = {src: _text(root / src) for src, _ in PAGES}
    license_text = _text(root / LICENSE[0])
    description = _tagline(sources["README.md"])
    if not (root / EXAMPLE_ATLAS).is_file():
        raise SiteError("allowlisted file missing: %s" % EXAMPLE_ATLAS)
    out = _prepare_out(root, Path(out))
    try:
        _write(out / MARKER, "staged by scripts/build_docs_site.py\n")
        _write(out / "_config.yml", _config(description))
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
