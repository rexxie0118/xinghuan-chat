"""Small local artifact store for imported-Agent runs.

Local runner artifacts are copied out of the ephemeral workspace before cleanup.
The UI downloads them through an authenticated endpoint; no host path is exposed.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import zipfile
from pathlib import Path
from typing import Any, Iterable

from .util import iso


class LocalArtifactStore:
    def __init__(self, data_dir: Path | str) -> None:
        self.root = Path(data_dir) / "artifacts"
        self.root.mkdir(parents=True, exist_ok=True)

    def persist(
        self,
        *,
        run_id: str,
        input_dir: Path,
        output_dir: Path,
        initial_paths: set[str],
    ) -> list[dict[str, Any]]:
        target = self.root / run_id
        target.mkdir(parents=True, exist_ok=True)
        artifacts: list[dict[str, Any]] = []

        new_inputs = [
            path
            for path in input_dir.rglob("*")
            if path.is_file() and not path.is_symlink()
            and path.relative_to(input_dir).as_posix() not in initial_paths
        ]
        output_files = [
            path for path in output_dir.rglob("*")
            if path.is_file() and not path.is_symlink()
        ]

        if new_inputs:
            bundle = target / "workspace.zip"
            with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
                for path in sorted(input_dir.rglob("*")):
                    if path.is_file() and not path.is_symlink():
                        zf.write(path, path.relative_to(input_dir).as_posix())
                for path in sorted(output_dir.rglob("*")):
                    if path.is_file() and not path.is_symlink():
                        zf.write(path, f"output/{path.relative_to(output_dir).as_posix()}")
            artifacts.append(self._meta(bundle, "workspace.zip"))
            return artifacts

        for path in output_files:
            rel = path.relative_to(output_dir).as_posix()
            target_file = target / rel
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target_file)
            artifacts.append(self._meta(target_file, rel))
        return artifacts

    @staticmethod
    def _meta(path: Path, name: str) -> dict[str, Any]:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return {"name": name, "size": path.stat().st_size, "sha256": digest}

    def path_for(self, run_id: str, name: str) -> Path | None:
        base = (self.root / run_id).resolve()
        candidate = (base / name).resolve()
        if not candidate.is_relative_to(base) or not candidate.is_file():
            return None
        return candidate


def record_run(
    conn,
    *,
    run_id: str,
    user_id: str,
    agent_id: str,
    agent_name: str,
    execution: dict[str, Any],
) -> None:
    """Persist run ownership so artifacts can be downloaded safely."""
    import json

    with conn:
        conn.execute(
            "INSERT INTO agent_runs(id, user_id, agent_id, agent_name,"
            " created_at, execution_json) VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET"
            " user_id=excluded.user_id, agent_id=excluded.agent_id,"
            " agent_name=excluded.agent_name, created_at=excluded.created_at,"
            " execution_json=excluded.execution_json",
            (run_id, user_id, agent_id, agent_name, iso(), json.dumps(execution, ensure_ascii=False)),
        )


def owns_run(conn, run_id: str, user_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM agent_runs WHERE id=? AND user_id=?", (run_id, user_id)
    ).fetchone()
    return row is not None


def cleanup_artifacts(data_dir: Path | str, run_id: str) -> None:
    shutil.rmtree(Path(data_dir) / "artifacts" / run_id, ignore_errors=True)
