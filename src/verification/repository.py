"""SQLite persistence for structured evidence, progression and publications."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Mapping, Optional

from .models import (
    Claim, EntityType, EventStatus, EventType, RawDocument,
    SourceIdentity, SourceKind, SourceProfile, VerificationDecision,
)


class RepositoryError(RuntimeError):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _loads(value: Optional[str], default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


class VerificationRepository:
    """Small transactional database used as the publication ledger.

    The repository never silently starts fresh after corruption or a schema
    mismatch.  Initialization raises ``RepositoryError``; the runtime catches it
    and disables publishing for the run.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        schema_version: int = 1,
        busy_timeout_ms: int = 5000,
        journal_mode: str = "DELETE",
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self.conn = sqlite3.connect(str(self.path), timeout=busy_timeout_ms / 1000)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            mode = str(journal_mode or "DELETE").upper()
            if mode not in {"DELETE", "WAL", "TRUNCATE", "PERSIST", "MEMORY", "OFF"}:
                raise RepositoryError(f"unsupported SQLite journal mode: {mode}")
            self.conn.execute(f"PRAGMA journal_mode={mode}")
            self._initialize(schema_version)
        except Exception as exc:
            try:
                self.conn.close()
            except Exception:
                pass
            if isinstance(exc, RepositoryError):
                raise
            raise RepositoryError(f"cannot initialize verification database {self.path}: {exc}") from exc

    @classmethod
    def from_config(cls, database_config: Mapping[str, Any]) -> "VerificationRepository":
        return cls(
            database_config["path"],
            schema_version=int(database_config["schema_version"]),
            busy_timeout_ms=int(database_config["busy_timeout_ms"]),
            journal_mode=str(database_config["journal_mode"]),
        )

    def _initialize(self, expected_version: int) -> None:
        with self.transaction():
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sources (
                    source_id TEXT PRIMARY KEY,
                    publisher_group TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    profile_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    publisher_group TEXT NOT NULL,
                    source_verified INTEGER NOT NULL,
                    source_resolution TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    url TEXT,
                    published_at TEXT,
                    fetched_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS claims (
                    claim_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL REFERENCES documents(document_id),
                    source_id TEXT NOT NULL,
                    publisher_group TEXT NOT NULL,
                    article_category TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_status TEXT NOT NULL,
                    subject_type TEXT NOT NULL,
                    facts_json TEXT NOT NULL,
                    fact_support_json TEXT NOT NULL,
                    entity_certainty REAL NOT NULL,
                    event_certainty REAL NOT NULL,
                    sport_confidence REAL NOT NULL,
                    sport_signals_json TEXT NOT NULL,
                    league_relevant INTEGER NOT NULL,
                    warnings_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS stories (
                    story_id TEXT PRIMARY KEY,
                    family_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    subject_id TEXT,
                    current_status TEXT NOT NULL,
                    verification_level TEXT NOT NULL,
                    facts_json TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_stories_family ON stories(family_id);

                CREATE TABLE IF NOT EXISTS story_claims (
                    story_id TEXT NOT NULL REFERENCES stories(story_id),
                    claim_id TEXT NOT NULL REFERENCES claims(claim_id),
                    attached_at TEXT NOT NULL,
                    PRIMARY KEY (story_id, claim_id)
                );

                CREATE TABLE IF NOT EXISTS decisions (
                    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    story_id TEXT NOT NULL,
                    family_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_status TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    decision_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_decisions_story ON decisions(story_id);

                CREATE TABLE IF NOT EXISTS publications (
                    publication_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    story_id TEXT NOT NULL,
                    family_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_status TEXT NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE,
                    facts_json TEXT NOT NULL,
                    source_ids_json TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    platform_post_id TEXT,
                    published_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_publications_family
                    ON publications(family_id, published_at DESC);

                CREATE TABLE IF NOT EXISTS source_outcomes (
                    outcome_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_id TEXT NOT NULL,
                    story_id TEXT NOT NULL,
                    claim_id TEXT,
                    outcome TEXT NOT NULL,
                    weight REAL NOT NULL,
                    authority_document_id TEXT,
                    observed_at TEXT NOT NULL,
                    UNIQUE(source_id, story_id, claim_id, outcome)
                );
                CREATE INDEX IF NOT EXISTS idx_source_outcomes_source
                    ON source_outcomes(source_id);
                """
            )
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(expected_version),),
                )
            elif int(row["value"]) != expected_version:
                raise RepositoryError(
                    f"database schema {row['value']} != expected {expected_version}; "
                    "publishing disabled until migrated"
                )
            check = self.conn.execute("PRAGMA integrity_check").fetchone()
            if not check or check[0] != "ok":
                raise RepositoryError(f"SQLite integrity check failed: {check[0] if check else 'unknown'}")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            try:
                with self.conn:
                    yield
            except Exception:
                raise

    def register_sources(self, profiles: Iterable[SourceProfile]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction():
            for profile in profiles:
                payload = {
                    "id": profile.id,
                    "display_name": profile.display_name,
                    "handles": profile.handles,
                    "domains": profile.domains,
                    "official_for": profile.official_for,
                    "official_scope": profile.official_scope,
                    "allowed_events": profile.allowed_events,
                    "allowed_milestones": profile.allowed_milestones,
                    "prior_mean": profile.prior_mean,
                    "prior_strength": profile.prior_strength,
                }
                self.conn.execute(
                    """
                    INSERT INTO sources(source_id, publisher_group, kind, profile_json, updated_at)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(source_id) DO UPDATE SET
                        publisher_group=excluded.publisher_group,
                        kind=excluded.kind,
                        profile_json=excluded.profile_json,
                        updated_at=excluded.updated_at
                    """,
                    (profile.id, profile.publisher_group, profile.kind.value, _json(payload), now),
                )

    def record_claim(self, claim: Claim) -> None:
        doc = claim.document
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction():
            self.conn.execute(
                """
                INSERT OR IGNORE INTO documents(
                    document_id, source_id, publisher_group, source_verified,
                    source_resolution, title, body, url, published_at, fetched_at,
                    metadata_json, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    doc.id,
                    doc.source.profile_id,
                    doc.source.publisher_group,
                    int(doc.source.verified),
                    doc.source.resolution,
                    doc.title,
                    doc.body,
                    doc.url,
                    doc.published_at,
                    doc.fetched_at,
                    _json(doc.metadata),
                    now,
                ),
            )
            self.conn.execute(
                """
                INSERT OR IGNORE INTO claims(
                    claim_id, document_id, source_id, publisher_group,
                    article_category, event_type, event_status, subject_type,
                    facts_json, fact_support_json, entity_certainty,
                    event_certainty, sport_confidence, sport_signals_json,
                    league_relevant, warnings_json, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    claim.id,
                    doc.id,
                    claim.source_id,
                    claim.publisher_group,
                    claim.article_category.value,
                    claim.event_type.value,
                    claim.status.value,
                    claim.subject_type.value,
                    _json(claim.facts),
                    _json(claim.fact_support),
                    claim.entity_certainty,
                    claim.event_certainty,
                    claim.sport_confidence,
                    _json(claim.sport_signals),
                    int(claim.league_relevant),
                    _json(claim.warnings),
                    now,
                ),
            )

    def upsert_story(
        self,
        decision: VerificationDecision,
        claims: Iterable[Claim],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        subject_id = decision.verified_facts.get("subject_id")
        with self.transaction():
            prior = self.conn.execute(
                "SELECT first_seen FROM stories WHERE story_id=?",
                (decision.story_id,),
            ).fetchone()
            first_seen = prior["first_seen"] if prior else now
            self.conn.execute(
                """
                INSERT INTO stories(
                    story_id, family_id, event_type, subject_id, current_status,
                    verification_level, facts_json, first_seen, last_seen
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(story_id) DO UPDATE SET
                    family_id=excluded.family_id,
                    event_type=excluded.event_type,
                    subject_id=excluded.subject_id,
                    current_status=excluded.current_status,
                    verification_level=excluded.verification_level,
                    facts_json=excluded.facts_json,
                    last_seen=excluded.last_seen
                """,
                (
                    decision.story_id,
                    decision.family_id,
                    decision.event_type.value,
                    subject_id,
                    decision.status.value,
                    decision.decision.value,
                    _json(decision.verified_facts),
                    first_seen,
                    now,
                ),
            )
            for claim in claims:
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO story_claims(story_id, claim_id, attached_at)
                    VALUES(?,?,?)
                    """,
                    (decision.story_id, claim.id, now),
                )

    def record_decision(self, decision: VerificationDecision) -> None:
        with self.transaction():
            self.conn.execute(
                """
                INSERT INTO decisions(
                    story_id, family_id, decision, event_type, event_status,
                    fingerprint, confidence, decision_json, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    decision.story_id,
                    decision.family_id,
                    decision.decision.value,
                    decision.event_type.value,
                    decision.status.value,
                    decision.fingerprint,
                    decision.confidence,
                    _json(decision.to_dict()),
                    decision.created_at,
                ),
            )

    def claims_for_family(
        self, family_id: str, *, observed_since: Optional[str] = None
    ) -> list[Claim]:
        params: list[Any] = [family_id]
        where = "s.family_id=?"
        if observed_since:
            where += " AND c.created_at>=?"
            params.append(observed_since)
        rows = self.conn.execute(
            f"""
            SELECT c.*, d.title, d.body, d.url, d.published_at, d.fetched_at,
                   d.source_verified, d.source_resolution, d.metadata_json,
                   d.source_id AS document_source_id,
                   d.publisher_group AS document_publisher_group,
                   src.kind AS source_kind
            FROM stories s
            JOIN story_claims sc ON sc.story_id=s.story_id
            JOIN claims c ON c.claim_id=sc.claim_id
            JOIN documents d ON d.document_id=c.document_id
            LEFT JOIN sources src ON src.source_id=d.source_id
            WHERE {where}
            ORDER BY c.created_at DESC
            LIMIT 500
            """,
            params,
        ).fetchall()
        claims: list[Claim] = []
        for row in rows:
            try:
                source = SourceIdentity(
                    profile_id=row["document_source_id"],
                    publisher_group=row["document_publisher_group"],
                    kind=SourceKind(row["source_kind"] or "UNKNOWN"),
                    verified=bool(row["source_verified"]),
                    resolution=row["source_resolution"],
                )
                document = RawDocument(
                    id=row["document_id"],
                    title=row["title"],
                    body=row["body"],
                    url=row["url"],
                    published_at=row["published_at"],
                    fetched_at=row["fetched_at"],
                    source=source,
                    metadata=_loads(row["metadata_json"], {}),
                )
                claims.append(Claim(
                    id=row["claim_id"],
                    document=document,
                    article_category=EventType(row["article_category"]),
                    event_type=EventType(row["event_type"]),
                    status=EventStatus(row["event_status"]),
                    facts=_loads(row["facts_json"], {}),
                    fact_support=_loads(row["fact_support_json"], {}),
                    subject_type=EntityType(row["subject_type"]),
                    entity_certainty=float(row["entity_certainty"]),
                    event_certainty=float(row["event_certainty"]),
                    sport_confidence=float(row["sport_confidence"]),
                    sport_signals=_loads(row["sport_signals_json"], []),
                    league_relevant=bool(row["league_relevant"]),
                    extraction_method="database_replay",
                    warnings=_loads(row["warnings_json"], []),
                ))
            except Exception:
                continue
        return claims

    def decision_exists(
        self, story_id: str, fingerprint: str, decision: str = "PUBLISH"
    ) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 FROM decisions
            WHERE story_id=? AND fingerprint=? AND decision=?
            ORDER BY decision_id DESC LIMIT 1
            """,
            (story_id, fingerprint, decision),
        ).fetchone()
        return row is not None

    def has_publication_fingerprint(self, fingerprint: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM publications WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        return row is not None

    def latest_publication(self, family_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            """
            SELECT * FROM publications
            WHERE family_id=? ORDER BY published_at DESC, publication_id DESC LIMIT 1
            """,
            (family_id,),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["facts"] = _loads(result.pop("facts_json"), {})
        result["source_ids"] = _loads(result.pop("source_ids_json"), [])
        return result

    def mark_published(
        self,
        decision: VerificationDecision,
        *,
        platform: str = "X",
        platform_post_id: Optional[str] = None,
        published_at: Optional[str] = None,
    ) -> None:
        if not decision.may_publish:
            raise RepositoryError("refusing to record publication for non-publishable decision")
        timestamp = published_at or datetime.now(timezone.utc).isoformat()
        with self.transaction():
            self.conn.execute(
                """
                INSERT INTO publications(
                    story_id, family_id, event_type, event_status, fingerprint,
                    facts_json, source_ids_json, platform, platform_post_id,
                    published_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    decision.story_id,
                    decision.family_id,
                    decision.event_type.value,
                    decision.status.value,
                    decision.fingerprint,
                    _json(decision.verified_facts),
                    _json(decision.source_ids),
                    platform,
                    platform_post_id,
                    timestamp,
                ),
            )

    def source_outcome_stats(self, source_id: str) -> Dict[str, float]:
        rows = self.conn.execute(
            """
            SELECT outcome, COUNT(*) AS n, COALESCE(SUM(weight), 0) AS weight
            FROM source_outcomes WHERE source_id=? GROUP BY outcome
            """,
            (source_id,),
        ).fetchall()
        result: Dict[str, float] = {
            "officially_confirmed": 0.0,
            "contradicted": 0.0,
            "corrected": 0.0,
            "unresolved": 0.0,
        }
        for row in rows:
            result[row["outcome"]] = float(row["weight"])
        return result

    def record_source_outcome(
        self,
        *,
        source_id: str,
        story_id: str,
        claim_id: Optional[str],
        outcome: str,
        weight: float,
        authority_document_id: Optional[str],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction():
            self.conn.execute(
                """
                INSERT OR IGNORE INTO source_outcomes(
                    source_id, story_id, claim_id, outcome, weight,
                    authority_document_id, observed_at
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    source_id,
                    story_id,
                    claim_id,
                    outcome,
                    float(weight),
                    authority_document_id,
                    now,
                ),
            )

    def close(self) -> None:
        with self._lock:
            self.conn.close()
