"""WP1 guards (§2.12): read-only reviewer, QA that never modifies the project."""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402,F401
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "wp1"))

from wp1helpers import EnvCase  # noqa: E402

from vlooplib import guard  # noqa: E402


class GuardTest(EnvCase):
    def setUp(self):
        super().setUp()
        self.root = self.make_repo(files={"a.ts": "a\n", "src/a.ts": "x\n"})
        self.tmpdir = self.tmp / "tmpdir"
        self.tmpdir.mkdir()
        os.environ["TMPDIR"] = str(self.tmpdir)
        self.runs = self.state / "runs"

    def bash(self, role, cmd):
        return guard.check(role, {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(self.root),
                                  "agent_type": f"verify-{role}"})

    def write(self, role, path, tool="Write"):
        return guard.check(role, {"tool_name": tool, "tool_input": {"file_path": str(path)}, "cwd": str(self.root)})

    def decision(self, out):
        return out["hookSpecificOutput"]["permissionDecision"] if out else None

    # ------------------------------------------------------------------ reviewer

    def test_reviewer_allow(self):
        for cmd in ("git diff HEAD -- a.ts", "rg foo src | head -20", "sed -n 1,20p a.ts",
                    "git log --oneline -5 2>/dev/null", "git show HEAD:a.ts | wc -l", "cat a.ts 2>&1 | nl",
                    "cd src && ls -la", "git diff $(git merge-base HEAD master) -- a.ts",
                    "FOO=1 timeout 5 git status", "find . -name '*.ts' -type f", "git branch --show-current",
                    "awk '{print $1}' a.ts", "jq . x.json >/dev/null", "echo '>' | cat", "git config --get user.name",
                    "grep -rn 'a && b' src"):
            out = self.bash("reviewer", cmd)
            self.assertEqual(self.decision(out), "allow", cmd)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecisionReason"], "reviewer guard: read-only")
        out = self.write("reviewer", self.runs / "r1" / "attempt-1" / "reviewer.json")
        self.assertEqual(self.decision(out), "allow")

    def test_reviewer_deny(self):
        for cmd, why in (("git checkout .", "git checkout"), ("echo x > f", "redirection"),
                         ("sed -i s/a/b/ f", "sed -i"), ("rm a", "rm not allowed"), ("npm test", "npm not allowed"),
                         ("find . -delete", "-delete"), ("cat a.ts | tee b", "tee"), ("git branch new", "git branch"),
                         ("sed 1p a.ts", "requires -n"), ("awk '{system(\"rm x\")}' a", "awk"),
                         ("git diff --output=x.patch", "--output"), ("ls; python3 -c 'x'", "python3"),
                         ("echo $(rm -rf x)", "rm"), ("echo 'unbalanced", "unparseable"),
                         ("git log >> log.txt", "redirection")):
            out = self.bash("reviewer", cmd)
            self.assertEqual(self.decision(out), "deny", cmd)
            reason = out["hookSpecificOutput"]["permissionDecisionReason"]
            self.assertTrue(reason.startswith("reviewer guard: "), reason)
            self.assertIn(why, reason, cmd)
        out = self.write("reviewer", self.root / "a.ts", tool="Edit")
        self.assertEqual(self.decision(out), "deny")
        self.assertIn("write only your verdict file under", out["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertIsNone(guard.check("reviewer", {"tool_name": "Read", "tool_input": {"file_path": "/x"}}))

    # ------------------------------------------------------------------ qa

    def test_qa_deny(self):
        for cmd in ("npm install x", "git stash", "sed -i '' s/a/b/ src/a.ts", "rm src/a.ts", "echo x > src/a.ts",
                    "npx jest -u", "npx eslint --fix .", "npx prettier --write .", "git commit -am x",
                    "git branch -D old", "git config user.name x", "yarn add left-pad", "pnpm install",
                    "pip install requests", "poetry lock", "npx npm install x", "npm audit fix",
                    "perl -pi -e s/a/b/ src/a.ts", "cp /tmp/x src/a.ts", "mv src/a.ts /tmp/a.ts",
                    "touch newfile", "cat x | tee src/out.txt", "npx nx g lib foo", "npx prisma migrate dev",
                    "npx prisma db push", "npx vitest run --update-snapshots", "npx nx lint app --fix",
                    "bash -c 'rm src/a.ts'", "cd src && rm a.ts", "dd if=/dev/zero of=src/a.ts count=1"):
            out = self.bash("qa", cmd)
            self.assertEqual(self.decision(out), "deny", cmd)
            reason = out["hookSpecificOutput"]["permissionDecisionReason"]
            self.assertTrue(reason.startswith("qa guard: "), reason)
            self.assertIn("QA must not modify the project", reason)
        out = self.write("qa", self.root / "src" / "a.ts", tool="Edit")
        self.assertEqual(self.decision(out), "deny")
        self.assertIn("QA never edits project files", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_qa_no_output(self):
        for cmd in ("npm ci", "npx jest --findRelatedTests src/a.ts", "curl -s http://localhost:4317",
                    'rm -rf "$TMPDIR/x"', "mkdir -p /tmp/qa-x", f"echo x > {self.tmpdir}/notes.txt",
                    f"cp src/a.ts {self.tmpdir}/a.ts", "npx tsc --noEmit -p tsconfig.json 2>&1 | tail -5",
                    "git status --porcelain", "git diff", "yarn install --frozen-lockfile",
                    "pnpm install --frozen-lockfile", "npm test", "npx playwright test", "echo 'unbalanced",
                    f"python3 - <<'EOF'\nimport os\nos.system('rm x')\nEOF\n", "npm run build > /dev/null"):
            self.assertIsNone(self.bash("qa", cmd), cmd)
        self.assertIsNone(self.write("qa", self.runs / "r" / "attempt-1" / "qa.json"))
        self.assertIsNone(self.write("qa", self.tmpdir / "x.txt"))
        self.assertIsNone(self.write("qa", "/tmp/x.txt"))

    # ------------------------------------------------------------------ cli / hook routing

    def test_cli_guard_and_hook_routing(self):
        payload = {"session_id": "g1", "cwd": str(self.root), "tool_name": "Bash", "agent_type": "verify-reviewer",
                   "hook_event_name": "PreToolUse", "tool_input": {"command": "rm -rf src"}}
        p = self.vloop("guard", "reviewer", stdin=json.dumps(payload))
        self.assertEqual(p.returncode, 0)
        self.assertEqual(json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        out = self.hook_json("pre-tool", payload)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")
        payload["agent_type"] = "verify-qa"
        payload["tool_input"]["command"] = "npm ci"
        self.assertEqual(self.hook("pre-tool", payload).stdout, "")
        self.assertIsNone(self.session("g1"))

    def test_namespaced_agents_and_env_role(self):
        payload = {"session_id": "g2", "cwd": str(self.root), "tool_name": "Write", "hook_event_name": "PreToolUse",
                   "agent_type": "agent-verify-loop:verify-reviewer", "tool_input": {"file_path": str(self.root / "a.ts")}}
        self.assertEqual(self.decision(self.hook_json("pre-tool", payload)), "deny")
        payload["agent_type"] = "agent-verify-loop:verify-qa"
        self.assertEqual(self.decision(self.hook_json("pre-tool", payload)), "deny")
        payload["tool_input"]["file_path"] = str(self.tmpdir / "x.txt")
        self.assertEqual(self.hook("pre-tool", payload).stdout, "")
        payload.pop("agent_type")
        payload["tool_input"]["file_path"] = str(self.root / "a.ts")
        out = self.hook_json("pre-tool", payload, env={"VLOOP_GUARD_ROLE": "reviewer", "VERIFY_LOOP_MODE": "off"})
        self.assertEqual(self.decision(out), "deny")
        self.assertIsNone(self.session("g2"))
        payload["agent_type"] = "other-plugin:verify-reviewer-ish"
        self.assertEqual(self.hook("pre-tool", payload).stdout, "")

    def test_apply_patch(self):
        def patch(*paths, tool="apply_patch"):
            body = "".join(f"*** Add File: {p}\n+x\n" for p in paths)
            return {"tool_name": tool, "cwd": str(self.root),
                    "tool_input": {"command": f"*** Begin Patch\n{body}*** End Patch\n"}}
        self.assertEqual(self.decision(guard.check("reviewer", patch("a.ts"))), "deny")
        self.assertEqual(self.decision(guard.check("reviewer", patch(str(self.runs / "r" / "v.json")))), "allow")
        self.assertEqual(self.decision(guard.check("reviewer", patch(str(self.runs / "v.json"), "src/a.ts"))), "deny")
        self.assertEqual(self.decision(guard.check("qa", patch("src/a.ts", tool="Edit"))), "deny")
        self.assertIsNone(guard.check("qa", patch(str(self.tmpdir / "s.txt"), str(self.runs / "q.json"))))
        self.assertEqual(self.decision(guard.check("qa", patch(str(self.tmpdir / "s.txt"), "../escape.ts"))), "deny")
        self.assertEqual(self.decision(guard.check("qa", {"tool_name": "apply_patch", "cwd": str(self.root),
                                                          "tool_input": {"command": "*** Begin Patch\n"}})), "deny")
        heredoc = {"tool_name": "Bash", "cwd": str(self.root), "tool_input": {
            "command": ["bash", "-lc", "apply_patch <<'EOF'\n*** Begin Patch\n*** Update File: a.ts\n@@\n-a\n+b\n"
                                       "*** End Patch\nEOF\n"]}}
        self.assertEqual(self.decision(guard.check("qa", heredoc)), "deny")
        self.assertEqual(self.decision(guard.check("reviewer", heredoc)), "deny")

    def test_shell_apply_patch_with_trailing_command_is_checked_as_bash(self):
        verdict = str(self.runs / "r" / "v.json")
        pure = f"apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: {verdict}\n+x\n*** End Patch\nEOF\n"

        def shell(cmd):
            return {"tool_name": "Bash", "cwd": str(self.root), "tool_input": {"command": ["bash", "-lc", cmd]}}
        self.assertEqual(self.decision(guard.check("reviewer", shell(pure))), "allow")
        bare = f"apply_patch\n*** Begin Patch\n*** Add File: {verdict}\n+x\nrm -rf src\n*** End Patch\n"
        unquoted = f"apply_patch <<EOF\n*** Begin Patch\n*** Add File: {verdict}\n+$(rm -rf src)\n*** End Patch\nEOF\n"
        for cmd in (pure + "rm -rf src", pure.replace("<<'EOF'", "<<'EOF' && rm -rf src", 1), bare, unquoted):
            self.assertIsNone(guard.patch_paths(shell(cmd)))
            self.assertEqual(self.decision(guard.check("reviewer", shell(cmd))), "deny")
        for cmd in (pure + "rm -rf src", pure.replace("<<'EOF'", "<<'EOF' && rm -rf src", 1)):
            self.assertEqual(self.decision(guard.check("qa", shell(cmd))), "deny")
        cr = pure.replace("<<'EOF'\n", "<<'EOF'\r $(touch marker)\n", 1)
        self.assertIsNone(guard.patch_paths(shell(cr)))
        self.assertEqual(self.decision(guard.check("reviewer", shell(cr))), "deny")

    def test_embedded_shell_patch_targets_are_checked(self):
        def shell(cmd):
            return {"tool_name": "Bash", "cwd": str(self.root), "tool_input": {"command": ["bash", "-lc", cmd]}}
        body = "*** Begin Patch\n*** Add File: {p}\n+x\n*** End Patch\n"
        for cmd in (f"apply_patch <<EOF\n{body.format(p='src/a.ts')}EOF\n",
                    f"cd src && apply_patch <<'EOF'\n{body.format(p='a.ts')}EOF\n",
                    f"apply_patch\n{body.format(p='src/a.ts')}"):
            self.assertEqual(self.decision(guard.check("qa", shell(cmd))), "deny")
            self.assertEqual(self.decision(guard.check("reviewer", shell(cmd))), "deny")
        scratch = f"apply_patch <<EOF\n{body.format(p=str(self.tmpdir / 's.txt'))}EOF\n"
        self.assertIsNone(guard.check("qa", shell(scratch)))

    def test_embedded_patch_reads_every_heredoc_and_distrusts_cd(self):
        def shell(cmd):
            return {"tool_name": "Bash", "cwd": str(self.root), "tool_input": {"command": ["bash", "-lc", cmd]}}
        body = "*** Begin Patch\n*** Add File: {p}\n+x\n*** End Patch\n"
        two = (f"apply_patch <<'A'\n{body.format(p=str(self.tmpdir / 's.txt'))}A\n"
               f"apply_patch <<'B'\n{body.format(p='src/a.ts')}B\n")
        self.assertEqual(self.decision(guard.check("qa", shell(two))), "deny")
        cd_rel = f"cd {self.tmpdir} && apply_patch <<EOF\n{body.format(p='s.txt')}EOF\n"
        self.assertEqual(self.decision(guard.check("qa", shell(cd_rel))), "deny")
        cd_abs = f"cd {self.tmpdir} && apply_patch <<EOF\n{body.format(p=str(self.tmpdir / 's.txt'))}EOF\n"
        self.assertIsNone(guard.check("qa", shell(cd_abs)))
        printf = "printf '*** Begin Patch\\n*** Add File: src/a.ts\\n+x\\n*** End Patch\\n' | apply_patch"
        self.assertEqual(self.decision(guard.check("qa", shell(printf))), "deny")
        heredoc = "apply_patch <<EOF\n" + body.format(p="src/a.ts") + "EOF\n"
        for cmd in ("bash -c \"apply_patch <<'EOF'\n" + body.format(p="src/a.ts") + "EOF\"",
                    "FOO=1 sudo /usr/local/bin/" + heredoc, "for f in x; do " + heredoc + "done",
                    "if true; then " + heredoc + "fi", "! " + heredoc, "\\" + heredoc, "env -i " + heredoc,
                    "timeout -s KILL 5 " + heredoc, "/usr/bin/env " + heredoc, "nice -n 5 " + heredoc,
                    "echo $(" + heredoc + ")", "rg x src; " + heredoc, "rg -n apply_patch src && " + heredoc):
            self.assertEqual(self.decision(guard.check("qa", shell(cmd))), "deny", cmd)
        for cmd in ("rg apply_pat''ch src; " + heredoc, "apply_pat''ch <<EOF\n" + body.format(p="src/a.ts") + "EOF\n",
                    "printf '*** Add File: src/a.ts\\n+x\\n*** Add File: " + str(self.tmpdir / "s.txt")
                    + "\\n+y\\n*** End Patch\\n' | apply_patch"):
            self.assertEqual(self.decision(guard.check("qa", shell(cmd))), "deny", cmd)
        tmp_cwd = {"tool_name": "Bash", "cwd": str(self.tmpdir), "tool_input": {
            "command": ["bash", "-lc", f"env -C {self.root} " + heredoc]}}
        self.assertEqual(self.decision(guard.check("qa", tmp_cwd)), "deny")
        self.assertEqual(self.decision(guard.check("reviewer", shell("rg --pre sh foo README.md"))), "deny")
        self.assertEqual(self.decision(guard.check("reviewer", shell("rg --pre-glob '*.gz' foo"))), "allow")

    def test_search_mentioning_apply_patch_is_not_a_patch(self):
        def shell(cmd):
            return {"tool_name": "Bash", "cwd": str(self.root), "tool_input": {"command": ["bash", "-lc", cmd]}}
        for cmd in ("rg -n 'apply_patch' src; rg -n '\\*\\*\\* Begin Patch' src",
                    "grep -rn \"*** Add File:\" . && grep -rn apply_patch ."):
            self.assertIsNone(guard.embedded_patch_paths(shell(cmd)))
            self.assertEqual(self.decision(guard.check("reviewer", shell(cmd))), "allow")
            self.assertIsNone(guard.check("qa", shell(cmd)))

    def test_tokenizer(self):
        segs, subs = guard.segments("A=1 timeout -s KILL 5 git log | head -2 && echo `pwd` 2>&1; cat <<'EOF'\nrm x\nEOF\nls")
        self.assertEqual([s["words"][0] for s in segs], ["git", "head", "echo", "cat", "ls"])
        self.assertEqual(subs, ["pwd"])
        self.assertEqual(segs[2]["redirs"], [(">&", "1")])


if __name__ == "__main__":
    unittest.main()
