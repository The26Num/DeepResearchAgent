"""Task-scoped archive authority, reading ledger, skill state and durable permits."""

from contextlib import contextmanager
from contextvars import ContextVar
import json
import re
from threading import Event, RLock
from uuid import uuid4

from app.research.resource_models import RunResourceState, TaskResourceRecord, ReadingRecord
from app.research.source_archive import SourceArchive, text_hash, ArchiveIntegrityError
from app.research.skills import SkillRegistry
from app.schemas.source import normalize_source_url

_CURRENT = ContextVar("research_task_resources", default=None)


class ResearchBudgetExceeded(RuntimeError):
    pass


class ResearchContextExceeded(ValueError):
    pass


def active_resources():
    return _CURRENT.get()


def estimated_tokens(messages, tools=()):
    """Explicit conservative estimate, not tokenizer-exact accounting."""
    size = 32
    for message in messages:
        value = message.model_dump(mode="json") if hasattr(message, "model_dump") else message
        size += len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")) + 32
    for item in tools:
        if hasattr(item, "args_schema"):
            schema = item.args_schema.model_json_schema() if item.args_schema else {}
            value = {"name": item.name, "description": item.description, "parameters": schema}
        else:
            value = item
        size += len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")) + 32
    return size


class RunResources:
    def __init__(self, settings, run_id=None, state=None, *, on_permit=None, product_root=None):
        self.settings = settings
        self.state = (state or RunResourceState()).model_copy(deep=True)
        self.run_id = run_id or self.state.run_id or uuid4().hex
        if self.state.run_id and self.state.run_id != self.run_id:
            raise ArchiveIntegrityError("Resource manifest belongs to another Run.")
        self.state.run_id = self.run_id
        self.archive = SourceArchive(settings.source_archive_path, self.run_id,
                                     max_bytes=settings.max_run_archive_bytes, max_snapshots=settings.max_run_archive_snapshots)
        self.registry = SkillRegistry(self.archive.root, self.run_id, pins=self.state.skills,
                                      file_limit=settings.max_skill_file_chars, storage_limit=settings.max_run_archive_bytes,
                                      product_root=product_root)
        self.state.skills = self.registry.pin()
        self.on_permit = on_permit
        self.tasks = {}
        self.lock = RLock()

    def validate(self):
        self.registry.validate()
        for record in self.state.tasks.values():
            for ref in record.snapshots.values():
                self.archive.read(ref)
            for reading in record.readings:
                ref = record.snapshots.get(reading.snapshot_id)
                if ref is None or reading.run_id != self.run_id or reading.task_id != record.task_id:
                    raise ArchiveIntegrityError("Reading ledger has a missing/mismatched snapshot.")
                body, _, _ = self.archive.read(ref)
                self.archive.verify_span(ref, reading.start_char, reading.end_char,
                                         body[reading.start_char:reading.end_char], reading.text_hash)
            for skill_id, version in record.active_skills.items():
                if skill_id not in self.state.skills or self.state.skills[skill_id].version != version:
                    raise ArchiveIntegrityError("Task skill version does not match the Run pin.")

    def start_task(self, task, allowed_tasks=()):
        with self.lock:
            if task.id in self.tasks and not self.tasks[task.id].closed.is_set():
                raise RuntimeError("Task already has an active resource attempt.")
            previous = (self.tasks[task.id].snapshot() if task.id in self.tasks
                        else self.state.tasks.get(task.id, TaskResourceRecord(task_id=task.id)))
            resource = TaskResources(self, task, previous, allowed_tasks)
            self.tasks[task.id] = resource
            return resource

    def record(self, task_id):
        with self.lock:
            task = self.tasks.get(task_id)
            stored = self.state.tasks.get(task_id)
        return task.snapshot() if task else stored.model_copy(deep=True) if stored else None

    def snapshot(self):
        with self.lock:
            ids = set(self.state.tasks) | set(self.tasks)
        return RunResourceState(run_id=self.run_id, skills=self.state.skills,
                                tasks={key: self.record(key) for key in ids})

    def commit(self, task_id):
        with self.lock:
            task = self.tasks.get(task_id)
            if task:
                with task.lock:
                    task.record.committed = True
            elif task_id in self.state.tasks:
                self.state.tasks[task_id].committed = True

    def persist(self, record):
        if self.on_permit:
            self.on_permit(self.run_id, self.state.skills, record)

    def close(self):
        for resource in list(self.tasks.values()):
            resource.close()


class TaskResources:
    def __init__(self, run, task, previous, allowed):
        self.run, self.task = run, task
        self.record = previous.model_copy(deep=True)
        self.record.attempt_id, self.record.attempts = uuid4().hex, previous.attempts + 1
        self.record.allowed_tasks = list(dict.fromkeys(allowed))
        self.record.committed = False
        self.runtime = None
        self.lock, self.permit_lock = RLock(), RLock()
        self.closed = Event()
        cfg = run.settings
        if not self.record.limits:
            from app.schemas.reresearch import ReResearchTask
            search, fetch = ((cfg.reresearch_max_search_calls, cfg.reresearch_max_fetch_calls) if isinstance(task, ReResearchTask)
                             else {"discovery": (3, 4), "analysis": (2, 3), "synthesis": (0, 0)}[task.task_type])
            self.record.limits = {
                "web_search": search, "fetch_webpage": fetch, "list_sources": cfg.max_source_catalog_calls,
                "search_sources": cfg.max_source_search_calls, "read_source": cfg.max_source_read_calls,
                "load_research_skill": cfg.max_skill_load_calls, "read_skill_reference": cfg.max_skill_reference_calls,
                "invalid_tool": 2, "source_chars": cfg.max_task_source_chars,
                "read_chars": cfg.max_source_read_chars, "preview_chars": cfg.source_preview_chars,
                "download_bytes": cfg.max_task_download_bytes, "response_bytes": cfg.max_source_response_bytes,
                "skills": cfg.max_research_skills, "skill_chars": cfg.max_task_skill_chars,
                "model_calls": cfg.max_research_model_calls, "context_tokens": cfg.max_research_context_estimated_tokens,
                "output_tokens": cfg.research_model_max_tokens, "estimated_tokens": cfg.max_task_estimated_tokens,
            }
        # Explicit runtime policy applies to existing attempts as well. Keep
        # the original limit and cumulative accounting; never reset usage.
        enforced = cfg.enforce_task_estimated_token_budget
        if enforced and self.record.estimated_tokens > self.record.limits["estimated_tokens"]:
            raise ResearchBudgetExceeded(
                "Cannot re-enable the task estimated-token quota: recorded usage already exceeds its saved limit. "
                "Continue with the quota disabled or start a new Run; usage was not reset."
            )
        self.record.estimated_token_budget_enforced = enforced
        self.next_context_estimate = 0

    @contextmanager
    def activate(self):
        token = _CURRENT.set(self)
        try:
            self.check_open()
            yield self
        finally:
            _CURRENT.reset(token)

    def check_open(self):
        if self.closed.is_set():
            raise ResearchBudgetExceeded("Task resource attempt is closed; late results cannot be published.")
        if self.runtime:
            self.runtime.remaining()

    def close(self):
        with self.lock:
            self.closed.set()

    def snapshot(self):
        with self.lock:
            return self.record.model_copy(deep=True)

    def persist(self, *, final=False):
        with self.lock:
            if not final:
                self.check_open()
            self.record.revision += 1
            snapshot = self.record.model_copy(deep=True)
        self.run.persist(snapshot)

    def remaining(self, name):
        with self.lock:
            remaining = self.record.limits.get(name, 0) - self.record.calls.get(name, 0)
            if name in {"read_source", "search_sources", "fetch_webpage"} and self.record.reserved_source_chars >= self.record.limits["source_chars"]:
                return 0
            if name == "load_research_skill" and not self.record.limits["skills"]:
                return 0
            return max(0, remaining)

    def reserve_tool(self, name):
        with self.permit_lock:
            self.check_open()
            with self.lock:
                if name not in self.record.limits or self.remaining(name) <= 0:
                    raise ResearchBudgetExceeded(f"Tool budget exhausted or unregistered: {name}.")
                self.record.calls[name] = self.record.calls.get(name, 0) + 1
                self.record.tool_history.append(name)
            self.persist()
            self.check_open()

    def reserve_chars(self, requested):
        with self.permit_lock:
            self.check_open()
            with self.lock:
                count = min(requested, self.record.limits["source_chars"] - self.record.reserved_source_chars)
                if count <= 0:
                    raise ResearchBudgetExceeded("Cumulative source text budget exhausted.")
                self.record.reserved_source_chars += count
            self.persist()
            return count

    def reserve_download(self):
        with self.permit_lock:
            self.check_open()
            with self.lock:
                count = self.record.limits["response_bytes"]
                if self.record.reserved_download_bytes + count > self.record.limits["download_bytes"]:
                    raise ResearchBudgetExceeded("Task download byte budget exhausted.")
                self.record.reserved_download_bytes += count
            self.persist()
            return count

    def reserve_model(self, purpose, messages, tools=()):
        with self.permit_lock:
            self.check_open()
            estimate = max(estimated_tokens(messages, tools), self.next_context_estimate if purpose == "researcher" else 0)
            output = self.record.limits["output_tokens"] if purpose != "extraction" else self.run.settings.evidence_extraction_max_tokens
            with self.lock:
                calls = sum(self.record.model_calls.values())
                maximum = self.record.limits["model_calls"]
                # Four permits are reserved up front: one memo and up to three
                # extraction requests (native rejection, JSON, compact recovery).
                normal = self.record.model_calls.get("researcher", 0)
                if calls >= maximum:
                    raise ResearchBudgetExceeded(f"Task model-call budget exhausted (used={calls}, limit={maximum}).")
                if purpose == "researcher" and normal >= maximum - 4:
                    raise ResearchBudgetExceeded(f"Task research-loop call budget exhausted (used={normal}, limit={maximum - 4}); reserved finalization permits remain separate.")
                if purpose == "memo_recovery" and self.record.model_calls.get(purpose, 0) >= 1:
                    raise ResearchBudgetExceeded("Task memo-recovery budget exhausted (used=1, limit=1); it does not reset on Resume.")
                if purpose == "extraction" and self.record.model_calls.get(purpose, 0) >= 3:
                    raise ResearchBudgetExceeded("Task evidence-extraction call budget exhausted (used=3, limit=3); no additional model request was sent.")
                if estimate + output > self.record.limits["context_tokens"]:
                    raise ResearchContextExceeded("Complete model input, tools and output reserve exceed the estimated context limit.")
                reserve = (4 * 8192 + self.record.limits["output_tokens"]
                           + 3 * self.run.settings.evidence_extraction_max_tokens) if purpose == "researcher" else 0
                if (self.record.estimated_token_budget_enforced
                        and self.record.estimated_tokens + estimate + output + reserve > self.record.limits["estimated_tokens"]):
                    raise ResearchBudgetExceeded(
                        f"Task cumulative estimated-token budget exhausted (used={self.record.estimated_tokens}, "
                        f"request_with_output={estimate + output}, finalization_reserve={reserve}, "
                        f"limit={self.record.limits['estimated_tokens']})."
                    )
                self.record.model_calls[purpose] = self.record.model_calls.get(purpose, 0) + 1
                self.record.estimated_tokens += estimate + output
            self.persist()
            self.check_open()

    def add_usage(self, input_tokens, output_tokens):
        if not self.closed.is_set():
            with self.lock:
                self.record.actual_input_tokens += input_tokens
                self.record.actual_output_tokens += output_tokens
                self.record.usage_responses += 1

    def refs(self):
        refs = {}
        for task_id in self.record.allowed_tasks:
            record = self.run.record(task_id)
            if record and record.committed:
                refs.update(record.snapshots)
        with self.lock:
            refs.update(self.record.snapshots)
        return refs

    def ref(self, snapshot_id):
        self.check_open()
        value = self.refs().get(snapshot_id)
        if value is None:
            raise PermissionError("Snapshot is not authorized for this Run/task.")
        return value

    def remember(self, ref):
        self.check_open()
        if ref.run_id != self.run.run_id:
            raise PermissionError("Cross-Run source access is forbidden.")
        self.run.archive.read(ref)
        with self.lock:
            self.check_open()
            if self.record.snapshots.get(ref.snapshot_id) == ref:
                return
            self.record.snapshots[ref.snapshot_id] = ref
        self.persist()

    def cached(self, url):
        key = normalize_source_url(url)
        return next((ref for ref in reversed(list(self.refs().values())) if ref.status == "ready"
                     and key in {normalize_source_url(ref.url), normalize_source_url(ref.requested_url)}), None)

    def remember_search_urls(self, urls):
        """Private navigation leads only; no reading/Evidence authority is granted."""
        from urllib.parse import urlsplit, parse_qs
        with self.lock:
            self.check_open()
            changed = False
            for url in urls:
                if not isinstance(url, str) or len(url) > 2000 or len(self.record.search_urls) >= 15:
                    continue
                try:
                    parsed = urlsplit(url)
                except ValueError:
                    continue
                if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                        or {key.lower() for key in parse_qs(parsed.query)} & {"token", "api_key", "apikey", "password", "authorization"}):
                    continue
                if url not in self.record.search_urls:
                    self.record.search_urls.append(url)
                    changed = True
        if changed:
            self.persist()

    def record_span(self, ref, start, end, content, kind):
        self.check_open()
        if ref.status != "ready":
            raise ArchiveIntegrityError("Only successfully extracted source text can enter the reading ledger.")
        self.run.archive.verify_span(ref, start, end, content, text_hash(content))
        self.remember(ref)
        with self.lock:
            self.check_open()
            row = ReadingRecord(run_id=self.run.run_id, task_id=self.task.id, attempt_id=self.record.attempt_id,
                                snapshot_id=ref.snapshot_id, start_char=start, end_char=end, text_hash=text_hash(content),
                                kind=kind, sequence=len(self.record.readings) + 1)
            self.record.readings.append(row)
            self.record.returned_source_chars += len(content)
        self.persist()
        return row

    def capture_legacy(self, page, max_chars=None):
        """Compatibility for trusted injected tool adapters; explicitly excerpt-only."""
        if page.get("snapshot_id"):
            ref = self.ref(page["snapshot_id"])
            start, end = page["start_char"], page["end_char"]
            self.run.archive.verify_span(ref, start, end, page["text"], text_hash(page["text"]))
            sequence = page.get("reading_sequence")
            if not any(item.sequence == sequence and item.snapshot_id == ref.snapshot_id and item.text_hash == text_hash(page["text"])
                       for item in self.snapshot().readings):
                self.record_span(ref, start, end, page["text"], "adapter_preview")
            return page
        body = page["text"]
        if len(body.encode("utf-8")) > self.record.limits["response_bytes"]:
            raise ResearchBudgetExceeded("Supplied tool body exceeds the response byte limit; no truncated archive was created.")
        ref = self.run.archive.put(requested_url=page.get("requested_url") or page["url"], url=page["url"],
                                   title=page.get("title", ""), raw=body.encode("utf-8"), text=body,
                                   task_id=self.task.id, representation="legacy_supplied_excerpt",
                                   parser_version="legacy-excerpt-text-v1", content_type="text/plain")
        existing = self.snapshot()
        row = (next((row for row in existing.readings if row.snapshot_id == ref.snapshot_id), None)
               if max_chars is None else None)
        if row is None:
            cap = self.record.limits["read_chars"] if max_chars is None else min(max_chars, self.record.limits["preview_chars"])
            count = self.reserve_chars(min(len(body), cap))
            row = self.record_span(ref, 0, count, body[:count], "adapter_preview")
        return {**page, "url": ref.url, "title": ref.title,
                "published_at": page.get("published_at") if isinstance(page.get("published_at"), str) and len(page["published_at"]) <= 100 else None,
                "text": body[row.start_char:row.end_char], "snapshot_id": ref.snapshot_id,
                "start_char": row.start_char, "end_char": row.end_char, "body_hash": ref.body_hash,
                "body_chars": len(body), "reading_sequence": row.sequence, "representation": "legacy_supplied_excerpt",
                "preview_only": row.end_char - row.start_char < len(body)}

    def reading_pages(self):
        """Union overlapping ranges; never concatenate disjoint excerpts."""
        record = self.snapshot()
        groups = {}
        for row in record.readings:
            ref = record.snapshots[row.snapshot_id]
            body, _, _ = self.run.archive.read(ref)
            self.run.archive.verify_span(ref, row.start_char, row.end_char, body[row.start_char:row.end_char], row.text_hash)
            groups.setdefault(row.snapshot_id, []).append((row.start_char, row.end_char))
        pages = []
        for key, ranges in groups.items():
            ref = record.snapshots[key]
            body, metadata, _ = self.run.archive.read(ref)
            merged = []
            for start, end in sorted(ranges):
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
                else:
                    merged.append((start, end))
            for start, end in merged:
                pages.append({"url": ref.url, "title": ref.title, "published_at": metadata.get("published_at"),
                              "text": body[start:end], "snapshot_id": key, "body_hash": ref.body_hash,
                              "start_char": start, "end_char": end})
        return pages

    def verify_evidence(self, origin, content):
        ref = self.ref(origin.snapshot_id)
        if origin.body_hash != ref.body_hash:
            raise ArchiveIntegrityError("Evidence body hash does not match its snapshot.")
        if not any(page["snapshot_id"] == origin.snapshot_id and page["start_char"] <= origin.start_char
                   and origin.end_char <= page["end_char"] for page in self.reading_pages()):
            raise ArchiveIntegrityError("Evidence range was not actually returned to this Researcher.")
        self.run.archive.verify_span(ref, origin.start_char, origin.end_char, content, origin.text_hash)

    def load_skill(self, skill_id):
        self.check_open()
        with self.permit_lock:
            if skill_id in self.record.active_skills:
                return {"skill_id": skill_id, "version": self.record.active_skills[skill_id], "already_active": True}
            text = self.run.registry.read(skill_id)
            with self.lock:
                if len(self.record.active_skills) >= self.record.limits["skills"] or self.record.skill_chars + len(text) > self.record.limits["skill_chars"]:
                    raise ResearchBudgetExceeded("Active research skill count/text budget exhausted.")
                pin = self.run.registry.pins[skill_id]
                self.record.active_skills[skill_id] = pin.version
                self.record.skill_chars += len(text)
            self.persist()
            return {"skill_id": skill_id, "version": pin.version, "text": text,
                    "references": [name for name in pin.files if name != "SKILL.md"], "not_evidence": True}

    def skill_reference(self, skill_id, name):
        if skill_id not in self.record.active_skills:
            raise PermissionError("Load the research skill before reading its references.")
        if not name.startswith("references/"):
            raise PermissionError("Only pinned skill references are readable.")
        text = self.run.registry.read(skill_id, name)
        key = skill_id + ":" + name
        with self.lock:
            if key in self.record.active_references:
                return {"skill_id": skill_id, "reference": name, "already_active": True}
            if self.record.skill_chars + len(text) > self.record.limits["skill_chars"]:
                raise ResearchBudgetExceeded("Research skill text budget exhausted.")
            self.record.active_references.append(key)
            self.record.skill_chars += len(text)
        self.persist()
        return {"skill_id": skill_id, "reference": name, "text": text, "not_evidence": True}

    def method_text(self):
        record = self.snapshot()
        chunks = [f"ACTIVE METHOD {key} version={version}\n" + self.run.registry.read(key) for key, version in record.active_skills.items()]
        for key in record.active_references:
            skill_id, name = key.split(":", 1)
            chunks.append(f"METHOD REFERENCE {key}\n" + self.run.registry.read(skill_id, name))
        return "\n\n".join(chunks)

    def list_sources(self, *, offset=0, limit=5, task_id=None, keyword=""):
        if offset < 0 or not 1 <= limit <= 10 or len(keyword) > 300:
            raise ValueError("Use a nonnegative offset and limit between 1 and 10.")
        refs = self.refs()
        if task_id:
            if task_id != self.task.id and task_id not in self.record.allowed_tasks:
                raise PermissionError("Source owner task is not authorized.")
            record = self.run.record(task_id)
            refs = {key: value for key, value in refs.items() if record and key in record.snapshots}
        values = [value for value in refs.values() if keyword.casefold() in (value.title + " " + value.url).casefold()]
        cards = []
        for ref in values[offset:offset + limit]:
            _, meta, sections = self.run.archive.read(ref)
            card = {"snapshot_id": ref.snapshot_id, "title": ref.title, "url": ref.url,
                          "status": ref.status, "readable": ref.status == "ready", "body_chars": ref.body_chars,
                          "representation": meta["representation"], "section_count": len(sections), "sections": sections[:5]}
            if cards and len(json.dumps([*cards, card], ensure_ascii=False)) > 8000:
                break
            cards.append(card)
        next_offset = offset + len(cards)
        return {"sources": cards, "total": len(values), "next_offset": next_offset if next_offset < len(values) else None}

    def search_sources(self, query, limit=5):
        if not query.strip() or len(query) > 300 or not 1 <= limit <= 5:
            raise ValueError("Search requires a query and limit between 1 and 5.")
        terms = set(re.findall(r"[a-zA-Z0-9_]{2,}", query.casefold()))
        chinese = re.findall(r"[\u4e00-\u9fff]+", query)
        terms.update(word[i:i + 2] for word in chinese for i in range(len(word) - 1))
        terms.update(chinese)
        terms = sorted(terms, key=lambda term: (-len(term), term))[:24]
        hits = []
        for ref in self.refs().values():
            self.check_open()
            if ref.status != "ready":
                continue
            body, _, sections = self.run.archive.read(ref)
            # Positions are found in original text; Unicode casefold can change
            # length, so use case-insensitive regex against the original string.
            for section in sections:
                start, end = section["start_char"], section["end_char"]
                part = body[start:end]
                matches, score = [], 0
                from itertools import islice
                for term in terms:
                    found = list(islice(re.finditer(re.escape(term), part, re.I), 8))
                    if found:
                        score += len(found) * len(term)
                        matches.append((len(term), found[0].start()))
                if not matches:
                    continue
                position = start + max(matches, key=lambda item: (item[0], -item[1]))[1]
                left, right = max(start, position - 80), min(end, position + 200)
                hits.append((score, ref.snapshot_id, section["section_id"], left, right))
        hits.sort(key=lambda item: (-item[0], item[1], item[3]))
        count = self.reserve_chars(min(len(hits), limit) * 280) if hits else 0
        result = []
        for _, key, section, start, end in hits[:limit]:
            if count <= 0:
                break
            body, _, _ = self.run.archive.read(self.ref(key))
            end = min(end, start + count)
            result.append({"snapshot_id": key, "section_id": section, "start_char": start, "end_char": end,
                           "preview": body[start:end], "navigation_only": True, "requires_read_source": True})
            count -= end - start
            with self.lock:
                self.record.returned_source_chars += end - start
        return {"hits": result, "evidence_eligible": False, "instruction": "Use read_source to confirm these navigation previews."}

    def read_source(self, snapshot_id, *, start_char=0, end_char=None, section_id=None, max_chars=None):
        ref = self.ref(snapshot_id)
        if ref.status != "ready":
            raise ValueError("Snapshot has no successfully extracted readable body.")
        body, _, sections = self.run.archive.read(ref)
        if section_id is not None:
            section = next((item for item in sections if item["section_id"] == section_id), None)
            if section is None:
                raise ValueError("Unknown source section.")
            start_char, end_char = section["start_char"], section["end_char"]
        requested_end = len(body) if end_char is None else end_char
        if not 0 <= start_char < requested_end <= len(body):
            raise ValueError("Source read range is out of bounds.")
        cap = self.record.limits["read_chars"]
        if max_chars is not None:
            if max_chars < 1:
                raise ValueError("max_chars must be positive.")
            cap = min(cap, max_chars)
        count = self.reserve_chars(min(cap, requested_end - start_char))
        end = min(start_char + count, requested_end)
        text = body[start_char:end]
        row = self.record_span(ref, start_char, end, text, "read_source")
        return {"snapshot_id": snapshot_id, "url": ref.url, "title": ref.title, "text": text,
                "start_char": start_char, "end_char": end, "body_chars": len(body), "offset_unit": "python_unicode_characters",
                "next_start_char": end if end < requested_end else None, "range_complete": end == requested_end,
                "entire_source_read": start_char == 0 and end == len(body), "reading_sequence": row.sequence}
