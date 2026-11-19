"""命令行入口：kb <command> ...

设计目标是"一条命令就能验证一件事"：
  kb init      建库并看清单
  kb demo      一键跑完整链路（离线可跑）
  kb ingest    摄入文档
  kb ask       带引用问答
  kb query     只看召回，不生成答案（调检索用）
  kb graph     看/导出本体，合并重复实体
  kb memory    手工写入、检索、遗忘
  kb stats     体检
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .answer import synthesize
from .config import db_path as cfg_db_path
from .config import load_config
from .db import analyzer_stats, connect, init_schema, meta_get, table_counts
from .evaluate import (
    build_draft_cases,
    check_cases,
    compare_reports,
    load_cases,
    render_markdown,
    render_text,
    run_eval,
    save_cases,
)
from .ingest import Ingester, remove_document
from .dreaming import Dreamer
from .memory import MemoryStore
from .terms import TermsStore
from .ontology import Ontology
from .providers import ProviderError, build_provider
from .retrieval import Retriever
from .util import human_bytes, truncate
from .vectors import KIND_CHUNK, KIND_MEMORY, put_vectors, vector_models

# ---------------------------------------------------------------- 输出


def _tty() -> bool:
    return sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _tty() else text


def bold(t: str) -> str:
    return _c("1", t)


def dim(t: str) -> str:
    return _c("2", t)


def cyan(t: str) -> str:
    return _c("36", t)


def green(t: str) -> str:
    return _c("32", t)


def yellow(t: str) -> str:
    return _c("33", t)


def red(t: str) -> str:
    return _c("31", t)


def hr(width: int = 72) -> str:
    return dim("─" * width)


def emit(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- 上下文


class Ctx:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.config = load_config(
            getattr(args, "config", None),
            provider=getattr(args, "provider", None),
            db_path=getattr(args, "db", None),
        )
        self._conn = None
        self._provider = None

    @property
    def db(self) -> Path:
        return cfg_db_path(self.config)

    def conn(self, *, create: bool = False):
        if self._conn is None:
            if not create and not self.db.exists():
                raise SystemExit(
                    f"知识库还没初始化：{self.db}\n先运行 `kb init`（离线可用）"
                )
            self._conn = connect(self.db, create=True)
            init_schema(self._conn)
        return self._conn

    def provider(self):
        if self._provider is None:
            try:
                self._provider = build_provider(self.config)
            except ProviderError as exc:
                raise SystemExit(red(f"模型后端不可用：{exc}")) from exc
        return self._provider

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


# ---------------------------------------------------------------- init


def cmd_init(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    path = ctx.db
    if args.reset and path.exists():
        if not args.yes:
            print(yellow(f"将删除现有数据库：{path}"))
            if input("确认？输入 yes 继续：").strip() != "yes":
                print("已取消")
                return 1
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(path) + suffix)
            if f.exists():
                f.unlink()
    conn = ctx.conn(create=True)
    init_schema(conn)
    counts = table_counts(conn)
    print(f"{green('✓')} 知识库就绪：{bold(str(path))}")
    print(f"  schema_version={meta_get(conn, 'schema_version')}  provider={ctx.config['provider']['kind']}")
    print(f"  核心类型：实体 {counts['entity_types']} 种 / 关系 {counts['relation_types']} 种")
    print(f"  当前数据：文档 {counts['documents']} / 分块 {counts['chunks']} / 实体 {counts['entities']} / 记忆 {counts['memories']}")
    if ctx.config["provider"]["kind"] == "mock":
        print(dim("  离线模式：向量为特征哈希，答案走抽取式摘要；接真模型见 README「切换到 ARK」。"))
    return 0


# ---------------------------------------------------------------- ingest


def cmd_ingest(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    conn = ctx.conn()
    provider = ctx.provider()

    def progress(msg: str) -> None:
        print(dim(msg), file=sys.stderr)

    ingester = Ingester(conn, ctx.config, provider, progress=progress)
    try:
        report = ingester.ingest_paths(
            args.paths, doc_type=args.type, recursive=not args.no_recursive, force=args.force
        )
    except FileNotFoundError as exc:
        print(red(str(exc)))
        return 2

    if args.json:
        emit(report.to_dict())
        return 0

    totals = report.totals
    print()
    for doc in report.docs:
        icon = {"created": green("+"), "updated": yellow("~"), "unchanged": dim("="), "error": red("×")}[doc.status]
        title = truncate(doc.title, 40)
        extra = f"{doc.chunks} 块" if doc.status != "error" else doc.detail
        if doc.status in ("created", "updated"):
            extra += f" · 新实体 {doc.entities_new} · 关系 {doc.relations} · 记忆 {doc.memories}"
        print(f" {icon} {title:<42} {dim(extra)}")
    print(hr())
    print(
        f"  文档 {totals['created']} 新增 / {totals['updated']} 更新 / "
        f"{totals['unchanged']} 未变 / {totals['errors']} 失败"
    )
    print(
        f"  分块 {totals['chunks']} · 实体 {totals['entities_new']} 新建 + "
        f"{totals['entities_matched']} 复用 · 关系 {totals['relations']} · 记忆 {totals['memories']}"
    )
    return 0 if totals["errors"] == 0 else 1


# ---------------------------------------------------------------- query / ask


def _print_hit(idx: int, hit, show_text: bool = True) -> None:
    ranks = []
    if hit.vector_rank:
        ranks.append(f"V#{hit.vector_rank}")
    if hit.fts_rank:
        ranks.append(f"K#{hit.fts_rank}")
    if hit.graph_rank:
        ranks.append(f"G#{hit.graph_rank}")
    tag = dim(" ".join(ranks))
    print(f" {cyan(f'[{idx}]')} {bold(hit.citation)}  {dim(f'score={hit.score:.4f}')} {tag}")
    if show_text:
        body = hit.text.replace("\n", " ")
        print(f"     {dim(truncate(body, 160))}")


def cmd_query(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    conn = ctx.conn()
    provider = ctx.provider()
    retriever = Retriever(conn, ctx.config, provider)
    result = retriever.search(
        args.text, k=args.k, include_history_memory=args.history,
        include_episodes=getattr(args, "episodes", False),
    )
    if args.json:
        payload = result.to_dict()
        payload["timings"] = result.timings
        emit(payload)
        return 0

    if result.warnings:
        for w in result.warnings:
            print(yellow(f"! {w}"))
    print(f"\n{bold('查询')}：{result.query}   {dim(str(result.timings))}\n")
    if not result.hits:
        print(dim("  （无命中）"))
        return 0
    print(bold("文档片段"))
    for i, hit in enumerate(result.hits, 1):
        _print_hit(i, hit)
    if result.edges:
        print(f"\n{bold('图谱证据')}")
        for e in result.edges[:8]:
            ev = dim(f"  «{truncate(e['evidence'], 40)}»") if e.get("evidence") else ""
            print(f"  {e['src_name']} --{cyan(e['type'])}--> {e['dst_name']}{ev}")
    if result.memories:
        print(f"\n{bold('相关记忆')}")
        for m in result.memories:
            if m["score"] <= 0.02:
                continue
            score = m["score"]
            head = f"{m['kind']:<10} {truncate(m['content'], 70)}"
            print(f"  {dim(head)}  {dim(f'{score:.2f}')}")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    conn = ctx.conn()
    provider = ctx.provider()
    retriever = Retriever(conn, ctx.config, provider)
    result = retriever.search(
        args.text, k=args.k, include_history_memory=args.history,
        include_episodes=getattr(args, "episodes", False),
    )
    answer = synthesize(args.text, result, provider, ctx.config)

    if args.json:
        payload = answer.to_dict()
        payload["retrieval"] = {
            "hits": [h.to_dict() for h in result.hits],
            "timings": result.timings,
        }
        emit(payload)
        return 0

    print(f"\n{bold('问')}：{args.text}")
    print(hr())
    if answer.warnings and not args.quiet_warnings:
        for w in answer.warnings:
            print(yellow(f"! {w}"))
    print(answer.text)
    if answer.citations:
        print(f"\n{bold('来源')}")
        for cit in answer.citations:
            print(f"  [{cit.index}] {cit.label()}")
            print(f"      {dim(truncate(cit.snippet.replace(chr(10), ' '), 110))}")
    print(
        f"\n{dim(f'模式={answer.mode} 置信度={answer.confidence} 引用={len(answer.citations)}')}"
    )
    if args.show_context:
        print(f"\n{bold('召回明细')}")
        for i, hit in enumerate(result.hits, 1):
            _print_hit(i, hit)
    return 0


# ---------------------------------------------------------------- graph


def cmd_graph(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    conn = ctx.conn()
    onto = Ontology(conn)
    action = args.action

    if action == "types":
        if args.json:
            emit(
                {
                    "entity_types": onto.type_stats(),
                    "relation_types": onto.relation_type_stats(),
                }
            )
            return 0
        print(bold("实体类型"))
        for row in onto.type_stats():
            core = "" if row["is_core"] else yellow(" [自动发现]")
            print(f"  {row['name']:<8} {row['entities']:>5} 个{core}  {dim(row['description'])}")
        print(f"\n{bold('关系类型')}")
        for row in onto.relation_type_stats():
            core = "" if row["is_core"] else yellow(" [自动发现]")
            print(f"  {row['name']:<8} {row['edges']:>5} 条{core}  {dim(row['description'])}")
        return 0

    if action == "entities":
        if args.json:
            rows = conn.execute(
                "SELECT id, name, type_name, mention_count FROM entities"
                " ORDER BY mention_count DESC LIMIT ?",
                (args.limit,),
            ).fetchall()
            emit([dict(r) for r in rows])
            return 0
        rows = conn.execute(
            "SELECT e.id, e.name, e.type_name, e.mention_count,"
            " (SELECT COUNT(*) FROM relations r WHERE r.src_id=e.id OR r.dst_id=e.id) AS degree"
            " FROM entities e ORDER BY degree DESC, e.mention_count DESC LIMIT ?",
            (args.limit,),
        ).fetchall()
        for r in rows:
            label = f"[{r['type_name']}]"
            print(
                f"  {r['name']:<14} {dim(f'{label:<12}')} 度数 {r['degree']:>3}"
                f"  提及 {r['mention_count']:>2}"
            )
        return 0

    if action == "show":
        matches = onto.search_entities(args.name, limit=5)
        if not matches:
            print(yellow(f"没找到实体：{args.name}"))
            return 1
        target = matches[0]
        sub = onto.subgraph([target["id"]], hops=args.hops, min_weight=args.min_weight)
        if args.json:
            emit({"focus": dict(target), **sub})
            return 0
        print(f"{bold(target['name'])} {dim('[' + target['type_name'] + ']')} {target['description']}")
        if len(matches) > 1:
            print(dim(f"  其他候选：{'、'.join(m['name'] for m in matches[1:])}"))
        print()
        for e in sub["edges"]:
            ev = dim(f"  «{truncate(e['evidence'], 40)}»") if e.get("evidence") else ""
            print(f"  {e['src_name']} --{cyan(e['type'])}--> {e['dst_name']}{ev}")
        if not sub["edges"]:
            print(dim("  （没有关系边）"))
        print(dim(f"\n  节点 {len(sub['nodes'])} / 边 {len(sub['edges'])}"))
        return 0

    if action == "export":
        text = onto.export(args.format, limit=args.limit)
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
            print(green(f"✓ 已写入 {args.output}"))
        else:
            print(text)
        return 0

    if action == "merge":
        src = onto.search_entities(args.source, limit=1)
        dst = onto.search_entities(args.target, limit=1)
        if not src or not dst:
            print(red("源或目标实体不存在"))
            return 1
        stats = onto.merge_entities(src[0]["id"], dst[0]["id"])
        print(
            green(f"✓ 已把「{src[0]['name']}」并入「{dst[0]['name']}」：")
            + f"关系 {stats['relations']} / 别名 {stats['aliases']} / 提及 {stats['mentions']}"
        )
        return 0

    raise SystemExit(f"未知 graph 子命令：{action}")


# ---------------------------------------------------------------- memory


def cmd_memory(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    conn = ctx.conn()
    provider = ctx.provider()
    store = MemoryStore(conn, ctx.config, provider)
    action = args.action

    if action == "add":
        try:
            result = store.add(
                kind=args.kind,
                content=args.content,
                subject=args.subject or "",
                predicate=args.predicate or "",
                object_text=args.object or "",
                importance=args.importance,
                confidence=args.confidence,
            )
        except ValueError as exc:
            print(red(str(exc)))
            return 2
        if args.json:
            emit(result)
            return 0
        label = {"created": green("+ 新建"), "duplicate": dim("= 已存在（已强化）"), "superseded": yellow("~ 已替代旧值")}[result["status"]]
        print(f"{label}  {result['id']}")
        for old in result["superseded"]:
            print(dim(f"    旧记忆 {old} 已标记为失效（保留历史）"))
        return 0

    if action == "list":
        rows = store.list(kind=args.kind, include_expired=args.all, limit=args.limit)
        if args.json:
            emit(rows)
            return 0
        for m in rows:
            state = "" if not m["valid_to"] else dim(f" [已失效→{m['superseded_by'] or '-'}]")
            tail = f"sal={m['salience']:.2f} hits={m['hits']}"
            print(
                f"  {dim(m['id'][:14])} {m['kind']:<10}"
                f" {truncate(m['content'], 60):<62} {dim(tail)}{state}"
            )
        if not rows:
            print(dim("  （空）"))
        return 0

    if action == "search":
        rows = store.recall(
            args.text, k=args.k, include_history=args.all,
            include_episodes=args.episodes, reinforce=False,
        )
        if args.json:
            emit(rows)
            return 0
        for m in rows:
            score = m["score"]
            print(f"  {dim(f'{score:.3f}')} {m['kind']:<10} {truncate(m['content'], 70)}")
        if not rows:
            print(dim("  （无匹配记忆）"))
        return 0

    if action == "history":
        rows = store.history(args.subject, args.predicate)
        if args.json:
            emit(rows)
            return 0
        for m in rows:
            span = f"{m['valid_from']} → {m['valid_to'] or '现在'}"
            print(f"  {dim(span)}  {m['object_text'] or m['content']}")
        if not rows:
            print(dim("  （该槽位没有历史）"))
        return 0

    if action == "forget":
        ok = store.forget(args.id, hard=args.hard)
        print(green("✓ 已遗忘") if ok else red("× 未找到该记忆"))
        return 0 if ok else 1

    raise SystemExit(f"未知 memory 子命令：{action}")


# ---------------------------------------------------------------- docs / stats / reindex / doctor


def cmd_docs(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    conn = ctx.conn()
    if args.action == "list":
        rows = conn.execute(
            """SELECT d.id, d.title, d.source_uri, d.doc_type, d.updated_at,
                      (SELECT COUNT(*) FROM chunks c WHERE c.doc_id=d.id) AS chunks
               FROM documents d ORDER BY d.updated_at DESC LIMIT ?""",
            (args.limit,),
        ).fetchall()
        if args.json:
            emit([dict(r) for r in rows])
            return 0
        for r in rows:
            print(f"  {dim(r['id'][:14])} {truncate(r['title'], 34):<36} {r['chunks']:>3} 块  {dim(r['source_uri'])}")
        if not rows:
            print(dim("  （还没有文档，先 kb ingest <路径>）"))
        return 0
    if args.action == "rm":
        targets = conn.execute(
            "SELECT id, title FROM documents WHERE id LIKE ? OR source_uri LIKE ?",
            (f"%{args.target}%", f"%{args.target}%"),
        ).fetchall()
        if not targets:
            print(red("× 未找到匹配文档"))
            return 1
        for t in targets:
            remove_document(conn, t["id"])
            print(green(f"✓ 已删除《{t['title']}》"))
        return 0
    raise SystemExit(f"未知 docs 子命令：{args.action}")


def cmd_stats(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    conn = ctx.conn()
    onto = Ontology(conn)
    counts = table_counts(conn)
    mem_stats = MemoryStore(conn, ctx.config, ctx.provider() if not args.no_provider else None).stats()
    onto_stats = onto.stats()
    size = sum(p.stat().st_size for p in [ctx.db, Path(str(ctx.db) + "-wal")] if p.exists())

    payload = {
        "db": str(ctx.db),
        "size": human_bytes(size),
        "counts": counts,
        "fts": analyzer_stats(conn),
        "vector_models": vector_models(conn),
        "ontology": onto_stats,
        "memory": mem_stats,
        "embedding_model": meta_get(conn, "embedding_model"),
        "last_ingest_at": meta_get(conn, "last_ingest_at"),
    }
    if args.json:
        emit(payload)
        return 0

    print(bold("存储"))
    print(f"  {payload['db']}  {dim(payload['size'])}")
    print(f"  文档 {counts['documents']} · 分块 {counts['chunks']} · 向量 {counts['vectors']} · FTS {payload['fts']['fts_rows']}")
    print(bold("\n本体"))
    print(f"  实体 {onto_stats['entities']} · 关系 {onto_stats['relations']} · 类型 {counts['entity_types']}（自动发现 {onto_stats['pending_entity_types']}）")
    for hook in onto_stats["top_hubs"]:
        if hook["degree"]:
            print(f"    · {hook['name']} [{hook['type_name']}] 度数 {hook['degree']}")
    print(bold("\n记忆"))
    active = mem_stats["active"]
    print(f"  活跃 {sum(active.values())} 条 {dim(str(active))} · 已失效 {mem_stats['superseded']} 条")
    print(bold("\n索引"))
    print(f"  embedding={meta_get(conn, 'embedding_model')}  向量模型分布={payload['vector_models']}")
    print(f"  最近摄取={meta_get(conn, 'last_ingest_at')}")
    return 0


def cmd_reindex(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    conn = ctx.conn()
    provider = ctx.provider()
    rows = conn.execute(
        "SELECT id, doc_id, text FROM chunks ORDER BY doc_id, ord"
    ).fetchall()
    if not rows:
        print(dim("没有分块需要重建"))
        return 0
    titles = {
        r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM documents").fetchall()
    }
    texts = [f"{titles.get(r['doc_id'], '')}\n{r['text']}" for r in rows]
    matrix = provider.embedder.embed(texts)
    put_vectors(conn, KIND_CHUNK, provider.embedding_model, [r["id"] for r in rows], matrix)
    print(green(f"✓ 重建 {len(rows)} 个分块向量，模型={provider.embedding_model}"))

    if not args.skip_memory:
        mems = conn.execute("SELECT id, content FROM memories").fetchall()
        if mems:
            mmat = provider.embedder.embed([m["content"] for m in mems])
            put_vectors(conn, KIND_MEMORY, provider.embedding_model, [m["id"] for m in mems], mmat)
            print(green(f"✓ 重建 {len(mems)} 条记忆向量"))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    cfg = ctx.config
    prov = cfg["provider"]
    print(bold("配置"))
    print(f"  provider       {prov['kind']}")
    print(f"  base_url       {prov.get('base_url')}")
    print(f"  chat_model     {prov.get('chat_model')}")
    print(f"  embedding      {prov.get('embedding_model')}")
    print(f"  db             {ctx.db}")
    print(f"  config_file    {cfg.get('_config_path') or dim('（使用内置默认值）')}")

    print(bold("\n连通性"))
    try:
        provider = ctx.provider()
    except SystemExit:
        print(red("  × provider 初始化失败"))
        return 2
    if provider.is_mock:
        print(yellow("  · 离线模式（mock）：不发起任何网络请求"))
        print(dim("    切换真实模型：export ARK_API_KEY=... && KB_PROVIDER=ark kb doctor"))
    else:
        import time as _time

        t0 = _time.perf_counter()
        vec = provider.embedder.embed(["知识库连通性检查"])
        print(green(f"  ✓ embedding ok  dim={vec.shape[1]}  {(_time.perf_counter() - t0) * 1000:.0f}ms"))
        if args.chat:
            t0 = _time.perf_counter()
            out = provider.chat.complete("只回答一个词。", "说“好”", max_tokens=8)
            print(green(f"  ✓ chat ok  {(_time.perf_counter() - t0) * 1000:.0f}ms  → {out[:30]!r}"))

    if ctx.db.exists():
        conn = ctx.conn()
        counts = table_counts(conn)
        print(bold("\n数据"))
        print(f"  文档 {counts['documents']} / 分块 {counts['chunks']} / 实体 {counts['entities']} / 记忆 {counts['memories']}")
        stored = meta_get(conn, "embedding_model")
        if stored and stored != provider.embedding_model:
            print(yellow(f"  ! 库里向量模型为 {stored}，当前为 {provider.embedding_model}：需要 kb reindex --embeddings"))
        elif not stored and counts["chunks"]:
            print(yellow("  ! 库里没有向量元数据，建议 kb reindex"))
    else:
        print(dim("\n  （数据库尚未创建）"))
    return 0


# ---------------------------------------------------------------- eval


DEFAULT_SET = "eval/golden.jsonl"


def cmd_eval(args: argparse.Namespace) -> int:
    action = args.action
    ctx = Ctx(args)

    if action == "build":
        conn = ctx.conn()
        cases = build_draft_cases(conn, limit=args.limit, min_weight=args.min_weight)
        if not cases:
            print(red("没有可生成草稿题的关系：先摄取文档，并确认抽取跑出了带证据的关系"))
            return 1
        n = save_cases(args.out, cases)
        print(green(f"✓ 生成 {n} 道草稿题 → {args.out}"))
        kinds: dict[str, int] = {}
        for c in cases:
            kinds[c.kind] = kinds.get(c.kind, 0) + 1
        print(dim(f"  题型分布 {kinds}"))
        print(dim("  草稿题来自已抽取关系（问题模板化、证据是关系来源分块）。"))
        print(dim("  建议人工删掉噪音、补几种问法，再作为基准集长期使用。"))
        return 0

    if action == "check":
        conn = ctx.conn()
        try:
            cases = load_cases(args.set)
        except (FileNotFoundError, ValueError) as exc:
            print(red(str(exc)))
            return 2
        issues = check_cases(conn, cases)
        if args.json:
            emit({"cases": len(cases), "issues": issues})
            return 0 if (not issues or args.allow_stale) else 1
        if not issues:
            print(green(f"✓ {len(cases)} 题标注全部有效"))
            return 0
        print(yellow(f"发现 {len(issues)} 处标注失效："))
        for item in issues[:30]:
            print(f"  × [{item['id']}] {item['field']}={item['value']}  {item['reason']}")
        print(dim("  重建过分块后，建议用 expected_sections 而不是 expected_chunks。"))
        return 0 if args.allow_stale else 1

    if action == "compare":
        base = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        cand = json.loads(Path(args.candidate).read_text(encoding="utf-8"))
        diff = compare_reports(base, cand)
        if args.json:
            emit(diff)
            return 0
        print(bold("对比"))
        print(f"  基线：{diff['baseline']['generated_at']}  {diff['baseline']['provider']}")
        print(f"  候选：{diff['candidate']['generated_at']}  {diff['candidate']['provider']}")
        scope = diff["scope"]
        print(f"  共同题目 {scope['shared_cases']} 题"
              f"（基线 {scope['baseline_cases']} / 候选 {scope['candidate_cases']}）")
        for warning in diff["warnings"]:
            print(yellow(f"  ! {warning}"))
        print()
        print(f"  {'指标':<18}{'基线':>10}{'候选':>10}{'变化':>12}")
        for row in diff["rows"]:
            before, after, delta = row["baseline"], row["candidate"], row["delta"]
            if before is None or after is None:
                print(f"  {row['metric']:<18}{'n/a':>10}{'n/a':>10}{'n/a':>12}")
                continue
            arrow = "" if abs(delta) < 1e-9 else (green("↑") if delta > 0 else red("↓"))
            print(f"  {row['metric']:<18}{before:>10.3f}{after:>10.3f}{delta:>+10.3f} {arrow}")
        if diff["latency_delta_ms"] is not None:
            print(dim(f"  平均延迟变化 {diff['latency_delta_ms']:+.1f}ms"))
        return 0

    # run
    conn = ctx.conn()
    provider = ctx.provider()
    try:
        cases = load_cases(args.set)
    except (FileNotFoundError, ValueError) as exc:
        print(red(str(exc)))
        if isinstance(exc, FileNotFoundError):
            print(dim("  先 `kb eval build` 生成草稿题，或手写 eval/golden.jsonl"))
        return 2
    if args.exclude_drafts:
        cases = [c for c in cases if not c.draft]
    if not cases:
        print(yellow("评测集是空的"))
        return 1
    with_answer = args.answer if args.answer is not None else provider.is_mock
    if args.answer is None and not provider.is_mock:
        print(dim("  （真实模型默认只跑检索；加 --answer 才会调用 LLM 生成答案，会产生费用）"))
    report = run_eval(
        conn, ctx.config, provider, cases,
        k=args.k, with_answer=with_answer, limit=args.limit,
        progress=lambda m: print(dim(m), file=sys.stderr),
    )
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(dim(f"  完整结果 → {args.out}"), file=sys.stderr)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(render_markdown(report), encoding="utf-8")
        print(dim(f"  Markdown 报告 → {args.report}"), file=sys.stderr)
    if args.json:
        emit(report)
    else:
        print()
        print(render_text(report))
    if args.fail_under is not None:
        hit = report["metrics"]["hit@k"]
        if hit is None or hit < args.fail_under:
            print(red(f"\n✗ hit@k={hit} 低于阈值 {args.fail_under}"))
            return 1
    return 0


# ---------------------------------------------------------------- agent（ADK）


def _load_adk():
    """ADK 是可选依赖：只有 `kb agent` 命令需要它。"""
    try:
        from kb_adk.config import build_adk_config
        from kb_adk.eval import run_agent_eval
        from kb_adk.runtime import KbRuntime
    except ImportError as exc:  # pragma: no cover - 取决于环境
        msg = (
            f"ADK 版不可用：{exc}\n"
            "  uv pip install google-adk（或 uv pip install -e '.[adk]'）\n"
            "  安装后用 .venv/bin/python -m kb agent ... 运行"
        )
        raise SystemExit(red(msg)) from exc
    return build_adk_config, KbRuntime, run_agent_eval


def cmd_agent(args: argparse.Namespace) -> int:
    build_adk_config, KbRuntime, run_agent_eval = _load_adk()
    if getattr(args, "db", None):
        os.environ["KB_DB"] = args.db
    try:
        adk_cfg = build_adk_config(
            config_path=getattr(args, "config", None),
            model=getattr(args, "model", None),
            mode=getattr(args, "mode", None),
            user_id=getattr(args, "user", None),
        )
        runtime = KbRuntime(adk_cfg)
    except Exception as exc:  # noqa: BLE001 - 初始化失败要给出可读原因
        print(red(f"初始化失败：{type(exc).__name__}: {exc}"))
        return 2

    if adk_cfg.note:
        print(yellow(f"! {adk_cfg.note}"))

    try:
        if args.action == "info":
            info = runtime.describe()
            if args.json:
                emit(info)
                return 0
            for key, value in info.items():
                print(f"  {key:<12} {value}")
            return 0

        if args.action == "tools":
            rows = []
            for fn in runtime.toolbox.all_tools():
                doc = (fn.__doc__ or "").strip().splitlines()
                rows.append((fn.__name__, doc[0] if doc else ""))
            if args.json:
                emit([{"name": n, "summary": s} for n, s in rows])
                return 0
            print(bold(f"工具（{len(rows)}）"))
            for name, summary in rows:
                print(f"  {cyan(name):<22} {dim(summary)}")
            return 0

        if args.action == "ask":
            reply = runtime.ask(args.text)
            if args.json:
                emit(reply.to_dict())
                return 0
            print(f"\n{bold('问')}：{args.text}")
            print(hr())
            print(reply.text or dim("（空回答）"))
            if reply.citations:
                print(f"\n{bold('来源')}")
                for cit in reply.citations:
                    print(f"  [{cit.index}] {cit.label()}  {dim(cit.chunk_id)}")
            trace = " → ".join(reply.tool_calls) or "无"
            print(dim(f"\n编排={reply.mode} 模型={reply.model} 工具={trace} 耗时={reply.elapsed_ms}ms"))
            return 0

        if args.action == "chat":
            print(bold("ADK 知识库对话"))
            print(dim("  空行退出 · /reset 重置会话 · /tools 看工具"))
            while True:
                try:
                    question = input(f"\n{cyan('你>')} ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not question:
                    break
                if question in ("exit", "quit", "/exit"):
                    break
                if question == "/reset":
                    runtime.reset()
                    print(dim("会话已重置"))
                    continue
                if question == "/tools":
                    for fn in runtime.toolbox.all_tools():
                        print(f"  {fn.__name__}")
                    continue
                reply = runtime.ask(question, fresh=False)
                print(f"\n{reply.text}")
                if reply.citations:
                    for cit in reply.citations:
                        print(dim(f"  [{cit.index}] {cit.label()}"))
                print(dim(f"  工具={(' → '.join(reply.tool_calls)) or '无'} 耗时={reply.elapsed_ms}ms"))
            return 0

        if args.action == "eval":
            cases = load_cases(args.set)
            if args.limit:
                cases = cases[: args.limit]
            if not cases:
                print(yellow("评测集是空的"))
                return 1
            report = run_agent_eval(
                runtime, cases, k=args.k,
                progress=lambda m: print(dim(m), file=sys.stderr),
            )
            if args.out:
                Path(args.out).parent.mkdir(parents=True, exist_ok=True)
                Path(args.out).write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(dim(f"  完整结果 → {args.out}"), file=sys.stderr)
            if args.report:
                Path(args.report).parent.mkdir(parents=True, exist_ok=True)
                Path(args.report).write_text(render_markdown(report), encoding="utf-8")
                print(dim(f"  Markdown 报告 → {args.report}"), file=sys.stderr)
            if args.json:
                emit(report)
                return 0
            print()
            print(render_text(report))
            usage = (report["meta"].get("tool_usage") or {})
            if usage:
                print(dim("\n工具调用：" + "、".join(f"{k}×{v}" for k, v in usage.items())))
            return 0

        raise SystemExit(f"未知 agent 子命令：{args.action}")
    finally:
        runtime.close()


# ---------------------------------------------------------------- dream

def _print_draft(draft: dict, idx: int) -> None:
    mark = {"merge": cyan("合并"), "supersede": yellow("覆盖"), "review": green("复核")}.get(
        draft["action"], draft["action"])
    suffix = ""
    if draft["action"] == "review":
        state = {"": "待复核", "manual": "待人工确认",
                 "confirmed": "已确认", "rejected": "已否定"}.get(
            draft.get("reviewed") or "", draft.get("reviewed"))
        sim = float(draft.get("similarity") or 0)
        suffix = "  " + dim(f"相似度 {sim:.2f} · {state}")
    print(f"  {idx}. [{mark}] {draft['reason']}{suffix}")


def cmd_dream(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    conn = ctx.conn()
    try:
        store = Dreamer(conn, ctx.config, ctx.provider())
        action = getattr(args, "action", "run")

        if action == "run":
            drafts = store.propose()
            if getattr(args, "json", False):
                emit(drafts)
                return 0
            if not drafts:
                print(green("记忆很干净，没有需要整理的重复或冲突。"))
                return 0
            pending = store.pending()
            print(bold(f"生成 {len(pending)} 条草稿（原记忆未修改）"))
            for i, draft in enumerate(pending, 1):
                _print_draft(draft, i)
            if any(d["action"] == "review" for d in pending):
                print(dim("\n  语义候选需复核：kb dream review --all，再 apply --all；"
                          "不满意：discard --all"))
            else:
                print(dim("\n  确认无误后：kb dream apply --all；不满意：kb dream discard --all"))
            return 0

        if action == "diff":
            drafts = store.pending()
            if getattr(args, "json", False):
                emit(drafts)
                return 0
            if not drafts:
                print(dim("没有待处理的草稿。先运行 `kb dream run`。"))
                return 0
            print(bold(f"待处理草稿（{len(drafts)}）"))
            for i, draft in enumerate(drafts, 1):
                _print_draft(draft, i)
            return 0

        if action == "review":
            drafts = [d for d in store.pending() if d["action"] == "review"]
            if not drafts:
                print(dim("没有待复核的语义候选。"))
                return 0
            if not getattr(args, "all", False) and not getattr(args, "draft_id", None):
                print(red("需要 --id，或加 --all"))
                return 2
            targets = drafts if getattr(args, "all", False) else [
                d for d in drafts if d["id"] == args.draft_id]
            for draft in targets:
                result = store.review(draft["id"])
                print(f"  {draft['id'][:12]} → {result['status']}" + (
                    f"（{result.get('reason', '')}）" if result.get("reason") else ""))
            return 0

        if action == "apply":
            if getattr(args, "all", False):
                drafts = store.pending()
                if not drafts:
                    print(dim("没有待处理的草稿。"))
                    return 0
                for draft in drafts:
                    try:
                        result = store.apply(draft["id"], force=getattr(args, "force", False))
                    except ValueError as exc:
                        print(yellow(f"跳过 {draft['id'][:12]}：{exc}"))
                        continue
                    print(green(f"已合并 {len(result['superseded'])} 条 → {result['kept'][:12]}"))
                return 0
            if not getattr(args, "draft_id", None):
                print(red("需要 --id，或加 --all"))
                return 2
            result = store.apply(args.draft_id, force=getattr(args, "force", False))
            print(green(f"已应用：保留 {result['kept'][:12]}，降级 {len(result['superseded'])} 条"))
            return 0

        if action == "discard":
            if getattr(args, "all", False):
                n = store.discard_all()
                print(dim(f"已丢弃 {n} 条草稿（原记忆不变）"))
                return 0
            if not getattr(args, "draft_id", None):
                print(red("需要 --id，或加 --all"))
                return 2
            store.discard(args.draft_id)
            print(dim("已丢弃该草稿"))
            return 0

        print(red(f"未知 dream 子命令：{action}"))
        return 2
    finally:
        ctx.close()


# ---------------------------------------------------------------- term

def cmd_term(args: argparse.Namespace) -> int:
    ctx = Ctx(args)
    conn = ctx.conn()
    try:
        store = TermsStore(conn, ctx.config)
        action = getattr(args, "action", "list")

        if action == "seed":
            report = store.seed_from_entities(limit=args.limit)
            print(bold("术语草稿"))
            print(f"  新建 {report['created']} · 更新 {report['updated']} · 合计 {report['total']}")
            print(dim("  草稿不参与检索增强；审核后运行 `kb term certify --all`"))
            return 0

        if action == "list":
            rows = store.list(certified_only=getattr(args, "certified", False),
                              limit=getattr(args, "limit", 50))
            if getattr(args, "json", False):
                emit(rows)
                return 0
            if not rows:
                print(yellow("还没有术语，先运行 `kb term seed`"))
                return 0
            print(bold(f"术语（{len(rows)}）"))
            for row in rows:
                mark = green("✓") if row["certified"] else dim("·")
                syn = ("／" + "、".join(row["synonyms"][:3])) if row["synonyms"] else ""
                print(f"  {mark} {cyan(row['name']):<14} {dim(row['kind']):<8} {dim(row['description'][:40])}{dim(syn)}")
            return 0

        if action == "certify":
            if getattr(args, "all", False):
                n = store.certify_all()
                print(green(f"已认证 {n} 个术语"))
                return 0
            if not args.name:
                print(red("需要术语名，或加 --all"))
                return 2
            term = store.certify(args.name, description=getattr(args, "description", None))
            print(green(f"已认证：{term['name']}"))
            return 0

        if action == "check":
            if store.count(certified_only=True) == 0:
                print(yellow("还没有已认证的术语，覆盖度恒为 0。"))
                print(dim("  先 `kb term seed` 生成草稿，再用 `kb term certify --all` 或逐个认证。"))
                return 1
            result = store.check_answerable(args.text)
            if getattr(args, "json", False):
                emit(result)
                return 0
            verdict = green("可以回答") if result["answerable"] else yellow("语义层覆盖不足")
            print(f"{bold('问题')}：{args.text}")
            print(f"{bold('覆盖度')}：{result['ratio']:.2f}（{result['total'] - len(result['missing'])}/{result['total']}）· {verdict}")
            if result["matched_terms"]:
                print(f"  {dim('命中术语')}：{'、'.join(result['matched_terms'][:6])}")
            if result["missing"]:
                print(f"  {dim('未覆盖')}：{'、'.join(result['missing'][:8])}")
            return 0

        print(red(f"未知 term 子命令：{action}"))
        return 2
    finally:
        ctx.close()


# ---------------------------------------------------------------- demo


DEMO_QUESTIONS = [
    ("北极星项目的负责人是谁？", "图谱 + 文档混合：关系边直接给出答案"),
    ("统一登录服务和订单中心分别依赖什么？", "多跳：一次问两个实体的邻域"),
    ("订单中心从 MySQL 迁移到了什么？", "关系抽取：迁移类事实"),
    ("我们为什么放弃自建消息队列？", "决策类知识 + 记忆"),
]


def cmd_demo(args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parent.parent
    samples = root / "samples"
    if not samples.exists():
        print(red(f"找不到示例目录：{samples}"))
        return 2
    ctx = Ctx(args)
    if args.reset and ctx.db.exists():
        for suffix in ("", "-wal", "-shm"):
            f = Path(str(ctx.db) + suffix)
            if f.exists():
                f.unlink()
    conn = ctx.conn(create=True)
    provider = ctx.provider()
    print(bold(f"\n① 摄取示例文档  {dim(str(samples))}"))
    ingester = Ingester(conn, ctx.config, provider, progress=lambda m: None)
    report = ingester.ingest_paths([str(samples)])
    t = report.totals
    print(
        f"   文档 {t['created']} 篇 · 分块 {t['chunks']} · 实体 {t['entities_new']} 新建"
        f" · 关系 {t['relations']} · 记忆 {t['memories']}"
    )
    if t["errors"]:
        for d in report.docs:
            if d.status == "error":
                print(red(f"   × {d.source_uri}: {d.detail}"))

    print(bold("\n② 本体概览"))
    onto = Ontology(conn)
    for row in onto.type_stats()[:6]:
        print(f"   {row['name']:<6} {row['entities']:>3} 个实体")
    hubs = conn.execute(
        """SELECT e.name, e.type_name, COUNT(r.id) AS degree FROM entities e
           LEFT JOIN relations r ON r.src_id=e.id OR r.dst_id=e.id
           GROUP BY e.id HAVING degree > 0 ORDER BY degree DESC LIMIT 4"""
    ).fetchall()
    hub_labels = [f"{h['name']}({h['degree']})" for h in hubs]
    print(f"   核心节点：{'、'.join(hub_labels) or '（无）'}")

    retriever = Retriever(conn, ctx.config, provider)
    print(bold("\n③ 问答"))
    for question, why in DEMO_QUESTIONS:
        result = retriever.search(question, k=args.k)
        answer = synthesize(question, result, provider, ctx.config)
        print(hr())
        print(f"{cyan('Q')} {question}   {dim(why)}")
        body = answer.text.strip().splitlines()
        for line in body[:6]:
            print(f"  {line}")
        for cit in answer.citations[:3]:
            print(dim(f"  [{cit.index}] {cit.label()}"))
        print(dim(f"  模式={answer.mode} 置信度={answer.confidence}"))

    print(hr())
    print(bold("\n④ 下一步"))
    print("   kb query \"...\"          只看召回（调检索用）")
    print("   kb graph show 北极星项目  看某实体的多跳邻域")
    print("   kb graph export --format dot -o graph.dot")
    print("   kb memory list           看长期记忆")
    print("   kb eval run              跑 27 题基准集（回归/换模型对比）")
    print("   export ARK_API_KEY=... KB_PROVIDER=ark kb doctor   切到真模型")
    return 0


# ---------------------------------------------------------------- argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kb",
        description="RAG memory + ontology 知识库",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "常用路径：\n"
            "  kb init && kb ingest samples/ && kb ask \"北极星项目的负责人是谁？\"\n"
            "  KB_PROVIDER=ark ARK_API_KEY=... kb doctor --chat\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"kb {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="配置文件路径（默认 ./kb.config.json）")
    common.add_argument("--db", help="数据库路径（覆盖配置）")
    common.add_argument("--provider", help="mock / ark / openai（覆盖配置与环境变量）")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", parents=[common], help="初始化知识库")
    p.add_argument("--reset", action="store_true", help="删掉现有库重建")
    p.add_argument("--yes", action="store_true", help="跳过删除确认")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("ingest", parents=[common], help="摄取文件或目录")
    p.add_argument("paths", nargs="+")
    p.add_argument("--type", default=None, help="文档类型标签，默认 note")
    p.add_argument("--no-recursive", action="store_true")
    p.add_argument("--force", action="store_true", help="内容未变也重新处理")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("query", parents=[common], help="只看检索结果")
    p.add_argument("text")
    p.add_argument("-k", type=int, default=None)
    p.add_argument("--history", action="store_true", help="包含已失效记忆")
    p.add_argument("--episodes", action="store_true", help="包含文档摄入时间线")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_query)

    p = sub.add_parser("ask", parents=[common], help="带引用问答")
    p.add_argument("text")
    p.add_argument("-k", type=int, default=None)
    p.add_argument("--history", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--show-context", action="store_true", help="同时打印召回明细")
    p.add_argument("-q", "--quiet-warnings", action="store_true")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("graph", parents=[common], help="查看 / 导出本体")
    gsub = p.add_subparsers(dest="action", required=True)
    g = gsub.add_parser("types", parents=[common], help="类型与规模")
    g.add_argument("--json", action="store_true")
    g.set_defaults(func=cmd_graph)
    g = gsub.add_parser("entities", parents=[common], help="实体列表")
    g.add_argument("--limit", type=int, default=30)
    g.add_argument("--json", action="store_true")
    g.set_defaults(func=cmd_graph)
    g = gsub.add_parser("show", parents=[common], help="实体的多跳邻域")
    g.add_argument("name")
    g.add_argument("--hops", type=int, default=2)
    g.add_argument("--min-weight", type=float, default=0.3)
    g.add_argument("--json", action="store_true")
    g.set_defaults(func=cmd_graph)
    g = gsub.add_parser("export", parents=[common], help="导出 json / dot")
    g.add_argument("--format", choices=["json", "dot"], default="json")
    g.add_argument("--limit", type=int, default=5000)
    g.add_argument("-o", "--output")
    g.set_defaults(func=cmd_graph)
    g = gsub.add_parser("merge", parents=[common], help="合并重复实体")
    g.add_argument("source")
    g.add_argument("target")
    g.set_defaults(func=cmd_graph)

    p = sub.add_parser("memory", parents=[common], help="长期记忆管理")
    msub = p.add_subparsers(dest="action", required=True)
    m = msub.add_parser("add", parents=[common], help="写入一条记忆")
    m.add_argument("content")
    m.add_argument("--kind", default="fact", choices=["fact", "preference", "episode", "task"])
    m.add_argument("--subject", default="")
    m.add_argument("--predicate", default="")
    m.add_argument("--object", default="")
    m.add_argument("--importance", type=float, default=0.6)
    m.add_argument("--confidence", type=float, default=0.8)
    m.add_argument("--json", action="store_true")
    m.set_defaults(func=cmd_memory)
    m = msub.add_parser("list", parents=[common], help="列出记忆")
    m.add_argument("--kind", default=None)
    m.add_argument("--all", action="store_true", help="包含已失效")
    m.add_argument("--limit", type=int, default=30)
    m.add_argument("--json", action="store_true")
    m.set_defaults(func=cmd_memory)
    m = msub.add_parser("search", parents=[common], help="按语义/词法召回记忆")
    m.add_argument("text")
    m.add_argument("-k", type=int, default=6)
    m.add_argument("--all", action="store_true")
    m.add_argument("--episodes", action="store_true", help="包含文档摄入时间线")
    m.add_argument("--json", action="store_true")
    m.set_defaults(func=cmd_memory)
    m = msub.add_parser("history", parents=[common], help="某槽位的版本历史")
    m.add_argument("subject")
    m.add_argument("predicate")
    m.add_argument("--json", action="store_true")
    m.set_defaults(func=cmd_memory)
    m = msub.add_parser("forget", parents=[common], help="遗忘（软删/硬删）")
    m.add_argument("id")
    m.add_argument("--hard", action="store_true")
    m.set_defaults(func=cmd_memory)

    p = sub.add_parser("dream", parents=[common], help="离线整理记忆：产草稿，人工确认后落地")
    dsub = p.add_subparsers(dest="action", required=True)
    a = dsub.add_parser("run", help="扫描重复/冲突，生成草稿")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_dream)
    a = dsub.add_parser("diff", help="查看待处理草稿")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_dream)
    a = dsub.add_parser("review", help="让 LLM 复核语义候选（mock 环境标记为人工确认）")
    a.add_argument("--id", dest="draft_id")
    a.add_argument("--all", action="store_true")
    a.set_defaults(func=cmd_dream)
    a = dsub.add_parser("apply", help="应用草稿（旧记忆打 valid_to）")
    a.add_argument("--id", dest="draft_id")
    a.add_argument("--all", action="store_true")
    a.add_argument("--force", action="store_true", help="未复核的语义候选也强制合并")
    a.set_defaults(func=cmd_dream)
    a = dsub.add_parser("discard", help="丢弃草稿")
    a.add_argument("--id", dest="draft_id")
    a.add_argument("--all", action="store_true")
    a.set_defaults(func=cmd_dream)

    p = sub.add_parser("term", parents=[common], help="术语层：生成 / 浏览 / 认证 / 覆盖度")
    tsub = p.add_subparsers(dest="action", required=True)
    a = tsub.add_parser("seed", help="从现有实体生成术语草稿")
    a.add_argument("--limit", type=int, default=40)
    a.set_defaults(func=cmd_term)
    a = tsub.add_parser("list", help="列出术语")
    a.add_argument("--certified", action="store_true")
    a.add_argument("--limit", type=int, default=50)
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_term)
    a = tsub.add_parser("certify", help="认证术语（人工确认后才参与检索增强）")
    a.add_argument("name", nargs="?")
    a.add_argument("--all", action="store_true")
    a.add_argument("--description")
    a.set_defaults(func=cmd_term)
    a = tsub.add_parser("check", help="检索前覆盖度检查")
    a.add_argument("text")
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_term)

    p = sub.add_parser("docs", parents=[common], help="文档管理")
    dsub = p.add_subparsers(dest="action", required=True)
    d = dsub.add_parser("list", parents=[common])
    d.add_argument("--limit", type=int, default=30)
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_docs)
    d = dsub.add_parser("rm", parents=[common], help="按 id 或路径片段删除")
    d.add_argument("target")
    d.set_defaults(func=cmd_docs)

    p = sub.add_parser("stats", parents=[common], help="库体检")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-provider", action="store_true")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("reindex", parents=[common], help="用当前模型重建向量")
    p.add_argument("--embeddings", action="store_true", help="兼容旧参数（默认就会重建）")
    p.add_argument("--skip-memory", action="store_true")
    p.set_defaults(func=cmd_reindex)

    p = sub.add_parser("doctor", parents=[common], help="配置与连通性检查")
    p.add_argument("--chat", action="store_true", help="额外测试 chat 通道（会产生费用）")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("eval", parents=[common], help="评测：跑标注集 / 生成草稿 / 对比")
    esub = p.add_subparsers(dest="action", required=True)
    e = esub.add_parser("run", parents=[common], help="在标注集上评测")
    e.add_argument("--set", default=DEFAULT_SET, help=f"标注集路径（默认 {DEFAULT_SET}）")
    e.add_argument("-k", type=int, default=8)
    e.add_argument("--answer", action=argparse.BooleanOptionalAction, default=None,
                   help="是否生成答案（默认：mock 生成、真实模型不生成）")
    e.add_argument("--limit", type=int, default=None)
    e.add_argument("--exclude-drafts", action="store_true", help="跳过 draft 标记的题")
    e.add_argument("--json", action="store_true")
    e.add_argument("--out", help="把完整结果写成 JSON")
    e.add_argument("--report", help="把 Markdown 报告写到指定路径")
    e.add_argument("--fail-under", type=float, default=None,
                   help="hit@k 低于该值时退出码为 1（给 CI 用）")
    e.set_defaults(func=cmd_eval, action="run")

    e = esub.add_parser("build", parents=[common], help="从已抽取关系生成草稿题")
    e.add_argument("--out", default="eval/golden.draft.jsonl")
    e.add_argument("--limit", type=int, default=40)
    e.add_argument("--min-weight", type=float, default=0.6)
    e.set_defaults(func=cmd_eval, action="build")

    e = esub.add_parser("check", parents=[common], help="校验标注是否失效")
    e.add_argument("--set", default=DEFAULT_SET)
    e.add_argument("--allow-stale", action="store_true", help="有失效标注也返回 0")
    e.add_argument("--json", action="store_true")
    e.set_defaults(func=cmd_eval, action="check")

    e = esub.add_parser("compare", parents=[common], help="对比两次评测结果")
    e.add_argument("baseline")
    e.add_argument("candidate")
    e.add_argument("--json", action="store_true")
    e.set_defaults(func=cmd_eval, action="compare")

    p = sub.add_parser("agent", parents=[common], help="ADK 版：agent 问答 / 评测（需 google-adk）")
    asub = p.add_subparsers(dest="action", required=True)

    a = asub.add_parser("ask", parents=[common], help="单轮提问（走 ADK agent）")
    a.add_argument("text")
    a.add_argument("--model", choices=["mock", "ark", "openai", "gemini"], default=None)
    a.add_argument("--mode", choices=["pipeline", "single"], default=None)
    a.add_argument("--user", default=None)
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_agent, action="ask")

    a = asub.add_parser("chat", parents=[common], help="多轮对话（复用 session）")
    a.add_argument("--model", choices=["mock", "ark", "openai", "gemini"], default=None)
    a.add_argument("--mode", choices=["pipeline", "single"], default=None)
    a.add_argument("--user", default=None)
    a.set_defaults(func=cmd_agent, action="chat")

    a = asub.add_parser("eval", parents=[common], help="用 ADK agent 跑评测集")
    a.add_argument("--set", default=DEFAULT_SET)
    a.add_argument("-k", type=int, default=8)
    a.add_argument("--limit", type=int, default=None)
    a.add_argument("--model", choices=["mock", "ark", "openai", "gemini"], default=None)
    a.add_argument("--mode", choices=["pipeline", "single"], default=None)
    a.add_argument("--user", default=None)
    a.add_argument("--json", action="store_true")
    a.add_argument("--out")
    a.add_argument("--report")
    a.set_defaults(func=cmd_agent, action="eval")

    a = asub.add_parser("tools", parents=[common], help="列出 agent 可用的工具")
    a.add_argument("--model", choices=["mock", "ark", "openai", "gemini"], default=None)
    a.add_argument("--mode", choices=["pipeline", "single"], default=None)
    a.add_argument("--user", default=None)
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_agent, action="tools")

    a = asub.add_parser("info", parents=[common], help="查看编排与模型配置")
    a.add_argument("--model", choices=["mock", "ark", "openai", "gemini"], default=None)
    a.add_argument("--mode", choices=["pipeline", "single"], default=None)
    a.add_argument("--user", default=None)
    a.add_argument("--json", action="store_true")
    a.set_defaults(func=cmd_agent, action="info")

    p = sub.add_parser("demo", parents=[common], help="一键跑示例")
    p.add_argument("--reset", action="store_true")
    p.add_argument("-k", type=int, default=6)
    p.set_defaults(func=cmd_demo)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
