"""Codex apply_patch envelope parsing."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402,F401

from vlooplib.platforms import applypatch  # noqa: E402
from vlooplib.platforms.applypatch import PatchOp  # noqa: E402

FULL = """*** Begin Patch
*** Add File: src/new.js
+export const n = 1;
+*** Update File: not/a/header.js
*** Update File: a.js
*** Move to: lib/a.js
@@ def f():
-a
+b
*** Delete File: old.js
*** Update File: b.js
@@
 context
-x
+y
*** End Patch
"""


class ParseTest(unittest.TestCase):
    def test_all_ops(self):
        self.assertEqual(applypatch.parse(FULL), [
            PatchOp("add", "src/new.js"), PatchOp("update", "a.js", "lib/a.js"), PatchOp("delete", "old.js"),
            PatchOp("update", "b.js")])
        self.assertEqual(applypatch.paths(FULL), ["src/new.js", "a.js", "lib/a.js", "old.js", "b.js"])

    def test_crlf_spaces_and_paths_with_spaces(self):
        text = "*** Begin Patch\r\n   *** Add File: docs dir/my file.md   \r\n+x\r\n*** Update File:  a b.js\r\n" \
               "  *** Move to: c d.js \r\n@@\r\n-a\r\n+b\r\n*** End Patch\r\n"
        self.assertEqual(applypatch.parse(text), [PatchOp("add", "docs dir/my file.md"),
                                                  PatchOp("update", "a b.js", "c d.js")])
        self.assertEqual(applypatch.paths("*** Begin Patch\r*** Delete File: x.js\r*** End Patch"), ["x.js"])

    def test_missing_markers(self):
        self.assertEqual(applypatch.paths("*** Update File: a.js\n@@\n-a\n+b\n"), ["a.js"])
        self.assertEqual(applypatch.paths("*** Begin Patch\n*** Delete File: z.js\n"), ["z.js"])

    def test_heredoc_wrapped(self):
        text = "apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: h.js\n+x\n*** End Patch\nEOF\n"
        self.assertEqual(applypatch.paths(text), ["h.js"])
        text = 'cd sub && apply_patch <<"PATCH"\n*** Update File: q.js\n@@\n-a\n+b\nPATCH\necho done\n'
        self.assertEqual(applypatch.paths(text), ["q.js"])
        self.assertEqual(applypatch.paths("apply_patch <<EOF\n*** Delete File: d.js\nEOF"), ["d.js"])

    def test_garbage(self):
        for text in ("", None, "echo hi", "*** Add File:   \n+x", "*** Move to: x.js\n", 42):
            self.assertEqual(applypatch.paths(text), [], text)
        self.assertEqual(applypatch.paths("*** Update File: a.js\n*** Update File: a.js\n"), ["a.js"])

    def test_looks_like_patch(self):
        self.assertTrue(applypatch.looks_like_patch(FULL))
        self.assertTrue(applypatch.looks_like_patch("*** Delete File: x"))
        self.assertFalse(applypatch.looks_like_patch("cat a.js"))
        self.assertFalse(applypatch.looks_like_patch(["*** Begin Patch"]))


if __name__ == "__main__":
    unittest.main()
