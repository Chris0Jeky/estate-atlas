"""URL conversion and adversarial controls for the documentation staging checks."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_build_docs_site as staging_tests


class DocsSiteLinkTests(unittest.TestCase):
    def setUp(self):
        self.script = staging_tests.load_script()
        self.mapping = dict(self.script.PAGES)

    def rewrite(self, text, source="README.md"):
        return self.script.rewrite_links(text, source, self.mapping)

    def test_encoded_published_paths_resolve_before_lookup(self):
        self.assertEqual(self.rewrite("[Design](docs/%44ESIGN.md?q=%2F#part%20one)"),
                         "[Design](docs/DESIGN.html?q=%2F#part%20one)")
        self.assertEqual(self.rewrite("[Home](../%52EADME.md)", "docs/DESIGN.md"),
                         "[Home](../index.html)")

    def test_unpublished_urls_are_decoded_and_encoded_exactly_once(self):
        for target in ("notes/a%20b.md", "notes/percent%2520.md", "notes/caf%C3%A9.md", "notes/a%23b.md"):
            with self.subTest(target=target):
                self.assertEqual(self.rewrite("[Source](" + target + ")"),
                                 "[Source](" + self.script.REPO_BLOB + target + ")")

    def test_staged_destinations_are_url_encoded_too(self):
        self.mapping["docs/a b#c.md"] = "docs/a b#c.md"
        self.assertEqual(self.rewrite("[Space](docs/a%20b%23c.md)"), "[Space](docs/a%20b%23c.html)")

    def test_angle_delimited_inline_targets_keep_brackets_and_titles(self):
        self.assertEqual(self.rewrite('[Design](<docs/DESIGN.md> "About")'),
                         '[Design](<docs/DESIGN.html> "About")')
        self.assertEqual(self.rewrite('[Notes](<notes/a b(c).md>)'),
                         '[Notes](<' + self.script.REPO_BLOB + 'notes/a%20b%28c%29.md>)')
        self.assertEqual(self.rewrite('[Design](<docs/%44ESIGN.md#part>)'),
                         '[Design](<docs/DESIGN.html#part>)')

    def test_reference_definitions_share_the_same_encoding_rules(self):
        self.assertEqual(self.rewrite('[d]: <docs/%44ESIGN.md> "Title"'),
                         '[d]: <docs/DESIGN.html> "Title"')
        self.assertEqual(self.rewrite('> [d]: notes/a%20b.md'),
                         '> [d]: ' + self.script.REPO_BLOB + 'notes/a%20b.md')

    def test_external_rooted_anchors_and_code_stay_unchanged(self):
        for text in ('[root](/docs/DESIGN.md)', '[host](//example.com/a)', '[anchor](<#part>)',
                     '[remote](<https://example.com/a%20b>)', '`[x](<docs/%44ESIGN.md>)`',
                     '```md\n[x](docs/%44ESIGN.md)\n```', '[bad](docs/%FF.md)'):
            with self.subTest(text=text):
                self.assertEqual(self.rewrite(text), text)

    def test_workflow_assertion_rejects_echo_comments_and_the_wrong_test_pattern(self):
        workflow = (staging_tests.ROOT / '.github/workflows/pages.yml').read_text(encoding='utf-8')
        test_run = next(line for line in workflow.splitlines() if 'run: python -m unittest' in line)
        mutations = [test_run.replace('run: python', 'run: echo python'),
                     test_run.replace('run:', '# run:'),
                     test_run.replace('-p ', '-p missing_')]
        case = staging_tests.BuildDocsSiteTest('test_workflow_runs_the_tests_and_watches_their_inputs')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / '.github/workflows/pages.yml'
            target.parent.mkdir(parents=True)
            for replacement in mutations:
                with self.subTest(replacement=replacement):
                    target.write_text(workflow.replace(test_run, replacement), encoding='utf-8')
                    with patch.object(staging_tests, 'ROOT', root):
                        with self.assertRaises(AssertionError):
                            case.test_workflow_runs_the_tests_and_watches_their_inputs()

    def test_link_audit_rejects_broken_reference_definitions_and_html_attributes(self):
        for fragment in ('[bad]: missing.html', '[bad]: <missing.html>',
                         '<a href="missing.html">bad</a>', '<img src="missing.svg" alt="bad">'):
            with self.subTest(fragment=fragment):
                case = staging_tests.BuildDocsSiteTest('test_every_relative_link_in_the_staged_pages_resolves')
                case.setUp()
                try:
                    # Add raw source to a published Markdown page. The audit, not network access, must reject it.
                    original = case.module._page
                    def page(*args, **kwargs):
                        result = original(*args, **kwargs)
                        return result.replace('{% endraw %}', fragment + '\n{% endraw %}')
                    with patch.object(case.module, '_page', page):
                        with self.assertRaises(AssertionError):
                            case.test_every_relative_link_in_the_staged_pages_resolves()
                finally:
                    case.doCleanups()


if __name__ == '__main__':
    unittest.main()
