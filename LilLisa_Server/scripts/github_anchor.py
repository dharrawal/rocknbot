"""
github_anchor.py
====================================
Builds GitHub anchor links for techsupport_qa_pairs.md headings (e.g.
"## Zookeeper GC Logging Setup in 7.3.X" -> ".../techsupport_qa_pairs.md#zookeeper-gc-logging-setup-in-73x"),
and the slug algorithm those links depend on. Used at ingest time
(techsupport_qa_ingest.py, techsupport_contextual_reembed.py) to set
node.metadata["github_url"], and by the one-time backfill script
(backfill_github_urls.py) for entries added before this existed.

Slug algorithm mirrors GitHub's own markdown heading-to-anchor renderer (the
`github-slugger` npm package, which GitHub uses internally) -- see
github_slug()'s docstring for the exact rules. This needs to be precise: a
wrong slug produces a link that silently resolves to the top of the file
instead of the heading, not an error, so there's no way to detect a wrong
slug except by getting the algorithm right in the first place.

Duplicate headings: GitHub numbers repeated headings within one file in
document order ("foo", "foo-1", "foo-2", ...) -- see GithubAnchorSlugger,
which must be fed every heading in the file in top-to-bottom order for its
numbering to come out right. compute_github_urls_for_titles() below is the
one entry point everything else should use for this reason -- it always
takes the FULL, in-order title list, never a single title in isolation.

Org/repo for the URL are parsed from GITHUB_REPO_URL in
scripts/env/github_push.env (not hardcoded) -- see parse_org_repo().

Scope note (verified, not guessed): this only replicates GitHub's stripped
character set for ASCII text. Verified two ways -- (1) decoded directly from
the ASCII prefix of github-slugger's actual strip regex, and (2) cross-checked
against facebook/react's live, publicly rendered README.md heading anchors
(e.g. "Code of Conduct" -> "code-of-conduct", "Good First Issues" ->
"good-first-issues"), all exact matches. GitHub's real regex additionally
strips a large range of non-ASCII punctuation (confirmed against the same
README: "React \xb7 [badges]" rendered as anchor id "react------", i.e. the
middle-dot character "\xb7" was stripped, which this module's ASCII-only
_DISALLOWED_CHARS_PATTERN would NOT strip) -- deliberately not replicated
here, since techsupport titles are DSPy-generated short plain-English
technical phrases (see techsupport_qa_ingest.GenerateTechsupportTitle) with
no realistic path to containing such characters, and guessing at the exact
extended-Unicode ranges from memory risked introducing new, unverifiable
bugs instead of the "close enough" this module is meant to avoid. If a title
ever DOES contain such a character, its slug (and only that one entry's
link) could be wrong.
"""

import re
import unicodedata
from pathlib import Path
from typing import Dict, List
from urllib.parse import urlsplit

from dotenv import dotenv_values

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH = SCRIPT_DIR / "env" / "github_push.env"

# The filename as it exists at the root of the pushed repo (github_sync.py
# copies techsupport_qa_pairs.md there directly, no subfolder).
MARKDOWN_FILENAME_IN_REPO = "techsupport_qa_pairs.md"

_COMBINING_MARKS_PATTERN = re.compile("[\u0300-\u036f]")
# ASCII chars GitHub's slugger strips (control chars plus most ASCII
# punctuation) -- but NOT '-' (0x2d) or '_' (0x5f), which survive untouched.
# Decoded from the ASCII prefix of github-slugger's actual strip regex:
# controls, then '!'-',' (! " # $ % & ' ( ) * + ,), '.', '/', ':'-'@'
# (: ; < = > ? @), '['-'^' ([ \ ] ^), '`', and '{'-DEL ({ | } ~ DEL).
_DISALLOWED_CHARS_PATTERN = re.compile(r"[\x00-\x1f\x21-\x2c\x2e\x2f\x3a-\x40\x5b-\x5e\x60\x7b-\x7f]")


def github_slug(heading_text: str) -> str:
    """Slugify a single heading the way GitHub's renderer does, WITHOUT
    duplicate-suffix handling -- see GithubAnchorSlugger for that, since
    duplicates require tracking state across every heading in the file, not
    just the one being slugged.

    Algorithm: Unicode-normalize (NFKD) and drop combining marks (so
    accented characters degrade to their plain-ASCII base letter, e.g.
    "é" -> "e"), lowercase, strip the ASCII punctuation/control characters
    GitHub's slugger strips (notably NOT '-' or '_', which survive as-is),
    then replace each literal space with a hyphen individually -- runs of
    multiple spaces become runs of multiple hyphens, NOT collapsed to one,
    matching GitHub's real (if slightly odd) behavior, e.g.
    "Foo & Bar" -> "foo--bar" (the "&" is removed, leaving two spaces).
    """
    normalized = unicodedata.normalize("NFKD", heading_text)
    without_marks = _COMBINING_MARKS_PATTERN.sub("", normalized)
    lowered = without_marks.lower()
    stripped = _DISALLOWED_CHARS_PATTERN.sub("", lowered)
    return stripped.replace(" ", "-")


class GithubAnchorSlugger:
    """Stateful slugger mirroring the `github-slugger` package's duplicate
    handling exactly: the first heading with a given base slug keeps it
    as-is; the 2nd, 3rd, ... heading sharing that same base slug get
    '-1', '-2', ... appended. Must be fed headings in the same top-to-bottom
    order they appear in the file for the numbering to match GitHub's
    actual rendering of that file.
    """

    def __init__(self) -> None:
        self._occurrences: Dict[str, int] = {}

    def slug(self, heading_text: str) -> str:
        base = github_slug(heading_text)
        result = base
        while result in self._occurrences:
            self._occurrences[base] += 1
            result = f"{base}-{self._occurrences[base]}"
        self._occurrences[result] = 0
        return result


def parse_org_repo(repo_url: str) -> str:
    """Extracts 'org/repo' from an https://github.com/<org>/<repo>[.git] URL."""
    path = urlsplit(repo_url).path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return path


def load_github_repo_url() -> str:
    """Reads GITHUB_REPO_URL from scripts/env/github_push.env -- the same
    file github_sync.py reads GITHUB_TOKEN/GITHUB_REPO_URL from (only the
    repo URL is needed here; no token, since this only builds link text)."""
    env = dict(dotenv_values(str(ENV_PATH)))
    repo_url = env.get("GITHUB_REPO_URL")
    if not repo_url:
        raise RuntimeError(f"GITHUB_REPO_URL not found in {ENV_PATH}")
    return repo_url


def compute_github_urls_for_titles(titles_in_order: List[str]) -> List[str]:
    """The one entry point ingest/backfill/reembed code should use: given
    EVERY heading title in techsupport_qa_pairs.md, in file order, returns
    the parallel list of full GitHub anchor URLs
    (".../blob/main/techsupport_qa_pairs.md#{slug}"), with duplicate-heading
    numbering resolved correctly per GithubAnchorSlugger. Raises if
    GITHUB_REPO_URL isn't configured -- callers that consider a GitHub link
    optional (e.g. ingest) should catch and fall back to no link rather than
    fail their whole operation over this.
    """
    org_repo = parse_org_repo(load_github_repo_url())
    slugger = GithubAnchorSlugger()
    return [
        f"https://github.com/{org_repo}/blob/main/{MARKDOWN_FILENAME_IN_REPO}#{slugger.slug(title)}"
        for title in titles_in_order
    ]
