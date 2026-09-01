"""
Unit tests for GitHub heading-anchor slugging (github_anchor.py).

Locks in github-slugger-compatible ASCII behavior documented on
GithubAnchorSlugger and github_slug: duplicate suffixes, punctuation
stripping, multi-space hyphens, and facebook/react README fixtures.

Run from lil-lisa-cron-scripts:
    PYTHONPATH=. python3 tests/test_github_anchor.py
"""

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

from github_anchor import GithubAnchorSlugger, github_slug  # noqa: E402


class GithubSlugTests(unittest.TestCase):
    def test_punctuation_is_stripped_not_hyphenated(self):
        self.assertEqual(github_slug("Zookeeper GC Logging Setup in 7.3.X"), "zookeeper-gc-logging-setup-in-73x")
        self.assertEqual(github_slug("Hello, World!"), "hello-world")
        self.assertEqual(github_slug("C++ vs. C#"), "c-vs-c")

    def test_multi_space_becomes_multiple_hyphens(self):
        self.assertEqual(github_slug("Foo  Bar"), "foo--bar")
        self.assertEqual(github_slug("Foo   Bar"), "foo---bar")
        self.assertEqual(github_slug("Foo & Bar"), "foo--bar")

    def test_hyphen_and_underscore_survive(self):
        self.assertEqual(github_slug("already-hyphenated_heading"), "already-hyphenated_heading")

    def test_readme_code_of_conduct(self):
        self.assertEqual(github_slug("Code of Conduct"), "code-of-conduct")

    def test_readme_good_first_issues(self):
        self.assertEqual(github_slug("Good First Issues"), "good-first-issues")


class GithubAnchorSluggerDuplicateTests(unittest.TestCase):
    def test_duplicates_foo_foo_1_foo_2(self):
        slugger = GithubAnchorSlugger()
        self.assertEqual(slugger.slug("foo"), "foo")
        self.assertEqual(slugger.slug("foo"), "foo-1")
        self.assertEqual(slugger.slug("foo"), "foo-2")

    def test_duplicate_readme_headings_keep_independent_counters(self):
        slugger = GithubAnchorSlugger()
        self.assertEqual(slugger.slug("Code of Conduct"), "code-of-conduct")
        self.assertEqual(slugger.slug("Good First Issues"), "good-first-issues")
        self.assertEqual(slugger.slug("Code of Conduct"), "code-of-conduct-1")
        self.assertEqual(slugger.slug("Good First Issues"), "good-first-issues-1")


if __name__ == "__main__":
    unittest.main()
