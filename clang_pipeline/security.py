from __future__ import annotations

import ipaddress
import re
import socket
import tarfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse, quote

from .execution import AnalysisError


class SecurityError(AnalysisError):
    code = "security_policy_denied"


def normalize_commit_sha(commit: str | None) -> str | None:
    if isinstance(commit, str) and re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        return commit.lower()
    return commit


def github_archive_url(url: str, branch=None, commit=None) -> str:
    commit = normalize_commit_sha(commit)
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.port not in {None, 443} or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SecurityError("sandbox accepts only public GitHub HTTPS repository URLs")
    parts = parsed.path.strip("/").split("/")
    if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", p) and p not in {".", ".."} for p in parts):
        raise SecurityError("expected https://github.com/owner/repository")
    owner, repo = parts
    repo = repo.removesuffix(".git")
    if not repo:
        raise SecurityError("empty repository name")
    ref = commit or branch or "HEAD"
    if not isinstance(ref, str) or len(ref) > 200 or not re.fullmatch(r"[A-Za-z0-9_./-]+", ref) or ".." in ref or ref.startswith(("/", "-")):
        raise SecurityError("invalid branch or commit")
    if commit and not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise SecurityError("commit must be a full 40-character SHA")
    return f"https://codeload.github.com/{owner}/{repo}/tar.gz/{quote(ref, safe='')}"


def public_host(host: str):
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise SecurityError("repository endpoint resolved to a non-public address")


def extract_regular_tar(source, destination: Path, *, max_files=20000,
                        max_bytes=1024 * 1024 * 1024, strip_root=False):
    """Never extract links/devices, never use extractall on untrusted archives."""
    destination.mkdir(parents=True, exist_ok=True)
    count = total = 0
    with tarfile.open(fileobj=source, mode="r|*") as archive:
        for member in archive:
            parts = PurePosixPath(member.name).parts
            if member.name.startswith("/") or "\\" in member.name or any(p in {"..", "."} or ":" in p for p in parts):
                raise SecurityError("unsafe archive path")
            if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                raise SecurityError("archive contains a link or special file")
            count += 1
            total += member.size
            if count > max_files or total > max_bytes or member.size < 0:
                raise SecurityError("archive exceeds file or expanded-byte quota")
            if strip_root:
                parts = parts[1:]
            if not parts:
                continue
            if any(p.lower() in {".git", ".ssh", ".aws", ".env", "agents.md", "claude.md", ".claude", ".codex", "gold"} for p in parts):
                continue
            path = destination.joinpath(*parts)
            if not path.resolve().is_relative_to(destination.resolve()) or path.is_symlink():
                raise SecurityError("archive path escaped destination")
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            incoming = archive.extractfile(member)
            if incoming is None:
                raise SecurityError("invalid archive entry")
            with incoming, path.open("xb") as output:
                remaining = member.size
                while remaining:
                    chunk = incoming.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise SecurityError("truncated archive")
                    output.write(chunk)
                    remaining -= len(chunk)
    return {"files": count, "bytes": total}
