from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .db import (
    CURRENT_TABLE,
    HISTORY_ROWS_TABLE,
    HISTORY_TABLE,
    RESTORES_TABLE,
    RESTORE_PREVIEW_META_TABLE,
    ServiceError,
    advance_records_generation,
    db_session,
    expire_all_previews,
    get_records_generation,
    get_revision,
)
from .migration import _fault_enabled

CANDIDATE_PREFIX = "records_restore_candidate_"
DIFF_LIMIT = 200
FORMAL_COLUMNS = ("id", "code", "label", "legacy_id")
# Entities are tracked across versions by the stable legacy_id; the row primary
# key is just another updatable column and may differ between generations.
ENTITY_KEY = "legacy_id"


class RestorePreviewPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_id: int = Field(..., ge=1)
    selected_legacy_ids: list[int] | None = Field(default=None)


class RestoreCommitPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_id: str = Field(..., min_length=32, max_length=64, pattern="^[0-9a-f]+$")
    # Server-stored generation is authoritative; the client echo stays optional
    # for compatibility and must match when provided.
    records_generation: int | None = None


def _candidate_name(preview_id: str) -> str:
    return f"{CANDIDATE_PREFIX}{preview_id}"


def _formal_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (CURRENT_TABLE,),
    ).fetchone()
    return row is not None


def _require_locked_version(conn: sqlite3.Connection, version_id: int) -> sqlite3.Row:
    version = conn.execute(
        f"""
        SELECT version_id, kind, migration_id, restore_id, source_revision,
               source_version_id, generation, row_count, locked
          FROM {HISTORY_TABLE}
         WHERE version_id=?
        """,
        (version_id,),
    ).fetchone()
    if version is None:
        raise ServiceError(
            404,
            "version_not_found",
            "history version does not exist",
        )
    if not version["locked"]:
        # An unlocked row only exists while an archiving transaction is open.
        raise ServiceError(409, "version_not_ready", "history version is not sealed")
    return version


def _history_rows(conn: sqlite3.Connection, version_id: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT id, code, label, legacy_id
              FROM {HISTORY_ROWS_TABLE}
             WHERE version_id=?
             ORDER BY id
            """,
            (version_id,),
        )
    ]


def _formal_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _formal_table_exists(conn):
        return []
    return [
        dict(row)
        for row in conn.execute(
            f"SELECT id, code, label, legacy_id FROM {CURRENT_TABLE} ORDER BY id"
        )
    ]


def _row_signature(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row[column] for column in FORMAL_COLUMNS)


def _validate_selection(
    selected: list[int] | None,
    history_by_legacy_id: dict[int, dict[str, Any]],
) -> list[int]:
    """Validate the requested legacy-id selection against the sealed version."""
    if selected is None:
        return []
    if not selected or len(selected) != len(set(selected)):
        raise ServiceError(
            422, "invalid_selection", "select one or more unique legacy IDs"
        )
    missing = [legacy_id for legacy_id in selected if legacy_id not in history_by_legacy_id]
    if missing:
        raise ServiceError(
            422,
            "invalid_selection",
            f"selected legacy ID missing in history version: {missing[0]}",
        )
    return list(selected)


def _build_candidate_rows(
    current_rows: list[dict[str, Any]],
    source_rows: list[dict[str, Any]],
    selected: list[int],
) -> list[dict[str, Any]]:
    """Build the FULL next formal table for a restore.

    A full restore (no selection) reproduces the sealed version wholesale.
    A partial restore replaces only the selected stable entities (keyed by
    legacy_id) with their historical content; every other current entity stays
    exactly as it is, so an unselected record can never be removed.
    """
    if not selected:
        return [dict(row) for row in source_rows]

    source_by_legacy_id = {row[ENTITY_KEY]: row for row in source_rows}
    selected_set = set(selected)
    candidate_rows: list[dict[str, Any]] = []
    current_legacy_ids: set[int] = set()
    for current in current_rows:
        legacy_id = current[ENTITY_KEY]
        current_legacy_ids.add(legacy_id)
        # Only selected entities are replaced by their historical content; an
        # unselected entity that merely exists in the source version is kept.
        chosen = source_by_legacy_id[legacy_id] if legacy_id in selected_set else None
        candidate_rows.append(dict(chosen) if chosen is not None else dict(current))
    # Selected entities that no longer exist in the current formal table are
    # brought back from history.
    candidate_rows.extend(
        dict(source_by_legacy_id[legacy_id])
        for legacy_id in selected
        if legacy_id not in current_legacy_ids
    )
    candidate_rows.sort(key=lambda row: (row["id"], row[ENTITY_KEY]))
    return candidate_rows


def _diff_against_current(
    conn: sqlite3.Connection,
    candidate_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare the full candidate table with the current formal table.

    Entities are matched by the stable legacy_id so that a row whose primary
    key changed between versions is reported as a field change rather than as a
    remove plus an add.
    """
    if not _formal_table_exists(conn):
        return {
            "formal_exists": False,
            "added": [
                {"id": row["id"], ENTITY_KEY: row[ENTITY_KEY]} for row in candidate_rows
            ],
            "removed": [],
            "changed": [],
            "truncated": False,
            "added_count": len(candidate_rows),
            "removed_count": 0,
            "changed_count": 0,
        }

    current_rows = _formal_rows(conn)
    current_by_entity = {row[ENTITY_KEY]: row for row in current_rows}
    candidate_by_entity = {row[ENTITY_KEY]: row for row in candidate_rows}

    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []

    for row in candidate_rows:
        current = current_by_entity.get(row[ENTITY_KEY])
        if current is None:
            added.append({"id": row["id"], ENTITY_KEY: row[ENTITY_KEY]})
        elif _row_signature(current) != _row_signature(row):
            changed_columns = [
                column for column in ("id", "code", "label") if current[column] != row[column]
            ]
            changed.append(
                {
                    "id": row["id"],
                    ENTITY_KEY: row[ENTITY_KEY],
                    "columns": changed_columns,
                    "current": {column: current[column] for column in ("id", "code", "label")},
                    "candidate": {column: row[column] for column in ("id", "code", "label")},
                }
            )
    for row in current_rows:
        if row[ENTITY_KEY] not in candidate_by_entity:
            removed.append({"id": row["id"], ENTITY_KEY: row[ENTITY_KEY]})

    detail_count = len(added) + len(removed) + len(changed)
    return {
        "formal_exists": True,
        "added": added[:DIFF_LIMIT],
        "removed": removed[:DIFF_LIMIT],
        "changed": changed[:DIFF_LIMIT],
        "truncated": detail_count > DIFF_LIMIT,
        "added_count": len(added),
        "removed_count": len(removed),
        "changed_count": len(changed),
    }


def _create_candidate_table(conn, candidate_name: str) -> None:
    # The candidate carries the final formal-table constraints, so building it
    # during the read-only dry run re-verifies NOT NULL / PRIMARY KEY / UNIQUE
    # against the FULL next table, including retained (unselected) current rows.
    conn.execute(
        f"""
        CREATE TABLE {candidate_name} (
            id INTEGER PRIMARY KEY,
            code TEXT NOT NULL UNIQUE,
            label TEXT NOT NULL,
            legacy_id INTEGER NOT NULL UNIQUE
        )
        """
    )


def _insert_candidate(conn, candidate_name: str, candidate_rows: list[dict[str, Any]]) -> None:
    conn.executemany(
        f"""
        INSERT INTO {candidate_name}(id, code, label, legacy_id)
        VALUES (?, ?, ?, ?)
        """,
        [
            (row["id"], row["code"], row["label"], row["legacy_id"])
            for row in candidate_rows
        ],
    )


def create_restore_preview(
    payload: RestorePreviewPayload,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Build a candidate formal table from a history version without switching."""
    preview_id = uuid.uuid4().hex
    candidate_name = _candidate_name(preview_id)

    with db_session(db_path) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            version = _require_locked_version(conn, payload.version_id)
            records_generation = get_records_generation(conn)
            source_rows = _history_rows(conn, version["version_id"])
            selected = _validate_selection(
                payload.selected_legacy_ids,
                {row[ENTITY_KEY]: row for row in source_rows},
            )
            stored_count = conn.execute(
                f"SELECT COUNT(*) FROM {HISTORY_ROWS_TABLE} WHERE version_id=?",
                (version["version_id"],),
            ).fetchone()[0]
            if stored_count != version["row_count"] or len(source_rows) != version["row_count"]:
                raise ServiceError(
                    500,
                    "history_integrity",
                    "retained history row count does not match its sealed metadata",
                )

            candidate_rows = _build_candidate_rows(
                _formal_rows(conn), source_rows, selected
            )
            diff = _diff_against_current(conn, candidate_rows)

            _create_candidate_table(conn, candidate_name)
            # Constraint failures here cover the whole candidate table: a
            # historical value that clashes with a retained (unselected) row on
            # id / code / legacy_id fails the dry run with 422 rather than being
            # promised as a clean restore.
            try:
                _insert_candidate(conn, candidate_name, candidate_rows)
            except sqlite3.IntegrityError as exc:
                raise ServiceError(
                    422,
                    "constraint_failed",
                    f"restored rows would violate the formal table constraints: {exc}",
                ) from exc
            candidate_count = conn.execute(f"SELECT COUNT(*) FROM {candidate_name}").fetchone()[0]
            if candidate_count != len(candidate_rows):
                raise RuntimeError("candidate row count does not match computed restore set")

            selection_json = json.dumps(selected) if selected else None
            diff_summary = {
                key: diff[key]
                for key in ("formal_exists", "added_count", "removed_count", "changed_count", "truncated")
                if key in diff
            }
            conn.execute(
                f"""
                INSERT INTO {RESTORE_PREVIEW_META_TABLE}
                    (preview_id, source_version_id, records_generation,
                     row_count, diff_summary_json, selected_legacy_ids_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    preview_id,
                    version["version_id"],
                    records_generation,
                    candidate_count,
                    json.dumps(diff_summary),
                    selection_json,
                ),
            )
            conn.commit()

            source_description = {
                "version_id": version["version_id"],
                "kind": version["kind"],
                "migration_id": version["migration_id"],
                "restore_id": version["restore_id"],
                "source_version_id": version["source_version_id"],
                "generation": version["generation"],
                "source_revision": version["source_revision"],
                "row_count": version["row_count"],
            }
            return {
                "ok": True,
                "preview_id": preview_id,
                "records_generation": records_generation,
                "row_count": candidate_count,
                "selected_legacy_ids": list(selected) if selected else None,
                "source": source_description,
                "diff": diff,
                "candidate_rows": candidate_rows,
            }
        except Exception:
            conn.rollback()
            # A failed dry run leaves neither a candidate nor metadata behind.
            conn.execute(f"DROP TABLE IF EXISTS {candidate_name}")
            conn.execute(
                f"DELETE FROM {RESTORE_PREVIEW_META_TABLE} WHERE preview_id=?",
                (preview_id,),
            )
            raise


def _discard_restore_preview(conn: sqlite3.Connection, preview_id: str) -> None:
    conn.execute(f"DROP TABLE IF EXISTS {_candidate_name(preview_id)}")
    conn.execute(
        f"DELETE FROM {RESTORE_PREVIEW_META_TABLE} WHERE preview_id=?",
        (preview_id,),
    )


def _expected_candidate_rows(
    conn: sqlite3.Connection,
    preview: sqlite3.Row,
    source_version: sqlite3.Row,
) -> tuple[list[dict[str, Any]], list[int] | None]:
    """Recompute the full candidate from sealed history and the current table."""
    source_rows = _history_rows(conn, source_version["version_id"])
    if len(source_rows) != source_version["row_count"]:
        raise ServiceError(
            500,
            "history_integrity",
            "retained history row count does not match its sealed metadata",
        )
    selection_json = preview["selected_legacy_ids_json"]
    selected: list[int] | None = json.loads(selection_json) if selection_json else None
    if selected is None:
        candidate_rows = [dict(row) for row in source_rows]
    else:
        candidate_rows = _build_candidate_rows(_formal_rows(conn), source_rows, selected)
    return candidate_rows, selected


def commit_restore(
    payload: RestoreCommitPayload,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Seal the current formal table and switch in the previewed candidate."""
    with db_session(db_path) as conn:
        candidate_name = _candidate_name(payload.preview_id)
        try:
            conn.execute("BEGIN IMMEDIATE")
            current_generation = get_records_generation(conn)
            if (
                payload.records_generation is not None
                and payload.records_generation != current_generation
            ):
                raise ServiceError(
                    409,
                    "stale_preview",
                    "formal table generation changed after preview; preview the restore again",
                )

            preview = conn.execute(
                f"""
                SELECT preview_id, source_version_id, records_generation, row_count,
                       selected_legacy_ids_json
                  FROM {RESTORE_PREVIEW_META_TABLE}
                 WHERE preview_id=?
                """,
                (payload.preview_id,),
            ).fetchone()
            if preview is None:
                raise ServiceError(
                    404,
                    "preview_not_found",
                    "restore preview was not accepted or has expired",
                )
            if preview["records_generation"] != current_generation:
                raise ServiceError(
                    409,
                    "stale_preview",
                    "formal table generation changed after this restore preview; preview again",
                )

            candidate_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (candidate_name,),
            ).fetchone()
            if not candidate_exists:
                raise ServiceError(
                    409,
                    "candidate_missing",
                    "restore candidate table is missing; preview the restore again",
                )

            candidate_count = conn.execute(f"SELECT COUNT(*) FROM {candidate_name}").fetchone()[0]
            if candidate_count != preview["row_count"]:
                raise ServiceError(
                    409,
                    "candidate_changed",
                    "restore candidate changed; preview the restore again",
                )

            source_version = _require_locked_version(conn, preview["source_version_id"])

            if not _formal_table_exists(conn):
                raise ServiceError(
                    409,
                    "formal_table_missing",
                    "no formal table exists to seal; run a migration first",
                )

            # Recompute the full candidate from the sealed history and the
            # current formal table, then compare row by row against the parked
            # candidate so a tampered or stale candidate can never be switched
            # in.  The candidate table itself carries the final constraints.
            expected_rows, selected = _expected_candidate_rows(conn, preview, source_version)
            candidate_rows = [
                dict(row)
                for row in conn.execute(
                    f"SELECT id, code, label, legacy_id FROM {candidate_name} ORDER BY id, legacy_id"
                )
            ]
            if len(candidate_rows) != len(expected_rows) or any(
                _row_signature(candidate_rows[i]) != _row_signature(expected_rows[i])
                for i in range(len(expected_rows))
            ):
                raise ServiceError(
                    409,
                    "candidate_changed",
                    "restore candidate no longer matches the sealed history and current table; preview again",
                )

            current_revision = get_revision(conn)
            new_generation = current_generation + 1
            old_count = conn.execute(f"SELECT COUNT(*) FROM {CURRENT_TABLE}").fetchone()[0]

            cur = conn.execute(
                f"""
                INSERT INTO {RESTORES_TABLE}
                    (preview_id, source_version_id, base_generation, new_generation,
                     archived_version_id, row_count, selected_legacy_ids_json)
                VALUES (?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    payload.preview_id,
                    source_version["version_id"],
                    current_generation,
                    new_generation,
                    candidate_count,
                    json.dumps(selected) if selected else None,
                ),
            )
            restore_id = int(cur.lastrowid)

            # Seal the current formal table as a brand-new read-only version.
            conn.execute(
                f"""
                INSERT INTO {HISTORY_TABLE}
                    (kind, migration_id, restore_id, replaced_table,
                     source_revision, source_version_id, generation, row_count, locked)
                VALUES ('restore', NULL, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    restore_id,
                    CURRENT_TABLE,
                    current_revision,
                    source_version["version_id"],
                    current_generation,
                    old_count,
                ),
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
                raise RuntimeError("injected/copy mismatch while sealing current formal table")
            conn.execute(
                f"UPDATE {HISTORY_TABLE} SET locked=1 WHERE version_id=?",
                (archived_version_id,),
            )
            conn.execute(
                f"UPDATE {RESTORES_TABLE} SET archived_version_id=? WHERE restore_id=?",
                (archived_version_id, restore_id),
            )

            if _fault_enabled(conn, "restore_switch"):
                raise RuntimeError("injected failure before restore switch")

            # DDL is transactional in SQLite: the switch and the generation
            # advance either both happen or both roll back to the old table.
            conn.execute(f"DROP TABLE {CURRENT_TABLE}")
            conn.execute(f"ALTER TABLE {candidate_name} RENAME TO {CURRENT_TABLE}")
            advanced_generation = advance_records_generation(conn)
            if advanced_generation != new_generation:
                raise RuntimeError("formal table generation mismatch after restore switch")
            expire_all_previews(conn)
            conn.commit()

            return {
                "ok": True,
                "restore_id": restore_id,
                "preview_id": payload.preview_id,
                "base_generation": current_generation,
                "new_generation": new_generation,
                "source_version_id": source_version["version_id"],
                "row_count": candidate_count,
                "selected_legacy_ids": list(selected) if selected else None,
                "archived_version": {
                    "version_id": archived_version_id,
                    "row_count": old_count,
                },
            }
        except ServiceError:
            conn.rollback()
            # A rejected restore can never be retried with the same preview.
            _discard_restore_preview(conn, payload.preview_id)
            raise
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            _discard_restore_preview(conn, payload.preview_id)
            raise ServiceError(
                422, "constraint_failed", f"restore constraint failed: {exc}"
            ) from exc
        except Exception:
            conn.rollback()
            _discard_restore_preview(conn, payload.preview_id)
            raise
