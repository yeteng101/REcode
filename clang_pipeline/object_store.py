from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol


class ObjectStore(Protocol):
    def put_file(self, key: str, source: Path) -> dict: ...
    def open(self, key: str) -> BinaryIO: ...
    def delete_prefix(self, prefix: str) -> None: ...


def validate_key(key: str) -> str:
    if not key or "\\" in key or PurePosixPath(key).is_absolute() or any(
        part in {"", ".", ".."} for part in key.split("/")
    ):
        raise ValueError("invalid object key")
    return key


class FileObjectStore:
    """Atomic file objects on a persistent volume independent of workspaces."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key):
        target = self.root.joinpath(*validate_key(key).split("/")).resolve()
        if not target.is_relative_to(self.root):
            raise ValueError("object path escapes store")
        return target

    def put_file(self, key: str, source: Path) -> dict:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".upload-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as output, source.open("rb") as incoming:
                shutil.copyfileobj(incoming, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
            if os.name != "nt":
                directory = os.open(target.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return {"key": key, "size": target.stat().st_size, "backend": "file"}

    def open(self, key: str) -> BinaryIO:
        return self._path(key).open("rb")

    def delete_prefix(self, prefix: str) -> None:
        target = self._path(prefix)
        if target.exists():
            try:
                shutil.rmtree(target)
            except FileNotFoundError:
                pass


class S3ObjectStore:
    """S3-compatible objects; boto3 uses the normal SDK credential chain."""

    def __init__(self, bucket: str, *, prefix: str = "recode", endpoint_url=None, client=None):
        if not bucket:
            raise ValueError("S3 bucket is required")
        self.bucket = bucket
        self.prefix = validate_key(prefix.strip("/")) if prefix.strip("/") else ""
        if client is None:
            try:
                import boto3
            except ImportError as exc:
                raise RuntimeError("S3 storage requires boto3; install requirements-jobs-s3.txt") from exc
            client = boto3.client("s3", endpoint_url=endpoint_url)
        self.client = client

    def _key(self, key):
        return "/".join(part for part in (self.prefix, validate_key(key)) if part)

    def put_file(self, key: str, source: Path) -> dict:
        self.client.upload_file(str(source), self.bucket, self._key(key))
        return {"key": key, "size": source.stat().st_size, "backend": "s3"}

    def open(self, key: str) -> BinaryIO:
        return self.client.get_object(Bucket=self.bucket, Key=self._key(key))["Body"]

    def delete_prefix(self, prefix: str) -> None:
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self._key(prefix) + "/"):
            objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if objects:
                response = self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": objects, "Quiet": True})
                if response.get("Errors"):
                    raise OSError(f"S3 deletion failed: {response['Errors']}")
