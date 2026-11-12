"""Transports: registry, auto/hybrid selection, third-party modules, Herdr command lines and exit codes, headless
argv for Claude and Codex (TOML `-c` values), headless runs with fake binaries, the hybrid loop flow."""
from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "wp2"))
import wp2helpers as h  # noqa: E402
from wp2helpers import VL  # noqa: E402

from helpers import REPO, HermeticCase  # noqa: E402
from vlooplib import config, platforms, transports  # noqa: E402
from vlooplib.platforms import codex_cmds  # noqa: E402
from vlooplib.transports import common, headless, herdr  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
FAKE_CODEX = FIX / "fake-codex.py"
FAKE_CLAUDE = FIX / "wp2" / "fake-claude.py"
FAKE_HERDR = FIX / "wp2" / "fake-herdr-agent"

try:
    import tomllib
except ImportError:
    tomllib = None


def exe(path: Path, text: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def c_values(cmd: list[str]) -> dict:
    """Parse every `-c key=value` of a codex argv as TOML into one dict."""
    def merge(a, b):
        for k, v in b.items():
            if isinstance(v, dict) and isinstance(a.get(k), dict):
                merge(a[k], v)
            else:
                a[k] = v
    out: dict = {}
    for i, a in enumerate(cmd):
        if a == "-c":
            merge(out, tomllib.loads(cmd[i + 1]))
    return out


class RegistryTest(HermeticCase):
    def tearDown(self):
        transports._REGISTRY.pop("ext-file", None)
        super().tearDown()

    def test_builtins(self):
        self.assertEqual({"agent", "headless", "herdr"} - set(transports.names()), set())

    def test_auto_and_herdr_availability(self):
        self.assertEqual(transports.select({}).name, "agent")
        os.environ["HERDR_ENV"] = "1"
        os.environ["PATH"] = str(self.tmp / "nobin")
        self.assertEqual(transports.select({}).name, "agent", "no herdr-agent anywhere")
        os.environ["VERIFY_HERDR_AGENT"] = str(self.tmp / "missing")
        self.assertEqual(transports.select({}).name, "agent", "VERIFY_HERDR_AGENT must exist")
        os.environ["VERIFY_HERDR_AGENT"] = str(FAKE_HERDR)
        self.assertEqual(transports.select({"transport": "auto"}).name, "herdr")
        os.environ["HERDR_AGENT_DEPTH"] = "1"
        self.assertEqual(transports.select({}).name, "agent", "nested Herdr agents use the in-session transport")
        os.environ["HERDR_AGENT_DEPTH"] = "0"
        del os.environ["VERIFY_HERDR_AGENT"]
        exe(self.tmp / "bin" / "herdr-agent")
        os.environ["PATH"] = str(self.tmp / "bin")
        self.assertEqual(transports.select({}).name, "herdr", "found on PATH")
        exe(self.home / ".claude" / "skills" / "herdr-agent" / "bin" / "herdr-agent")
        os.environ["PATH"] = str(self.tmp / "nobin")
        self.assertEqual(transports.select({}).name, "herdr", "found in the herdr-agent skill")
        os.environ["HERDR_ENV"] = "0"
        self.assertEqual(transports.select({}).name, "agent")

    def test_explicit_and_unknown(self):
        self.assertEqual(transports.select({"transport": "headless"}).name, "headless")
        self.assertEqual(transports.select({"transport": "agent"}).name, "agent")
        self.assertEqual(transports.select({"transport": "nope"}).name, "agent")
        self.assertFalse(transports.get("headless").auto)

    def test_hybrid_rule(self):
        sel = transports.select
        self.assertEqual(sel({}, "reviewer").name, "agent")
        self.assertEqual(sel({}, "qa", browser=False).name, "agent")
        self.assertEqual(sel({}, "qa", browser=True).name, "headless")
        self.assertEqual(sel({}, "reviewer", browser=True).name, "agent")
        self.assertEqual(sel({"transport": "headless"}, "reviewer").name, "headless")
        os.environ.update({"HERDR_ENV": "1", "VERIFY_HERDR_AGENT": str(FAKE_HERDR)})
        self.assertEqual(sel({}, "qa", browser=True).name, "herdr", "herdr attaches the MCP config itself")
        self.assertEqual(sel({"transport": "agent"}, "qa", browser=True).name, "headless")

    def test_for_role_uses_the_run_transport(self):
        os.environ.update({"HERDR_ENV": "1", "VERIFY_HERDR_AGENT": str(FAKE_HERDR)})
        ctx = {"transport": "agent"}
        self.assertEqual(transports.for_role(ctx, {}, "reviewer", {"scope": None}).name, "agent")
        self.assertEqual(transports.for_role(ctx, {}, "qa", {"scope": "browser"}).name, "headless")
        self.assertEqual(transports.for_role({}, {}, "qa", {"scope": "browser"}).name, "herdr")

    def test_third_party_module_from_file(self):
        mod = self.tmp / "ext_file.py"
        mod.write_text(textwrap.dedent("""\
            from vlooplib.transports import Transport

            class Ext(Transport):
                name = "ext-file"
                priority = 50
                supports_browser = False

                def available(self, cfg):
                    return bool(cfg.get("ext_ok"))

                def spawn_plan(self, role, ctx, attempt, prompt_path, model, effort):
                    return [f"ext {role}"]

            TRANSPORT = Ext()
            """))
        cfg = {"transport_modules": [str(mod)]}
        self.assertEqual(transports.select(cfg).name, "agent")
        self.assertEqual(transports.select({**cfg, "ext_ok": True}).name, "ext-file")
        self.assertEqual(transports.select({"transport": "ext-file"}, "qa", browser=True).name, "headless")
        self.assertEqual(transports.get("ext-file").spawn_plan("qa", {}, 1, "/p", "m", "e"), ["ext qa"])
        self.assertEqual(transports.load_modules({"transport_modules": [str(self.tmp / "missing.py")]})[0][:1], "/")

    def test_hook_context(self):
        hc = transports.hook_context({"session_id": "me"})
        self.assertEqual((hc.target_sid, hc.is_child, hc.suppress_gate), ("me", False, False))
        os.environ["VLOOP_GUARD_ROLE"] = "qa"
        hc = transports.hook_context({"session_id": "me"})
        self.assertEqual((hc.target_sid, hc.is_child, hc.suppress_gate), ("me", False, True))
        del os.environ["VLOOP_GUARD_ROLE"]
        os.environ.update({"HERDR_AGENT_DEPTH": "1", "HERDR_PARENT_SESSION_ID": "parent"})
        hc = transports.hook_context({"session_id": "child"})
        self.assertEqual((hc.target_sid, hc.is_child, hc.suppress_gate), ("parent", True, True))

    def test_strip_env_union(self):
        keys = transports.strip_env()
        for k in ("HERDR_ENV", "HERDR_PARENT_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "VLOOP_GUARD_ROLE"):
            self.assertIn(k, keys)
        os.environ.update({"HERDR_TAB_ID": "t", "CLAUDE_CODE_SESSION_ID": "s"})
        env = transports.child_env({"X": "1"})
        self.assertNotIn("HERDR_TAB_ID", env)
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", env)
        self.assertEqual(env["X"], "1")


class AgentNamesTest(HermeticCase):
    def test_plain_install(self):
        self.assertEqual(common.agent_names("reviewer"), ("verify-reviewer", "agent-verify-loop:verify-reviewer"))
        self.assertEqual(common.agent_names("retro"), ("vault-keeper", None))
        line = transports.get("agent").spawn_plan("qa", {"run_id": "R", "_cfg": {}}, 2, "/s/q.prompt.md", "haiku",
                                                  "low")
        self.assertEqual(line, ['Agent(subagent_type="verify-qa", model="haiku", description="verify qa #2", '
                                'prompt="Your task file is /s/q.prompt.md. Read it completely and follow it.")'])
        self.assertEqual(transports.get("agent").plan_footer(["reviewer", "qa"]),
                         ['  if a subagent_type above is not in your agent list, use its other form: '
                          '"agent-verify-loop:verify-reviewer", "agent-verify-loop:verify-qa"'])

    def test_plugin_install(self):
        os.environ["CLAUDE_PLUGIN_ROOT"] = str(REPO)
        self.assertEqual(common.agent_name("qa"), "agent-verify-loop:verify-qa")
        line = transports.get("agent").spawn_plan("qa", {"_cfg": {}}, 1, "/s/q.prompt.md", "sonnet", "low")[0]
        self.assertIn('subagent_type="agent-verify-loop:verify-qa"', line)
        self.assertIn('"verify-qa"', transports.get("agent").plan_footer(["qa"])[0])

    def test_plugin_cache_install(self):
        from vlooplib import paths

        skill = self.home / ".claude" / "plugins" / "cache" / "m" / "agent-verify-loop" / "1.0" / "skills" / "verify-loop"
        skill.mkdir(parents=True)
        (skill.parents[1] / ".claude-plugin").mkdir()
        (skill.parents[1] / ".claude-plugin" / "plugin.json").write_text("{}")
        old = paths.skill_dir
        paths.skill_dir = lambda: skill
        try:
            self.assertTrue(common.plugin_installed())
        finally:
            paths.skill_dir = old
        self.assertFalse(common.plugin_installed())

    def test_codex_spawn_instructions(self):
        platforms.set_override("codex")
        line = transports.get("agent").spawn_plan("reviewer", {"_cfg": config.defaults()}, 1, "/s/r.prompt.md",
                                                  "sonnet", "medium")[0]
        self.assertEqual(line, 'spawn the custom agent `verify-reviewer` as a subagent for verify reviewer #1 with '
                               'exactly this message: "Your task file is /s/r.prompt.md. Read it completely and '
                               'follow it." — then wait for it to finish')
        cfg = {"platforms": {"codex": {"models": {"reviewer": "gpt-5.5"}}}}
        line = transports.get("agent").spawn_plan("reviewer", {"_cfg": cfg}, 1, "/s/r.prompt.md", "sonnet", "m")[0]
        self.assertIn("`verify-reviewer` (model gpt-5.5)", line)
        self.assertEqual(transports.get("agent").plan_footer(["reviewer"]), [])
        self.assertEqual(transports.get("agent").label, "Codex subagents")


class HerdrCommandLineTest(h.HermeticCase):
    def setUp(self):
        super().setUp()
        os.environ["VERIFY_HERDR_AGENT"] = str(exe(self.tmp / "x" / "herdr-agent"))
        os.environ["TMPDIR"] = "/private/tmp/"
        self.bin = os.environ["VERIFY_HERDR_AGENT"]
        self.cfg = config.defaults()
        self.run = Path("/s/runs/R")
        self.att = self.run / "attempt-1"

    def test_reviewer(self):
        cmd = herdr.role_cmd("reviewer", run_dir=self.run, att_dir=self.att, repo_root="/repo", model="sonnet",
                             effort="medium", cfg=self.cfg)
        prompt = "Your task file is /s/runs/R/attempt-1/reviewer.prompt.md. Read it completely and follow it."
        self.assertEqual(cmd, [
            self.bin, "run", "verify-review", "--model", "sonnet", "--effort", "medium", "--cwd", "/repo",
            "--timeout", "1200", "--json", "--prompt", prompt, "--", "--agent", "verify-reviewer",
            "--permission-mode", "auto", "--strict-mcp-config", "--allowedTools", "Read", "Grep", "Glob",
            "Bash(git diff *)", "Bash(git log *)", "Bash(git show *)", "Bash(git grep *)", "Bash(git blame *)",
            "Bash(git ls-files *)", "Bash(git rev-parse *)", "Bash(git status *)", "Bash(git cat-file *)",
            "Bash(git merge-base *)", "Bash(rg *)", "Bash(wc *)", "Bash(ls *)", "Bash(head *)", "Bash(tail *)",
            "Bash(sed -n *)", "Read(//s/runs/R/**)", "Edit(//s/runs/R/**)"])

    def test_qa_mcp_config_and_frontmatter(self):
        d = self.tmp / "att"
        d.mkdir()
        (d / "mcp.json").write_text("{}")
        cmd = herdr.role_cmd("qa", run_dir=self.run, att_dir=d, repo_root="/repo", model="sonnet", effort="medium",
                             cfg=self.cfg)
        i = cmd.index("--")
        self.assertEqual(cmd[i + 1:i + 9], ["--agent", "verify-qa", "--permission-mode", "auto", "--strict-mcp-config",
                                            "--mcp-config", str(d / "mcp.json"), "--allowedTools"])
        self.assertEqual(cmd[cmd.index("--timeout") + 1], "1800")
        self.assertEqual(cmd[-4:], ["Read(//s/runs/R/**)", "Edit(//s/runs/R/**)", "Read(//private/tmp/**)",
                                    "Edit(//private/tmp/**)"])
        for t in ("mcp__playwright", "Bash(npm ci *)", "Bash(nohup *)", "Skill", "Write"):
            self.assertIn(t, cmd)
        cmd = herdr.role_cmd("qa", run_dir=self.run, att_dir=d, repo_root="/repo", model="sonnet", effort="medium",
                             cfg={**self.cfg, "qa_mcp_transport": "frontmatter"})
        self.assertNotIn("--strict-mcp-config", cmd)
        self.assertNotIn("--mcp-config", cmd)

    def test_plugin_agent_name(self):
        os.environ["CLAUDE_PLUGIN_ROOT"] = str(REPO)
        cmd = herdr.role_cmd("reviewer", run_dir=self.run, att_dir=self.att, repo_root="/repo", model="sonnet",
                             effort="medium", cfg=self.cfg)
        self.assertEqual(cmd[cmd.index("--agent") + 1], "agent-verify-loop:verify-reviewer")

    def test_retro(self):
        os.environ["VERIFY_VAULT_CLI"] = "/nonexistent/vault"
        cmd = herdr.retro_cmd(run_dir=self.run, cwd="/repo", has_repo=True, cfg=self.cfg)
        self.assertEqual(cmd[:15], [self.bin, "run", "vault-retro", "--model", "sonnet", "--effort", "medium",
                                    "--cwd", "/repo", "--timeout", "1200", "--json", "--prompt",
                                    "Your task file is /s/runs/R/retro/task.md. Read it completely and follow it.",
                                    "--"])
        vr = os.path.expanduser("~/claude-vault")
        self.assertEqual(cmd[-3:], [f"Read(/{vr}/**)", f"Edit(/{vr}/**)", "Read(//s/runs/R/**)"])
        self.assertIn(f"Bash({VL} ledger *)", cmd)

    def test_wait_cmd(self):
        self.assertEqual(herdr.wait_cmd("id1", 30), [self.bin, "wait", "id1", "--close", "--json", "--timeout", "30"])


class HeadlessArgvTest(HermeticCase):
    def setUp(self):
        super().setUp()
        os.environ["TMPDIR"] = str(self.tmp / "t")
        self.run = self.tmp / "runs" / "R"
        self.att = self.run / "attempt-1"
        self.att.mkdir(parents=True)
        self.cfg = config.defaults()

    def mcp(self):
        (self.att / "mcp.json").write_text(json.dumps({"mcpServers": {"playwright": {
            "type": "stdio", "command": "/opt/pw mcp/bin/pw-mcp",
            "args": ["--pointer", str(self.att / "pw-pointer.json"), "quote\"d", "ünï"],
            "env": {"A B": "1"}}}}))

    def test_claude(self):
        self.mcp()
        cmd, stdin = headless.build("qa", platform="claude", att_dir=self.att, run_dir=self.run, model="sonnet",
                                    effort="medium", cfg=self.cfg)
        self.assertIsNone(stdin)
        prompt = f"Your task file is {self.att}/qa.prompt.md. Read it completely and follow it."
        self.assertEqual(cmd[:14], ["claude", "-p", prompt, "--agent", "verify-qa", "--model", "sonnet", "--effort",
                                    "medium", "--output-format", "json", "--permission-mode", "auto",
                                    "--strict-mcp-config"])
        self.assertEqual(cmd[14:16], ["--mcp-config", str(self.att / "mcp.json")])
        self.assertIn("mcp__playwright", cmd)
        self.assertIn(f"Edit(/{self.run}/**)", cmd)
        self.assertEqual(cmd.count(prompt), 1)
        cmd, _ = headless.build("reviewer", platform="claude", att_dir=self.att, run_dir=self.run, model="opus",
                                effort="high", cfg=self.cfg)
        self.assertNotIn("--mcp-config", cmd)
        self.assertNotIn("Write", cmd)

    def test_codex_reviewer(self):
        cmd, stdin = headless.build("reviewer", platform="codex", att_dir=self.att, run_dir=self.run, model=None,
                                    effort="medium", cfg=self.cfg)
        self.assertEqual(cmd[:7], ["codex", "exec", "-", "--json", "-s", "read-only", "--skip-git-repo-check"])
        self.assertEqual(cmd[cmd.index("-o") + 1], str(self.att / "reviewer.final.md"))
        self.assertNotIn("-m", cmd)
        self.assertNotIn("--ephemeral", cmd)
        self.assertIn("You are", stdin)
        self.assertTrue(stdin.rstrip().endswith(
            f"Your task file is {self.att}/reviewer.prompt.md. Read it completely and follow it."))
        self.assertNotIn("---\nname:", stdin)

    @unittest.skipIf(tomllib is None, "tomllib needs Python 3.11")
    def test_codex_qa_values_are_toml(self):
        self.mcp()
        cmd, _ = headless.build("qa", platform="codex", att_dir=self.att, run_dir=self.run, model="gpt-5.5",
                                effort="low", cfg=self.cfg)
        self.assertEqual(cmd[cmd.index("-s") + 1], "workspace-write")
        self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-5.5")
        vals = c_values(cmd)
        self.assertEqual(vals["model_reasoning_effort"], "low")
        self.assertEqual(vals["sandbox_workspace_write"]["writable_roots"], [str(self.run), str(self.tmp / "t")])
        pw = vals["mcp_servers"]["playwright"]
        self.assertEqual(pw["command"], "/opt/pw mcp/bin/pw-mcp")
        self.assertEqual(pw["args"], ["--pointer", str(self.att / "pw-pointer.json"), "quote\"d", "ünï"])
        self.assertEqual(pw["env"], {"A B": "1"})

    @unittest.skipIf(tomllib is None, "tomllib needs Python 3.11")
    def test_toml_value(self):
        for v in ("a\\b\n\t\"x\"", "😀", [1, "x", True], {"k-1": "v", "a.b": [1.5]}, False, 3):
            self.assertEqual(tomllib.loads(f"k={codex_cmds.toml_value(v)}")["k"], v)

    def test_headless_cmd_prefix(self):
        os.environ["VERIFY_HEADLESS_CMD"] = json.dumps(["python3", "/x/fake.py"])
        cmd, _ = headless.build("reviewer", platform="codex", att_dir=self.att, run_dir=self.run, model=None,
                                effort="low", cfg=self.cfg)
        self.assertEqual(cmd[:3], ["python3", "/x/fake.py", "exec"])


class HeadlessRunTest(h.Case):
    """HeadlessTransport.spawn / wait with fake binaries (no loop around it)."""

    def setUp(self):
        super().setUp()
        self.root = self.make_repo()
        self.run = self.tmp / "runs" / "R"
        self.att = self.run / "attempt-1"
        self.att.mkdir(parents=True)
        (self.att / "reviewer.prompt.md").write_text("review")
        self.ctx = {"run_id": "R", "session_id": "parent-sid", "repo": {"root": str(self.root)}}
        self.log = self.tmp / "codex.jsonl"
        self.verdict = self.tmp / "verdict.md"
        self.verdict.write_text("Done.\n```json\n" + json.dumps(h.reviewer_pass()) + "\n```\n")
        os.environ.update({"VERIFY_HEADLESS_CMD": json.dumps([sys.executable, str(FAKE_CODEX)]),
                           "FAKE_CODEX_LOG": str(self.log), "FAKE_CODEX_OUTPUT": str(self.verdict),
                           "HERDR_ENV": "1", "CLAUDE_CODE_SESSION_ID": "parent-sid"})
        self.t = transports.get("headless")

    def spawn(self, cfg=None):
        return self.t.spawn("reviewer", self.ctx, 1, self.att / "reviewer.prompt.md", "sonnet", "medium",
                            cfg or config.defaults())

    def test_codex_done_with_usage_and_env(self):
        platforms.set_override("codex")
        res = self.spawn()
        self.assertEqual(res.status, "done", res.stderr)
        self.assertIn('"verdict": "pass"', res.result)
        self.assertEqual(res.info["usage"]["tokens"], {"input": 200, "cache_write": 0, "cache_read": 1000,
                                                       "output": 90})
        self.assertEqual(res.info["usage"]["reasoning_tokens"], 40)
        self.assertEqual((res.info["session_id"], res.info["model"], res.info["platform"]),
                         ("019a-fake-thread", "inherit", "codex"))
        call = json.loads(self.log.read_text().splitlines()[0])
        self.assertEqual(call["env"]["VLOOP_GUARD_ROLE"], "reviewer")
        self.assertEqual(call["env"]["VLOOP_RUN_DIR"], str(self.run))
        self.assertEqual(call["env"]["VLOOP_PARENT_SESSION_ID"], "parent-sid")
        self.assertIsNone(call["env"]["HERDR_ENV"])
        self.assertIsNone(call["env"]["CLAUDE_CODE_SESSION_ID"])
        self.assertEqual(os.path.realpath(call["cwd"]), str(self.root))
        self.assertIn("verify-loop", call["stdin"].lower())
        self.assertTrue((self.att / "reviewer.headless.stdout").is_file())
        self.assertEqual((self.att / "reviewer.headless.rc").read_text().strip(), "0")

    def test_codex_failure_is_error(self):
        platforms.set_override("codex")
        os.environ["FAKE_CODEX_RC"] = "2"
        res = self.spawn()
        self.assertEqual((res.status, res.rc), ("error", 2))
        self.assertIn("simulated failure", res.stderr)

    def test_timeout_then_wait(self):
        platforms.set_override("codex")
        os.environ["FAKE_CODEX_SLEEP"] = "1.5"
        cfg = config.defaults()
        cfg["timeouts_s"] = {"reviewer": 0.1}
        res = self.spawn(cfg)
        self.assertEqual(res.status, "timeout")
        self.assertTrue(self.t.waitable(res.info))
        self.assertEqual(self.t.where(res.info), f"pid {res.info['pid']}")
        held = dict(headless._procs)
        headless._procs.clear()  # as if `spawn-wait` ran in another process
        res = self.t.wait(json.loads(json.dumps(res.info)), 30)
        self.assertEqual(res.status, "done", res.stderr)
        for p in held.values():
            p.wait()

    def test_claude_done(self):
        os.environ["VERIFY_HEADLESS_CMD"] = json.dumps([sys.executable, str(FAKE_CLAUDE)])
        vf = self.tmp / "v.json"
        vf.write_text(json.dumps(h.reviewer_pass()))
        os.environ["FAKE_CLAUDE_OUTPUT"] = str(vf)
        res = self.spawn()
        self.assertEqual(res.status, "done", res.stderr)
        self.assertEqual(json.loads(res.result)["verdict"], "pass")
        self.assertEqual(res.info["usage"]["model"], "claude-haiku-5-5")
        self.assertEqual(res.info["usage"]["cost_usd"], 0.0042)
        argv = json.loads((self.tmp / "claude-calls.jsonl").read_text().splitlines()[0])["argv"]
        self.assertEqual(argv[0], "-p")
        self.assertTrue(argv[1].startswith("Your task file is "))
        self.assertEqual(argv[2:4], ["--agent", "verify-reviewer"])

    def test_missing_binary_is_error(self):
        os.environ["VERIFY_HEADLESS_CMD"] = json.dumps([str(self.tmp / "nope")])
        res = self.spawn()
        self.assertEqual(res.status, "error")


class ExitCodeTest(h.Case):
    herdr = True

    def setUp(self):
        super().setUp()
        self.root = self.make_repo()
        self.prompt(self.root)
        self.code_change(self.root)
        self.rid, _ = self.begin(self.root)
        self.triage_output("checks")
        self.brief(self.rid)
        self.att = self.run_dir(self.rid) / "attempt-1"

    def agent(self, role):
        return self.ctx(self.rid)["attempts"][0]["agents"][role]

    def test_blocked_with_doc_then_spawn_wait(self):
        self.herdr_role("reviewer", exit=3, verdict=None, screen="Trust this folder?")
        rc, out, _ = self.vl("spawn", "reviewer", "--run", self.rid)
        self.assertEqual(rc, 3)
        self.assertEqual(out.splitlines(), [
            "Trust this folder?",
            f"blocked: answer the dialog in tab verify-review-1a2b3c yourself only if the user tells you to — show this "
            f"to the user; then: {VL} spawn-wait reviewer --run {self.rid}"])
        self.assertEqual((self.agent("reviewer")["status"], self.agent("reviewer")["herdr_id"]),
                         ("blocked", "verify-review-1a2b3c"))
        self.herdr_role("reviewer", wait=True, exit=0, verdict=h.reviewer_pass(), result="done")
        out = self.ok("spawn-wait", "reviewer", "--run", self.rid, "--timeout", "30")
        self.assertTrue(out.startswith("reviewer: PASS · 0 blocking"))
        self.assertEqual(out.splitlines()[-1], "waiting for: qa")
        wait = [c for c in self.herdr_calls() if c[0] == "wait"][-1]
        self.assertEqual(wait, ["wait", "verify-review-1a2b3c", "--close", "--json", "--timeout", "30"])
        self.assertEqual(self.agent("reviewer")["status"], "done")

    def test_blocked_without_doc(self):
        self.herdr_role("qa", exit=3, doc=False, stderr="[herdr-agent] error: folder not trusted: /x")
        rc, out, _ = self.vl("spawn", "qa", "--run", self.rid)
        self.assertEqual(rc, 3)
        self.assertEqual(out.splitlines()[-1], f"blocked: [herdr-agent] error: folder not trusted: /x — show this to "
                                               f"the user; after they fix it: {VL} spawn qa --run {self.rid}")
        self.assertEqual(self.agent("qa")["status"], "blocked")
        rc, _, err = self.vl("spawn-wait", "qa", "--run", self.rid)
        self.assertEqual(rc, 2)
        self.assertIn("no herdr agent recorded for qa", err)

    def test_timeout(self):
        self.herdr_role("reviewer", exit=4, verdict=None)
        rc, out, _ = self.vl("spawn", "reviewer", "--run", self.rid)
        self.assertEqual(rc, 4)
        self.assertEqual(out.strip(), f"timeout: still running in tab verify-review-1a2b3c; later: {VL} spawn-wait "
                                      f"reviewer --run {self.rid}")
        self.assertFalse((self.att / "reviewer.error.json").exists())

    def test_dead_and_no_result(self):
        self.herdr_role("reviewer", exit=5, verdict=None)
        rc, out, _ = self.vl("spawn", "reviewer", "--run", self.rid)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("reviewer: INCONCLUSIVE · agent died"))
        self.assertEqual(json.loads((self.att / "reviewer.error.json").read_text())["status"], "dead")
        self.herdr_role("qa", exit=6, verdict=None)
        rc, out, _ = self.vl("spawn", "qa", "--run", self.rid)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("qa: INCONCLUSIVE · no result"))
        self.assertEqual(out.splitlines()[-1], f"next: {VL} judge --run {self.rid}")
        out = self.ok("judge", "--run", self.rid)
        self.assertTrue(out.startswith("RESULT: INCONCLUSIVE"))
        self.assertIn("review INCONCLUSIVE (agent dead)", out)

    def test_error_exit(self):
        self.herdr_role("reviewer", exit=1, doc=False, stderr="[herdr-agent] error: herdr not running")
        rc, out, _ = self.vl("spawn", "reviewer", "--run", self.rid)
        self.assertEqual(rc, 1)
        err = json.loads((self.att / "reviewer.error.json").read_text())
        self.assertEqual(err, {"status": "error", "detail": "[herdr-agent] error: herdr not running"})

    def test_verdict_only_in_final_message(self):
        self.herdr_role("reviewer", exit=0, verdict=None, result="@verdict", message_verdict=h.reviewer_fail())
        out = self.ok("spawn", "reviewer", "--run", self.rid)
        self.assertTrue(out.startswith("reviewer: FAIL · 1 major"))
        v = json.loads((self.att / "reviewer.json").read_text())
        self.assertEqual((v["run_id"], v["attempt"], v["mode"]), (self.rid, 1, "full"))
        self.assertIn("```json", (self.att / "reviewer.final.md").read_text())

    def test_no_verdict_anywhere(self):
        self.herdr_role("reviewer", exit=0, verdict=None, result="I could not finish.")
        out = self.ok("spawn", "reviewer", "--run", self.rid)
        self.assertTrue(out.startswith("reviewer: INCONCLUSIVE · bad verdict"))
        self.assertEqual(json.loads((self.att / "reviewer.error.json").read_text())["status"], "bad_verdict")

    def test_spawn_command_and_events(self):
        self.herdr_role("qa", exit=0, verdict=h.qa_pass(), result="ok")
        self.ok("spawn", "qa", "--run", self.rid)
        call = self.herdr_calls()[-1]
        self.assertEqual(call[:4], ["run", "verify-qa", "--model", "haiku"])
        self.assertIn(f"Your task file is {self.att}/qa.prompt.md. Read it completely and follow it.", call)
        self.assertNotIn("--mcp-config", call)
        ev = {e["event"]: e["data"] for e in self.events() if e["event"] in ("spawn", "agent_done")}
        self.assertEqual(ev["spawn"], {"role": "qa", "transport": "herdr", "model": "haiku", "effort": "medium",
                                       "herdr_id": None})
        self.assertEqual((ev["agent_done"]["status"], ev["agent_done"]["herdr_id"], ev["agent_done"]["session_id"],
                          ev["agent_done"]["verdict"]), ("done", "verify-qa-1a2b3c", "sess-qa", "pass"))
        self.assertTrue(json.loads((self.att / "integrity.json").read_text())["before"])

    def test_spawn_preconditions(self):
        rc, _, err = self.vl("spawn", "retro", "--run", self.rid)
        self.assertEqual(rc, 2)
        self.assertIn("no retro task", err)
        rc, _, err = self.vl("spawn", "reviewer", "--run", "nope")
        self.assertEqual(rc, 2)

    def test_retro_spawn(self):
        (self.run_dir(self.rid) / "retro").mkdir()
        (self.run_dir(self.rid) / "retro" / "task.md").write_text("retro task")
        self.herdr_role("retro", exit=0, result="Retro recorded: 1 lesson.\n- lesson x")
        out = self.ok("spawn", "retro", "--run", self.rid)
        lines = out.splitlines()
        self.assertRegex(lines[0], r"^retro: done · Retro recorded: 1 lesson\. · \d+s$")
        self.assertEqual(lines[1:], ["Retro recorded: 1 lesson.", "- lesson x"])
        self.assertEqual((self.run_dir(self.rid) / "retro" / "result.md").read_text(),
                         "Retro recorded: 1 lesson.\n- lesson x")
        call = self.herdr_calls()[-1]
        self.assertEqual(call[:3], ["run", "vault-retro", "--model"])
        self.assertEqual(call[call.index("--cwd") + 1], str(self.root))
        names = [e["event"] for e in self.events()]
        self.assertIn("retro_spawn", names)
        done = [e for e in self.events() if e["event"] == "retro_done"][-1]["data"]
        self.assertEqual((done["transport"], done["herdr_id"], done["session_id"], done["status"]),
                         ("herdr", "vault-retro-1a2b3c", "sess-retro", "done"))


class HybridLoopTest(h.Case):
    """Agent transport + browser QA: the reviewer runs in-session, browser QA headless through `vloop spawn qa`."""

    def setUp(self):
        super().setUp()
        self.write_config(qa_browser_consent="always")
        self.root = self.make_repo()
        self.prompt(self.root)
        self.code_change(self.root)
        self.edit(self.root, "src/page.html", "<p>new</p>\n", append=True)
        self.rid, _ = self.begin(self.root, "--app-url", "https://localhost:8100")
        self.triage_output("browser")
        self.out = self.brief(self.rid)
        self.att = self.run_dir(self.rid) / "attempt-1"

    def test_mixed_lines_and_headless_qa(self):
        lines = self.out.splitlines()
        i = lines.index("next (Agent tool + headless, parallel — all calls in ONE message, run_in_background: true):")
        self.assertTrue(lines[i + 1].startswith('  Agent(subagent_type="verify-reviewer", model="sonnet"'))
        self.assertEqual(lines[i + 2], f"  Bash: {VL} spawn qa --run {self.rid}")
        self.assertEqual(lines[i + 3], f"then: {VL} collect --run {self.rid}")
        self.assertIn("agent-verify-loop:verify-reviewer", lines[i + 4])
        agents = self.ctx(self.rid)["attempts"][0]["agents"]
        self.assertEqual(set(agents), {"reviewer"})
        rc, _, err = self.vl("spawn", "reviewer", "--run", self.rid)
        self.assertEqual(rc, 2)
        self.assertIn("this session uses the Agent-tool transport", err)

        vf = self.tmp / "qa.json"
        vf.write_text(json.dumps(h.qa_pass("browser")))
        os.environ.update({"VERIFY_HEADLESS_CMD": json.dumps([sys.executable, str(FAKE_CLAUDE)]),
                           "FAKE_CLAUDE_OUTPUT": str(vf)})
        out = self.ok("spawn", "qa", "--run", self.rid)
        self.assertTrue(out.startswith("qa: PASS"), out)
        qa = self.ctx(self.rid)["attempts"][0]["agents"]["qa"]
        self.assertEqual((qa["transport"], qa["status"], qa["usage"]["cost_usd"]), ("headless", "done", 0.0042))
        argv = json.loads((self.tmp / "claude-calls.jsonl").read_text().splitlines()[-1])["argv"]
        self.assertEqual(argv[argv.index("--mcp-config") + 1], str(self.att / "mcp.json"))
        ev = [e["data"] for e in self.events() if e["event"] == "spawn"][-1]
        self.assertEqual((ev["transport"], ev["pid"]), ("headless", None))

        (self.att / "reviewer.json").write_text(json.dumps(h.reviewer_pass()))
        out = self.ok("collect", "--run", self.rid)
        self.assertEqual(out.splitlines()[-1], f"next: {VL} judge --run {self.rid}")
        out = self.ok("judge", "--run", self.rid)
        self.assertTrue(out.startswith("RESULT: PASS"), out)



class HeadlessAllRolesTest(h.Case):
    def test_plan_lines(self):
        self.write_config(transport="headless")
        root = self.make_repo()
        self.prompt(root)
        self.code_change(root)
        rid, _ = self.begin(root)
        self.triage_output("checks")
        out = self.brief(rid)
        self.assertEqual(self.ctx(rid)["transport"], "headless")
        self.assertEqual(out.splitlines()[-3:], [
            "next (headless, parallel — one Bash call per line in ONE message, run_in_background: true):",
            f"  {VL} spawn reviewer --run {rid}", f"  {VL} spawn qa --run {rid}"])
        self.assertEqual(self.ctx(rid)["attempts"][0]["agents"], {}, "vloop-run roles are recorded at spawn")

if __name__ == "__main__":
    unittest.main()
