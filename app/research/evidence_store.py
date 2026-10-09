"""Run-local registry. Identity, provenance and reference changes commit together."""

from dataclasses import dataclass
from threading import RLock

from app.schemas.claim import Claim
from app.schemas.evidence import Evidence
from app.schemas.evidence_bundle import TaskEvidenceBundle
from app.schemas.source import Source, normalize_source_url


@dataclass(frozen=True)
class SourceProvenance:
    discovered_by_task: str
    used_by_tasks: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceProvenance:
    extracted_by_task: str
    used_by_tasks: tuple[str, ...]
    referenced_by_claim_ids: tuple[str, ...]

    @property
    def is_cited(self) -> bool:
        return bool(self.referenced_by_claim_ids)


class EvidenceStore:
    def __init__(self) -> None:
        self._sources: dict[str, Source] = {}
        self._evidence: dict[str, Evidence] = {}
        self._claims: dict[str, Claim] = {}
        self._source_urls: dict[str, str] = {}
        self._source_aliases: dict[str, str] = {}
        self._evidence_aliases: dict[str, str] = {}
        self._evidence_alias_origins: dict[str, tuple[str, str | None]] = {}
        self._evidence_keys: dict[tuple[str, str], str] = {}
        self._source_users: dict[str, tuple[str, ...]] = {}
        self._evidence_users: dict[str, tuple[str, ...]] = {}
        self._evidence_claims: dict[str, tuple[str, ...]] = {}
        self._source_evidence: dict[str, tuple[str, ...]] = {}
        self._lock = RLock()

    @property
    def counts(self) -> dict[str, int]:
        with self._lock:
            return {"sources": len(self._sources), "evidence": len(self._evidence), "claims": len(self._claims)}

    @property
    def reuse_counts(self) -> dict[str, int]:
        """Distinct additional (artifact, task) uses, unaffected by repeated merges."""
        with self._lock:
            return {
                "source_reuse": sum(len(users) - 1 for users in self._source_users.values()),
                "evidence_reuse": sum(len(users) - 1 for users in self._evidence_users.values()),
            }

    @staticmethod
    def _append(index: dict[str, tuple[str, ...]], key: str, value: str) -> None:
        if value not in index.get(key, ()):
            index[key] = (*index.get(key, ()), value)

    @staticmethod
    def _key(item: Evidence) -> tuple[str, str]:
        # Case and punctuation remain significant; preserve first content/location.
        return item.source_id, " ".join(item.content.split())

    def merge(self, bundle: TaskEvidenceBundle) -> TaskEvidenceBundle:
        """Remap Source -> Evidence -> Claim atomically; no Claim text dedup.

        Returned bundles contain canonical Sources/Claims and only Evidence
        originally extracted by this task. Cross-task reuse lives in Claim refs.
        Provenance is Store-owned metadata, never supplied by local bundles.
        """
        snapshot = TaskEvidenceBundle.model_validate(bundle.model_dump())
        with self._lock:
            names = ("_sources", "_evidence", "_claims", "_source_urls", "_source_aliases",
                     "_evidence_aliases", "_evidence_alias_origins", "_evidence_keys", "_source_users", "_evidence_users",
                     "_evidence_claims", "_source_evidence")
            state = {name: getattr(self, name).copy() for name in names}
            sources, evidence, claims = (state[name] for name in ("_sources", "_evidence", "_claims"))
            urls, source_aliases, evidence_aliases, keys = (state[name] for name in (
                "_source_urls", "_source_aliases", "_evidence_aliases", "_evidence_keys"))
            alias_origins = state["_evidence_alias_origins"]
            source_users, evidence_users, evidence_claims, source_evidence = (state[name] for name in (
                "_source_users", "_evidence_users", "_evidence_claims", "_source_evidence"))
            local_sources: dict[str, Source] = {}
            local_evidence: dict[str, Evidence] = {}
            local_claims: list[Claim] = []

            for source in snapshot.sources:
                key = normalize_source_url(source.url)
                known = source_aliases.get(source.source_id)
                if known and normalize_source_url(sources[known].url) != key:
                    raise ValueError(f"Conflicting source_id: {source.source_id}.")
                canonical_id = urls.get(key, source.source_id)
                if canonical_id not in sources:
                    sources[canonical_id] = source
                    urls[key] = canonical_id
                    self._append(source_users, canonical_id, source.task_id)
                source_aliases[source.source_id] = canonical_id
                self._append(source_users, canonical_id, snapshot.task_id)
                local_sources[canonical_id] = sources[canonical_id]

            for item in snapshot.evidence:
                source_id = source_aliases.get(item.source_id, item.source_id)
                if source_id not in sources:
                    raise ValueError(f"Evidence {item.evidence_id} references unknown source_id: {item.source_id}.")
                normalized = item.model_copy(update={"source_id": source_id}, deep=True)
                key = self._key(normalized)
                known = evidence_aliases.get(item.evidence_id)
                if known:
                    original = evidence[known]
                    if self._key(original) != key or alias_origins[item.evidence_id] != (item.task_id, item.location):
                        raise ValueError(f"Conflicting evidence_id: {item.evidence_id}.")
                canonical_id = keys.get(key, item.evidence_id)
                if canonical_id not in evidence:
                    evidence[canonical_id] = normalized
                    keys[key] = canonical_id
                    self._append(evidence_users, canonical_id, item.task_id)
                    self._append(source_evidence, source_id, canonical_id)
                evidence_aliases[item.evidence_id] = canonical_id
                alias_origins[item.evidence_id] = (item.task_id, item.location)
                self._append(evidence_users, canonical_id, snapshot.task_id)
                self._append(source_users, source_id, snapshot.task_id)
                if evidence[canonical_id].task_id == snapshot.task_id:
                    local_evidence[canonical_id] = evidence[canonical_id]

            for item in snapshot.claims:
                refs = list(dict.fromkeys(evidence_aliases.get(key, key) for key in item.evidence_ids))
                missing = [key for key in refs if key not in evidence]
                if missing:
                    raise ValueError(f"Claim {item.claim_id} references unknown evidence_ids: {', '.join(missing)}.")
                canonical = item.model_copy(update={"evidence_ids": refs}, deep=True)
                if item.claim_id in claims and claims[item.claim_id] != canonical:
                    raise ValueError(f"Conflicting claim_id: {item.claim_id}.")
                claims[item.claim_id] = canonical
                local_claims.append(canonical)
                for key in refs:
                    self._append(evidence_users, key, item.task_id)
                    self._append(evidence_claims, key, item.claim_id)
                    self._append(source_users, evidence[key].source_id, item.task_id)

            merged = TaskEvidenceBundle(task_id=snapshot.task_id, sources=list(local_sources.values()),
                                        evidence=list(local_evidence.values()), claims=local_claims)
            for name in names:
                setattr(self, name, state[name])
            return merged.model_copy(deep=True)

    def add_source(self, source: Source) -> Source:
        snapshot = Source.model_validate(source.model_dump())
        return self.merge(TaskEvidenceBundle(task_id=snapshot.task_id, sources=[snapshot])).sources[0]

    def add_evidence(self, evidence: Evidence) -> Evidence:
        snapshot = Evidence.model_validate(evidence.model_dump())
        with self._lock:
            self.merge(TaskEvidenceBundle(task_id=snapshot.task_id, evidence=[snapshot]))
            return self.get_evidence(snapshot.evidence_id)

    def add_claim(self, claim: Claim) -> Claim:
        snapshot = Claim.model_validate(claim.model_dump())
        return self.merge(TaskEvidenceBundle(task_id=snapshot.task_id, claims=[snapshot])).claims[0]

    def get_source(self, source_id: str) -> Source:
        with self._lock:
            key = self._source_aliases.get(source_id, source_id)
            if key not in self._sources:
                raise KeyError(f"Unknown source_id: {source_id}.")
            return self._sources[key].model_copy(deep=True)

    def get_evidence(self, evidence_id: str) -> Evidence:
        with self._lock:
            key = self._evidence_aliases.get(evidence_id, evidence_id)
            if key not in self._evidence:
                raise KeyError(f"Unknown evidence_id: {evidence_id}.")
            return self._evidence[key].model_copy(deep=True)

    def get_claim(self, claim_id: str) -> Claim:
        with self._lock:
            if claim_id not in self._claims:
                raise KeyError(f"Unknown claim_id: {claim_id}.")
            return self._claims[claim_id].model_copy(deep=True)

    def get_evidence_for_claim(self, claim_id: str) -> list[Evidence]:
        with self._lock:
            return [self.get_evidence(key) for key in self.get_claim(claim_id).evidence_ids]

    def get_source_for_evidence(self, evidence_id: str) -> Source:
        with self._lock:
            return self.get_source(self.get_evidence(evidence_id).source_id)

    def get_sources_for_claim(self, claim_id: str) -> list[Source]:
        with self._lock:
            return list({item.source_id: item for item in (
                self.get_source_for_evidence(key) for key in self.get_claim(claim_id).evidence_ids
            )}.values())

    def get_claims_for_task(self, task_id: str) -> list[Claim]:
        with self._lock:
            return [item.model_copy(deep=True) for item in self._claims.values() if item.task_id == task_id]

    def get_evidence_for_task(self, task_id: str) -> list[Evidence]:
        """Evidence extracted or used by this task, including cross-task references."""
        with self._lock:
            return [self.get_evidence(key) for key, users in self._evidence_users.items() if task_id in users]

    def get_sources_for_task(self, task_id: str) -> list[Source]:
        with self._lock:
            return [self.get_source(key) for key, users in self._source_users.items() if task_id in users]

    def get_source_provenance(self, source_id: str) -> SourceProvenance:
        with self._lock:
            item = self.get_source(source_id)
            return SourceProvenance(item.task_id, self._source_users[item.source_id])

    def get_evidence_provenance(self, evidence_id: str) -> EvidenceProvenance:
        with self._lock:
            item = self.get_evidence(evidence_id)
            return EvidenceProvenance(item.task_id, self._evidence_users[item.evidence_id],
                                      self._evidence_claims.get(item.evidence_id, ()))

    def get_cited_evidence(self) -> list[Evidence]:
        with self._lock:
            return [self.get_evidence(key) for key in self._evidence_claims]

    def get_claims_using_evidence(self, evidence_id: str) -> list[Claim]:
        with self._lock:
            key = self.get_evidence(evidence_id).evidence_id
            return [self.get_claim(claim_id) for claim_id in self._evidence_claims.get(key, ())]

    def get_evidence_from_source(self, source_id: str) -> list[Evidence]:
        with self._lock:
            key = self.get_source(source_id).source_id
            return [self.get_evidence(evidence_id) for evidence_id in self._source_evidence.get(key, ())]
