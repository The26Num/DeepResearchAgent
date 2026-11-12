"""Product research methods, pinned per Run; unrelated to developer .agents skills."""

from hashlib import sha256
import json
from pathlib import Path
import re

from app.research.resource_models import SkillPin
from app.research.source_archive import atomic_bytes, ArchiveIntegrityError

CATALOG = (
    {"skill_id": "academic_review", "name": "学术研究综述", "when": "论文、学术进展、实验与基线", "description": "原始论文、实验可比性和研究局限。"},
    {"skill_id": "technical_comparison", "name": "技术方案比较", "when": "工具、框架、系统选型与性能比较", "description": "版本、比较维度、适用条件与公平基准。"},
    {"skill_id": "industry_analysis", "name": "行业研究", "when": "市场、产业、业务与出海", "description": "统计口径、时间地域、事实与预测区分。"},
)


class SkillRegistry:
    def __init__(self, archive_root, run_id, *, file_limit=12000, storage_limit=128 * 1024 * 1024, pins=None, product_root=None):
        self.root, self.run_id, self.file_limit = Path(archive_root).resolve(), run_id, file_limit
        self.product_root = Path(product_root) if product_root else Path(__file__).resolve().parents[1] / "research_skills"
        self.pins = dict(pins or {})
        self.storage_limit = storage_limit
        self._cache = {}

    def pin(self):
        if self.pins:
            self.validate()
            return self.pins
        for card in CATALOG:
            skill_id = card["skill_id"]
            base = self.product_root / skill_id
            files = {"SKILL.md": base / "SKILL.md"}
            files.update((p.relative_to(base).as_posix(), p) for p in sorted((base / "references").glob("*.md")))
            if len(files) > 16 or any(name != "SKILL.md" and not re.fullmatch(r"references/[a-zA-Z0-9_-]+\.md", name) for name in files):
                raise ValueError("Research skill references exceed the file count or name limits.")
            contents = {}
            for name, path in files.items():
                if path.is_symlink():
                    raise ArchiveIntegrityError("Runtime skill symlinks are not allowed.")
                value = path.read_bytes()
                if not value or len(value.decode("utf-8")) > self.file_limit:
                    raise ValueError("Runtime skill file is empty or exceeds its full-load size limit.")
                contents[name] = value
            hashes = {name: sha256(value).hexdigest() for name, value in contents.items()}
            version = sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
            relative = f"{self.run_id}/skills/{skill_id}/{version}"
            directory = self.root / self.run_id
            occupied = sum(path.stat().st_size for path in directory.rglob("*") if path.is_file()) if directory.exists() else 0
            added = sum(len(value) for name, value in contents.items() if not (self.root / relative / name).exists())
            if occupied + added > self.storage_limit:
                raise ValueError("Pinned research skills exceed the Run archive storage budget.")
            for name, value in contents.items():
                target = self.root / relative / name
                if target.exists() and target.read_bytes() != value:
                    raise ArchiveIntegrityError("Pinned skill version is corrupt; refusing to overwrite it.")
                if not target.exists():
                    atomic_bytes(target, value)
            self.pins[skill_id] = SkillPin(skill_id=skill_id, version=version, relative_dir=relative, files=hashes)
        return self.pins

    def catalog(self):
        return [dict(card, version=self.pins[card["skill_id"]].version) for card in CATALOG if card["skill_id"] in self.pins]

    def read(self, skill_id, name="SKILL.md"):
        if skill_id not in {item["skill_id"] for item in CATALOG} or skill_id not in self.pins:
            raise PermissionError("Unknown research skill.")
        pin = self.pins[skill_id]
        if (not re.fullmatch(r"[0-9a-f]{64}", pin.version)
                or pin.relative_dir != f"{self.run_id}/skills/{skill_id}/{pin.version}"
                or name not in pin.files or (name != "SKILL.md" and not re.fullmatch(r"references/[a-zA-Z0-9_-]+\.md", name))):
            raise PermissionError("Research skill reference is not allowed.")
        path = self.root / pin.relative_dir / name
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise ArchiveIntegrityError("Research skill path is not allowed.")
        try:
            value = path.read_bytes()
        except OSError as error:
            raise ArchiveIntegrityError("Pinned research skill is missing; cannot silently use another version.") from error
        if sha256(value).hexdigest() != pin.files[name]:
            raise ArchiveIntegrityError("Pinned research skill hash mismatch.")
        key = (skill_id, pin.version, name)
        if key not in self._cache:
            text = value.decode("utf-8")
            if len(text) > self.file_limit:
                raise ArchiveIntegrityError("Pinned research skill exceeds its full-load limit.")
            self._cache[key] = text
        return self._cache[key]

    def validate(self):
        for skill_id, pin in self.pins.items():
            expected = sha256(json.dumps(pin.files, sort_keys=True).encode()).hexdigest()
            if expected != pin.version:
                raise ArchiveIntegrityError("Pinned research skill manifest hash mismatch.")
            for name in pin.files:
                self.read(skill_id, name)
