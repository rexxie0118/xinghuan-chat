"""Artifact storage: local filesystem or S3-compatible object storage."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .artifacts import LocalArtifactStore


class S3ArtifactStore:
    """Local staging + S3 upload. Downloads still use a local cache path."""

    def __init__(self, settings: Any) -> None:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover - optional infra
            raise RuntimeError("S3 对象存储需要安装 boto3") from exc
        self.bucket = os.getenv("WEBCHAT_S3_BUCKET", getattr(settings, "s3_bucket", ""))
        if not self.bucket:
            raise RuntimeError("WEBCHAT_S3_BUCKET 未配置")
        self.prefix = (os.getenv("WEBCHAT_S3_PREFIX") or getattr(settings, "s3_prefix", "artifacts")).strip("/")
        self.local = LocalArtifactStore(settings.data_dir)
        self.client = boto3.client(
            "s3",
            endpoint_url=os.getenv("WEBCHAT_S3_ENDPOINT") or getattr(settings, "s3_endpoint", None) or None,
            region_name=os.getenv("WEBCHAT_S3_REGION") or getattr(settings, "s3_region", None) or None,
            config=Config(s3={"addressing_style": "path"}),
        )

    def persist(self, **kwargs: Any) -> list[dict[str, Any]]:
        artifacts = self.local.persist(**kwargs)
        run_id = kwargs["run_id"]
        for artifact in artifacts:
            source = self.local.path_for(run_id, artifact["name"])
            if source is None:
                continue
            key = f"{self.prefix}/{run_id}/{artifact['name']}".lstrip("/")
            self.client.upload_file(str(source), self.bucket, key)
            artifact["object_key"] = key
        return artifacts

    def path_for(self, run_id: str, name: str) -> Path | None:
        local = self.local.path_for(run_id, name)
        if local is not None:
            return local
        key = f"{self.prefix}/{run_id}/{name}".lstrip("/")
        target = self.local.root / run_id / name
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.client.download_file(self.bucket, key, str(target))
        except Exception:  # noqa: BLE001
            return None
        return target


def build_artifact_store(settings: Any):
    kind = (os.getenv("WEBCHAT_OBJECT_STORE") or getattr(settings, "object_store", "local") or "local").lower()
    if kind in {"s3", "minio", "oss", "r2"}:
        return S3ArtifactStore(settings)
    return LocalArtifactStore(settings.data_dir)
