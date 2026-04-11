from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Iterator, Literal

import yaml
from sqlalchemy import Float, ForeignKey, Integer, Text, create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "standardizer.db"
SECTIONIZER_CONFIG_PATH = PROJECT_ROOT / "agents" / "sectionizer" / "config.json"
WEBSITE_ARTICLES_DIR = PROJECT_ROOT.parent / "newsletter_website" / "src" / "content" / "articles"
DATABASE_URL = f"sqlite:///{DB_PATH.as_posix()}"
HomepageSlot = Literal["headline", "latest"]
HOMEPAGE_SLOTS: tuple[HomepageSlot, ...] = ("headline", "latest")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


class Base(DeclarativeBase):
    pass


class SectionizerCategory(Base):
    __tablename__ = "sectionizer_categories"

    sectionizer_category_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    objective: Mapped[str | None] = mapped_column(Text, nullable=True)
    min_score: Mapped[float] = mapped_column(Float, nullable=False)
    rules: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class HoarderOutput(Base):
    __tablename__ = "hoarder_outputs"

    hoarder_output_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_created_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_modified_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    screened_at: Mapped[str | None] = mapped_column(Text, nullable=True)


class HoarderSourceRun(Base):
    __tablename__ = "hoarder_source_runs"

    hoarder_source_run_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    item_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class HoarderSourceArtifact(Base):
    __tablename__ = "hoarder_source_artifacts"

    hoarder_source_artifact_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_at: Mapped[str] = mapped_column(Text, nullable=False)


class ScreenedFile(Base):
    __tablename__ = "screened_files"

    screened_file_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_created_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_modified_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_selected: Mapped[int] = mapped_column(Integer, nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    processed_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    hoarder_output_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Document(Base):
    __tablename__ = "documents"

    doc_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str | None] = mapped_column(Text, nullable=True)
    text_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False)
    extraction_status: Mapped[str] = mapped_column(Text, nullable=False)
    extraction_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_sha256: Mapped[str | None] = mapped_column(Text, nullable=True)
    modified_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    persisted_at: Mapped[str] = mapped_column(Text, nullable=False)


class MediaAsset(Base):
    __tablename__ = "media_assets"

    media_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[int] = mapped_column(Integer, ForeignKey("documents.doc_id"), nullable=False)
    artifact_path: Mapped[str] = mapped_column(Text, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class DocumentScreenedFile(Base):
    __tablename__ = "document_screened_files"

    document_screened_file_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[int] = mapped_column(Integer, ForeignKey("documents.doc_id"), nullable=False)
    screened_file_id: Mapped[int] = mapped_column(Integer, ForeignKey("screened_files.screened_file_id"), nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class HoarderOutputMediaAsset(Base):
    __tablename__ = "hoarder_output_media_assets"

    hoarder_output_media_asset_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hoarder_output_id: Mapped[int] = mapped_column(Integer, ForeignKey("hoarder_outputs.hoarder_output_id"), nullable=False)
    media_id: Mapped[int] = mapped_column(Integer, ForeignKey("media_assets.media_id"), nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class SectionizerOutput(Base):
    __tablename__ = "sectionizer_outputs"

    sectionizer_output_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[int] = mapped_column(Integer, ForeignKey("documents.doc_id"), nullable=False)
    category_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("sectionizer_categories.sectionizer_category_id"), nullable=True)
    homepage_slot: Mapped[HomepageSlot | None] = mapped_column(Text, nullable=True)
    homepage_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    homepage_active: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_path: Mapped[str] = mapped_column(Text, nullable=False)
    source_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    match_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_timestamp: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class SectionizerOutputDocument(Base):
    __tablename__ = "sectionizer_output_documents"

    sectionizer_output_document_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sectionizer_output_id: Mapped[int] = mapped_column(Integer, ForeignKey("sectionizer_outputs.sectionizer_output_id"), nullable=False)
    doc_id: Mapped[int] = mapped_column(Integer, ForeignKey("documents.doc_id"), nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class NewsletterRun(Base):
    __tablename__ = "newsletter_runs"

    newsletter_run_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_timestamp: Mapped[str] = mapped_column(Text, nullable=False)
    llm_instruction: Mapped[str | None] = mapped_column(Text, nullable=True)
    llm_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    output_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    output_html: Mapped[str | None] = mapped_column(Text, nullable=True)


class NewsletterRunSectionizerOutput(Base):
    __tablename__ = "newsletter_run_sectionizer_outputs"

    newsletter_run_sectionizer_output_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    newsletter_run_id: Mapped[int] = mapped_column(Integer, ForeignKey("newsletter_runs.newsletter_run_id"), nullable=False)
    sectionizer_output_id: Mapped[int] = mapped_column(Integer, ForeignKey("sectionizer_outputs.sectionizer_output_id"), nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)


class NewsletterRunConfig(Base):
    __tablename__ = "newsletter_run_configs"

    newsletter_run_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    newsletter_date: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


class HomepageItem(Base):
    __tablename__ = "homepage_items"

    homepage_item_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_id: Mapped[str] = mapped_column(Text, nullable=False)
    slot: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False)


engine = create_engine(DATABASE_URL, future=True)
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    future=True,
)


def _ensure_column(table_name: str, column_name: str, column_sql: str) -> None:
    inspector = inspect(engine)
    existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
    if column_name not in existing_columns:
        with engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}"))


def _ensure_homepage_slot_constraints() -> None:
    allowed_values_sql = ", ".join(f"'{slot}'" for slot in HOMEPAGE_SLOTS)
    trigger_specs = {
        "validate_sectionizer_outputs_homepage_slot_insert": f"""
            CREATE TRIGGER IF NOT EXISTS validate_sectionizer_outputs_homepage_slot_insert
            BEFORE INSERT ON sectionizer_outputs
            FOR EACH ROW
            WHEN NEW.homepage_slot IS NOT NULL
             AND TRIM(NEW.homepage_slot) <> ''
             AND NEW.homepage_slot NOT IN ({allowed_values_sql})
            BEGIN
              SELECT RAISE(ABORT, 'invalid sectionizer_outputs.homepage_slot');
            END;
        """,
        "validate_sectionizer_outputs_homepage_slot_update": f"""
            CREATE TRIGGER IF NOT EXISTS validate_sectionizer_outputs_homepage_slot_update
            BEFORE UPDATE OF homepage_slot ON sectionizer_outputs
            FOR EACH ROW
            WHEN NEW.homepage_slot IS NOT NULL
             AND TRIM(NEW.homepage_slot) <> ''
             AND NEW.homepage_slot NOT IN ({allowed_values_sql})
            BEGIN
              SELECT RAISE(ABORT, 'invalid sectionizer_outputs.homepage_slot');
            END;
        """,
    }
    with engine.begin() as connection:
        for sql in trigger_specs.values():
            connection.execute(text(sql))


def _load_sectionizer_seed_categories() -> list[dict[str, object]]:
    if not SECTIONIZER_CONFIG_PATH.exists():
        return []

    try:
        raw_config = json.loads(SECTIONIZER_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    raw_sections = raw_config.get("sections") if isinstance(raw_config, dict) else None
    if not isinstance(raw_sections, list):
        return []

    categories: list[dict[str, object]] = []
    for raw_section in raw_sections:
        if not isinstance(raw_section, dict):
            continue

        name = str(raw_section.get("name") or "").strip()
        if not name:
            continue

        objective = str(raw_section.get("objective") or "").strip() or None
        try:
            min_score = float(raw_section.get("min_score"))
        except (TypeError, ValueError):
            min_score = 0.0

        rules = raw_section.get("rules")
        if not isinstance(rules, list):
            rules = []

        categories.append(
            {
                "name": name,
                "objective": objective,
                "min_score": max(0.0, min(1.0, min_score)),
                "rules": json.dumps(rules, ensure_ascii=True),
            }
        )

    return categories


def _seed_sectionizer_categories() -> None:
    seed_categories = _load_sectionizer_seed_categories()
    if not seed_categories:
        return

    session = SessionLocal()
    try:
        existing_count = session.query(SectionizerCategory).count()
        if existing_count > 0:
            return

        timestamp = utc_now_iso()
        for category in seed_categories:
            session.add(
                SectionizerCategory(
                    name=str(category["name"]),
                    objective=category["objective"] if isinstance(category["objective"], str) or category["objective"] is None else None,
                    min_score=float(category["min_score"]),
                    rules=str(category["rules"]),
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
        session.commit()
    finally:
        session.close()


def _extract_frontmatter(raw_text: str) -> dict[str, object]:
    if not raw_text.startswith("---"):
        return {}

    match = re.match(r"^---\r?\n(.*?)\r?\n---", raw_text, flags=re.DOTALL)
    if not match:
        return {}

    try:
        loaded = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _load_homepage_seed_items() -> list[dict[str, object]]:
    if not WEBSITE_ARTICLES_DIR.exists():
        return []

    article_rows: list[dict[str, object]] = []
    for article_file in WEBSITE_ARTICLES_DIR.glob("*/index.mdx"):
        try:
            frontmatter = _extract_frontmatter(article_file.read_text(encoding="utf-8"))
        except OSError:
            continue

        article_id = article_file.parent.name
        if not article_id:
            continue

        article_rows.append(
            {
                "article_id": article_id,
                "is_main": bool(frontmatter.get("isMainHeadline") is True),
                "is_sub": bool(frontmatter.get("isSubHeadline") is True),
                "published_time": str(frontmatter.get("publishedTime") or ""),
            }
        )

    article_rows.sort(key=lambda row: str(row.get("published_time") or ""), reverse=True)
    if not article_rows:
        return []

    seed_items: list[dict[str, object]] = []
    used_article_ids: set[str] = set()

    main_articles = [row for row in article_rows if bool(row.get("is_main"))]
    if main_articles:
        article_id = str(main_articles[0]["article_id"])
        seed_items.append({"source_id": article_id, "slot": "headline", "position": 1})
        used_article_ids.add(article_id)

    sub_articles = [row for row in article_rows if bool(row.get("is_sub")) and str(row.get("article_id")) not in used_article_ids]
    for index, row in enumerate(sub_articles[:4], start=1):
        article_id = str(row["article_id"])
        seed_items.append({"source_id": article_id, "slot": "headline", "position": index + 1})
        used_article_ids.add(article_id)

    latest_articles = [row for row in article_rows if str(row.get("article_id")) not in used_article_ids]
    for index, row in enumerate(latest_articles[:6], start=1):
        article_id = str(row["article_id"])
        seed_items.append({"source_id": article_id, "slot": "latest", "position": index})
        used_article_ids.add(article_id)

    return seed_items


def _seed_homepage_items() -> None:
    seed_items = _load_homepage_seed_items()
    if not seed_items:
        return

    session = SessionLocal()
    try:
        existing_count = session.query(HomepageItem).count()
        if existing_count > 0:
            return

        timestamp = utc_now_iso()
        for item in seed_items:
            session.add(
                HomepageItem(
                    source_type="article",
                    source_id=str(item["source_id"]),
                    slot=str(item["slot"]),
                    position=int(item["position"]),
                    is_active=1,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
        session.commit()
    finally:
        session.close()


def ensure_standardizer_schema() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    _ensure_column("hoarder_outputs", "screened_at", "TEXT")
    _ensure_column("screened_files", "hoarder_output_id", "INTEGER")
    _ensure_column("screened_files", "processed_at", "TEXT")
    _ensure_column("sectionizer_outputs", "category_id", "INTEGER")
    _ensure_column("sectionizer_outputs", "homepage_slot", "TEXT")
    _ensure_column("sectionizer_outputs", "homepage_position", "INTEGER")
    _ensure_column("sectionizer_outputs", "homepage_active", "INTEGER")
    _ensure_column("sectionizer_outputs", "source_path", "TEXT")
    _ensure_column("sectionizer_outputs", "llm_instruction", "TEXT")
    _ensure_column("sectionizer_outputs", "llm_content", "TEXT")
    _ensure_column("sectionizer_outputs", "output_json", "TEXT")
    _ensure_column("sectionizer_outputs", "match_count", "INTEGER")
    _ensure_column("newsletter_runs", "llm_instruction", "TEXT")
    _ensure_column("newsletter_runs", "llm_content", "TEXT")
    _ensure_column("newsletter_runs", "output_html", "TEXT")
    _ensure_homepage_slot_constraints()
    _seed_sectionizer_categories()


@contextmanager
def session_scope() -> Iterator[Session]:
    ensure_standardizer_schema()
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
