import os
import sqlite3

import pytest

from app.db import default_db_path


MAPPING_PREVIEW_PATH = '/api/migrations/preview'
MAPPING_COMMIT_PATH = '/api/migrations/commit'
RESTORE_PREVIEW_PATH = '/api/restores/preview'
RESTORE_COMMIT_PATH = '/api/restores/commit'


def _db_path():
    return os.environ.get('MIGRATION_DB') or default_db_path()


def _setup_gen2_with_changed_first_row(client):
    """Gen 1: Alpha/Beta/Gamma. Gen 2: Alpha II/Beta/Gamma/Delta.

    Sealed version 2 holds the generation-1 rows (legacy ids 1..3); the gen-2
    formal table additionally holds legacy id 4 and a changed label for id 1.
    """
    from tests.conftest import commit_valid, preview_valid

    commit_valid(client, preview_valid(client))  # gen 1
    with sqlite3.connect(_db_path()) as direct:
        direct.execute('PRAGMA busy_timeout=30000')
        direct.execute("UPDATE legacy_records SET raw_name='  Alpha II  ' WHERE legacy_id=1")
        direct.execute(
            "INSERT INTO legacy_records(legacy_id, code, raw_name, note) VALUES (4, 'D-004', 'Delta', NULL)"
        )
    commit_valid(client, preview_valid(client))  # gen 2


def restore_preview(client, version_id, selected=None):
    payload = {'version_id': version_id}
    if selected is not None:
        payload['selected_legacy_ids'] = selected
    response = client.post(RESTORE_PREVIEW_PATH, json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['ok'] is True
    return body


def restore_commit(client, preview, generation=None):
    return client.post(
        RESTORE_COMMIT_PATH,
        json={
            'preview_id': preview['preview_id'],
            'records_generation': (
                generation if generation is not None else preview['records_generation']
            ),
        },
    )


def _ids(state):
    return [row['id'] for row in state['records']]


def test_partial_restore_preview_keeps_unselected_rows_and_scopes_diff_to_selection(client):
    _setup_gen2_with_changed_first_row(client)

    preview = restore_preview(client, 2, selected=[1])
    assert preview['selected_legacy_ids'] == [1]
    # The candidate is the FULL next formal table: selected entity 1 reverts to
    # the sealed version, every other current row survives untouched.
    assert preview['row_count'] == 4
    assert [(row['id'], row['legacy_id'], row['label']) for row in preview['candidate_rows']] == [
        (1, 1, 'Alpha'),
        (2, 2, 'Beta'),
        (3, 3, 'Gamma'),
        (4, 4, 'Delta'),
    ]
    diff = preview['diff']
    assert diff['added_count'] == 0
    assert diff['removed_count'] == 0
    assert diff['changed_count'] == 1
    change = diff['changed'][0]
    assert change['legacy_id'] == 1
    assert change['columns'] == ['label']

    # The dry run did not modify the formal table.
    state = client.get('/api/state').json()
    assert state['records_generation'] == 2
    assert [row['label'] for row in state['records']] == ['Alpha II', 'Beta', 'Gamma', 'Delta']

    response = restore_commit(client, preview)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['selected_legacy_ids'] == [1]
    assert result['new_generation'] == 3
    assert result['row_count'] == 4
    # The whole pre-restore formal table is sealed, so the "partial" operation
    # stays explainable afterwards.
    assert result['archived_version'] == {'version_id': 3, 'row_count': 4}

    state = client.get('/api/state').json()
    assert state['records_generation'] == 3
    assert state['records'] == [
        {'id': 1, 'code': 'A-001', 'label': 'Alpha', 'legacy_id': 1},
        {'id': 2, 'code': 'B-002', 'label': 'Beta', 'legacy_id': 2},
        {'id': 3, 'code': 'C-003', 'label': 'Gamma', 'legacy_id': 3},
        {'id': 4, 'code': 'D-004', 'label': 'Delta', 'legacy_id': 4},
    ]
    archived = client.get('/api/history/3/rows').json()['rows']
    assert [(row['id'], row['legacy_id'], row['label']) for row in archived] == [
        (1, 1, 'Alpha II'),
        (2, 2, 'Beta'),
        (3, 3, 'Gamma'),
        (4, 4, 'Delta'),
    ]
    with sqlite3.connect(_db_path()) as direct:
        ledger = direct.execute(
            'SELECT source_version_id, base_generation, new_generation, row_count, '
            'selected_legacy_ids_json FROM restores'
        ).fetchone()
        assert ledger == (2, 2, 3, 4, '[1]')


def test_partial_restore_selects_by_stable_legacy_id_when_row_id_differs(client):
    from tests.conftest import commit_valid, preview_valid

    commit_valid(client, preview_valid(client))              # gen 1, rows 1..3
    inserted = client.post(
        '/api/legacy',
        json={'legacy_id': 4, 'code': 'D-004', 'raw_name': 'Delta', 'note': None},
    )
    assert inserted.status_code == 200
    commit_valid(client, preview_valid(client))              # gen 2, rows 1..4

    # Give the entity with legacy_id 4 a different primary key in the formal
    # table, then seal it via a full restore so a later history version holds
    # (id=10, legacy_id=4).
    with sqlite3.connect(_db_path()) as direct:
        direct.execute('PRAGMA busy_timeout=30000')
        direct.execute('UPDATE records SET id=10 WHERE legacy_id=4')
    assert restore_commit(client, restore_preview(client, 2)).status_code == 200  # gen 3 rows 1..3

    # Selecting by the row primary key must not pick the stable entity up: the
    # selection space is legacy_id, and legacy_id 10 is absent from history.
    misselected = client.post(
        RESTORE_PREVIEW_PATH, json={'version_id': 3, 'selected_legacy_ids': [10]}
    )
    assert misselected.status_code == 422
    assert misselected.json()['error']['code'] == 'invalid_selection'
    state = client.get('/api/state').json()
    assert state['restore_previews'] == []
    assert state['shadow_tables'] == []

    # The existing target (present as id=10) must not be judged missing.
    preview = restore_preview(client, 3, selected=[4])
    assert preview['row_count'] == 4
    assert preview['diff']['added'] == [{'id': 10, 'legacy_id': 4}]
    assert preview['diff']['removed'] == []
    assert preview['diff']['changed'] == []

    assert restore_commit(client, preview).status_code == 200
    state = client.get('/api/state').json()
    assert state['records_generation'] == 4
    assert [(row['id'], row['legacy_id']) for row in state['records']] == [
        (1, 1), (2, 2), (3, 3), (10, 4),
    ]


def test_partial_restore_preview_rejects_unique_conflict_with_retained_row(client):
    from tests.conftest import commit_valid, preview_valid

    commit_valid(client, preview_valid(client))              # gen 1, rows 1..3 (version 2 content)
    inserted = client.post(
        '/api/legacy',
        json={'legacy_id': 4, 'code': 'D-004', 'raw_name': 'Delta', 'note': None},
    )
    assert inserted.status_code == 200
    commit_valid(client, preview_valid(client))              # gen 2, rows 1..4

    # Current entity 1 moved off code A-001, and retained entity 4 took it.
    with sqlite3.connect(_db_path()) as direct:
        direct.execute('PRAGMA busy_timeout=30000')
        direct.execute("UPDATE records SET code='A-005' WHERE legacy_id=1")
        direct.execute("UPDATE records SET code='A-001' WHERE legacy_id=4")

    # Restoring historical entity 1 (code A-001) while keeping current entity 4
    # would violate the full formal-table UNIQUE constraint: the dry run must
    # fail instead of advertising a clean preview.
    rejected = client.post(
        RESTORE_PREVIEW_PATH, json={'version_id': 2, 'selected_legacy_ids': [1]}
    )
    assert rejected.status_code == 422
    assert rejected.json()['error']['code'] == 'constraint_failed'

    state = client.get('/api/state').json()
    assert state['records_generation'] == 2
    assert state['restore_previews'] == []
    assert state['shadow_tables'] == []
    assert [row['code'] for row in state['records']] == ['A-005', 'B-002', 'C-003', 'A-001']


@pytest.mark.parametrize('selection', [[], [1, 1], [99]])
def test_partial_restore_preview_validates_selection(client, selection):
    from tests.conftest import commit_valid, preview_valid

    commit_valid(client, preview_valid(client))
    response = client.post(
        RESTORE_PREVIEW_PATH, json={'version_id': 1, 'selected_legacy_ids': selection}
    )
    assert response.status_code == 422
    assert response.json()['error']['code'] == 'invalid_selection'
    state = client.get('/api/state').json()
    assert state['restore_previews'] == []
    assert state['shadow_tables'] == []


def test_full_restore_preview_response_marks_selection_as_null(client):
    from tests.conftest import commit_valid, preview_valid

    commit_valid(client, preview_valid(client))
    preview = restore_preview(client, 1)
    assert preview['selected_legacy_ids'] is None
    committed = restore_commit(client, preview)
    assert committed.status_code == 200
    assert committed.json()['selected_legacy_ids'] is None


def test_build_candidate_rows_semantics_without_a_database():
    from app.restore import _build_candidate_rows

    current = [
        {'id': 10, 'code': 'A-005', 'label': 'Alpha II', 'legacy_id': 1},
        # Unselected, and present in the history with DIFFERENT values.
        {'id': 20, 'code': 'B-099', 'label': 'Beta Now', 'legacy_id': 2},
        {'id': 4, 'code': 'D-004', 'label': 'Delta', 'legacy_id': 4},
    ]
    history = [
        {'id': 1, 'code': 'A-001', 'label': 'Alpha', 'legacy_id': 1},
        {'id': 2, 'code': 'B-002', 'label': 'Beta', 'legacy_id': 2},
        {'id': 3, 'code': 'C-003', 'label': 'Gamma', 'legacy_id': 3},
    ]
    # Only entities 1 (revert, including its old primary key) and 3 (deleted,
    # brought back) are selected. Entity 2 exists in the source version but is
    # unselected, so its current values survive; unselected entity 4 survives.
    candidate = _build_candidate_rows(current, history, [1, 3])
    assert [(row['id'], row['legacy_id'], row['code'], row['label']) for row in candidate] == [
        (1, 1, 'A-001', 'Alpha'),
        (3, 3, 'C-003', 'Gamma'),
        (4, 4, 'D-004', 'Delta'),
        (20, 2, 'B-099', 'Beta Now'),
    ]
    # Full restore reproduces the history version exactly.
    assert _build_candidate_rows(current, history, []) == history
