from __future__ import annotations

import json
import re
import sqlite3
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .db import (
    CURRENT_TABLE,
    HISTORY_ROWS_TABLE,
    HISTORY_TABLE,
    INT64_MAX,
    INT64_MIN,
    LEGACY_TABLE,
    MIGRATIONS_TABLE,
    PREVIEW_META_TABLE,
    TARGET_COLUMNS,
    ServiceError,
    advance_records_generation,
    db_session,
    expire_all_previews,
    get_records_generation,
    get_revision,
)

LEGACY_COLUMNS = {"legacy_id", "code", "raw_name", "note"}
DECIMAL_INT_RE = re.compile(r"^-?[0-9]+$")
MAX_TEXT_LENGTH = 1_000


class FieldMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(..., pattern="^(copy|trim|decimal_int|constant)$")
    source_column: str | None = None
    value: Any = None

    @field_validator("source_column")
    @classmethod
    def validate_source_column(cls, value: str | None) -> str | None:
        if value is not None and value not in LEGACY_COLUMNS:
            raise ValueError(f"unknown source column: {value}")
        return value


class MappingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: FieldMapping
    code: FieldMapping
    label: FieldMapping

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return self.model_dump()


class LegacyRowPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    legacy_id: int
    code: str
    raw_name: str | None = None
    note: str | None = None


class CommitPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_id: str = Field(..., min_length=32, max_length=64, pattern="^[0-9a-f]+$")
    source_revision: int
    # Optional so older clients keep working; the generation stored server side
    # by the preview remains the authority that is always re-checked.
    records_generation: int | None = None


def _mapping_source(mapping: dict[str, Any]) -> str:
    if mapping["type"] == "constant":
        return f"constant:{json.dumps(mapping.get('value'), ensure_ascii=False, separators=(',', ':'))}"
    return f"{mapping['type']}:{mapping.get('source_column')}"


def _require_source(mapping: dict[str, Any], field_name: str) -> str:
    source = mapping.get("source_column")
    if not source:
        raise ServiceError(
            422,
            "mapping_without_source",
            f"Mapping for {field_name} requires source_column",
        )
    return source


def _apply_mapping(value: Any, mapping: dict[str, Any]) -> Any:
    mapping_type = mapping["type"]
    if mapping_type == "copy":
        return value
    if mapping_type == "trim":
        if value is None:
            return None
        if not isinstance(value, str):
            raise ServiceError(422, "trim_requires_text", "trim can only be applied to text")
        return value.strip()
    if mapping_type == "decimal_int":
        if value is None:
            return None
        if not isinstance(value, str) or not DECIMAL_INT_RE.fullmatch(value):
            raise ServiceError(422, "not_decimal_integer", "value is not a decimal integer")
        try:
            parsed = int(value, 10)
        except ValueError as exc:
            raise ServiceError(422, "not_decimal_integer", "value is not a decimal integer") from exc
        if not INT64_MIN <= parsed <= INT64_MAX:
            raise ServiceError(422, "integer_out_of_range", "integer is outside SQLite signed 64-bit range")
        return parsed
    if mapping_type == "constant":
        constant = mapping.get("value")
        if isinstance(constant, str) and len(constant) > MAX_TEXT_LENGTH:
            raise ServiceError(422, "constant_too_long", "constant text is too long")
        return constant
    raise ServiceError(422, "unknown_mapping", f"unsupported mapping type: {mapping_type}")


def _validate_output(field_name: str, value: Any) -> str | None:
    if value is None:
        return "required"
    if field_name == "id":
        if isinstance(value, bool) or not isinstance(value, int):
            return "integer_required"
        if not INT64_MIN <= value <= INT64_MAX:
            return "integer_out_of_range"
    else:
        if not isinstance(value, str):
            return "text_required"
        if value == "":
            return "required"
        if len(value) > MAX_TEXT_LENGTH:
            return "text_too_long"
    return None


def _fault_enabled(conn, name: str) -> bool:
    row = conn.execute("SELECT enabled FROM fault_injection_state WHERE name=?", (name,)).fetchone()
    return bool(row and row[0])


def _create_shadow(conn, shadow_name: str) -> None:
    # The shadow intentionally has no new-table constraints.  Constraints are
    # checked row by row so one failed row does not abort the complete report.
    conn.execute(
        f"""
        CREATE TABLE {shadow_name} (
            shadow_row_id INTEGER PRIMARY KEY,
            legacy_id INTEGER,
            id_value ANY,
            code_value ANY,
            label_value ANY
        )
        """
    )


def create_preview(mappings: MappingsPayload, db_path: str | None = None) -> dict[str, Any]:
    mapping_data = mappings.as_dict()
    preview_id = uuid.uuid4().hex
    shadow_name = f"migration_shadow_{preview_id}"

    with db_session(db_path) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            source_revision = get_revision(conn)
            records_generation = get_records_generation(conn)
            _create_shadow(conn, shadow_name)

            source_rows = conn.execute(
                f"SELECT rowid AS row_number, legacy_id, code, raw_name, note FROM {LEGACY_TABLE}"
            ).fetchall()

            transformed: list[dict[str, Any]] = []
            for source in source_rows:
                source_dict = dict(source)
                output: dict[str, Any] = {}
                transform_errors: list[dict[str, str]] = []
                for field_name in TARGET_COLUMNS:
                    mapping = mapping_data[field_name]
                    source_value = None
                    if mapping["type"] != "constant":
                        source_column = _require_source(mapping, field_name)
                        source_value = source_dict[source_column]
                    try:
                        output[field_name] = _apply_mapping(source_value, mapping)
                    except ServiceError as exc:
                        output[field_name] = None
                        transform_errors.append(
                            {
                                "field": field_name,
                                "code": exc.code,
                                "message": exc.message,
                                "mapping": _mapping_source(mapping),
                            }
                        )
                transformed.append(
                    {
                        "row_number": source_dict["row_number"],
                        "legacy_id": source_dict["legacy_id"],
                        "output": output,
                        "transform_errors": transform_errors,
                    }
                )

            conn.executemany(
                f"""
                INSERT INTO {shadow_name}
                    (shadow_row_id, legacy_id, id_value, code_value, label_value)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["row_number"],
                        row["legacy_id"],
                        row["output"].get("id"),
                        row["output"].get("code"),
                        row["output"].get("label"),
                    )
                    for row in transformed
                ],
            )

            if _fault_enabled(conn, "preview_copy"):
                raise RuntimeError("injected failure after shadow copy")

            failures = _validate_shadow_rows(conn, shadow_name, mapping_data, transformed)
            if failures:
                conn.rollback()
                return {
                    "preview_id": None,
                    "source_revision": source_revision,
                    "records_generation": records_generation,
                    "ok": False,
                    "row_count": len(source_rows),
                    "mappings": mapping_data,
                    "failures": failures,
                }

            conn.execute(
                f"""
                INSERT INTO {PREVIEW_META_TABLE}
                    (preview_id, source_revision, records_generation, row_count, mappings_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (preview_id, source_revision, records_generation, len(source_rows), json.dumps(mapping_data)),
            )
            conn.commit()
            return {
                "preview_id": preview_id,
                "source_revision": source_revision,
                "records_generation": records_generation,
                "ok": True,
                "row_count": len(source_rows),
                "mappings": mapping_data,
                "failures": [],
            }
        except Exception:
            conn.rollback()
            raise


def _validate_shadow_rows(
    conn,
    shadow_name: str,
    mapping_data: dict[str, dict[str, Any]],
    transformed: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    seen_values: dict[str, dict[Any, int]] = {"id": {}, "code": {}}
    shadow_rows = conn.execute(
        f"""
        SELECT shadow_row_id, legacy_id, id_value, code_value, label_value
          FROM {shadow_name}
         ORDER BY shadow_row_id
        """
    ).fetchall()

    for shadow, prepared in zip(shadow_rows, transformed):
        errors: list[dict[str, str]] = list(prepared["transform_errors"])
        values = {
            "id": shadow["id_value"],
            "code": shadow["code_value"],
            "label": shadow["label_value"],
        }
        for field_name, value in values.items():
            code = _validate_output(field_name, value)
            if code:
                errors.append(
                    {
                        "field": field_name,
                        "code": code,
                        "message": _validation_message(code),
                        "mapping": _mapping_source(mapping_data[field_name]),
                    }
                )
                continue
            if field_name not in seen_values:
                continue
            first_row = seen_values[field_name].get(value)
            if first_row is not None:
                errors.append(
                    {
                        "field": field_name,
                        "code": "duplicate",
                        "message": f"duplicate value already used by source row {first_row}",
                        "mapping": _mapping_source(mapping_data[field_name]),
                    }
                )
            else:
                seen_values[field_name][value] = shadow["shadow_row_id"]

        legacy_id = shadow["legacy_id"]
        if legacy_id is None or not isinstance(legacy_id, int) or isinstance(legacy_id, bool):
            errors.append(
                {
                    "field": "legacy_id",
                    "code": "integer_required",
                    "message": "source legacy_id must be an integer",
                    "mapping": "copy:legacy_id",
                }
            )

        if errors:
            failures.append(
                {
                    "row_number": shadow["shadow_row_id"],
                    "legacy_id": shadow["legacy_id"],
                    "values": values,
                    "errors": errors,
                }
            )
    return failures


def _discard_failed_preview(conn: sqlite3.Connection, preview_id: str, pending_name: str) -> None:
    """Remove artifacts from a failed commit without touching the formal table."""
    conn.execute(f"DROP TABLE IF EXISTS {pending_name}")
    conn.execute(f"DROP TABLE IF EXISTS migration_shadow_{preview_id}")
    conn.execute(f"DELETE FROM {PREVIEW_META_TABLE} WHERE preview_id=?", (preview_id,))


def _validation_message(code: str) -> str:
    return {
        "required": "value is required",
        "integer_required": "value must be an integer",
        "integer_out_of_range": "integer is outside signed 64-bit range",
        "text_required": "value must be text",
        "text_too_long": "text is too long",
    }.get(code, code)


def commit_preview(payload: CommitPayload, db_path: str | None = None) -> dict[str, Any]:
    with db_session(db_path) as conn:
        pending_name = f"records_pending_{payload.preview_id}"
        shadow_name = f"migration_shadow_{payload.preview_id}"
        try:
            conn.execute("BEGIN IMMEDIATE")
            current_revision = get_revision(conn)
            if current_revision != payload.source_revision:
                raise ServiceError(
                    409,
                    "stale_preview",
                    "source table changed after preview; recalculate and preview again",
                )

            current_generation = get_records_generation(conn)
            preview = conn.execute(
                f"""
                SELECT preview_id, source_revision, records_generation, row_count, mappings_json
                  FROM {PREVIEW_META_TABLE}
                 WHERE preview_id=?
                """,
                (payload.preview_id,),
            ).fetchone()
            if preview is None:
                raise ServiceError(404, "preview_not_found", "preview was not accepted or has expired")
            if preview["source_revision"] != payload.source_revision:
                raise ServiceError(
                    409,
                    "stale_preview",
                    "commit revision does not match the revision used by this preview",
                )
            if payload.records_generation is not None and payload.records_generation != current_generation:
                raise ServiceError(
                    409,
                    "stale_preview",
                    "formal table generation changed after preview; preview again",
                )
            if preview["records_generation"] != current_generation:
                raise ServiceError(
                    409,
                    "stale_preview",
                    "formal table generation changed after this preview; a restore or newer commit landed; preview again",
                )

            shadow_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (shadow_name,),
            ).fetchone()
            if not shadow_exists:
                raise ServiceError(409, "shadow_missing", "shadow table is missing; preview again")

            shadow_count = conn.execute(f"SELECT COUNT(*) FROM {shadow_name}").fetchone()[0]
            if shadow_count != preview["row_count"]:
                raise ServiceError(409, "shadow_changed", "shadow table changed; preview again")

            new_generation = current_generation + 1
            cur = conn.execute(
                f"""
                INSERT INTO {MIGRATIONS_TABLE}
                    (preview_id, source_revision, committed_revision,
                     base_generation, new_generation, row_count)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.preview_id,
                    preview["source_revision"],
                    current_revision,
                    current_generation,
                    new_generation,
                    preview["row_count"],
                ),
            )
            migration_id = int(cur.lastrowid)
            old_count = conn.execute(f"SELECT COUNT(*) FROM {CURRENT_TABLE}").fetchone()[0]
            # Version ids are their own AUTOINCREMENT lineage: a restore also
            # creates versions, so a version id must never be forced to equal a
            # migration id.
            conn.execute(
                f"""
                INSERT INTO {HISTORY_TABLE}
                    (kind, migration_id, restore_id, replaced_table,
                     source_revision, source_version_id, generation, row_count, locked)
                VALUES ('migration', ?, NULL, ?, ?, NULL, ?, ?, 0)
                """,
                (migration_id, CURRENT_TABLE, preview["source_revision"],
                 current_generation, old_count),
            )
            archived_version_id = conn.execute(
                "SELECT last_insert_rowid()"
            ).fetchone()[0]

            conn.execute(
                f"""
                INSERT INTO {HISTORY_ROWS_TABLE}
                    (version_id, id, code, label, legacy_id)
                SELECT ?, id, code, label, legacy_id
                  FROM {CURRENT_TABLE}
                """,
                (archived_version_id,),
            )
            copied_old_count = conn.execute(
                f"SELECT COUNT(*) FROM {HISTORY_ROWS_TABLE} WHERE version_id=?",
                (archived_version_id,),
            ).fetchone()[0]
            if copied_old_count != old_count:
                raise RuntimeError("injected/copy mismatch while preserving old version")
            conn.execute(
                f"UPDATE {HISTORY_TABLE} SET locked=1 WHERE version_id=?",
                (archived_version_id,),
            )

            conn.execute(f"DROP TABLE IF EXISTS {pending_name}")
            conn.execute(
                f"""
                CREATE TABLE {pending_name} (
                    id INTEGER PRIMARY KEY,
                    code TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL,
                    legacy_id INTEGER NOT NULL UNIQUE
                )
                """
            )
            conn.execute(
                f"""
                INSERT INTO {pending_name}(id, code, label, legacy_id)
                SELECT id_value, code_value, label_value, legacy_id
                  FROM {shadow_name}
                """
            )
            pending_count = conn.execute(f"SELECT COUNT(*) FROM {pending_name}").fetchone()[0]
            if pending_count != preview["row_count"]:
                raise RuntimeError("pending row count does not match accepted preview")

            if _fault_enabled(conn, "commit_switch"):
                raise RuntimeError("injected failure before table switch")

            # DDL is transactional in SQLite.  This drop/rename pair therefore
            # either replaces the formal table completely or rolls back to the
            # exact previous table.
            conn.execute(f"DROP TABLE {CURRENT_TABLE}")
            conn.execute(f"ALTER TABLE {pending_name} RENAME TO {CURRENT_TABLE}")
            advanced_generation = advance_records_generation(conn)
            if advanced_generation != new_generation:
                raise RuntimeError("formal table generation mismatch after switch")
            # Every surviving preview saw the previous generation and could
            # otherwise be replayed against the new formal table.
            expire_all_previews(conn)
            conn.commit()

            return {
                "ok": True,
                "migration_id": migration_id,
                "preview_id": payload.preview_id,
                "source_revision": preview["source_revision"],
                "committed_revision": current_revision,
                "base_generation": current_generation,
                "new_generation": new_generation,
                "row_count": pending_count,
                "old_version": {
                    "version_id": archived_version_id,
                    "row_count": old_count,
                },
            }
        except ServiceError:
            conn.rollback()
            # A rejected commit is never replayable as-is; remove its preview
            # and shadow so the page can force the client to recalculate.
            _discard_failed_preview(conn, payload.preview_id, pending_name)
            raise
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            _discard_failed_preview(conn, payload.preview_id, pending_name)
            raise ServiceError(422, "constraint_failed", f"new-table constraint failed: {exc}") from exc
        except Exception:
            conn.rollback()
            _discard_failed_preview(conn, payload.preview_id, pending_name)
            raise
