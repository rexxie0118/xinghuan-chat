"""Deterministic memory-quality evaluations.

Run:
    python -m webchat.memory_evals
    python -m webchat.memory_evals --json

These are behavioral regressions, not a benchmark leaderboard. They cover
constraint recall, supersession, scope boundaries, approval, provenance,
quality gating and rollback using a temporary SQLite database.
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import repository as repo
from .db import connect, init_db
from .user_memory import UserMemoryService


@dataclass
class MemoryEvalResult:
    name: str
    ok: bool
    detail: str = ""
    duration_ms: int = 0

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
        }


def _setup(tmp: Path) -> tuple[UserMemoryService, dict[str, dict[str, Any]]]:
    db = tmp / "memory-eval.sqlite3"
    init_db(db)
    conn = connect(db)
    try:
        users = {
            "product": repo.user_public(repo.create_user(
                conn, username="product", display_name="产品", password_hash="x",
                department="产品部",
            )),
            "engineering": repo.user_public(repo.create_user(
                conn, username="engineering", display_name="研发", password_hash="x",
                department="研发部",
            )),
            "manager": repo.user_public(repo.create_user(
                conn, username="manager", display_name="经理", password_hash="x",
                department="产品部", role="manager",
            )),
            "admin": repo.user_public(repo.create_user(
                conn, username="admin", display_name="管理员", password_hash="x",
                department="产品部", role="admin",
            )),
        }
    finally:
        conn.close()
    return UserMemoryService(db), users


def _early_constraint(service: UserMemoryService, users: dict[str, dict[str, Any]]) -> str:
    user = users["product"]
    service.remember(user["id"], kind="preference", content="回答尽量简洁")
    recalled = service.recall(user["id"], "帮我写一份周报", user=user)
    assert any("简洁" in item["content"] for item in recalled), recalled
    return "cross-topic preference recalled without lexical overlap"


def _stale_fact(service: UserMemoryService, users: dict[str, dict[str, Any]]) -> str:
    user = users["product"]
    service.remember(
        user["id"], kind="fact", content="我的名字是张三",
        subject="用户", predicate="名字", object_text="张三",
    )
    service.remember(
        user["id"], kind="fact", content="我的名字是李四",
        subject="用户", predicate="名字", object_text="李四",
    )
    current = service.list(user["id"], user=user)
    assert len(current) == 1 and "李四" in current[0]["content"], current
    recalled = service.recall(user["id"], "我叫什么？", user=user)
    assert not any("张三" in item["content"] for item in recalled), recalled
    return "new slot value supersedes old value; stale fact not recalled"


def _scope_boundary(service: UserMemoryService, users: dict[str, dict[str, Any]]) -> str:
    product = users["product"]
    engineering = users["engineering"]
    admin = users["admin"]
    dept = service.remember(
        product["id"], kind="fact", content="产品部共享流程",
        scope="department", scope_id="产品部",
    )
    org = service.remember(
        product["id"], kind="fact", content="全公司安全规范",
        scope="org", scope_id="org",
    )
    service.approve(dept["id"], approver=admin["id"], user=admin)
    service.approve(org["id"], approver=admin["id"], user=admin)
    own = service.recall(product["id"], "共享", user=product)
    other = service.recall(engineering["id"], "规范", user=engineering)
    assert any(item["id"] == dept["id"] for item in own)
    assert not any(item["id"] == dept["id"] for item in other)
    assert any(item["id"] == org["id"] for item in other)
    return "department memory hidden from other department; org memory shared"


def _approval_gate(service: UserMemoryService, users: dict[str, dict[str, Any]]) -> str:
    product = users["product"]
    manager = users["manager"]
    pending = service.remember(
        product["id"], kind="fact", content="待审批的部门经验",
        scope="department", scope_id="产品部",
    )
    assert pending["snapshot"]["status"] == "pending"
    before = service.recall(product["id"], "部门经验", user=product)
    assert not any(item["id"] == pending["id"] for item in before)
    approved = service.approve(pending["id"], approver=manager["id"], user=manager)
    assert approved and approved["status"] == "active"
    after = service.recall(product["id"], "部门经验", user=product)
    assert any(item["id"] == pending["id"] for item in after)
    return "shared memory pending until manager approval"


def _provenance_and_quality(service: UserMemoryService, users: dict[str, dict[str, Any]]) -> str:
    user = users["product"]
    skipped = service.remember(
        user["id"], kind="fact", content="临时", confidence=0.2, auto=True
    )
    assert skipped["status"] == "skipped"
    created = service.remember(
        user["id"], kind="fact", content="来源可追溯",
        source_uri="conversation:c1",
        evidence_snippet="用户原话",
    )
    row = service.snapshot(created["id"])
    assert row and row["source_uri"] == "conversation:c1"
    assert row["evidence_snippet"] == "用户原话"
    assert row["created_by"] == user["id"]
    return "low-confidence skipped; source/evidence/creator stored"


def _rollback(service: UserMemoryService, users: dict[str, dict[str, Any]]) -> str:
    user = users["product"]
    created = service.remember(user["id"], kind="fact", content="可回滚记忆")
    snapshot = created["snapshot"]
    assert service.forget(user["id"], created["id"], user=user)
    result = service.undo_event({"op": "delete", "snapshot": snapshot})
    assert result and result["action"] == "restore"
    assert any(item["id"] == created["id"] for item in service.list(user["id"], user=user))
    return "delete snapshot restores memory"


CASES: list[tuple[str, Callable[[UserMemoryService, dict[str, dict[str, Any]]], str]]] = [
    ("early_constraint", _early_constraint),
    ("stale_fact", _stale_fact),
    ("scope_boundary", _scope_boundary),
    ("approval_gate", _approval_gate),
    ("provenance_and_quality", _provenance_and_quality),
    ("rollback", _rollback),
]


def run_memory_evals() -> list[MemoryEvalResult]:
    out = []
    for name, case in CASES:
        tmp = Path(tempfile.mkdtemp(prefix="memory-eval-"))
        started = time.perf_counter()
        try:
            service, users = _setup(tmp)
            detail = case(service, users)
            out.append(
                MemoryEvalResult(name, True, detail, int((time.perf_counter() - started) * 1000))
            )
        except Exception as exc:  # noqa: BLE001
            out.append(
                MemoryEvalResult(
                    name,
                    False,
                    f"{type(exc).__name__}: {exc}",
                    int((time.perf_counter() - started) * 1000),
                )
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Xinghuan Chat memory evals")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    results = run_memory_evals()
    if args.json:
        print(json.dumps([result.public() for result in results], ensure_ascii=False, indent=2))
    else:
        for result in results:
            print(f"{'PASS' if result.ok else 'FAIL'}  {result.name}  ({result.duration_ms} ms)")
            if result.detail:
                print(f"      {result.detail}")
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
