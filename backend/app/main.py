from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .db import (
    CURRENT_TABLE,
    HISTORY_TABLE,
    LEGACY_TABLE,
    RESTORE_PREVIEW_META_TABLE,
    ServiceError,
    db_session,
    get_records_generation,
    get_revision,
    init_db,
)
from .migration import (
    HISTORY_ROWS_TABLE,
    CommitPayload,
    LegacyRowPayload,
    MappingsPayload,
    commit_preview,
    create_preview,
)
from .restore import (
    RestoreCommitPayload,
    RestorePreviewPayload,
    commit_restore,
    create_restore_preview,
)

ALLOWED_FAULTS = {"preview_copy", "commit_switch", "restore_switch"}


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="SQLite Shadow Migration", version="2.0.0", lifespan=lifespan)


class FaultPayload(BaseModel):
    name: str
    enabled: bool


@app.exception_handler(ServiceError)
async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal_error", "message": str(exc)}},
    )


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/admin/reset")
def reset_database() -> dict[str, object]:
    if os.environ.get("ALLOW_FAULT_INJECTION") != "1":
        return JSONResponse(
            status_code=403,
            content={"error": {"code": "forbidden", "message": "test controls are disabled"}},
        )
    init_db(reset=True)
    with db_session() as conn:
        return _state_payload(conn)


@app.get("/api/state")
def state() -> dict[str, object]:
    with db_session() as conn:
        return _state_payload(conn)


@app.get("/api/legacy")
def list_legacy() -> dict[str, object]:
    with db_session() as conn:
        rows = [dict(row) for row in conn.execute(f"SELECT * FROM {LEGACY_TABLE} ORDER BY legacy_id")]
        return {"revision": get_revision(conn), "rows": rows}


@app.post("/api/legacy")
def insert_legacy(payload: LegacyRowPayload) -> dict[str, object]:
    with db_session() as conn:
        try:
            conn.execute(
                f"INSERT INTO {LEGACY_TABLE}(legacy_id, code, raw_name, note) VALUES (?, ?, ?, ?)",
                (payload.legacy_id, payload.code, payload.raw_name, payload.note),
            )
        except sqlite3.IntegrityError as exc:
            return JSONResponse(
                status_code=409,
                content={"error": {"code": "legacy_constraint_failed", "message": str(exc)}},
            )
        return {"revision": get_revision(conn), "row": payload.model_dump()}


@app.get("/api/records")
def list_records() -> dict[str, object]:
    with db_session() as conn:
        rows = [dict(row) for row in conn.execute(f"SELECT * FROM {CURRENT_TABLE} ORDER BY id")]
        return {
            "generation": get_records_generation(conn),
            "rows": rows,
            "count": len(rows),
        }


@app.post("/api/migrations/preview")
def preview_migration(payload: MappingsPayload) -> dict[str, object]:
    return create_preview(payload)


@app.post("/api/migrations/commit")
def migration_commit(payload: CommitPayload) -> dict[str, object]:
    return commit_preview(payload)


@app.post("/api/restores/preview")
def preview_restore(payload: RestorePreviewPayload) -> dict[str, object]:
    return create_restore_preview(payload)


@app.post("/api/restores/commit")
def restore_commit(payload: RestoreCommitPayload) -> dict[str, object]:
    return commit_restore(payload)


@app.get("/api/history")
def history() -> dict[str, object]:
    with db_session() as conn:
        versions = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT v.version_id, v.kind, v.migration_id, v.restore_id,
                       v.replaced_table, v.source_revision, v.source_version_id,
                       v.generation, v.row_count, v.locked, v.created_at,
                       m.committed_revision
                  FROM {HISTORY_TABLE} v
             LEFT JOIN migrations m ON m.migration_id = v.migration_id
                 ORDER BY v.version_id
                """
            )
        ]
        return {"records_generation": get_records_generation(conn), "versions": versions}


@app.get("/api/history/{version_id}/rows")
def history_rows(version_id: int) -> dict[str, object]:
    with db_session() as conn:
        version = conn.execute(
            f"SELECT * FROM {HISTORY_TABLE} WHERE version_id=?",
            (version_id,),
        ).fetchone()
        if version is None:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "version_not_found", "message": "history version does not exist"}},
            )
        rows = [
            dict(row)
            for row in conn.execute(
                f"SELECT id, code, label, legacy_id FROM {HISTORY_ROWS_TABLE} WHERE version_id=? ORDER BY id",
                (version_id,),
            )
        ]
        return {"version": dict(version), "rows": rows}


@app.post("/api/test/faults")
def set_fault(payload: FaultPayload) -> dict[str, object]:
    if os.environ.get("ALLOW_FAULT_INJECTION") != "1":
        return JSONResponse(
            status_code=403,
            content={"error": {"code": "forbidden", "message": "fault injection is disabled"}},
        )
    if payload.name not in ALLOWED_FAULTS:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": "unknown_fault", "message": "unknown fault injection point"}},
        )
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO fault_injection_state(name, enabled) VALUES (?, ?)
            ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled
            """,
            (payload.name, 1 if payload.enabled else 0),
        )
        return {"name": payload.name, "enabled": payload.enabled}


def _state_payload(conn: sqlite3.Connection) -> dict[str, object]:
    legacy_rows = [dict(row) for row in conn.execute(f"SELECT * FROM {LEGACY_TABLE} ORDER BY legacy_id")]
    formal_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (CURRENT_TABLE,),
    ).fetchone()
    current_rows = (
        [dict(row) for row in conn.execute(f"SELECT * FROM {CURRENT_TABLE} ORDER BY id")]
        if formal_exists
        else []
    )
    versions = [dict(row) for row in conn.execute(f"SELECT * FROM {HISTORY_TABLE} ORDER BY version_id")]
    previews = [dict(row) for row in conn.execute("SELECT * FROM migration_preview_meta")]
    restore_previews = [dict(row) for row in conn.execute(f"SELECT * FROM {RESTORE_PREVIEW_META_TABLE}")]
    shadows = [
        row[0]
        for row in conn.execute(
            """
            SELECT name FROM sqlite_master
             WHERE type='table'
               AND (name LIKE 'migration_shadow_%'
                    OR name LIKE 'records_pending_%'
                    OR name LIKE 'records_restore_candidate_%')
             ORDER BY name
            """
        )
    ]
    return {
        "revision": get_revision(conn),
        "records_generation": get_records_generation(conn),
        "legacy": legacy_rows,
        "records": current_rows,
        "formal_table_exists": formal_exists is not None,
        "history": versions,
        "previews": previews,
        "restore_previews": restore_previews,
        "shadow_tables": shadows,
    }


_frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _frontend_dist.exists():
    app.mount("/assets", StaticFiles(directory=_frontend_dist / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def index() -> JSONResponse:  # pragma: no cover - exercised in browser only
        from fastapi.responses import FileResponse

        return FileResponse(_frontend_dist / "index.html")
