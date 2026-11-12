"""WP1 gitsnap: private snapshots, fingerprints, diffs. The user's .git must never be written."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402,F401
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "wp1"))

from wp1helpers import EnvCase, git, write  # noqa: E402

from vlooplib import gitsnap  # noqa: E402


def git_state(root: Path) -> tuple:
    """(object count, index mtime, refs listing) of the user's .git."""
    objs = 0
    for dirpath, _, files in os.walk(root / ".git" / "objects"):
        objs += len(files)
    idx = root / ".git" / "index"
    return objs, idx.stat().st_mtime_ns if idx.exists() else None, git(root, "for-each-ref")


class GitsnapTest(EnvCase):
    def setUp(self):
        super().setUp()
        self.root = self.make_repo(files={"a.js": "a\n", "src/b.ts": "b1\nb2\nb3\n", "README.md": "# r\n"})
        self.repo = gitsnap.repo_for(self.root)

    def test_repo_for(self):
        self.assertEqual(self.repo["root"], str(self.root))
        self.assertEqual(len(self.repo["key"]), 12)
        self.assertTrue(self.repo["common_dir"].endswith(".git"))
        sub = gitsnap.repo_for(self.root / "src" / "missing" / "x.ts")
        self.assertEqual(sub["key"], self.repo["key"])
        self.assertIsNone(gitsnap.repo_for(self.tmp / "nogit"))

    def test_dirty_untracked_baseline_and_session_diff(self):
        write(self.root / "a.js", "a dirty before session\n")
        write(self.root / "untracked.txt", "u\n")
        snap = gitsnap.snapshot(self.repo, "s1")
        self.assertEqual(snap["kind"], "snapshot")
        self.assertTrue(gitsnap.tree_exists(self.repo, snap["tree"]))
        files = git(self.root, "ls-tree", "-r", "--name-only", snap["tree"],
                    env={**os.environ, **gitsnap.git_env(self.repo)}).split()
        self.assertIn("untracked.txt", files)
        # session edits
        write(self.root / "src/b.ts", "b1\nB2\nb3\n")
        snap2 = gitsnap.snapshot(self.repo, "s1")
        rows = gitsnap.diff_numstat(self.repo, snap["tree"], snap2["tree"])
        self.assertEqual([r["path"] for r in rows], ["src/b.ts"])  # pre-session dirt is not in the diff
        self.assertEqual((rows[0]["added"], rows[0]["removed"], rows[0]["status"]), (1, 1, "M"))
        patch = gitsnap.diff_patch(self.repo, snap["tree"], snap2["tree"])
        self.assertIn("+B2", patch)
        self.assertNotIn("dirty", patch)

    def test_user_git_untouched(self):
        write(self.root / "a.js", "x\n")
        write(self.root / "new.txt", "n\n")
        status_before = git(self.root, "status", "--porcelain")
        before = git_state(self.root)
        snap = gitsnap.snapshot(self.repo, "s1")
        self.assertEqual(snap["kind"], "snapshot")
        gitsnap.fingerprint_tree(self.repo, snap["tree"], ["a.js"])
        gitsnap.diff_numstat(self.repo, gitsnap.head_tree(self.repo), snap["tree"])
        self.assertEqual(git_state(self.root), before)
        self.assertEqual(git(self.root, "status", "--porcelain"), status_before)
        self.assertFalse((self.root / ".git" / "index.lock").exists())

    def test_worktree(self):
        wt = self.tmp / "wt"
        git(self.root, "worktree", "add", "-q", "-b", "feat", str(wt))
        r = gitsnap.repo_for(wt)
        self.assertEqual(r["root"], os.path.realpath(wt))
        self.assertEqual(os.path.realpath(r["common_dir"]), os.path.realpath(self.root / ".git"))
        base = gitsnap.snapshot(r, "s1")
        self.assertEqual(base["kind"], "snapshot")
        write(wt / "a.js", "a\nwt\n")
        snap = gitsnap.snapshot(r, "s1")
        self.assertEqual([x["path"] for x in gitsnap.diff_numstat(r, base["tree"], snap["tree"])], ["a.js"])
        self.assertNotEqual(gitsnap.fingerprint_worktree(r, ["a.js"]),
                            gitsnap.fingerprint_tree(r, base["tree"], ["a.js"]))

    def test_head_fallback_on_timeout(self):
        snap = gitsnap.snapshot(self.repo, "s1", timeout=0.0)
        self.assertEqual(snap["kind"], "head-fallback")
        self.assertEqual(snap["tree"], gitsnap.head_tree(self.repo))

    def test_empty_repo(self):
        root = self.tmp / "empty"
        root.mkdir()
        git(root, "init", "-q", "-b", "master")
        r = gitsnap.repo_for(root)
        self.assertIsNone(gitsnap.head_tree(r))
        snap = gitsnap.snapshot(r, "s1", timeout=0.0)
        self.assertEqual(snap, {"tree": gitsnap.EMPTY_TREE, "kind": "empty", "ms": snap["ms"]})
        write(root / "x.js", "x\n")
        self.assertEqual(gitsnap.fingerprint_tree(r, gitsnap.EMPTY_TREE, ["x.js"]),
                         gitsnap.fingerprint_tree(r, gitsnap.EMPTY_TREE, ["x.js"]))
        self.assertNotEqual(gitsnap.fingerprint_worktree(r, ["x.js"]),
                            gitsnap.fingerprint_tree(r, gitsnap.EMPTY_TREE, ["x.js"]))
        snap = gitsnap.snapshot(r, "s1")  # a real snapshot works without commits too
        self.assertEqual(snap["kind"], "snapshot")
        self.assertEqual([x["path"] for x in gitsnap.diff_numstat(r, gitsnap.EMPTY_TREE, snap["tree"])], ["x.js"])

    def test_fingerprints(self):
        base = gitsnap.snapshot(self.repo, "s1")["tree"]
        rels = ["a.js", "src/b.ts", "gone.js"]
        fp1 = gitsnap.fingerprint_worktree(self.repo, rels)
        self.assertEqual(fp1, gitsnap.fingerprint_worktree(self.repo, list(reversed(rels))))  # stable
        self.assertEqual(fp1, gitsnap.fingerprint_tree(self.repo, base, rels))
        write(self.root / "a.js", "a\nmore\n")
        fp2 = gitsnap.fingerprint_worktree(self.repo, rels)
        self.assertNotEqual(fp1, fp2)  # unstable on change
        write(self.root / "a.js", "a\n")  # revert → equals baseline
        self.assertEqual(gitsnap.fingerprint_worktree(self.repo, rels), gitsnap.fingerprint_tree(self.repo, base, rels))
        (self.root / "a.js").unlink()  # deleted → "-"
        self.assertNotEqual(gitsnap.fingerprint_worktree(self.repo, rels), fp1)

    def test_rename_numstat(self):
        base = gitsnap.snapshot(self.repo, "s1")["tree"]
        git(self.root, "mv", "src/b.ts", "src/c.ts")
        git(self.root, "reset", "-q")  # keep the user's index as it was (rename only in the work tree)
        snap = gitsnap.snapshot(self.repo, "s1")["tree"]
        rows = gitsnap.diff_numstat(self.repo, base, snap)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["status"], rows[0]["path"], rows[0]["old_path"]), ("R", "src/c.ts", "src/b.ts"))

    def test_binary_and_paths_filter(self):
        base = gitsnap.snapshot(self.repo, "s1")["tree"]
        (self.root / "img.png").write_bytes(b"\x89PNG\x00\x01\x02")
        write(self.root / "a.js", "a\nb\n")
        snap = gitsnap.snapshot(self.repo, "s1")["tree"]
        rows = {r["path"]: r for r in gitsnap.diff_numstat(self.repo, base, snap)}
        self.assertTrue(rows["img.png"]["binary"])
        self.assertEqual(rows["img.png"]["status"], "A")
        only = gitsnap.diff_numstat(self.repo, base, snap, paths=["a.js"])
        self.assertEqual([r["path"] for r in only], ["a.js"])

    def test_diff_hunks(self):
        base = gitsnap.snapshot(self.repo, "s1")["tree"]
        write(self.root / "src/b.ts", "b1\nb2\nNEW\nb3\nTAIL1\nTAIL2\n")
        (self.root / "a.js").unlink()
        snap = gitsnap.snapshot(self.repo, "s1")["tree"]
        h = gitsnap.diff_hunks(self.repo, base, snap)
        self.assertEqual(h["src/b.ts"], [[3, 1], [5, 2]])
        self.assertNotIn("a.js", h)

    def test_whitespace_only(self):
        base = gitsnap.snapshot(self.repo, "s1")["tree"]
        self.assertFalse(gitsnap.whitespace_only(self.repo, base, base))
        write(self.root / "src/b.ts", "b1\n  b2   \nb3\n")
        ws = gitsnap.snapshot(self.repo, "s1")["tree"]
        self.assertTrue(gitsnap.whitespace_only(self.repo, base, ws))
        write(self.root / "src/b.ts", "b1\nb2x\nb3\n")
        real = gitsnap.snapshot(self.repo, "s1")["tree"]
        self.assertFalse(gitsnap.whitespace_only(self.repo, base, real))

    def test_misc_refs(self):
        head = gitsnap.resolve_commit(self.repo, "HEAD")
        self.assertTrue(head)
        self.assertEqual(gitsnap.commit_tree(self.repo, head), gitsnap.head_tree(self.repo))
        self.assertEqual(gitsnap.current_branch(self.repo), "master")
        self.assertEqual(gitsnap.default_branch(self.repo), "master")
        self.assertEqual(gitsnap.merge_base(self.repo, "HEAD", "master"), head)
        write(self.root / ".gitignore", "dist/\n")
        self.assertTrue(gitsnap.is_ignored(self.repo, "dist/x.js"))
        self.assertFalse(gitsnap.is_ignored(self.repo, "src/x.js"))
        self.assertEqual(gitsnap.file_line_count(str(self.root), "src/b.ts"), 3)
        self.assertIsNone(gitsnap.file_line_count(str(self.root), "nope"))
        self.assertTrue(gitsnap.tree_exists(self.repo, gitsnap.EMPTY_TREE))
        self.assertFalse(gitsnap.tree_exists(self.repo, "0" * 40))


if __name__ == "__main__":
    unittest.main()
