"""Tests for the Apple Container adapter using a fake `container` CLI."""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from webchat.sandbox import RunRequest
from webchat.sandbox.apple import AppleContainerRunner


FAKE_CLI = r'''#!{python}
import os, subprocess, sys

args = sys.argv[1:]
if not args or args[0] != "run":
    print("unsupported fake command", file=sys.stderr)
    raise SystemExit(2)
args = args[1:]
volumes = {{}}
env = {{}}
flags = {{"--rm", "--init", "--read-only", "-i"}}
value_opts = {{"--name", "--cap-drop", "--network", "--cpus", "--memory", "--tmpfs", "--volume", "--workdir", "--env"}}
i = 0
while i < len(args):
    arg = args[i]
    if arg in flags:
        i += 1
        continue
    if arg in value_opts:
        value = args[i + 1]
        if arg == "--volume":
            host, guest = value.split(":", 1)
            volumes[guest] = host
        elif arg == "--env":
            key, val = value.split("=", 1)
            env[key] = val
        i += 2
        continue
    if arg.startswith("--"):
        i += 1
        continue
    # image
    image = arg
    command = args[i + 1:]
    break
else:
    raise SystemExit("no image")

def map_path(value):
    for guest, host in sorted(volumes.items(), key=lambda x: -len(x[0])):
        value = value.replace(guest, host)
    return value

command = [map_path(x) for x in command]
if command and command[0] == "python":
    command[0] = sys.executable
env = {{k: map_path(v) for k, v in env.items()}}
env.setdefault("PATH", os.environ.get("PATH", ""))
env.setdefault("HOME", "/tmp")
proc = subprocess.run(
    command,
    input=sys.stdin.read(),
    text=True,
    env=env,
    cwd=map_path("/agent/in"),
    capture_output=True,
)
sys.stdout.write(proc.stdout)
sys.stderr.write(proc.stderr)
raise SystemExit(proc.returncode)
'''


class AppleSandboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="apple-sandbox-test-"))
        self.fake = self.tmp / "container-fake"
        self.fake.write_text(FAKE_CLI.format(python=sys.executable), encoding="utf-8")
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)
        self.settings = SimpleNamespace(
            data_dir=self.tmp / "data",
            apple_container_bin=str(self.fake),
            apple_container_image="python:3.12-slim",
            apple_container_cpus=2,
            apple_container_memory="1G",
            apple_container_network="default",
        )
        self.runner = AppleContainerRunner(self.settings)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_runs_in_container_and_persists_output(self):
        script = """
import json, os, sys
from pathlib import Path
payload = json.load(sys.stdin)
out = Path(os.environ["AGENT_OUTPUT_DIR"])
(out / "answer.txt").write_text("container:" + payload["question"], encoding="utf-8")
print("hello from container")
"""
        req = RunRequest(
            run_id="run_test123",
            files=[{"name": "run.py", "content": script}],
            entrypoint="run.py",
            runtime="python",
            timeout=10,
            question="ping",
            context={},
            agent_name="Container Test",
            agent_description="",
            agent_markdown="# test",
        )
        result = self.runner.run(req)
        self.assertTrue(result.ran, result.reason)
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertIn("hello from container", result.stdout)
        self.assertEqual([a["name"] for a in result.artifacts], ["answer.txt"])
        stored = self.tmp / "data" / "artifacts" / "run_test123" / "answer.txt"
        self.assertEqual(stored.read_text(), "container:ping")

    def test_missing_cli_is_a_clean_failure(self):
        self.runner.binary = str(self.tmp / "missing-container")
        req = RunRequest(
            run_id="run_missing",
            files=[{"name": "run.py", "content": "print('x')"}],
            entrypoint="run.py",
            runtime="python",
            timeout=5,
            question="x",
            context={},
            agent_name="Missing",
            agent_description="",
            agent_markdown="# x",
        )
        result = self.runner.run(req)
        self.assertFalse(result.ran)
        self.assertIn("找不到 Apple Container", result.reason)


if __name__ == "__main__":
    unittest.main()
