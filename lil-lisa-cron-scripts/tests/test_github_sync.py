"""
Unit tests for github_sync GIT_ASKPASS auth (token never in clone URL).

Run from lil-lisa-cron-scripts:
    PYTHONPATH=. python3 tests/test_github_sync.py
"""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

sys.modules.setdefault("git", MagicMock())
sys.modules["git"].Actor = MagicMock(return_value="actor")
sys.modules.setdefault("dotenv", MagicMock())
sys.modules["dotenv"].dotenv_values = MagicMock(return_value={})

import github_sync  # noqa: E402


class GitAskpassTests(unittest.TestCase):
    def test_askpass_script_reads_env_and_does_not_embed_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = github_sync._write_git_askpass(Path(tmp))
            text = path.read_text(encoding="utf-8")
            self.assertIn("$GITHUB_TOKEN", text)
            self.assertNotIn("ghp_", text)
            self.assertTrue(os.access(path, os.X_OK))
            self.assertTrue(stat.S_IMODE(path.stat().st_mode) & stat.S_IXUSR)

    def test_git_auth_env_sets_askpass_and_token(self):
        env = github_sync._git_auth_env("secret-token", Path("/tmp/git-askpass.sh"))
        self.assertEqual(env["GIT_ASKPASS"], "/tmp/git-askpass.sh")
        self.assertEqual(env["GITHUB_TOKEN"], "secret-token")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_https_url_with_embedded_user_is_rejected(self):
        with self.assertRaises(ValueError):
            github_sync._require_https_repo_url("https://ghp_abc@github.com/org/repo.git")

    def test_ssh_url_is_rejected(self):
        with self.assertRaises(ValueError):
            github_sync._require_https_repo_url("git@github.com:org/repo.git")

    def test_clone_uses_plain_url_and_askpass_env(self):
        repo_url = "https://github.com/org/techsupport-qa.git"
        token = "secret-token-not-for-url"

        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "verified"
            source_dir.mkdir()
            source_path = source_dir / github_sync.TECHSUPPORT_QA_MARKDOWN_FILENAME
            source_path.write_text("## New entry\n", encoding="utf-8")

            def fake_clone(url, to_path, **kwargs):
                self.assertEqual(url, repo_url)
                self.assertNotIn(token, url)
                self.assertEqual(kwargs["env"]["GITHUB_TOKEN"], token)
                self.assertTrue(kwargs["env"]["GIT_ASKPASS"].endswith("git-askpass.sh"))
                self.assertEqual(kwargs["multi_options"], ["--config", "credential.helper="])
                os.makedirs(to_path, exist_ok=True)
                Path(to_path, github_sync.TECHSUPPORT_QA_MARKDOWN_FILENAME).write_text(
                    "## old\n", encoding="utf-8"
                )
                repo = MagicMock()
                repo.git.custom_environment.return_value.__enter__.return_value = None
                repo.git.custom_environment.return_value.__exit__.return_value = False
                return repo

            with patch.object(github_sync, "load_env", return_value={
                "GITHUB_TOKEN": token,
                "GITHUB_REPO_URL": repo_url,
            }), patch.object(
                github_sync, "VERIFIED_TECHSUPPORT_QA_FOLDERPATH", source_dir
            ), patch.object(github_sync.git.Repo, "clone_from", side_effect=fake_clone):
                result = github_sync.push_verified_qa_pairs()

        self.assertTrue(result["pushed"])

    def test_load_env_overlays_process_environment_over_empty_file(self):
        with patch.object(
            github_sync,
            "dotenv_values",
            return_value={"GITHUB_TOKEN": "", "GITHUB_REPO_URL": "https://github.com/org/repo.git"},
        ), patch.dict(
            os.environ,
            {"GITHUB_TOKEN": "from-env-secret", "GITHUB_REPO_URL": "https://github.com/org/repo.git"},
            clear=False,
        ):
            env = github_sync.load_env()
        self.assertEqual(env["GITHUB_TOKEN"], "from-env-secret")
        self.assertEqual(env["GITHUB_REPO_URL"], "https://github.com/org/repo.git")

    def test_load_env_raises_when_token_missing_in_file_and_environment(self):
        with patch.object(
            github_sync,
            "dotenv_values",
            return_value={"GITHUB_TOKEN": "", "GITHUB_REPO_URL": ""},
        ), patch.dict(os.environ, {"GITHUB_TOKEN": "", "GITHUB_REPO_URL": ""}, clear=False):
            with self.assertRaises(RuntimeError) as ctx:
                github_sync.load_env()
        self.assertIn("GITHUB_TOKEN", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
