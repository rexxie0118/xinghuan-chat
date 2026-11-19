"""Agent orchestration.

Two engines share one result shape:

* local  — deterministic RAG + ontology demo built on the top-level ``kb``
           package. It works offline and exposes every tool step in the UI.
* external — POSTs the same context to an Agent service living in another repo.
           Failure is non-fatal: the request falls back to the local engine.

The HTTP contract is documented in knowledge/06-Agent接入规范.md and
examples/external_agent_server.py.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .agent_defs import ImportedAgent, cited_indices
from .config import Settings
from .knowledge import KnowledgeService
from .sandbox import RunRequest, SandboxRunner
from .util import new_id

LOG = logging.getLogger("webchat.agent")

StatusFn = Callable[[str, str], None]


def _step(
    title: str,
    detail: str,
    *,
    kind: str = "tool",
    status: str = "done",
    duration_ms: int = 0,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "title": title,
        "detail": detail,
        "kind": kind,
        "status": status,
        "duration_ms": duration_ms,
        "data": data or {},
    }


@dataclass
class AgentResult:
    answer: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    engine: str = "local"
    mode: str = "agent"
    graph: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def message_meta(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "engine": self.engine,
            "trace": self.trace,
            "warnings": self.warnings,
            "suggestions": self.suggestions,
            "graph": self.graph,
            **self.meta,
        }


SMALLTALK = re.compile(
    r"^(你好|您好|hi|hello|hey|嗨|在吗|你是谁|你叫什么|介绍一下你自己|你能做什么|帮助|help)[!！。？?\s]*$",
    re.IGNORECASE,
)


class AgentRunner:
    def __init__(self, settings: Settings, sandbox: SandboxRunner) -> None:
        self.settings = settings
        self.sandbox = sandbox

    # ------------------------------------------------------------------ entry

    def run(
        self,
        *,
        question: str,
        mode: str,
        user: dict[str, Any],
        conversation: dict[str, Any],
        history: list[dict[str, str]],
        kb: KnowledgeService,
        imported_agent: ImportedAgent | None = None,
        on_status: StatusFn | None = None,
    ) -> AgentResult:
        mode = "rag" if mode == "rag" else "agent"
        status = on_status or (lambda _title, _detail: None)

        smalltalk = self._smalltalk(kb, question, mode) if imported_agent is None else None
        if smalltalk is not None:
            status("组织回答", "这是一句寒暄，不触发检索")
            return smalltalk

        retrieval_query = self._retrieval_query(question, history)
        status("检索知识库", "向量 + 全文 + 图谱三路召回，RRF 融合")
        started = time.perf_counter()
        try:
            result = kb.retrieve(retrieval_query)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("retrieval failed")
            return AgentResult(
                answer="知识库检索暂时不可用。你仍然可以正常聊天，或稍后重试。",
                trace=[
                    _step("检索知识库", f"检索失败：{type(exc).__name__}: {exc}", kind="error"),
                ],
                warnings=["知识库检索失败"],
                engine="local",
                mode=mode,
            )
        retrieval_ms = int((time.perf_counter() - started) * 1000)

        base_trace = [
            _step(
                "理解问题",
                f"{'Agent 工具循环' if mode == 'agent' else 'RAG 直答'} · 问题已归一化",
                kind="think",
            ),
            _step(
                "检索知识库",
                self._retrieval_detail(result),
                duration_ms=retrieval_ms,
                data={
                    "hits": len(result.get("hits", [])),
                    "entities": len(result.get("graph", {}).get("nodes", [])),
                    "timings": result.get("stats", {}).get("timings", {}),
                },
            ),
        ]
        if mode == "agent":
            graph = result.get("graph", {})
            base_trace.append(
                _step(
                    "扩展本体图谱",
                    f"沿关系边扩展 2 跳：{len(graph.get('nodes', []))} 个实体 /"
                    f" {len(graph.get('edges', []))} 条关系",
                    data={"nodes": len(graph.get("nodes", [])), "edges": len(graph.get("edges", []))},
                )
            )
            base_trace.append(
                _step(
                    "组合证据",
                    "把引用片段、图谱关系与对话上下文放进同一次作答",
                    kind="think",
                )
            )

        if imported_agent is not None:
            status("运行导入 Agent", f"执行 {imported_agent.entrypoint or '知识库回答'}")
            return self._run_imported(
                agent=imported_agent,
                question=question,
                result=result,
                base_trace=base_trace,
                kb=kb,
                history=history,
            )

        if self.settings.agent_enabled:
            status("调用外部 Agent", "正在请求另一个仓库里的 Agent 服务")
            external = self._run_external(
                question=question,
                mode=mode,
                user=user,
                conversation=conversation,
                history=history,
                retrieval=result,
                base_trace=base_trace,
                kb=kb,
            )
            if external is not None:
                return external

        status("生成回答", "基于证据生成引用式回答")
        started = time.perf_counter()
        try:
            answer = kb.answer(question, result)
            answer_dict = answer.to_dict()
            answer_dict["answer"] = _clean_extractive(answer_dict.get("answer", ""))
        except Exception as exc:  # noqa: BLE001
            LOG.exception("answer failed")
            answer_dict = {
                "answer": "检索已完成，但回答生成失败。可以展开检索轨迹查看命中片段。",
                "citations": [],
                "mode": "error",
                "confidence": 0,
                "warnings": [f"{type(exc).__name__}: {exc}"],
            }
        answer_ms = int((time.perf_counter() - started) * 1000)

        trace = list(base_trace)
        trace.append(
            _step(
                "生成引用式回答",
                f"模式={answer_dict.get('mode')} · 置信度={answer_dict.get('confidence', 0)}",
                kind="answer",
                duration_ms=answer_ms,
            )
        )
        if answer_dict.get("citations"):
            trace.append(
                _step(
                    "校验引用",
                    f"{len(answer_dict['citations'])} 条引用已回填到原文片段",
                    kind="check",
                )
            )

        warnings = list(answer_dict.get("warnings") or []) + list(result.get("warnings") or [])
        return AgentResult(
            answer=answer_dict.get("answer", ""),
            citations=answer_dict.get("citations", []),
            trace=trace,
            suggestions=kb.suggestions(question, result),
            engine="local",
            mode=mode,
            graph=result.get("graph", {}),
            warnings=_dedupe(warnings),
            meta={
                "answer_mode": answer_dict.get("mode"),
                "confidence": answer_dict.get("confidence", 0),
                "timings": result.get("stats", {}).get("timings", {}),
                "kb_id": kb.info.id,
                "kb_name": kb.info.name,
                "retrieval": {
                    "hits": len(result.get("hits", [])),
                    "entities": len(result.get("graph", {}).get("nodes", [])),
                    "relations": len(result.get("graph", {}).get("edges", [])),
                },
            },
        )

    # ------------------------------------------------------------- imported

    def _run_imported(
        self,
        *,
        agent: ImportedAgent,
        question: str,
        result: dict[str, Any],
        base_trace: list[dict[str, Any]],
        kb: KnowledgeService,
        history: list[dict[str, str]],
    ) -> AgentResult:
        trace = list(base_trace)
        kind_label = "Skill" if agent.kind == "skill" else "Agent"
        trace.append(
            _step(
                f"加载导入 {kind_label}",
                f"{agent.name} · {len(agent.files)} 个参考文件"
                + (f" · 入口 {agent.entrypoint}" if agent.entrypoint else " · 未配置入口脚本"),
                kind="think",
            )
        )
        run_id = new_id("run")
        execution = self.sandbox.run(
            RunRequest(
                run_id=run_id,
                files=agent.files,
                entrypoint=agent.entrypoint,
                runtime=agent.runtime,
                timeout=agent.timeout,
                question=question,
                context={
                    "hits": result.get("hits", [])[:6],
                    "graph": result.get("graph", {}),
                    "query": question,
                },
                agent_name=agent.name,
                agent_description=agent.description,
                agent_markdown=agent.markdown,
                allow_exec=self.settings.allow_script_exec,
            )
        ).to_dict()
        warnings: list[str] = []
        answer_dict: dict[str, Any] | None = None
        used_script = False
        stdout = (execution.get("stdout") or "").strip()
        exit_code = execution.get("exit_code")

        if execution.get("ran"):
            status = "error" if execution.get("timed_out") or exit_code not in (0, None) else "done"
            trace.append(
                _step(
                    "执行脚本",
                    (
                        f"{agent.entrypoint} · exit={exit_code} · "
                        f"{execution.get('duration_ms', 0)} ms"
                        + (" · 超时已终止" if execution.get("timed_out") else "")
                    ),
                    kind="tool",
                    status=status,
                    duration_ms=int(execution.get("duration_ms") or 0),
                    data={
                        "exit_code": exit_code,
                        "stdout_chars": len(execution.get("stdout") or ""),
                        "stderr_chars": len(execution.get("stderr") or ""),
                    },
                )
            )
            if execution.get("timed_out"):
                warnings.append("脚本执行超时，已终止并回退到知识库回答")
            elif exit_code not in (0, None):
                warnings.append(f"脚本以 exit={exit_code} 退出，已回退到知识库回答")
            elif stdout:
                used_script = True
                indices = cited_indices(stdout, len(result.get("hits", [])))
                citations = self._citations_from_indices(indices, result)
                trace.append(
                    _step(
                        "整理脚本输出",
                        f"采用 stdout 作为回答 · {len(stdout)} 字符 · {len(citations)} 条引用",
                        kind="answer",
                    )
                )
                return AgentResult(
                    answer=stdout[:8000],
                    citations=citations,
                    trace=trace,
                    suggestions=kb.suggestions(question, result),
                    engine="imported",
                    mode="agent",
                    graph=result.get("graph", {}),
                    warnings=warnings,
                    meta={
                        "answer_mode": "script",
                        "confidence": 0.7,
                        "timings": result.get("stats", {}).get("timings", {}),
                        "kb_id": kb.info.id,
                        "kb_name": kb.info.name,
                        "agent_id": agent.id,
                        "agent_name": agent.name,
                        "agent_kind": agent.kind,
                        "execution": self._execution_meta(execution),
                    },
                )
            else:
                warnings.append("脚本没有 stdout，已回退到知识库回答")

        else:
            if agent.kind == "skill":
                if kb.provider.is_mock:
                    detail = (
                        "这是 Prompt Skill：没有入口脚本，需要真实 LLM 读取 SKILL.md；"
                        "当前 provider=mock，已回退知识库。配置 DeepSeek/ARK 后即可使用 Skill 指令。"
                    )
                    warnings.append("Prompt Skill 需要真实 LLM；当前 mock 模式无法执行 Skill 指令")
                else:
                    detail = "Prompt Skill 已加载，正在使用 SKILL.md 作为指令"
                trace.append(_step("加载 Prompt Skill", detail, kind="think"))
            else:
                trace.append(
                    _step(
                        "未执行脚本",
                        execution.get("reason", "没有可执行的入口脚本"),
                        kind="error" if execution.get("reason") else "think",
                    )
                )

        if agent.kind == "skill" and not execution.get("ran") and kb.provider.is_mock:
            trace.append(
                _step(
                    "需要真实 LLM",
                    "Prompt Skill 已加载，但 mock provider 无法读取 SKILL.md 生成内容",
                    kind="error",
                    status="error",
                )
            )
            return AgentResult(
                answer=(
                    f"### {agent.name} 已加载\n\n"
                    "这是一个 **Prompt Skill**：它没有独立入口脚本，需要真实 LLM 读取 "
                    "`SKILL.md` 并按指令完成工作。\n\n"
                    "当前知识库 provider 是 `mock`，无法执行 Skill 指令。请在 "
                    "`webchat/.env` 中配置 DeepSeek / ARK 后重启服务，再选择这个 Skill 重试。\n\n"
                    "如果你希望它直接运行脚本，可以给 Skill 配置 `entrypoint`，或按 Agent 类型导入。"
                ),
                citations=[],
                trace=trace,
                suggestions=["如何在 .env 配置 DeepSeek？", "Agent 和 Skill 有什么区别？"],
                engine="imported",
                mode="agent",
                graph=result.get("graph", {}),
                warnings=["Prompt Skill 需要真实 LLM；当前 mock 模式无法执行 Skill 指令"],
                meta={
                    "answer_mode": "skill_requires_llm",
                    "confidence": 1,
                    "kb_id": kb.info.id,
                    "kb_name": kb.info.name,
                    "agent_id": agent.id,
                    "agent_name": agent.name,
                    "agent_kind": agent.kind,
                    "requires_model": True,
                    "execution": self._execution_meta(execution),
                },
            )

        # A Skill is often an instruction pack without a single runnable
        # entrypoint. If a real model is configured, use the Skill Markdown as
        # the system instruction with the same retrieved evidence.
        if not execution.get("ran") and agent.kind == "skill" and not kb.provider.is_mock:
            try:
                skill_answer, skill_citations = self._skill_answer(
                    agent=agent,
                    question=question,
                    result=result,
                    kb=kb,
                    history=history,
                )
                if skill_answer:
                    trace.append(
                        _step(
                            "Skill 指令作答",
                            f"使用 {agent.name} 的 Markdown 指令 + 知识库证据",
                            kind="answer",
                        )
                    )
                    return AgentResult(
                        answer=skill_answer,
                        citations=skill_citations,
                        trace=trace,
                        suggestions=kb.suggestions(question, result),
                        engine="imported",
                        mode="agent",
                        graph=result.get("graph", {}),
                        warnings=warnings,
                        meta={
                            "answer_mode": "skill_llm",
                            "confidence": 0.55,
                            "timings": result.get("stats", {}).get("timings", {}),
                            "kb_id": kb.info.id,
                            "kb_name": kb.info.name,
                            "agent_id": agent.id,
                            "agent_name": agent.name,
                            "agent_kind": agent.kind,
                            "execution": self._execution_meta(execution),
                        },
                    )
            except Exception as exc:  # noqa: BLE001
                LOG.warning("skill LLM answer failed: %s", exc)
                warnings.append(f"Skill 指令作答失败，已回退知识库：{type(exc).__name__}")

        # Fallback: use the same grounded answer path as Chat mode.
        try:
            fallback = kb.answer(question, result, history=history)
            answer_dict = fallback.to_dict()
            answer_dict["answer"] = _clean_extractive(answer_dict.get("answer", ""))
        except Exception as exc:  # noqa: BLE001
            LOG.exception("imported agent fallback failed")
            answer_dict = {
                "answer": "脚本已返回，但没有生成可展示的回答。",
                "citations": [],
                "mode": "error",
                "confidence": 0,
                "warnings": [f"{type(exc).__name__}: {exc}"],
            }
        trace.append(
            _step(
                "生成回答",
                f"回退知识库 · 模式={answer_dict.get('mode')}",
                kind="answer",
            )
        )
        warnings.extend(answer_dict.get("warnings") or [])
        return AgentResult(
            answer=answer_dict.get("answer", ""),
            citations=answer_dict.get("citations", []),
            trace=trace,
            suggestions=kb.suggestions(question, result),
            engine="imported",
            mode="agent",
            graph=result.get("graph", {}),
            warnings=_dedupe(warnings),
            meta={
                "answer_mode": answer_dict.get("mode"),
                "confidence": answer_dict.get("confidence", 0),
                "timings": result.get("stats", {}).get("timings", {}),
                "kb_id": kb.info.id,
                "kb_name": kb.info.name,
                "agent_id": agent.id,
                "agent_name": agent.name,
                "agent_kind": agent.kind,
                "execution": self._execution_meta(execution),
                "used_script": used_script,
            },
        )

    @staticmethod
    def _skill_answer(
        *,
        agent: ImportedAgent,
        question: str,
        result: dict[str, Any],
        kb: KnowledgeService,
        history: list[dict[str, str]],
    ) -> tuple[str, list[dict[str, Any]]]:
        hits = result.get("hits", [])[:6]
        if not hits:
            return "", []
        evidence = []
        for index, hit in enumerate(hits, 1):
            text = " ".join((hit.get("text") or "").split())[:900]
            heading = (
                " › " + str(hit.get("heading_path"))
                if hit.get("heading_path")
                else ""
            )
            evidence.append(
                f"[{index}] 《{hit.get('doc_title', '')}》{heading}\n{text}"
            )
        system = (
            "你正在执行一个可复用的 Skill。严格遵循下面的 Skill 指令；"
            "只根据提供的知识库证据回答，不要编造事实。引用证据时使用 [编号]。\n\n"
            f"# Skill: {agent.name}\n{agent.markdown}"
        )
        history_text = ""
        for message in history[-8:]:
            role = "用户" if message.get("role") == "user" else "助手"
            text = " ".join((message.get("content") or "").split())
            if text:
                history_text += f"{role}：{text[:700]}\n"
        user = (
            f"用户问题：{question}\n\n"
            + (f"对话历史：\n{history_text}\n" if history_text else "")
            + "知识库证据：\n" + "\n\n".join(evidence) + "\n\n"
            "请用中文简洁回答，并在句末标注引用编号。"
        )
        text = kb.provider.chat.complete(system, user, max_tokens=1000)
        text = (text or "").strip()
        if not text:
            return "", []
        indices = cited_indices(text, len(hits))
        return text, AgentRunner._citations_from_indices(indices, result)

    @staticmethod
    def _execution_meta(execution: dict[str, Any]) -> dict[str, Any]:
        if not execution:
            return {}
        return {
            "ran": bool(execution.get("ran")),
            "command": execution.get("command", ""),
            "exit_code": execution.get("exit_code"),
            "stdout": (execution.get("stdout") or "")[:6000],
            "stderr": (execution.get("stderr") or "")[:4000],
            "timed_out": bool(execution.get("timed_out")),
            "duration_ms": execution.get("duration_ms", 0),
            "files": execution.get("files", []),
            "reason": execution.get("reason", ""),
            "runner": execution.get("runner", "local"),
            "run_id": execution.get("run_id", ""),
            "artifacts": execution.get("artifacts", []),
        }

    @staticmethod
    def _citations_from_indices(
        indices: list[int], result: dict[str, Any]
    ) -> list[dict[str, Any]]:
        out = []
        hits = result.get("hits", [])
        for index in indices:
            if not (1 <= index <= len(hits)):
                continue
            hit = hits[index - 1]
            out.append(
                {
                    "index": index,
                    "chunk_id": hit["chunk_id"],
                    "doc_id": hit["doc_id"],
                    "doc_title": hit["doc_title"],
                    "heading_path": hit.get("heading_path", ""),
                    "snippet": hit.get("text", "")[:280],
                    "label": f"《{hit['doc_title']}》",
                }
            )
        return out

    # ------------------------------------------------------------- external

    def _run_external(
        self,
        *,
        question: str,
        mode: str,
        user: dict[str, Any],
        conversation: dict[str, Any],
        history: list[dict[str, str]],
        retrieval: dict[str, Any],
        base_trace: list[dict[str, Any]],
        kb: KnowledgeService,
    ) -> AgentResult | None:
        hits = []
        for i, hit in enumerate(retrieval.get("hits", [])[:6], 1):
            hits.append(
                {
                    "index": i,
                    "chunk_id": hit["chunk_id"],
                    "doc_title": hit["doc_title"],
                    "heading_path": hit.get("heading_path", ""),
                    "text": hit.get("text", "")[:1400],
                    "score": hit.get("score", 0),
                }
            )
        body = {
            "user": {"id": user["id"], "name": user["display_name"]},
            "conversation_id": conversation["id"],
            "knowledge_base": {
                "id": kb.info.id,
                "name": kb.info.name,
                "department": kb.info.department,
            },
            "mode": mode,
            "messages": history[-8:] + [{"role": "user", "content": question}],
            "context": {
                "question": question,
                "hits": hits,
                "graph": retrieval.get("graph", {}),
                "memories": retrieval.get("memories", [])[:4],
            },
        }
        headers = {"Content-Type": "application/json"}
        if self.settings.agent_token:
            headers["Authorization"] = f"Bearer {self.settings.agent_token}"
        started = time.perf_counter()
        try:
            resp = httpx.post(
                self.settings.agent_url.rstrip("/") + "/chat",
                json=body,
                headers=headers,
                timeout=self.settings.agent_timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            answer = str(data.get("answer") or "").strip()
            if not answer:
                raise ValueError("Agent 返回了空 answer")
        except Exception as exc:  # noqa: BLE001 - fall back, never break chat
            LOG.warning("external agent failed: %s", exc)
            local = self._trace_warning(
                f"外部 Agent 不可用（{type(exc).__name__}），已自动回退到本地 RAG"
            )
            base_trace.append(local)
            return None

        elapsed = int((time.perf_counter() - started) * 1000)
        citations = self._normalize_citations(data.get("citations"), hits)
        trace = list(base_trace)
        for raw in data.get("trace") or []:
            if isinstance(raw, str):
                trace.append(_step("外部 Agent", raw, kind="tool"))
            elif isinstance(raw, dict):
                trace.append(
                    _step(
                        str(raw.get("title") or "外部 Agent"),
                        str(raw.get("detail") or ""),
                        kind=str(raw.get("kind") or "tool"),
                        status=str(raw.get("status") or "done"),
                        duration_ms=int(raw.get("duration_ms") or 0),
                    )
                )
        trace.append(
            _step(
                "外部 Agent 返回",
                f"已调用独立 Agent 服务 · 耗时 {elapsed} ms",
                kind="answer",
                duration_ms=elapsed,
            )
        )
        suggestions = [str(s) for s in (data.get("suggestions") or []) if s][:3]
        return AgentResult(
            answer=answer,
            citations=citations,
            trace=trace,
            suggestions=suggestions or kb.suggestions(question, retrieval),
            engine="external",
            mode=mode,
            graph=retrieval.get("graph", {}),
            warnings=[],
            meta={
                "answer_mode": "agent",
                "confidence": data.get("confidence", 0),
                "agent_service": self.settings.agent_url,
                "timings": retrieval.get("stats", {}).get("timings", {}),
                "kb_id": kb.info.id,
                "kb_name": kb.info.name,
            },
        )

    @staticmethod
    def _normalize_citations(raw: Any, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_index = {h["index"]: h for h in hits}
        out: list[dict[str, Any]] = []
        for item in raw or []:
            if isinstance(item, int) and item in by_index:
                h = by_index[item]
                out.append(
                    {
                        "index": item,
                        "chunk_id": h["chunk_id"],
                        "doc_title": h["doc_title"],
                        "heading_path": h["heading_path"],
                        "snippet": h["text"][:280],
                        "label": f"《{h['doc_title']}》",
                    }
                )
            elif isinstance(item, dict):
                out.append(item)
        return out

    @staticmethod
    def _trace_warning(message: str) -> dict[str, Any]:
        return _step("外部 Agent", message, kind="error", status="error")

    # --------------------------------------------------------------- helpers

    @staticmethod
    def _retrieval_detail(result: dict[str, Any]) -> str:
        hits = result.get("hits", [])
        docs = list(dict.fromkeys(h["doc_title"] for h in hits[:4]))
        timings = result.get("stats", {}).get("timings", {})
        suffix = f" · {timings.get('total_ms', 0)} ms" if timings else ""
        return (
            f"混合召回 {len(hits)} 个片段"
            + (f"，主要来自 {'、'.join('《' + d + '》' for d in docs[:3])}" if docs else "")
            + suffix
        )

    @staticmethod
    def _retrieval_query(question: str, history: list[dict[str, str]]) -> str:
        """Resolve follow-ups and pronouns with the previous user turn."""
        q = question.strip()
        followup_markers = (
            "他", "她", "它", "这个", "那个", "上述", "他们", "这些", "该",
            "还有", "那", "呢", "继续", "再", "上一个", "前面",
        )
        is_followup = len(q) <= 18 or any(marker in q for marker in followup_markers)
        if is_followup and history:
            previous = [m for m in history if m["role"] == "user"]
            if previous:
                return f"{previous[-1]['content']} {q}"[:500]
        return q

    def _smalltalk(
        self, kb: KnowledgeService, question: str, mode: str
    ) -> AgentResult | None:
        if not SMALLTALK.match(question.strip()):
            return None
        text = (
            "你好，我是**星环 Chat** 的演示助手。\n\n"
            "我可以做三件事：\n"
            "- 在共享知识库里做 **RAG 检索**，回答时给出可点开的引用；\n"
            "- 在 **Agent 模式**下展示「检索 → 图谱扩展 → 作答」的工具轨迹；\n"
            "- 把每次对话按账号持久化，刷新或换设备后仍然在。\n\n"
            "试试问：「北极星项目的负责人是谁？」"
        )
        try:
            suggestions = kb.overview().get("sample_questions", [])[:3]
        except Exception:  # noqa: BLE001 - greeting must never fail
            suggestions = []
        return AgentResult(
            answer=text,
            trace=[_step("识别意图", "寒暄 / 能力介绍，不需要检索", kind="think")],
            suggestions=suggestions,
            engine="local",
            mode=mode,
            meta={"answer_mode": "smalltalk", "confidence": 1},
        )


def _clean_extractive(text: str) -> str:
    """Drop the engine's boilerplate preamble; the UI renders it as a badge."""
    text = re.sub(
        r"^（离线抽取式摘要，未调用大模型）与「.*?」最相关的原文：\s*",
        "",
        text,
        flags=re.DOTALL,
    )
    return text.strip()


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
