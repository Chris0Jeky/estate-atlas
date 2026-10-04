"""Tests for scripts/build_docs_site.py, the staging step of the GitHub Pages docs site.

The Jekyll build itself runs on the hosted runner; these tests cover only the staged folder.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "build_docs_site.py"
GITHUB_BLOB = "https://github.com/Chris0Jeky/estate-atlas/blob/main/"


def load_script():
    spec = importlib.util.spec_from_file_location("build_docs_site_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_root(parent: Path) -> Path:
    """A throwaway copy of just the inputs the site is built from."""
    root = parent / "repo"
    root.mkdir()
    for name in ("README.md", "LICENSE"):
        shutil.copyfile(ROOT / name, root / name)
    shutil.copytree(ROOT / "docs", root / "docs")
    shutil.copytree(ROOT / "examples" / "shop", root / "examples" / "shop")
    shutil.copytree(ROOT / "examples" / "workspace", root / "examples" / "workspace")
    shutil.copytree(ROOT / "scripts" / "docs_site", root / "scripts" / "docs_site")
    return root


class BuildDocsSiteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_script()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_root(Path(self._tmp.name))
        self.out = self.root / "_site_src"

    def build(self) -> Path:
        self.module.build(self.root, self.out)
        return self.out

    def read(self, name: str) -> str:
        return (self.out / name).read_text(encoding="utf-8")

    def tree(self) -> dict[str, bytes]:
        return {str(p.relative_to(self.out)): p.read_bytes()
                for p in sorted(self.out.rglob("*")) if p.is_file()}

    def test_index_has_front_matter_and_no_inline_nav(self) -> None:
        self.build()
        index = self.read("index.md")
        self.assertTrue(index.startswith("---\ntitle: "))
        self.assertIn("\nlayout: default\n", index)
        self.assertIn('\nsource_path: "README.md"\n', index)
        self.assertNotIn("[Home](index.html)", index)  # navigation now comes from the layout
        self.assertIn("**Your architecture as checked data.**", index)
        self.assertNotIn("\r", index)

    def test_nav_data_lists_every_page_with_its_jekyll_url(self) -> None:
        self.build()
        nav = self.read("_data/docs_nav.yml")
        self.assertIn('repo: "https://github.com/Chris0Jeky/estate-atlas"', nav)
        for label, url in (("Home", "/"), ("Design", "/docs/DESIGN.html"), ("Qualification", "/docs/QUALIFICATION.html"),
                           ("Origin identity", "/docs/ORIGIN_IDENTITY.html"), ("Example atlas", "/example.html")):
            self.assertIn('{title: "%s", url: "%s"}' % (label, url), nav)
        self.assertIn("groups:\n  - title: Documentation\n    pages:\n", nav)

    def test_theme_files_are_staged(self) -> None:
        self.build()
        layout = self.read("_layouts/default.html")
        self.assertIn("site.data.docs_nav", layout)
        self.assertIn("{{ content }}", layout)
        self.assertIn("--accent:", self.read("assets/css/docs.css"))

    def test_example_page_has_no_edit_link_source(self) -> None:
        self.build()
        self.assertNotIn("source_path", self.read("example.md"))

    def test_config(self) -> None:
        self.build()
        config = self.read("_config.yml")
        self.assertIn("title: estate-atlas", config)
        self.assertIn("description: Your architecture as checked data.", config)
        self.assertNotIn("theme:", config)  # the site's own layout and stylesheet replace the stock theme
        for plugin in ("jekyll-optional-front-matter", "jekyll-titles-from-headings", "jekyll-default-layout"):
            self.assertIn("  - " + plugin, config)
        # All link conversion happens in the staging script. The Jekyll plugin is on by default on GitHub Pages,
        # so it is switched off as well as left out of the list.
        self.assertNotIn("  - jekyll-relative-links", config)
        self.assertIn("relative_links:\n  enabled: false\n", config)

    def test_unpublished_path_link_becomes_github_blob(self) -> None:
        self.build()
        qualification = self.read("docs/QUALIFICATION.md")
        self.assertIn("](" + GITHUB_BLOB + "examples/workspace/README.md)", qualification)
        self.assertNotIn("](../examples/", qualification)

    def test_staged_doc_link_stays_relative(self) -> None:
        self.build()
        index = self.read("index.md")
        self.assertIn("](docs/DESIGN.html)", index)
        self.assertIn("](docs/QUALIFICATION.html)", index)
        self.assertNotIn("](docs/DESIGN.md", index)
        origin = self.read("docs/ORIGIN_IDENTITY.md")
        self.assertIn("](DESIGN.html)", origin)

    def test_rewrite_leaves_external_anchor_and_code_alone(self) -> None:
        rewrite = self.module.rewrite_links
        text = ("[ext](https://example.com/x) [mail](mailto:a@example.com) [a](#top)\n"
                "[repo](scripts/check_public.py#L3) [dir](tests/) [up](../index.md)\n"
                "`[code](tests/x.py)`\n```\n[fence](tests/y.py)\n```\n")
        out = rewrite(text, "README.md", {"README.md": "index.md"})
        self.assertIn("[ext](https://example.com/x)", out)
        self.assertIn("[mail](mailto:a@example.com)", out)
        self.assertIn("[a](#top)", out)
        self.assertIn("[repo](" + GITHUB_BLOB + "scripts/check_public.py#L3)", out)
        self.assertIn("[dir](" + GITHUB_BLOB + "tests/)", out)
        self.assertIn("`[code](tests/x.py)`", out)
        self.assertIn("[fence](tests/y.py)", out)

    def test_example_demo_and_page(self) -> None:
        self.build()
        html = self.read("example/shop-atlas.html")
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        page = self.read("example.md")
        self.assertIn("title: \"Example atlas: a fictional shop\"", page)
        self.assertIn("](example/shop-atlas.html)", page)
        self.assertIn("{% raw %}", page)
        self.assertIn(chr(10) + "# Example atlas: a fictional shop" + chr(10), page)
        self.assertIn(chr(10) + "## A tour of the estate" + chr(10), page)  # tour sits below the page h1
        self.assertIn(chr(10) + "#### Storefront" + chr(10), page)

    @unittest.skipIf(os.name == "nt", "POSIX file modes only")
    def test_staged_files_are_world_readable(self) -> None:
        import stat
        self.build()
        for path in sorted(self.out.rglob("*")):
            mode = stat.S_IMODE(path.stat().st_mode)
            want = 0o755 if path.is_dir() else 0o644
            self.assertEqual(mode, want, "%s is %o" % (path.relative_to(self.out), mode))

    def test_two_builds_are_byte_identical(self) -> None:
        self.build()
        first = self.tree()
        self.build()
        self.assertEqual(first, self.tree())
        self.assertIn("index.md", first)

    def test_missing_allowlisted_file_fails_closed(self) -> None:
        (self.root / "docs" / "QUALIFICATION.md").unlink()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = self.module.main(["--root", str(self.root), "--out", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("QUALIFICATION.md", err.getvalue())
        self.assertFalse(self.out.exists())

    def symlink(self, link: Path, target: Path, directory: bool = False) -> None:
        try:
            os.symlink(target, link, target_is_directory=directory)
        except (OSError, NotImplementedError) as err:
            self.skipTest("cannot create symlinks here: %s" % err)

    def test_rejects_symlinked_allowlisted_doc(self) -> None:
        secret = self.root / "secret.txt"
        secret.write_text("do not publish", encoding="utf-8")
        for rel in ("docs/DESIGN.md", "LICENSE", "examples/shop/atlas.json"):
            with self.subTest(rel=rel):
                path = self.root / rel
                original = path.read_bytes()
                path.unlink()
                self.symlink(path, secret)
                self.out.mkdir(exist_ok=True)
                (self.out / self.module.MARKER).write_text("x", encoding="utf-8")
                (self.out / "keep.txt").write_text("keep", encoding="utf-8")
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = self.module.main(["--root", str(self.root), "--out", str(self.out)])
                self.assertEqual(code, 1)
                self.assertIn("symlink", err.getvalue())
                self.assertTrue((self.out / "keep.txt").is_file())  # existing output untouched
                self.assertFalse(any("do not publish" in p.read_text(encoding="utf-8", errors="ignore")
                                     for p in self.out.rglob("*") if p.is_file()))
                path.unlink()
                path.write_bytes(original)

    def test_rejects_symlinked_parent_dir(self) -> None:
        real = self.root / "docs_real"
        (self.root / "docs").rename(real)
        self.symlink(self.root / "docs", real, directory=True)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = self.module.main(["--root", str(self.root), "--out", str(self.out)])
        self.assertEqual(code, 1)
        self.assertIn("symlink", err.getvalue())
        self.assertFalse(self.out.exists())

    def test_symlink_check_runs_on_every_component(self) -> None:
        """The same rejection without needing OS symlink support: report one component as a link."""
        from unittest import mock
        real = Path.is_symlink
        for flagged in ("DESIGN.md", "docs", "atlas.json", "LICENSE"):
            with self.subTest(flagged=flagged):
                self.out.mkdir(exist_ok=True)
                (self.out / self.module.MARKER).write_text("x", encoding="utf-8")
                (self.out / "keep.txt").write_text("keep", encoding="utf-8")
                with mock.patch.object(Path, "is_symlink",
                                       lambda self_, f=flagged: self_.name == f or real(self_)):
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        code = self.module.main(["--root", str(self.root), "--out", str(self.out)])
                self.assertEqual(code, 1)
                self.assertIn("symlink", err.getvalue())
                self.assertTrue((self.out / "keep.txt").is_file())

    def test_refuses_unsafe_out_paths(self) -> None:
        (self.root / ".git").mkdir()
        for bad in (self.root, self.root / "docs", self.root.parent, self.root / ".git", self.root / ".git" / "x"):
            with self.subTest(out=str(bad)), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(self.module.main(["--root", str(self.root), "--out", str(bad)]), 1)
        self.assertTrue((self.root / "docs" / "DESIGN.md").is_file())

    def test_refuses_to_clear_a_foreign_nonempty_dir(self) -> None:
        foreign = self.root / "scratch"
        foreign.mkdir()
        (foreign / "keep.txt").write_text("x", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.module.main(["--root", str(self.root), "--out", str(foreign)]), 1)
        self.assertTrue((foreign / "keep.txt").is_file())


    # --- code-aware link rewriting -------------------------------------------------------------------------

    STAGED = {"README.md": "index.md", "docs/DESIGN.md": "docs/DESIGN.md", "LICENSE": "LICENSE.txt"}

    def rewrite(self, text: str, source: str = "README.md") -> str:
        return self.module.rewrite_links(text, source, self.STAGED)

    def assertLeft(self, text: str, link: str) -> None:
        """``text`` rewritten as README.md still holds ``link`` byte for byte."""
        self.assertIn(link, self.rewrite(text))

    def assertRewritten(self, text: str, label: str, path: str) -> None:
        out = self.rewrite(text)
        self.assertIn("[%s](%s%s)" % (label, GITHUB_BLOB, path), out)
        self.assertNotIn("[%s](%s)" % (label, path), out)

    def test_staged_page_links_point_at_the_rendered_html(self) -> None:
        out = self.rewrite("[d](docs/DESIGN.md#x) [r](README.md) [l](LICENSE) [q](docs/DESIGN.md?a=1#y \"T\")")
        self.assertIn("[d](docs/DESIGN.html#x)", out)
        self.assertIn("[r](index.html)", out)
        self.assertIn("[l](LICENSE.txt)", out)
        self.assertIn("[q](docs/DESIGN.html?a=1#y \"T\")", out)
        up = self.rewrite("[r](../README.md#top) [s](DESIGN.md)", "docs/DESIGN.md")
        self.assertIn("[r](../index.html#top)", up)
        self.assertIn("[s](DESIGN.html)", up)

    def test_indented_code_block_is_left_alone(self) -> None:
        text = "Intro\n\n    [a](tests/a.py)\n    [b](tests/b.py)\n\n[c](tests/c.py)\n"
        out = self.rewrite(text)
        self.assertIn("\n    [a](tests/a.py)\n    [b](tests/b.py)\n", out)
        self.assertRewritten(text, "c", "tests/c.py")

    def test_indented_code_after_a_heading_a_tab_or_the_start_is_left_alone(self) -> None:
        self.assertLeft("    [a](tests/a.py)\n", "    [a](tests/a.py)")
        self.assertLeft("# H\n    [a](tests/a.py)\n", "\n    [a](tests/a.py)")
        self.assertLeft("Intro\n\n\t[a](tests/a.py)\n", "\t[a](tests/a.py)")
        self.assertLeft("Intro\n\n    first\n\n    [a](tests/a.py)\n", "    [a](tests/a.py)")  # across a blank

    def test_indentation_that_is_not_code_is_still_rewritten(self) -> None:
        # a paragraph continuation, a list item's own paragraph, and a three-space indent are all text
        self.assertRewritten("Intro\n    [a](tests/a.py)\n", "a", "tests/a.py")
        self.assertRewritten("- item\n\n    [b](tests/b.py)\n", "b", "tests/b.py")
        self.assertRewritten("1. item\n\n    [c](tests/c.py)\n", "c", "tests/c.py")
        self.assertRewritten("Intro\n\n   [d](tests/d.py)\n", "d", "tests/d.py")

    def test_code_indented_inside_a_list_item_is_left_alone(self) -> None:
        text = "- item\n\n      [a](tests/a.py)\n\n- next [b](tests/b.py)\n"
        self.assertIn("      [a](tests/a.py)", self.rewrite(text))
        self.assertRewritten(text, "b", "tests/b.py")

    def test_four_backtick_fence_containing_triple_backticks_is_left_alone(self) -> None:
        text = "````md\n```\n[a](tests/a.py)\n```\n[b](tests/b.py)\n````\n[c](tests/c.py)\n"
        out = self.rewrite(text)
        self.assertIn("[a](tests/a.py)", out)
        self.assertIn("[b](tests/b.py)", out)
        self.assertRewritten(text, "c", "tests/c.py")

    def test_a_fence_closes_only_on_a_long_enough_bare_line_of_its_own_character(self) -> None:
        text = ("```\n[a](tests/a.py)\n```py\n[b](tests/b.py)\n~~~\n[c](tests/c.py)\n``\n[d](tests/d.py)\n```\n"
                "[e](tests/e.py)\n")
        out = self.rewrite(text)
        for name in "abcd":
            self.assertIn("[%s](tests/%s.py)" % (name, name), out)
        self.assertRewritten(text, "e", "tests/e.py")
        tilde = "~~~~\n[a](tests/a.py)\n~~~\n[b](tests/b.py)\n```\n[c](tests/c.py)\n~~~~\n[d](tests/d.py)\n"
        out = self.rewrite(tilde)
        for name in "abc":
            self.assertIn("[%s](tests/%s.py)" % (name, name), out)
        self.assertRewritten(tilde, "d", "tests/d.py")

    def test_an_unclosed_fence_runs_to_the_end(self) -> None:
        self.assertLeft("[a](tests/a.py)\n```\n[b](tests/b.py)\n\n[c](tests/c.py)\n", "[c](tests/c.py)")

    def test_fence_in_a_list_item_and_in_a_blockquote(self) -> None:
        in_list = "- item\n\n  ```\n  [a](tests/a.py)\n  ```\n\n[b](tests/b.py)\n"
        self.assertLeft(in_list, "  [a](tests/a.py)")
        self.assertRewritten(in_list, "b", "tests/b.py")
        quoted = "> ```\n> [a](tests/a.py)\n> ```\n[b](tests/b.py)\n"
        self.assertLeft(quoted, "> [a](tests/a.py)")
        self.assertRewritten(quoted, "b", "tests/b.py")

    def test_double_backtick_span_containing_single_backticks_is_left_alone(self) -> None:
        text = "``a ` [x](tests/x.py) ` b`` and [y](tests/y.py)\n"
        self.assertLeft(text, "[x](tests/x.py)")
        self.assertRewritten(text, "y", "tests/y.py")

    def test_code_span_may_run_over_a_line_break(self) -> None:
        text = "see `a\n[x](tests/x.py)` end [y](tests/y.py)\n"
        self.assertLeft(text, "[x](tests/x.py)")
        self.assertRewritten(text, "y", "tests/y.py")

    def test_backticks_with_no_matching_closer_are_literal(self) -> None:
        self.assertRewritten("a ` [x](tests/x.py)\n", "x", "tests/x.py")
        self.assertRewritten("`` [x](tests/x.py) `\n", "x", "tests/x.py")
        self.assertRewritten("\\` [x](tests/x.py) \\`\n", "x", "tests/x.py")  # escaped backticks open nothing
        self.assertRewritten("`a\n\n[x](tests/x.py) b`\n", "x", "tests/x.py")  # a span never crosses a blank line

    def test_a_link_whose_label_is_code_is_rewritten(self) -> None:
        self.assertRewritten("[`code`](tests/x.py)\n", "`code`", "tests/x.py")

    def test_reference_definitions_follow_the_same_rules(self) -> None:
        out = self.rewrite("[a]: tests/a.py\n[b]: docs/DESIGN.md#s \"T\"\n[c]: <tests/c.py>\n\n"
                           "```\n[d]: tests/d.py\n```\n")
        self.assertIn("[a]: " + GITHUB_BLOB + "tests/a.py\n", out)
        self.assertIn("[b]: docs/DESIGN.html#s \"T\"\n", out)
        self.assertIn("[c]: <" + GITHUB_BLOB + "tests/c.py>\n", out)
        self.assertIn("[d]: tests/d.py", out)

    def test_headings_are_found_and_demoted_only_outside_code(self) -> None:
        text = "````md\n```\n# not a heading\n```\n# also not\n````\n\n    # indented code\n\n# Real\n## Sub\n"
        self.assertEqual(self.module._first_heading(text, "fallback"), "Real")
        demoted = self.module._demote_headings(text)
        self.assertIn("\n# not a heading\n", demoted)
        self.assertIn("\n# also not\n", demoted)
        self.assertIn("\n    # indented code\n", demoted)
        self.assertIn("\n## Real\n### Sub", demoted)

    # --- the raw wrapper ----------------------------------------------------------------------------------

    @staticmethod
    def liquid(source: str) -> str:
        """A small model of Liquid 4 (the engine behind GitHub's Jekyll): its tokenizer, ``raw`` and a quoted
        string output. Real Liquid is not available to these tests; the hosted Jekyll build is the other proof."""
        tokens = [t for t in re.split(r"(\{%.*?%\}|\{\{.*?\}\}?|\{\{|\{%)", source, flags=re.S) if t]
        out = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            index += 1
            if token.startswith("{%"):
                tag = re.fullmatch(r"\{%-?\s*(\w+)\s*(.*?)-?%\}", token, re.S)
                if not tag or tag.group(1) != "raw":
                    raise AssertionError("Liquid would act on the tag %r" % token)
                while True:
                    if index >= len(tokens):
                        raise AssertionError("Liquid: raw never closed")
                    inner = tokens[index]
                    index += 1
                    closing = re.fullmatch(r"(.*)\{%-?\s*(\w+)\s*(.*)?-?%\}", inner, re.S)
                    if closing:
                        out.append(closing.group(1))
                        if closing.group(2) == "endraw":
                            break
                    out.append(inner)
            elif token.startswith("{{"):
                string = re.fullmatch(r'\{\{\s*"([^"]*)"\s*\}\}', token)
                if not string:
                    raise AssertionError("Liquid would evaluate %r" % token)
                out.append(string.group(1))
            else:
                out.append(token)
        return "".join(out)

    def test_liquid_model_rejects_the_early_close(self) -> None:
        self.assertEqual(self.liquid("a {% raw %}x {{ y }} z{% endraw %} b"), "a x {{ y }} z b")
        with self.assertRaises(AssertionError):
            self.liquid("{% raw %}x {% endraw %} {% endraw %}")  # what an unescaped literal endraw does

    def test_embedded_liquid_delimiters_survive_the_raw_wrapper(self) -> None:
        bodies = [
            "plain text",
            "close it {% endraw %} then {% raw %} again",
            "{%endraw%} and {%- endraw -%} and {%  endraw  %} and {% endraw junk %}",
            "{% assign x = 1 %}{{ x }} {{ \"{{\" }} {{{ three }}} {{{%",
            "open {{ with no close and {% with no close",
            "inside a var {{ foo {% endraw %} bar }}",
            "{%{{{%%}}}}%}{{{{",
            "ends with a brace {",
        ]
        for body in bodies:
            with self.subTest(body=body):
                page = self.module._page("index.md", "T", body)
                wrapper = page[page.index("{% raw %}\n"):]
                self.assertEqual(self.liquid(wrapper), "\n" + body + "\n\n")

    def test_built_pages_survive_a_literal_endraw_in_the_source(self) -> None:
        design = self.root / "docs" / "DESIGN.md"
        design.write_text(design.read_text(encoding="utf-8") + "\nA literal {% endraw %} here.\n", encoding="utf-8")
        self.build()
        page = self.read("docs/DESIGN.md")
        rendered = self.liquid(page[page.index("\n\n{% raw %}\n") + 2:])
        self.assertIn("A literal {% endraw %} here.", rendered)

    # --- the stage marker ---------------------------------------------------------------------------------

    def make_stale_out(self, marker: str | None) -> Path:
        self.out.mkdir()
        if marker is not None:
            (self.out / self.module.MARKER).write_bytes(marker.encode("utf-8"))  # bytes: no newline translation
        (self.out / "keep.txt").write_text("keep", encoding="utf-8")
        return self.out

    def run_main(self) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = self.module.main(["--root", str(self.root), "--out", str(self.out)])
        return code, err.getvalue()

    def test_marker_with_foreign_contents_does_not_authorise_clearing(self) -> None:
        for content in ("", "x", "staged by scripts/build_docs_site.py", "staged by scripts/build_docs_site.py\nmore\n"):
            with self.subTest(content=content):
                out = self.make_stale_out(content)
                code, err = self.run_main()
                self.assertEqual(code, 1)
                self.assertIn("refusing", err)
                self.assertTrue((out / "keep.txt").is_file())
                shutil.rmtree(out)

    def test_marker_must_be_a_regular_file(self) -> None:
        out = self.make_stale_out(None)
        (out / self.module.MARKER).mkdir()
        code, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertTrue((out / "keep.txt").is_file())

    def test_a_symlinked_marker_with_the_right_text_is_refused(self) -> None:
        out = self.make_stale_out(None)
        genuine = self.root / "genuine-marker"
        genuine.write_bytes(self.module.MARKER_TEXT.encode("utf-8"))
        self.symlink(out / self.module.MARKER, genuine)
        code, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertTrue((out / "keep.txt").is_file())

    def test_a_marker_reported_as_a_link_is_refused_without_real_symlinks(self) -> None:
        from types import SimpleNamespace
        from unittest import mock
        out = self.make_stale_out(self.module.MARKER_TEXT)
        real_lstat = os.lstat

        def fake(path, *args, **kwargs):
            if Path(os.fspath(path)).name == self.module.MARKER:
                return SimpleNamespace(st_mode=stat.S_IFLNK | 0o777)
            return real_lstat(path, *args, **kwargs)

        with mock.patch.object(os, "lstat", fake):
            code, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertTrue((out / "keep.txt").is_file())

    def test_a_genuine_marker_still_allows_a_rebuild(self) -> None:
        out = self.make_stale_out(None)
        (out / self.module.MARKER).write_bytes(self.module.MARKER_TEXT.encode("utf-8"))
        code, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertFalse((out / "keep.txt").exists())
        self.assertTrue((out / "index.md").is_file())

    # --- reparse points (Windows junctions) on Python 3.11 --------------------------------------------------

    def test_a_reparse_point_component_is_refused_without_is_junction(self) -> None:
        """Windows reports a junction through ``st_file_attributes``; ``Path.is_junction`` only exists from 3.12."""
        from unittest import mock
        real_lstat = os.lstat
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

        class Flagged:
            def __init__(self, inner):
                self._inner = inner
                self.st_file_attributes = reparse

            def __getattr__(self, name):
                return getattr(self._inner, name)

        for flagged in ("DESIGN.md", "docs", "atlas.json", "LICENSE"):
            with self.subTest(flagged=flagged):
                self.make_stale_out(self.module.MARKER_TEXT)

                def fake(path, *args, _flag=flagged, **kwargs):
                    result = real_lstat(path, *args, **kwargs)
                    return Flagged(result) if Path(os.fspath(path)).name == _flag else result

                with mock.patch.object(os, "lstat", fake):
                    code, err = self.run_main()
                self.assertEqual(code, 1)
                self.assertTrue("symlink" in err or "junction" in err, err)
                self.assertTrue((self.out / "keep.txt").is_file())
                shutil.rmtree(self.out)

    # --- the staged output, as a whole ----------------------------------------------------------------------

    def test_every_relative_link_in_the_staged_pages_resolves(self) -> None:
        self.build()
        checked = 0
        for page in sorted(self.out.rglob("*.md")):
            text = page.read_text(encoding="utf-8")
            cut = text.index("\n\n{% raw %}\n") + 2
            body = self.liquid(text[cut:])  # the page as Jekyll emits it (navigation lives in the layout)
            body = re.sub(r"^ {0,3}(`{3,}|~{3,}).*?^ {0,3}\1[`~]*[ \t]*$", "", body, flags=re.S | re.M)
            body = re.sub(r"`[^`\n]*`", "", body)
            for target in re.findall(r"\]\(([^)\s]+)", body):
                if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target) or target.startswith("#"):
                    continue
                path = re.split(r"[#?]", target, maxsplit=1)[0]
                self.assertFalse(path.endswith(".md"), "%s links to a Markdown source: %s" % (page.name, target))
                resolved = (page.parent / path).resolve()
                if resolved.suffix == ".html" and not resolved.exists():
                    resolved = resolved.with_suffix(".md")  # Jekyll renders each staged .md page to .html
                self.assertTrue(resolved.is_file(), "%s links to %s, which is not staged" % (page.name, target))
                checked += 1
        self.assertGreater(checked, 3)  # body links only: the navigation lives in the layout now
        # Every navigation URL the layout renders must be a page Jekyll will produce.
        for url in re.findall(r'url: "([^"]+)"', self.read("_data/docs_nav.yml")):
            rel = "index.md" if url == "/" else url.lstrip("/")
            if rel.endswith(".html"):
                rel = rel[:-5] + ".md"
            self.assertTrue((self.out / rel).is_file(), "navigation points at %s, which is not staged" % url)

    def test_workflow_runs_the_tests_and_watches_their_inputs(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
        for path in ("LICENSE", "tests/test_build_docs_site.py", "scripts/build_docs_site.py"):
            self.assertEqual(workflow.count("      - %s\n" % path), 2, path + " must trigger push and pull_request")
        self.assertIn("unittest", workflow)
        self.assertLess(workflow.index("unittest"), workflow.index("scripts/build_docs_site.py --out"))


if __name__ == "__main__":
    unittest.main()
