"""End-to-end API tests with FastAPI's TestClient (no extra test dependency)."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from webchat.config import ROOT, Settings
from webchat.main import create_app


def parse_sse(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        event = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if data_lines:
            events.append((event, json.loads("\n".join(data_lines))))
    return events


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="webchat-test-"))
        settings = Settings(
            root=ROOT,
            data_dir=cls.tmp,
            db_path=cls.tmp / "webchat.sqlite3",
            kb_path=cls.tmp / "kb.sqlite3",
            kb_config_path=ROOT / "kb.config.json",
            knowledge_dir=ROOT / "knowledge",
            static_dir=ROOT / "static",
            agent_url=None,
            agent_token=None,
            agent_timeout=10,
            session_days=7,
            secure_cookie=False,
            kb_provider="mock",
            seed_demo_users=True,
        )
        cls.app = create_app(settings)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def login(self, client: TestClient, username: str = "demo", password: str = "demo1234"):
        resp = client.post("/api/auth/login", json={"username": username, "password": password})
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()["user"]

    def test_health_and_meta(self):
        with TestClient(self.app) as client:
            health = client.get("/api/health")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["knowledge_bases"]["product"]["documents"], 6)
            self.assertEqual(health.json()["knowledge_bases"]["engineering"]["documents"], 4)
            meta = client.get("/api/meta")
            self.assertEqual(meta.status_code, 200)
            self.assertEqual(meta.json()["agent_engine"], "local")
            self.assertEqual(len(meta.json()["knowledge_bases"]), 4)
            self.assertEqual(meta.json()["departments"], ["产品部", "研发部", "人事部"])

    def test_login_flow_and_bad_password(self):
        with TestClient(self.app) as client:
            bad = client.post("/api/auth/login", json={"username": "demo", "password": "nope"})
            self.assertEqual(bad.status_code, 401)
            user = self.login(client)
            self.assertEqual(user["username"], "demo")
            me = client.get("/api/auth/me")
            self.assertEqual(me.status_code, 200)
            self.assertEqual(me.json()["user"]["id"], user["id"])
            self.assertEqual(client.post("/api/auth/logout").status_code, 200)
            self.assertEqual(client.get("/api/auth/me").status_code, 401)

    def test_chat_persists_and_streams(self):
        with TestClient(self.app) as client:
            self.login(client)
            conv = client.post("/api/conversations", json={"mode": "agent"}).json()["conversation"]
            self.assertEqual(conv["title"], "新的对话")
            self.assertEqual(conv["kb_id"], "product")

            resp = client.post(
                f"/api/conversations/{conv['id']}/messages",
                json={"content": "北极星项目的负责人是谁？", "mode": "agent"},
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            events = parse_sse(resp.text)
            names = [name for name, _ in events]
            self.assertIn("meta", names)
            self.assertIn("status", names)
            self.assertIn("delta", names)
            self.assertIn("done", names)

            done = dict(events)["done"]
            message = done["message"]
            self.assertEqual(message["role"], "assistant")
            self.assertIn("林晓", message["content"])
            self.assertGreater(len(message["citations"]), 0)
            self.assertEqual(message["meta"]["mode"], "agent")
            self.assertGreater(len(message["meta"]["trace"]), 2)
            by_title = {step["title"]: step for step in message["meta"]["trace"]}
            self.assertTrue(by_title["理解问题"]["code"])
            self.assertTrue(by_title["检索知识库"]["logic"])

            history = client.get(f"/api/conversations/{conv['id']}").json()
            self.assertEqual(len(history["messages"]), 2)
            self.assertEqual(history["conversation"]["title"], "北极星项目的负责人是谁？")
            self.assertTrue(history["conversation"]["title"].startswith("北极星"))

    def test_rename_and_delete_conversation(self):
        with TestClient(self.app) as client:
            self.login(client)
            conv = client.post("/api/conversations", json={"mode": "rag"}).json()["conversation"]
            patched = client.patch(
                f"/api/conversations/{conv['id']}", json={"title": "一个更好的标题"}
            )
            self.assertEqual(patched.status_code, 200, patched.text)
            self.assertEqual(patched.json()["conversation"]["title"], "一个更好的标题")
            self.assertEqual(
                client.get(f"/api/conversations/{conv['id']}").json()["conversation"]["title"],
                "一个更好的标题",
            )
            deleted = client.delete(f"/api/conversations/{conv['id']}")
            self.assertEqual(deleted.status_code, 200)
            self.assertEqual(client.get(f"/api/conversations/{conv['id']}").status_code, 404)

    def test_user_isolation(self):
        with TestClient(self.app) as demo:
            self.login(demo)
            conv = demo.post("/api/conversations", json={"mode": "rag"}).json()["conversation"]
            conv_id = conv["id"]
            with TestClient(self.app) as alice:
                self.login(alice, "alice", "alice1234")
                self.assertEqual(alice.get(f"/api/conversations/{conv_id}").status_code, 404)
                self.assertEqual(alice.delete(f"/api/conversations/{conv_id}").status_code, 404)
                self.assertEqual(
                    alice.post(
                        f"/api/conversations/{conv_id}/messages",
                        json={"content": "偷看", "mode": "rag"},
                    ).status_code,
                    404,
                )
                self.assertEqual(alice.get("/api/conversations").json()["conversations"], [])
            self.assertEqual(demo.get(f"/api/conversations/{conv_id}").status_code, 200)

    def test_department_kb_access_and_filtering(self):
        expected = {
            ("demo", "demo1234"): {"product", "common"},
            ("alice", "alice1234"): {"engineering", "common"},
            ("bob", "bob1234"): {"hr", "common"},
        }
        with TestClient(self.app) as client:
            for (username, password), allowed in expected.items():
                client.cookies.clear()
                self.login(client, username, password)
                catalog = client.get("/api/kbs").json()
                ids = {kb["id"] for kb in catalog["knowledge_bases"]}
                self.assertEqual(ids, allowed)
                self.assertIn(catalog["default"], allowed)

            # A user can read the common KB but not another department's KB.
            client.cookies.clear()
            self.login(client, "alice", "alice1234")
            self.assertEqual(client.get("/api/kb/overview?kb=common").status_code, 200)
            self.assertEqual(client.get("/api/kb/overview?kb=hr").status_code, 403)
            self.assertEqual(client.get("/api/kb/overview?kb=product").status_code, 403)

            # Conversations are isolated by both user and KB.
            common_conv = client.post(
                "/api/conversations", json={"mode": "rag", "kb": "common"}
            ).json()["conversation"]
            self.assertEqual(common_conv["kb_id"], "common")
            product_conv = client.post(
                "/api/conversations", json={"mode": "rag", "kb": "product"}
            )
            self.assertEqual(product_conv.status_code, 403)
            engineering = client.get("/api/conversations?kb=engineering").json()
            common = client.get("/api/conversations?kb=common").json()
            self.assertEqual(len(engineering["conversations"]), 0)
            self.assertEqual(len(common["conversations"]), 1)
            self.assertEqual(common["conversations"][0]["id"], common_conv["id"])

    def test_imported_agent_script_execution(self):
        markdown = """---
name: 测试脚本 Agent
description: 用于端到端测试
entrypoint: run.py
runtime: python
timeout: 8
---
# 测试脚本 Agent

读取问题并返回脚本输出。
"""
        script = """import json, sys
question = sys.argv[1]
payload = json.load(sys.stdin)
hits = payload.get("context", {}).get("hits", [])
print(f"脚本收到：{question}；上下文 {len(hits)} 条 [1]")
"""
        with TestClient(self.app) as client:
            self.login(client)
            created = client.post(
                "/api/agents",
                json={
                    "markdown": markdown,
                    "files": [{"name": "run.py", "content": script}],
                },
            )
            self.assertEqual(created.status_code, 200, created.text)
            agent = created.json()["agent"]
            self.assertEqual(agent["name"], "测试脚本 Agent")
            self.assertEqual(agent["entrypoint"], "run.py")
            self.assertEqual(agent["files"], ["run.py"])
            self.assertEqual(client.get("/api/agents").json()["agents"][0]["id"], agent["id"])

            conv = client.post(
                "/api/conversations", json={"mode": "agent", "kb": "product"}
            ).json()["conversation"]
            resp = client.post(
                f"/api/conversations/{conv['id']}/messages",
                json={
                    "content": "北极星项目的负责人是谁？",
                    "mode": "agent",
                    "agent_id": agent["id"],
                },
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            done = dict(parse_sse(resp.text))["done"]
            message = done["message"]
            self.assertIn("脚本收到", message["content"])
            self.assertEqual(message["meta"]["engine"], "imported")
            self.assertEqual(message["meta"]["answer_mode"], "script")
            self.assertEqual(message["meta"]["execution"]["exit_code"], 0)
            self.assertEqual(message["meta"]["execution"]["command"], "python run.py")
            self.assertGreater(len(message["citations"]), 0)
            self.assertIn("执行脚本", [t["title"] for t in message["meta"]["trace"]])

            # Another user cannot see, run, or delete this Agent.
            with TestClient(self.app) as other:
                self.login(other, "bob", "bob1234")
                self.assertEqual(other.get(f"/api/agents/{agent['id']}").status_code, 404)
                self.assertEqual(
                    other.post(
                        f"/api/conversations/{conv['id']}/messages",
                        json={"content": "x", "mode": "agent", "agent_id": agent["id"]},
                    ).status_code,
                    404,
                )

            self.assertEqual(client.delete(f"/api/agents/{agent['id']}").status_code, 200)
            self.assertEqual(client.get(f"/api/agents/{agent['id']}").status_code, 404)

    def test_skill_import_and_nested_script_paths(self):
        skill_md = """---
name: 文档处理 Skill
description: 只包含指令和参考脚本的 Skill
---
# 文档处理 Skill

按步骤处理文档。
"""
        script = """import sys
print(f"nested script ok: {sys.argv[1]} [1]")
"""
        with TestClient(self.app) as client:
            self.login(client)
            created = client.post(
                "/api/agents",
                json={
                    "kind": "skill",
                    "markdown": skill_md,
                    "files": [{"name": "scripts/helper.py", "content": script}],
                },
            )
            self.assertEqual(created.status_code, 200, created.text)
            skill = created.json()["agent"]
            self.assertEqual(skill["kind"], "skill")
            self.assertEqual(skill["entrypoint"], "")
            self.assertEqual(skill["files"], ["scripts/helper.py"])

            # An Agent (not a Skill) auto-selects the first executable file,
            # including a nested path; the runner must materialize the directory.
            created = client.post(
                "/api/agents",
                json={
                    "kind": "agent",
                    "markdown": "---\nname: 嵌套脚本\n---\n# 嵌套脚本\n",
                    "files": [{"name": "scripts/run.py", "content": script}],
                },
            )
            self.assertEqual(created.status_code, 200, created.text)
            agent = created.json()["agent"]
            self.assertEqual(agent["kind"], "agent")
            self.assertEqual(agent["entrypoint"], "scripts/run.py")

            conv = client.post(
                "/api/conversations", json={"mode": "agent", "kb": "product"}
            ).json()["conversation"]
            resp = client.post(
                f"/api/conversations/{conv['id']}/messages",
                json={"content": "处理文档", "mode": "agent", "agent_id": agent["id"]},
            )
            done = dict(parse_sse(resp.text))["done"]["message"]
            self.assertIn("nested script ok", done["content"])
            self.assertEqual(done["meta"]["execution"]["command"], "python scripts/run.py")
            self.assertEqual(done["meta"]["agent_kind"], "agent")

            client.delete(f"/api/agents/{skill['id']}")
            client.delete(f"/api/agents/{agent['id']}")

    def test_agent_artifacts_and_authenticated_download(self):
        script = """import os
from pathlib import Path
out = Path(os.environ["AGENT_OUTPUT_DIR"])
(out / "result.txt").write_text("artifact-ok", encoding="utf-8")
print("artifact written")
"""
        with TestClient(self.app) as client:
            self.login(client)
            created = client.post(
                "/api/agents",
                json={
                    "kind": "agent",
                    "markdown": "---\nname: 产物 Agent\n---\n# 产物 Agent\n",
                    "files": [{"name": "run.py", "content": script}],
                },
            )
            self.assertEqual(created.status_code, 200, created.text)
            agent = created.json()["agent"]
            conv = client.post(
                "/api/conversations", json={"mode": "agent", "kb": "product"}
            ).json()["conversation"]
            resp = client.post(
                f"/api/conversations/{conv['id']}/messages",
                json={"content": "生成文件", "mode": "agent", "agent_id": agent["id"]},
            )
            done = dict(parse_sse(resp.text))["done"]["message"]
            execution = done["meta"]["execution"]
            self.assertTrue(execution["ran"])
            self.assertEqual([a["name"] for a in execution["artifacts"]], ["result.txt"])
            download = client.get(
                f"/api/agent-runs/{execution['run_id']}/artifacts/result.txt"
            )
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.text, "artifact-ok")
            client.delete(f"/api/agents/{agent['id']}")
            client.delete(f"/api/conversations/{conv['id']}")

    def test_agent_preflight_checks_and_download(self):
        script = """import os
from pathlib import Path
out = Path(os.environ["AGENT_OUTPUT_DIR"])
(out / "preflight.txt").write_text("ok", encoding="utf-8")
print("preflight-ok")
"""
        with TestClient(self.app) as client:
            self.login(client)
            created = client.post(
                "/api/agents",
                json={
                    "kind": "agent",
                    "markdown": "---\nname: Preflight Agent\n---\n# Preflight Agent\n",
                    "files": [{"name": "run.py", "content": script}],
                },
            ).json()["agent"]
            resp = client.post(
                f"/api/agents/{created['id']}/test",
                json={"question": "preflight"},
            )
            self.assertEqual(resp.status_code, 200, resp.text)
            data = resp.json()
            self.assertEqual(data["status"], "ok")
            self.assertEqual({c["id"] for c in data["checks"]}, {"entrypoint", "runtime", "run", "output", "artifacts"})
            self.assertTrue(all(c["ok"] for c in data["checks"]))
            self.assertEqual(data["artifacts"][0]["name"], "preflight.txt")
            download = client.get(data["artifacts"][0]["url"])
            self.assertEqual(download.status_code, 200)
            self.assertEqual(download.text, "ok")

            prompt = client.post(
                "/api/agents",
                json={
                    "kind": "skill",
                    "markdown": "---\nname: Prompt Only\n---\n# Prompt Only\n",
                    "files": [],
                },
            ).json()["agent"]
            prompt_test = client.post(
                f"/api/agents/{prompt['id']}/test", json={"question": "test"}
            )
            self.assertEqual(prompt_test.status_code, 200)
            self.assertEqual(prompt_test.json()["status"], "prompt")
            self.assertFalse(prompt_test.json()["checks"][0]["ok"])
            client.delete(f"/api/agents/{created['id']}")
            client.delete(f"/api/agents/{prompt['id']}")

    def test_pipeline_flag_and_html_extraction(self):
        from webchat.agent import AgentRunner

        self.assertTrue(
            AgentRunner._html_from_text("```html\n<!DOCTYPE html><html><body>x</body></html>\n```").startswith("<!DOCTYPE html>")
        )
        self.assertIn("<html", AgentRunner._html_from_text("<!DOCTYPE html><html><body>x</body></html>"))
        with TestClient(self.app) as client:
            self.login(client)
            created = client.post(
                "/api/agents",
                json={
                    "kind": "skill",
                    "markdown": "---\nname: Pipeline Demo\npipeline: true\n---\n# Pipeline Demo\n",
                    "files": [{"name": "scripts/run.sh", "content": "echo ok\n"}],
                    "entrypoint": "scripts/run.sh",
                },
            )
            self.assertEqual(created.status_code, 200, created.text)
            self.assertTrue(created.json()["agent"]["pipeline"])
            client.delete(f"/api/agents/{created.json()['agent']['id']}")

    def test_multi_turn_history_is_in_prompt_and_followup_query(self):
        from kb.answer import build_prompt
        from kb.retrieval import ChunkHit, RetrievalResult
        from webchat.agent import AgentRunner

        query = AgentRunner._retrieval_query(
            "那产品负责人呢？",
            [{"role": "user", "content": "北极星项目的负责人是谁？"}],
        )
        self.assertIn("北极星项目", query)
        self.assertIn("产品负责人", query)

        hit = ChunkHit(
            chunk_id="chk_1",
            doc_id="doc_1",
            doc_title="测试文档",
            heading_path="基本信息",
            text="北极星项目的负责人是林晓。",
            score=0.1,
        )
        result = RetrievalResult(query="那产品负责人呢？", hits=[hit])
        prompt, _ = build_prompt(
            "那产品负责人呢？",
            result,
            history=[
                {"role": "user", "content": "北极星项目的负责人是谁？"},
                {"role": "assistant", "content": "负责人是林晓。"},
            ],
        )
        self.assertIn("对话历史", prompt)
        self.assertIn("负责人是林晓", prompt)

    def test_github_url_parser(self):
        from webchat.github_importer import parse_github_url

        repo = parse_github_url("https://github.com/anthropics/skills/tree/main/skills/pdf")
        self.assertEqual((repo.owner, repo.repo, repo.ref, repo.path), ("anthropics", "skills", "main", "skills/pdf"))
        blob = parse_github_url("https://github.com/o/r/blob/dev/a/SKILL.md")
        self.assertEqual((blob.ref, blob.path, blob.manifest), ("dev", "a", "SKILL.md"))
        raw = parse_github_url("https://raw.githubusercontent.com/o/r/main/a/AGENT.md")
        self.assertEqual((raw.repo, raw.ref, raw.manifest), ("r", "main", "AGENT.md"))
        with self.assertRaises(Exception):
            parse_github_url("https://example.com/o/r")

    def test_kb_graph_and_entity(self):
        with TestClient(self.app) as client:
            self.login(client)
            overview = client.get("/api/kb/overview")
            self.assertEqual(overview.status_code, 200)
            self.assertGreaterEqual(overview.json()["stats"]["entities"], 20)
            graph = client.get("/api/kb/graph").json()
            self.assertGreater(len(graph["nodes"]), 4)
            self.assertGreater(len(graph["edges"]), 3)
            node = graph["nodes"][0]
            detail = client.get(f"/api/kb/entities/{node['id']}")
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.json()["name"], node["name"])

    def test_registration_validation(self):
        with TestClient(self.app) as client:
            short = client.post("/api/auth/register", json={"username": "ab", "password": "123456"})
            self.assertEqual(short.status_code, 422)
            bad = client.post("/api/auth/register", json={"username": "a b", "password": "123456"})
            self.assertEqual(bad.status_code, 400)
            ok = client.post(
                "/api/auth/register",
                json={
                    "username": "new_user",
                    "password": "secret12",
                    "display_name": "新用户",
                    "department": "研发部",
                },
            )
            self.assertEqual(ok.status_code, 200, ok.text)
            self.assertEqual(ok.json()["user"]["department"], "研发部")
            self.assertEqual(client.get("/api/auth/me").status_code, 200)
            kbs = client.get("/api/kbs").json()["knowledge_bases"]
            self.assertEqual({kb["id"] for kb in kbs}, {"engineering", "common"})
            bad_department = client.post(
                "/api/auth/register",
                json={"username": "bad_dept", "password": "secret12", "department": "不存在"},
            )
            self.assertEqual(bad_department.status_code, 400)


if __name__ == "__main__":
    unittest.main()
