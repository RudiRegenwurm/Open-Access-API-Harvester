# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Rudolf Kiechle

"""Corpus verification (MASTER_SPEC section 36).

An operational integrity tool, not a test helper: it scans the local corpus and the
state store and reports every disagreement between them — missing sidecars, missing
artifacts, checksum mismatches, invalid artifacts, and files nothing accounts for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .acquisition import PART_SUFFIX
from .config import Config
from .identity import artifact_path
from .models import ArtifactKind, DocumentStatus
from .state import StateStore
from .storage import check_artifact_file, list_corpus_documents, orphan_artifacts
from .util import utc_now_iso

LOGGER = logging.getLogger("harvester.verify")


@dataclass(slots=True)
class VerificationReport:
    checked_at: str
    storage_root: str
    documents_in_state: int = 0
    documents_in_corpus: int = 0
    completed_documents: int = 0
    artifacts_checked: int = 0
    sidecars_present: int = 0
    problems: list[dict[str, Any]] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    #: Abandoned ``.part`` files. Informational, not a corpus problem: they hold no
    #: validated content and the next non-dry run removes them once they are stale.
    temporary_files: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems and not self.orphans

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_at": self.checked_at,
            "storage_root": self.storage_root,
            "documents_in_state": self.documents_in_state,
            "documents_in_corpus": self.documents_in_corpus,
            "completed_documents": self.completed_documents,
            "artifacts_checked": self.artifacts_checked,
            "sidecars_present": self.sidecars_present,
            "problem_count": len(self.problems),
            "problems": self.problems,
            "orphans": self.orphans,
            "temporary_files": self.temporary_files,
        }


def verify_corpus(config: Config, store: StateStore, *, deep: bool = False) -> VerificationReport:
    """Check state against the filesystem."""
    root = Path(config.storage_root)
    report = VerificationReport(checked_at=utc_now_iso(), storage_root=str(root))

    document_ids = store.all_document_ids()
    report.documents_in_state = len(document_ids)
    report.documents_in_corpus = len(list_corpus_documents(root))

    for document_id in document_ids:
        status = store.get_document_status(document_id)
        artifacts = store.get_artifacts(document_id)

        if status is DocumentStatus.COMPLETED:
            report.completed_documents += 1
            sidecar_path = artifact_path(root, document_id, "json")
            if sidecar_path.exists():
                report.sidecars_present += 1
            else:
                report.problems.append(
                    {
                        "document_id": document_id,
                        "kind": "json",
                        "problem": "mandatory sidecar is missing for a COMPLETED document",
                    }
                )
            if ArtifactKind.PDF.value not in artifacts:
                report.problems.append(
                    {
                        "document_id": document_id,
                        "kind": "pdf",
                        "problem": "COMPLETED document has no PDF artifact recorded",
                    }
                )

        for record in artifacts.values():
            report.artifacts_checked += 1
            check = check_artifact_file(
                root,
                document_id,
                record,
                deep=deep,
                min_pdf_size_bytes=config.downloads.min_pdf_size_bytes,
                min_xml_size_bytes=config.downloads.min_xml_size_bytes,
            )
            if check.problem:
                report.problems.append(check.to_dict())

    report.orphans = orphan_artifacts(root, set(document_ids))
    if root.exists():
        report.temporary_files = sorted(p.name for p in root.glob(f"*{PART_SUFFIX}"))
    return report


def render_verification(report: VerificationReport) -> str:
    lines = [
        f"corpus verification of {report.storage_root}",
        f"  result             : {'OK' if report.ok else 'PROBLEMS FOUND'}",
        f"  documents in state : {report.documents_in_state}",
        f"  documents in corpus: {report.documents_in_corpus}",
        f"  completed documents: {report.completed_documents}",
        f"  artifacts checked  : {report.artifacts_checked}",
        f"  sidecars present   : {report.sidecars_present}",
        f"  problems           : {len(report.problems)}",
        f"  orphan files       : {len(report.orphans)}",
        f"  temporary files    : {len(report.temporary_files)}",
    ]
    for problem in report.problems[:50]:
        lines.append(
            f"    ! {problem.get('document_id')} [{problem.get('kind')}]: "
            f"{problem.get('problem')}"
        )
    if len(report.problems) > 50:
        lines.append(f"    ... and {len(report.problems) - 50} more")
    for orphan in report.orphans[:20]:
        lines.append(f"    ? orphan file with no state record: {orphan}")
    for temporary in report.temporary_files[:20]:
        lines.append(
            f"    i abandoned temporary file (removed by the next run once stale): {temporary}"
        )
    return "\n".join(lines)

