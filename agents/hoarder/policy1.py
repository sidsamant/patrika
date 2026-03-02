from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePath


HoarderDecision = str


@dataclass(frozen=True)
class HoarderMetadata:
    source_id: str
    source_path: str
    file_name: str
    extension: str
    source_location: str
    tags: list[str]
    ingested_at: str
    created_at: str | None = None
    updated_at: str | None = None
    authors: list[str] | None = None
    abstract: str | None = None
    has_images: bool | None = None
    front_image: str | None = None
    temporal: str | None = None
    status: str | None = None


@dataclass(frozen=True)
class HoarderAuditRecord:
    processed_at: str
    decision: HoarderDecision
    reason: str
    metadata: HoarderMetadata


FINAL_FOLDER_HINTS = ("/final/", "/approved/")
RELEASED_STATUS_HINTS = ("released", "frozen")
DRAFT_HINTS = ("draft", "wip", "work in progress")
ALLOWED_EXTENSIONS = {".md", ".txt", ".docx", ".pdf", ".html"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize(value: str | None) -> str:
    return (value or "").strip().lower()


def _includes_any(source: str | None, hints: tuple[str, ...]) -> bool:
    normalized = _normalize(source)
    return any(_normalize(hint) in normalized for hint in hints)


def _has_allowed_extension(file_name: str) -> bool:
    return PurePath(file_name).suffix.lower() in ALLOWED_EXTENSIONS


def _is_draft_by_name_or_tag(meta: HoarderMetadata) -> bool:
    if _includes_any(meta.file_name, DRAFT_HINTS):
        return True
    return any(_includes_any(tag, DRAFT_HINTS) for tag in meta.tags)


def _has_final_signal(meta: HoarderMetadata) -> bool:
    normalized_path = meta.source_path.replace("\\", "/")
    path_hint = _includes_any(normalized_path, FINAL_FOLDER_HINTS)
    status_hint = _includes_any(meta.status, RELEASED_STATUS_HINTS)
    return path_hint or status_hint


def _infer_from_path(path: str) -> tuple[str, str, str]:
    normalized = path.replace("\\", "/")
    p = PurePath(normalized)
    file_name = p.name or normalized
    extension = p.suffix.lower()
    source_location = str(p.parent).replace(".", "")
    return file_name, extension, source_location


def create_metadata(*, source_id: str, source_path: str, **kwargs: object) -> HoarderMetadata:
    file_name, extension, source_location = _infer_from_path(source_path)

    return HoarderMetadata(
        source_id=source_id,
        source_path=source_path,
        file_name=str(kwargs.get("file_name") or file_name),
        extension=str(kwargs.get("extension") or extension),
        source_location=str(kwargs.get("source_location") or source_location),
        tags=list(kwargs.get("tags") or []),
        ingested_at=str(kwargs.get("ingested_at") or _now_iso()),
        created_at=kwargs.get("created_at") if isinstance(kwargs.get("created_at"), str) else None,
        updated_at=kwargs.get("updated_at") if isinstance(kwargs.get("updated_at"), str) else None,
        authors=list(kwargs.get("authors") or []),
        abstract=kwargs.get("abstract") if isinstance(kwargs.get("abstract"), str) else None,
        has_images=kwargs.get("has_images") if isinstance(kwargs.get("has_images"), bool) else None,
        front_image=kwargs.get("front_image") if isinstance(kwargs.get("front_image"), str) else None,
        temporal=kwargs.get("temporal") if isinstance(kwargs.get("temporal"), str) else None,
        status=kwargs.get("status") if isinstance(kwargs.get("status"), str) else None,
    )


def evaluate_document_for_ingestion(meta: HoarderMetadata) -> HoarderAuditRecord:
    processed_at = _now_iso()

    if not _has_allowed_extension(meta.file_name):
        return HoarderAuditRecord(
            processed_at=processed_at,
            decision="rejected_non_document",
            reason="Skipped because extension is not in the allowed document list.",
            metadata=meta,
        )

    if _is_draft_by_name_or_tag(meta):
        return HoarderAuditRecord(
            processed_at=processed_at,
            decision="rejected_draft",
            reason="Skipped because file name or tags indicate a draft/work-in-progress state.",
            metadata=meta,
        )

    if not _has_final_signal(meta):
        return HoarderAuditRecord(
            processed_at=processed_at,
            decision="rejected_missing_final_signal",
            reason="Skipped because no final/approved folder hint or released/frozen status was detected.",
            metadata=meta,
        )

    return HoarderAuditRecord(
        processed_at=processed_at,
        decision="accepted",
        reason="Accepted for downstream standardization and vectorization.",
        metadata=meta,
    )
