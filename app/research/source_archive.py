"""Immutable, bounded response snapshots and exact Python-character offsets."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
from threading import RLock
from uuid import uuid4

from app.research.resource_models import SnapshotRef

PARSER_VERSION = "html-paragraph-text-v1"


class ArchiveIntegrityError(ValueError):
    pass


class ArchiveLimitError(RuntimeError):
    pass


def text_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def atomic_bytes(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + "." + uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def paragraph_index(text: str) -> list[dict]:
    ranges, start = [], 0
    for line in text.splitlines(keepends=True):
        end = start + len(line)
        if line.strip():
            ranges.append((start, end))
        start = end
    if not ranges and text:
        ranges = [(0, len(text))]
    # Index the whole text even for adversarial HTML with very many tiny lines.
    group = max(1, (len(ranges) + 4095) // 4096)
    return [{"section_id": f"p_{index // group}", "start_char": ranges[index][0],
             "end_char": ranges[min(len(ranges), index + group) - 1][1],
             "title": text[ranges[index][0]:ranges[index][1]].strip()[:100]}
            for index in range(0, len(ranges), group)]


class SourceArchive:
    def __init__(self, root, run_id, *, max_bytes=128 * 1024 * 1024, max_snapshots=128):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", run_id):
            raise ArchiveIntegrityError("Invalid archive Run ID.")
        self.root = Path(root).resolve()
        self.run_id, self.max_bytes, self.max_snapshots = run_id, max_bytes, max_snapshots
        self.directory = self.root / run_id
        self.lock = RLock()

    def _directory(self, snapshot_id):
        if not re.fullmatch(r"snap_[0-9a-f]{64}", snapshot_id):
            raise ArchiveIntegrityError("Invalid snapshot ID.")
        path = self.directory / "snapshots" / snapshot_id
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise ArchiveIntegrityError("Archive path is not permitted.")
        return path

    def put(self, *, requested_url, url, title, raw, text, task_id, content_type="text/html",
            encoding="utf-8", status="ready", published_at=None, representation="html_page", detail="",
            parser_version=PARSER_VERSION) -> SnapshotRef:
        body_hash, raw_hash = text_hash(text), sha256(raw).hexdigest()
        identity = json.dumps([url, body_hash, raw_hash, parser_version, status], separators=(",", ":"))
        snapshot_id = "snap_" + text_hash(identity)
        directory = self._directory(snapshot_id)
        with self.lock:
            if directory.exists():
                ref = self._reference(directory)
                self.read(ref)
                return ref
            metadata = {"snapshot_id": snapshot_id, "run_id": self.run_id, "requested_url": requested_url,
                        "url": url, "title": title[:500], "fetched_at": datetime.now(timezone.utc).isoformat(),
                        "content_type": content_type[:200], "encoding": encoding or "utf-8", "status": status,
                        "body_hash": body_hash, "raw_hash": raw_hash, "body_chars": len(text), "raw_bytes": len(raw),
                        "parser_version": parser_version, "discovered_by_task": task_id,
                        "published_at": published_at if published_at is None or len(published_at) <= 100 else None,
                        "representation": representation, "detail": detail[:300],
                        "offset_unit": "python_unicode_characters",
                        "complete_response": status == "ready" and representation != "legacy_supplied_excerpt"}
            files = {"raw.bin": raw, "body.txt": text.encode("utf-8"),
                     "metadata.json": json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                     "sections.json": json.dumps(paragraph_index(text), ensure_ascii=False, separators=(",", ":")).encode("utf-8")}
            # This scan enforces storage quotas only. It never grants source access.
            existing = list((self.directory / "snapshots").glob("snap_*/metadata.json"))
            occupied = sum(p.stat().st_size for p in self.directory.rglob("*") if p.is_file()) if self.directory.exists() else 0
            if len(existing) >= self.max_snapshots or occupied + sum(map(len, files.values())) > self.max_bytes:
                raise ArchiveLimitError("Run archive storage limit exceeded; no partial snapshot published.")
            staging = self.directory / ".staging" / uuid4().hex
            if not staging.resolve().is_relative_to(self.directory.resolve()):
                raise ArchiveIntegrityError("Archive staging path is not permitted.")
            staging.mkdir(parents=True, exist_ok=False)
            try:
                for name, data in files.items():
                    atomic_bytes(staging / name, data)
                directory.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staging, directory)
            finally:
                if staging.exists():
                    assert staging.resolve().is_relative_to(self.directory.resolve())
                    shutil.rmtree(staging)
            return self._reference(directory)

    def _reference(self, directory):
        raw = (directory / "metadata.json").read_bytes()
        meta = json.loads(raw)
        return SnapshotRef(**{key: meta[key] for key in ("snapshot_id", "run_id", "body_hash", "raw_hash", "parser_version",
                              "requested_url", "url", "title", "status", "body_chars", "discovered_by_task")},
                           relative_dir=f"{self.run_id}/snapshots/{meta['snapshot_id']}", metadata_hash=sha256(raw).hexdigest())

    def read(self, ref: SnapshotRef) -> tuple[str, dict, list[dict]]:
        if ref.run_id != self.run_id:
            raise ArchiveIntegrityError("Snapshot belongs to a different Run.")
        directory = self._directory(ref.snapshot_id)
        try:
            for name in ("metadata.json", "raw.bin", "body.txt", "sections.json"):
                if (directory / name).is_symlink():
                    raise ArchiveIntegrityError("Archive symlinks are not permitted.")
            if self._reference(directory) != ref:
                raise ArchiveIntegrityError("Snapshot metadata/hash does not match its checkpoint reference.")
            raw, body = (directory / "raw.bin").read_bytes(), (directory / "body.txt").read_bytes()
            if sha256(raw).hexdigest() != ref.raw_hash or sha256(body).hexdigest() != ref.body_hash:
                raise ArchiveIntegrityError("Archived response/body hash mismatch.")
            text = body.decode("utf-8")
            meta = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
            sections = json.loads((directory / "sections.json").read_text(encoding="utf-8"))
            if len(text) != ref.body_chars or sections != paragraph_index(text):
                raise ArchiveIntegrityError("Archive length/section index mismatch.")
            return text, meta, sections
        except ArchiveIntegrityError:
            raise
        except (OSError, UnicodeError, KeyError, ValueError) as error:
            raise ArchiveIntegrityError("Required archive files are missing or corrupt; original snapshot cannot be replaced.") from error

    def verify_span(self, ref, start, end, content, expected_hash):
        text, _, _ = self.read(ref)
        if not 0 <= start < end <= len(text) or text[start:end] != content or text_hash(content) != expected_hash:
            raise ArchiveIntegrityError("Evidence does not match the archived character range/hash.")
