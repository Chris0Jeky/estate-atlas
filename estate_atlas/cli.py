"""The command line: `python -m estate_atlas <verb>`.

Exit codes: 0 ok, 1 drift or stale, 2 invalid input. Every verb imports the modules it needs when it runs, so a
verb never depends on the modules of the others.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import platform
import sys
import tempfile
from pathlib import Path
from typing import Any

OK, DRIFT, INVALID = 0, 1, 2


class UsageError(Exception):
    """Invalid input: reported on stderr, exit code 2."""


# ------------------------------------------------------------------------------------------ helpers

def _out(text: str) -> None:
    if not text.endswith("\n"):
        text += "\n"
    sys.stdout.write(text)


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _write_file(path: str | Path, text: str) -> None:
    """Write text atomically (a temporary file beside the target, then a rename)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not text.endswith("\n"):
        text += "\n"
    fd, temp = tempfile.mkstemp(dir=str(target.parent), prefix="." + target.name + ".", suffix=".tmp")
    try:
        with open(fd, "wb") as stream:
            stream.write(text.encode("utf-8"))
        Path(temp).replace(target)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _emit(text: str, out: str | None) -> None:
    if out:
        _write_file(out, text)
    else:
        _out(text)


def _load(path: str) -> dict[str, Any]:
    """The atlas as plain JSON, validated."""
    from . import model
    doc = model.load(path)
    model.validate(doc)
    return doc


def _parsed(path: str) -> dict[str, Any]:
    """The atlas as the renderers and explainers want it (validated, then normalised)."""
    from . import render
    _load(path)
    return render.parse_atlas(render.read_json_file(Path(path)))


def _json_file(path: str, what: str) -> Any:
    from . import render
    try:
        return render.read_json_file(Path(path))
    except Exception as exc:
        raise UsageError("cannot load %s: %s" % (what, exc)) from None


def _traffic_doc(path: str | None) -> dict[str, Any] | None:
    if path is None:
        return None
    doc = _json_file(path, "--traffic")
    if not isinstance(doc, dict) or doc.get("schema") != "estate-atlas-traffic@1":
        raise UsageError("--traffic is not an estate-atlas-traffic@1 document")
    if "crosschecks" in doc:
        cross = doc["crosschecks"]
        if not isinstance(cross, dict):
            raise UsageError("--traffic crosschecks must be an object")
        if "off_status" in cross and not isinstance(cross["off_status"], list):
            raise UsageError("--traffic crosschecks.off_status must be a list")
    return doc


def _overrides(entries: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for entry in entries or []:
        name, sep, path = entry.partition("=")
        if not sep or not name or not path:
            raise UsageError("--repo wants name=path, got %r" % (entry,))
        out[name] = path
    return out


def _git_scope(args: argparse.Namespace) -> tuple[Any, dict[str, Any], str]:
    """(atlas doc, repositories, host) for the verbs that read evidence."""
    from . import check
    doc = _load(args.atlas)
    host = args.host or platform.node()
    return doc, check.repositories(doc, _overrides(args.repo), host,
                                   worktree=bool(getattr(args, "worktree", False))), host


def _run_check(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    from . import check
    doc, repos, host = _git_scope(args)
    report = check.check(doc, repos, host)
    return doc, repos, report


# ------------------------------------------------------------------------------------------ verbs

def cmd_validate(args: argparse.Namespace) -> int:
    doc = _load(args.atlas)
    _out("ok: %d components, %d contracts, %d flows" % (
        len(doc["components"]), len(doc["contracts"]), len(doc["flows"])))
    return OK


def cmd_check(args: argparse.Namespace) -> int:
    from . import check
    _, _, report = _run_check(args)
    _out(_dump(report) if args.json else check._summary(report))
    return DRIFT if report["status"] == "drift" else OK


def cmd_export(args: argparse.Namespace) -> int:
    from . import check
    doc, repos, host = _git_scope(args)
    report = check.check(doc, repos, host) if args.check else None
    _emit(_dump(check.export(doc, repos, report)), args.out)
    return DRIFT if report is not None and report["status"] == "drift" else OK


def cmd_render(args: argparse.Namespace) -> int:
    from . import render
    atlas = _parsed(args.atlas)
    if args.kind == "html":
        text = render.render_html(atlas)
    else:
        text = render.render_mermaid(atlas, args.view)
    _emit(text, args.out)
    return OK


def cmd_docs(args: argparse.Namespace) -> int:
    from . import docs
    traffic = Path(args.traffic) if args.traffic else None
    svg = Path(args.svg) if args.svg else None
    if args.action == "write":
        code, message = docs.write(Path(args.atlas), Path(args.doc), traffic, svg)
    else:
        code, message = docs.check_docs(Path(args.atlas), Path(args.doc), svg)
    if code == OK:
        _out(message)
    else:
        sys.stderr.write(message + "\n")
    return code


def _explain_text(doc: dict[str, Any]) -> str:
    lines = ["%s (%s)" % (doc["title"], doc["id"])]
    lines.extend(line["text"] for line in doc["lines"])
    return "\n".join(lines)


def cmd_explain(args: argparse.Namespace) -> int:
    from . import explain
    atlas = _parsed(args.atlas)
    traffic = _traffic_doc(args.traffic)
    live = None
    if args.live:
        live = _json_file(args.live, "--live")
        if isinstance(live, dict) and isinstance(live.get("live"), dict) and "schema" in live:
            live = live["live"]
        if not isinstance(live, dict):
            raise UsageError("--live must hold a JSON object")
    try:
        doc = explain.explain(atlas, args.id, traffic=traffic, live=live)
    except KeyError:
        raise UsageError("unknown component %r" % (args.id,)) from None
    _out(_dump(doc) if args.json else _explain_text(doc))
    return OK


def _public_tour(args: argparse.Namespace, atlas: dict[str, Any], traffic: dict[str, Any] | None) -> str:
    """The tour of the atlas as the overlay words it; UsageError (exit 2) when anything private would leak."""
    from . import explain, overlay
    if args.check:
        raise UsageError("--overlay cannot be combined with --check")
    spec = overlay.load_overlay(args.overlay)
    overlay.validate_overlay(spec, atlas)
    public = overlay.apply_overlay(atlas, spec)
    public_traffic = overlay.apply_overlay_traffic(traffic, spec) if traffic is not None else None
    if args.md:
        text = explain.render_tour_md(public, public_traffic)
        if "title" in spec:
            text = "# A tour of " + explain._esc(spec["title"]) + text[text.index("\n"):]
    else:
        text = _dump(explain.tour(public, public_traffic))
    found = overlay.find_leaks(atlas, spec, public, text, markdown=bool(args.md))
    if found:
        where: dict[str, list[str]] = {}
        for term, field in found:
            where.setdefault(term, []).append(field)
        raise UsageError("the public tour leaks private terms: %s" % "; ".join(
            "%r in %s" % (term, ", ".join(fields)) for term, fields in where.items()))
    return text


def cmd_tour(args: argparse.Namespace) -> int:
    from . import explain
    atlas = _parsed(args.atlas)
    # `is not None`, not truthiness: `--overlay ""` (an unset variable in a publish script) must reach
    # load_overlay and fail, never fall through to the private tour (review of #17)
    if args.overlay is not None:
        _emit(_public_tour(args, atlas, _traffic_doc(args.traffic)), args.out)
        return OK
    if args.check:
        ok, message = explain.check_tour(Path(args.check), atlas)
        if ok:
            _out(message)
            return OK
        sys.stderr.write(message + "\n")
        return DRIFT
    traffic = _traffic_doc(args.traffic)
    if args.md:
        _emit(explain.render_tour_md(atlas, traffic), args.out)
    else:
        _emit(_dump(explain.tour(atlas, traffic)), args.out)
    return OK


def cmd_route(args: argparse.Namespace) -> int:
    import time
    from . import traffic
    if not (args.journal or args.events or args.links):
        raise UsageError("give at least one of --journal, --events, --links")
    now: float | None = None
    if args.now == "wall":
        now = time.time()
    elif args.now is not None:
        try:
            now = float(args.now)
        except ValueError:
            raise UsageError("--now wants epoch seconds or 'wall', got %r" % (args.now,)) from None
    doc = _load(args.atlas)
    try:
        snapshot = traffic.route_files(doc, journal=args.journal, events=args.events,
                                       links=args.links, now=now)
    except ValueError as exc:
        raise UsageError(str(exc)) from None
    snapshot.pop("timing", None)  # wall-clock milliseconds: the library keeps them, the CLI output is byte-stable
    _emit(_dump(snapshot), args.out)
    return OK


# ------------------------------------------------------------------------------------------ parser

def _evidence_options(sub: argparse.ArgumentParser) -> None:
    sub.add_argument("--repo", action="append", metavar="NAME=PATH",
                     help="local checkout for a repo of the atlas (repeatable)")
    sub.add_argument("--host", help="host name used to pick repos[name].paths (default: this machine)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="estate-atlas",
        description="Architecture as checked data. Exit codes: 0 ok, 1 drift or stale, 2 invalid input.")
    verbs = parser.add_subparsers(dest="verb", required=True, metavar="verb")

    p = verbs.add_parser("validate", help="check that the atlas file is well formed")
    p.add_argument("atlas")
    p.set_defaults(run=cmd_validate)

    p = verbs.add_parser("check", help="prove the evidence against git")
    p.add_argument("atlas")
    _evidence_options(p)
    p.add_argument("--worktree", action="store_true",
                   help="read the checkouts' working files instead of origin/<default branch>")
    p.add_argument("--json", action="store_true", help="print the report as JSON")
    p.set_defaults(run=cmd_check)

    p = verbs.add_parser("export", help="the atlas with a hosted link on every reference")
    p.add_argument("atlas")
    _evidence_options(p)
    p.add_argument("--check", action="store_true", help="embed the check report")
    p.add_argument("--out", help="write here instead of standard output")
    p.set_defaults(run=cmd_export)

    p = verbs.add_parser("render", help="render the atlas as html or mermaid")
    p.add_argument("kind", choices=("html", "mermaid"))
    p.add_argument("atlas")
    p.add_argument("--view", choices=("flows", "components"), default="flows", help="mermaid view")
    p.add_argument("--out", help="write here instead of standard output")
    p.set_defaults(run=cmd_render)

    p = verbs.add_parser("docs", help="write or check the generated blocks of a Markdown doc")
    p.add_argument("action", choices=("write", "check"))
    p.add_argument("atlas")
    p.add_argument("--doc", required=True, help="the Markdown file that holds the marked blocks")
    p.add_argument("--traffic", help="an estate-atlas-traffic@1 file for the traffic block (write only)")
    p.add_argument("--svg", help="the overview SVG (default: atlas-glance.svg beside the doc)")
    p.set_defaults(run=cmd_docs)

    p = verbs.add_parser("explain", help="explain one component in plain English")
    p.add_argument("atlas")
    p.add_argument("id", help="a component id")
    p.add_argument("--traffic", help="an estate-atlas-traffic@1 file")
    p.add_argument("--live", help="a JSON file of live facts")
    p.add_argument("--json", action="store_true", help="print the estate-atlas-explain@1 document")
    p.set_defaults(run=cmd_explain)

    p = verbs.add_parser("tour", help="a walk through every layer and flow")
    p.add_argument("atlas")
    p.add_argument("--md", action="store_true", help="Markdown instead of JSON")
    p.add_argument("--traffic", help="an estate-atlas-traffic@1 file")
    p.add_argument("--check", metavar="DOC", help="exit 1 when this tour doc is stale")
    p.add_argument("--overlay", metavar="FILE",
                   help="an estate-atlas-overlay@1 file: publish the tour in public wording, and exit 2 "
                        "(printing nothing) when any private term leaks")
    p.add_argument("--out", help="write here instead of standard output")
    p.set_defaults(run=cmd_tour)

    p = verbs.add_parser("route", help="route record files onto the flows (prints estate-atlas-traffic@1)")
    p.add_argument("atlas")
    p.add_argument("--journal", help="JSONL of {id?, at, producer, verb, actor, subject}")
    p.add_argument("--events", help="JSONL of {id?, at, kind, source, nodes}")
    p.add_argument("--links", help="a JSON list of {source, from, rel, to, at?}")
    p.add_argument("--now", metavar="EPOCH|wall",
                   help="the instant to route at (default: the newest record, so a captured file always gives "
                        "the same answer, or epoch 0 when no record carries a time; 'wall' is the clock)")
    p.add_argument("--out", help="write here instead of standard output")
    p.set_defaults(run=cmd_route)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # Argparse can echo unknown arguments before an atlas or overlay has been loaded.
    # Include its accepted option abbreviations and --overlay=FILE syntax in this guard.
    public_tour = "tour" in argv and any(
        arg.startswith("--") and "--overlay".startswith(arg.split("=", 1)[0])
        for arg in argv if arg != "--")

    def report_error(exc: Exception | None = None) -> None:
        if public_tour:
            sys.stderr.write("estate-atlas: public tour refused; inspect the inputs privately.\n")
        else:
            sys.stderr.write("estate-atlas: %s\n" % (exc,))

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(Exception):
                reconfigure(encoding="utf-8")
    try:
        with contextlib.redirect_stderr(io.StringIO()) if public_tour else contextlib.nullcontext():
            args = build_parser().parse_args(argv)
    except SystemExit as exc:  # argparse: --help is 0, a usage error is 2
        if public_tour and exc.code:
            report_error()
        return exc.code if isinstance(exc.code, int) else INVALID
    public_tour = args.verb == "tour" and args.overlay is not None
    try:
        return args.run(args)
    except UsageError as exc:
        report_error(exc)
        return INVALID
    except (ValueError, OSError, ArithmeticError) as exc:  # AtlasError is a ValueError; OverflowError is an ArithmeticError
        report_error(exc)
        return INVALID
    except ImportError as exc:
        report_error(UsageError("this verb is not available yet: %s" % (exc,)))
        return INVALID


if __name__ == "__main__":
    sys.exit(main())
