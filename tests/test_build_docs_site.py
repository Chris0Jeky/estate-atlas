"""Tests for scripts/build_docs_site.py, the staging step of the GitHub Pages docs site.

The Jekyll build itself runs on the hosted runner; these tests cover only the staged folder.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
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

    def test_index_has_nav_and_front_matter(self) -> None:
        self.build()
        index = self.read("index.md")
        self.assertTrue(index.startswith("---\ntitle: "))
        self.assertIn("[Home](index.md)", index)
        self.assertIn("[Design](docs/DESIGN.md)", index)
        self.assertIn("[Qualification](docs/QUALIFICATION.md)", index)
        self.assertIn("[Origin identity](docs/ORIGIN_IDENTITY.md)", index)
        self.assertIn("[Example atlas](example.md)", index)
        self.assertIn("**Your architecture as checked data.**", index)
        self.assertNotIn("\r", index)

    def test_nav_in_docs_pages_uses_parent_links(self) -> None:
        self.build()
        design = self.read("docs/DESIGN.md")
        self.assertIn("[Home](../index.md)", design)
        self.assertIn("[Example atlas](../example.md)", design)
        self.assertIn("[Design](DESIGN.md)", design)

    def test_config(self) -> None:
        self.build()
        config = self.read("_config.yml")
        self.assertIn("title: estate-atlas", config)
        self.assertIn("description: Your architecture as checked data.", config)
        self.assertIn("theme: jekyll-theme-primer", config)
        for plugin in ("jekyll-relative-links", "jekyll-optional-front-matter",
                       "jekyll-titles-from-headings", "jekyll-default-layout"):
            self.assertIn("  - " + plugin, config)
        self.assertIn("relative_links:", config)
        self.assertIn("  enabled: true", config)
        self.assertIn("  collections: false", config)

    def test_unpublished_path_link_becomes_github_blob(self) -> None:
        self.build()
        qualification = self.read("docs/QUALIFICATION.md")
        self.assertIn("](" + GITHUB_BLOB + "examples/workspace/README.md)", qualification)
        self.assertNotIn("](../examples/", qualification)

    def test_staged_doc_link_stays_relative(self) -> None:
        self.build()
        index = self.read("index.md")
        self.assertIn("](docs/DESIGN.md)", index)
        self.assertIn("](docs/QUALIFICATION.md)", index)
        origin = self.read("docs/ORIGIN_IDENTITY.md")
        self.assertIn("](DESIGN.md)", origin)

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


if __name__ == "__main__":
    unittest.main()
