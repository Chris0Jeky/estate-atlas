"""Local go-public scan: does anything tracked in this repository look private?

Standard library only. Read-only against git (``ls-files``, ``log``, ``for-each-ref``); it never fetches, writes or
checks out anything.

    python scripts/check_public.py                  scan tracked text files and every tracked file name
    python scripts/check_public.py --history        scan ``git log -p --all`` text and ref names instead
    python scripts/check_public.py --terms FILE     read the private terms from FILE, not .public-scan.local

Two kinds of check run together:

* Generic patterns, committed here because they name nothing private: absolute user-home paths, default Windows host
  names, e-mail addresses (examples and no-reply forms are allowed) and ``.ts.net`` tailnet hosts.
* Private terms, one literal term per line (``#`` comments and blank lines ignored), read from a gitignored local file,
  by default ``.public-scan.local`` at the repository root. They are matched like the overlay leak gate matches words:
  folded (NFKC, casefold, whitespace collapsed), between ASCII-alphanumeric boundaries, so ``-`` and ``_`` end a
  token. A term of several words also matches with ``-``, ``_`` or nothing between them. One trailing letter or
  digit is tolerated (plurals, numbered forms) except on a term shorter than five characters, which matches as an
  exact word only, so a short private name does not flood the report with ordinary words that begin with it.
  A line ``allow: PATH = TERM`` in the same file accepts that term in that one file (and its name): a reviewed
  judgement kept next to the private list; the count of accepted hits is always printed. If the file is absent the
  script says so on stderr and still runs the generic patterns.

A hit prints ``path:line: <category>`` (a private term prints ``private-term #N``, N counting the terms in the file,
and no excerpt, so the report never repeats a private word). A ``user@host`` inside a URL (``https://user@host``) is
credential-style userinfo, not an address, and ``git@host`` is the ssh account name; neither counts as an e-mail. Exit 0 when clean, 1 on any hit, 2 on a usage error.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from estate_atlas.overlay import fold, leaks  # noqa: E402

DEFAULT_TERMS = ".public-scan.local"
SEPARATOR = re.compile(r"[\s_-]+")
SHORT_TERM = 5  # folded terms shorter than this match as exact words only

# Names after a home directory that are placeholders or shared system accounts, not a person.
PLACEHOLDER_NAMES = {"user", "username", "you", "me", "name", "yourname", "example", "public", "default", "runner",
                     "runneradmin", "someone", "foo", "test"}
ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "users.noreply.github.com", "localhost")
ALLOWED_EMAIL_SUFFIXES = (".example.com", ".example.org", ".example.net", ".invalid", ".test", ".localhost")

_NOT_PLACEHOLDER_START = r"(?![<$%{(\[*~])"
# The full account component: a Windows one may hold spaces, a Unix one may not; Unicode letters are fine.
WIN_ACCOUNT = _NOT_PLACEHOLDER_START + r"([^\\/\r\n\"'`<>|:*?]+)"
UNIX_ACCOUNT = _NOT_PLACEHOLDER_START + r"([^\\/\s\"'`<>|:*?,;)\]]+)"
HOME_PATTERNS = (
    re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]+Users[\\/]+" + WIN_ACCOUNT, re.IGNORECASE),
    re.compile(r"(?<![\w.:/\\~-])/home/" + UNIX_ACCOUNT),
    re.compile(r"(?<![\w.:/\\~-])/Users/" + UNIX_ACCOUNT),
)
HOST_PATTERN = re.compile(r"(?<![A-Za-z0-9])(?:DESKTOP|LAPTOP)-[A-Z0-9]{7}(?![A-Za-z0-9])", re.IGNORECASE)
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
TAILNET_PATTERN = re.compile(r"(?<![A-Za-z0-9-])[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.ts\.net(?![A-Za-z0-9-])",
                             re.IGNORECASE)


class Usage(Exception):
    pass


def mask(text: str) -> str:
    return text[:2] + "***"


def generic_hits(text: str) -> list[tuple[str, str]]:
    """``(category, masked excerpt)`` for each generic private-looking pattern in one line, in a fixed order."""
    hits: list[tuple[str, str]] = []
    for pattern in HOME_PATTERNS:
        for match in pattern.finditer(text):
            if match.group(1).strip().lower().rstrip(".") not in PLACEHOLDER_NAMES:
                hits.append(("home-path", mask(match.group(0))))
                break
    for match in HOST_PATTERN.finditer(text):
        hits.append(("windows-host", mask(match.group(0))))
        break
    # The address pattern backtracks per start position; a long line with no "@" never matches.
    if "@" in text:
        for match in EMAIL_PATTERN.finditer(text):
            local, domain = match.group(0).split("@", 1)[0], match.group(1).lower()
            if text[:match.start()].endswith("://"):
                continue  # URL userinfo
            if local == "git" or (domain in ALLOWED_EMAIL_DOMAINS or domain.endswith(ALLOWED_EMAIL_SUFFIXES)
                    or local.lower().startswith(("noreply", "no-reply"))):
                continue
            hits.append(("email", mask(match.group(0))))
            break
    for match in TAILNET_PATTERN.finditer(text):
        hits.append(("tailnet-host", mask(match.group(0))))
        break
    return hits


class Terms:
    """The private terms, their folded variants, the accepted (path, term) pairs, and the line matcher."""

    def __init__(self, terms: list[str], allowed: list[tuple[str, str]] = ()):
        self.count = len(terms)
        self.variants: dict[str, int] = {}
        for index, term in enumerate(terms, 1):
            parts = [part for part in SEPARATOR.split(fold(term)) if part]
            if not parts:
                continue
            self.variants.setdefault(fold(term), index)  # the term itself, literally
            for joiner in (" ", "-", "_", ""):
                self.variants.setdefault(joiner.join(parts), index)
        by_term = {fold(term): index for index, term in enumerate(terms, 1)}
        self.allowed = {(path, by_term[fold(term)]) for path, term in allowed if fold(term) in by_term}
        self.accepted = 0

    def indexes(self, text: str) -> list[int]:
        """The 1-based term numbers that occur in ``text``, ascending."""
        if not self.variants:
            return []
        folded = fold(text)
        candidates = [variant for variant in self.variants if variant in folded]
        if not candidates:
            return []
        loose = [v for v in candidates if len(v) >= SHORT_TERM]
        found = set(leaks(text, (), loose)) if loose else set()
        for variant in candidates:
            if len(variant) < SHORT_TERM and re.search(
                    r"(?<![a-z0-9])" + re.escape(variant) + r"(?![a-z0-9])", folded):
                found.add(variant)
        return sorted({self.variants[variant] for variant in found})

    def accepts(self, path: str, finding: str) -> bool:
        """True (and counted) when ``finding`` is a private-term hit that the local file accepts for ``path``."""
        match = re.fullmatch(r"private-term #(\d+)", finding)
        if match and (path, int(match.group(1))) in self.allowed:
            self.accepted += 1
            return True
        return False


def load_terms(path: Path, explicit: bool) -> Terms:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        if not explicit and isinstance(exc, FileNotFoundError):
            print(f"check_public: no {path.name} at {path.parent}: running the generic patterns only "
                  f"(put one private term per line there to scan for them too)", file=sys.stderr)
            return Terms([])
        raise Usage(f"cannot read the terms file {path}: {exc.strerror or type(exc).__name__}") from None
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise Usage(f"the terms file {path} is not UTF-8") from None
    terms: list[str] = []
    allowed: list[tuple[str, str]] = []
    for line in (line.strip() for line in text.splitlines()):
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("allow:"):
            path, _, term = line[len("allow:"):].partition("=")
            if path.strip() and term.strip():
                allowed.append((path.strip(), term.strip()))
            continue
        terms.append(line)
    return Terms(terms, allowed)


def git(repo: Path, *args: str) -> bytes:
    try:
        done = subprocess.run(["git", "-c", "core.quotepath=off", "-c", "core.fsmonitor=false", "-C", str(repo),
                               *args], capture_output=True)
    except OSError as exc:
        raise Usage(f"cannot run git: {exc.strerror or type(exc).__name__}") from None
    if done.returncode != 0:
        raise Usage(f"git {args[0]} failed in {repo}: {done.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return done.stdout


def findings(text: str, terms: Terms) -> list[str]:
    """The categories (with masked excerpts for generic ones) found in one line of text."""
    out = [f"{category}  [{excerpt}]" for category, excerpt in generic_hits(text)]
    out += [f"private-term #{index}" for index in terms.indexes(text)]
    return out


def shown(name: str, terms: Terms) -> str:
    """``name`` as it may be printed: a name (path or ref) that itself hits becomes a hash label, no source text."""
    if not findings(name, terms):
        return name
    return "<masked:#" + hashlib.sha1(name.encode("utf-8", "replace")).hexdigest()[:6] + ">"


def scan_text(text: str, terms: Terms):
    """``(line number, finding)`` for every hit in a block of text."""
    for number, line in enumerate(text.split("\n"), 1):
        for finding in findings(line, terms):
            yield number, finding


def decode_text(raw: bytes) -> str | None:
    """The text in one tracked file, or None when the file is binary.

    A leading BOM selects UTF-32 or UTF-16; both codecs consume the mark. UTF-32 LE has to be
    decided before UTF-16, because its mark begins with the UTF-16 LE bytes. Without a BOM, a NUL
    in the first 8192 bytes means binary. Everything else stays UTF-8, undecodable bytes replaced.
    """
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return raw.decode("utf-32", "replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    if b"\0" in raw[:8192]:
        return None
    return raw.decode("utf-8", "replace")


def scan_tree(repo: Path, terms: Terms) -> int:
    names = [n for n in git(repo, "ls-files", "-z").decode("utf-8", "replace").split("\0") if n]
    hits = 0
    for name in names:
        label = shown(name, terms)
        for finding in findings(name, terms):
            if terms.accepts(name, finding):
                continue
            print(f"{label}:0: {finding} (file name)")
            hits += 1
        target = repo / name
        if target.is_dir():
            continue  # a submodule or other directory-like entry
        try:
            raw = target.read_bytes()
        except FileNotFoundError:
            continue  # tracked but deleted from the working tree
        except OSError as exc:
            raise Usage(f"cannot read tracked file {label}: {exc.strerror or type(exc).__name__}") from None
        text = decode_text(raw)
        if text is None:
            continue  # binary
        for number, finding in scan_text(text, terms):
            if terms.accepts(name, finding):
                continue
            print(f"{label}:{number}: {finding}")
            hits += 1
    if hits:
        print(f"{hits} hit(s) in {len(names)} files", file=sys.stderr)
        return 1
    print(f"clean: {len(names)} files" + (f" ({terms.accepted} hit(s) accepted by allow lines)"
                                         if terms.accepted else ""))
    return 0


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def diff_header_paths(rest: str) -> list[str]:
    """The path(s) named by the text after ``diff --git``: one when both sides agree, else both."""
    if not rest.startswith("a/"):
        return []  # quoted names only appear for unusual bytes
    marks = [m.start() for m in re.finditer(" b/", rest)]
    for mark in marks:
        left, right = rest[2:mark], rest[mark + 3:]
        if left == right:
            return [left]
    if marks:
        return [rest[2:marks[0]], rest[marks[0] + 3:]]
    return []


def history_records(log_text: str, terms: Terms):
    """``(sha, path, line, finding)`` for each hit in a ``git log -p`` stream made with this script's format.

    ``line`` is 0 for a hit in a file name. Hunk payload is told from file headers by the line counts of the ``@@``
    header, never by its first characters, so a payload line such as ``+++ x`` is scanned as content.
    """
    sha = ""
    state = "outside"
    header_line = 0
    path = ""
    old_line = new_line = old_left = new_left = 0
    for line in log_text.split("\n"):
        if old_left > 0 or new_left > 0:
            if line.startswith("\\"):
                continue  # "\ No newline at end of file"
            if line[:1] == "+" and new_left > 0:
                new_left -= 1
                for finding in findings(line[1:], terms):
                    yield sha, path, new_line, finding
                new_line += 1
                continue
            if line[:1] == "-" and old_left > 0:
                old_left -= 1
                for finding in findings(line[1:], terms):
                    yield sha, path, old_line, finding
                old_line += 1
                continue
            if line[:1] == " " and old_left > 0 and new_left > 0:
                old_left -= 1  # context line (git can emit them despite -U0)
                new_left -= 1
                old_line += 1
                new_line += 1
                continue
            old_left = new_left = 0  # malformed or short hunk: treat the line as a header again
        if line.startswith("\x01"):
            sha, state, header_line = line[1:].strip(), "header", 0
            continue
        if state == "header":
            if line.startswith("\x02"):
                state = "diff"
                continue
            header_line += 1
            where = "(author)" if header_line <= 2 else "(commit message)"
            for finding in findings(line, terms):
                yield sha, where, header_line, finding
            continue
        if state != "diff":
            continue
        if line.startswith("diff --git "):
            named = diff_header_paths(line[len("diff --git "):])
            if named:
                path = named[-1]
                for name in named:
                    for finding in findings(name, terms):
                        yield sha, name, 0, finding
        elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
            name = line.split(" ", 2)[2]
            for finding in findings(name, terms):
                yield sha, name, 0, finding
        elif line.startswith("@@"):
            match = HUNK.match(line)
            if match:
                old_line, new_line = int(match.group(1)), int(match.group(3))
                old_left = int(match.group(2)) if match.group(2) is not None else 1
                new_left = int(match.group(4)) if match.group(4) is not None else 1


def scan_history(repo: Path, terms: Terms) -> int:
    refs = git(repo, "for-each-ref", "--format=%(refname)").decode("utf-8", "replace").splitlines()
    log = git(repo, "log", "-p", "--all", "-U0", "--no-color", "--no-ext-diff", "--no-textconv",
              "--src-prefix=a/", "--dst-prefix=b/", "--inter-hunk-context=0", "--no-renames",
              "--format=%x01%H%n%an <%ae>%n%cn <%ce>%n%B%x02").decode("utf-8", "replace")
    hits = 0
    for ref in refs:
        for finding in findings(ref, terms):
            print(f"{shown(ref, terms)}:0: {finding} (ref name)")
            hits += 1
    seen: set[tuple[str, str, str, bool]] = set()
    per_category: Counter[str] = Counter()
    commits_hit: set[str] = set()
    commits = log.count("\x01")
    for sha, path, number, finding in history_records(log, terms):
        category = finding.split("  [")[0]
        key = (sha, path, category, number == 0)
        if key in seen:
            continue
        seen.add(key)
        print(f"{sha[:12]} {shown(path, terms)}:{number}: {finding}" + (" (file name)" if number == 0 else ""))
        hits += 1
        commits_hit.add(sha)
    for category in sorted({key[2] for key in seen}):
        per_category[category] = len({key[0] for key in seen if key[2] == category})
    if hits:
        print(f"history: {hits} hit(s) in {len(commits_hit)} of {commits} commit(s), {len(refs)} refs checked")
        for category, count in sorted(per_category.items()):
            print(f"history: {category}: {count} commit(s)")
        return 1
    print(f"clean: {commits} commits, {len(refs)} refs")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check_public.py", description="Scan tracked files (or history) for "
                                     "private-looking content before going public.")
    parser.add_argument("--history", action="store_true", help="scan git log -p --all and ref names, not the tree")
    parser.add_argument("--terms", metavar="FILE", help=f"private terms file (default: {DEFAULT_TERMS} at the root)")
    parser.add_argument("--repo", metavar="DIR", default=".", help="the repository to scan (default: the current one)")
    args = parser.parse_args(argv)  # an unknown option exits 2 here
    try:
        top = git(Path(args.repo), "rev-parse", "--show-toplevel").decode("utf-8", "replace").strip()
        repo = Path(top)
        terms = load_terms(Path(args.terms) if args.terms else repo / DEFAULT_TERMS, explicit=bool(args.terms))
        return scan_history(repo, terms) if args.history else scan_tree(repo, terms)
    except Usage as exc:
        print(f"check_public: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
