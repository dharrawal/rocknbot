"""
Unit tests for GitHub heading-anchor slugging (github_anchor.py).

Locks in github-slugger-compatible ASCII behavior documented on
GithubAnchorSlugger and github_slug: duplicate suffixes, punctuation
stripping, multi-space hyphens, and facebook/react README fixtures.

Run from LilLisa_Server/cron:
    PYTHONPATH=. python3 tests/test_github_anchor.py
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

import github_anchor  # noqa: E402
from github_anchor import (  # noqa: E402
    GithubAnchorSlugger,
    github_slug,
    load_github_repo_url,
)


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


class LoadGithubRepoUrlTests(unittest.TestCase):
    """The prod image copies no env/ (build/dockerfile_prod), so the repo URL
    can arrive as an env var over an empty placeholder file -- the same case
    github_sync.load_env() supports."""

    FILE_URL = "https://github.com/file-org/file-repo"
    ENV_URL = "https://github.com/env-org/env-repo"

    def test_env_var_used_when_file_is_missing_the_key(self):
        with patch.object(github_anchor, "dotenv_values", return_value={}):
            with patch.dict(os.environ, {"GITHUB_REPO_URL": self.ENV_URL}):
                self.assertEqual(load_github_repo_url(), self.ENV_URL)

    def test_env_var_wins_over_file_placeholder(self):
        with patch.object(github_anchor, "dotenv_values", return_value={"GITHUB_REPO_URL": ""}):
            with patch.dict(os.environ, {"GITHUB_REPO_URL": self.ENV_URL}):
                self.assertEqual(load_github_repo_url(), self.ENV_URL)

    def test_file_used_when_env_var_absent(self):
        with patch.object(github_anchor, "dotenv_values", return_value={"GITHUB_REPO_URL": self.FILE_URL}):
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(load_github_repo_url(), self.FILE_URL)

    def test_raises_when_configured_nowhere(self):
        with patch.object(github_anchor, "dotenv_values", return_value={}):
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(RuntimeError):
                    load_github_repo_url()


if __name__ == "__main__":
    unittest.main()
