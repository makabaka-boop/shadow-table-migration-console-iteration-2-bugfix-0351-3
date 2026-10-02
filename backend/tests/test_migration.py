import sqlite3
import threading
import time

import pytest

from tests.conftest import VALID_MAPPINGS, commit_valid, preview_valid


def test_preview_reports_transform_constraint_and_uniqueness_failures(client):
    inserted = client.post(
        '/api/legacy',
        json={
            'legacy_id': 4,
            'code': 'D-004',
            'raw_name': '9223372036854775808',
            'note': 'id is id map to raw_name later',
        },
    )
    assert inserted.status_code == 200
    inserted = client.post(
        '/api/legacy',
        json={'legacy_id': 5, 'code': 'DUP', 'raw_name': 'D-004', 'note': None},
    )
    assert inserted.status_code == 200

    mappings = {
        'id': {'type': 'decimal_int', 'source_column': 'raw_name'},
        'code': {'type': 'copy', 'source_column': 'code'},
        'label': {'type': 'trim', 'source_column': 'raw_name'},
    }
    response = client.post('/api/migrations/preview', json=mappings)
    assert response.status_code == 200
    body = response.json()
    assert body['ok'] is False
    assert body['preview_id'] is None

    failures = {(failure['legacy_id'], error['field'], error['code'])
                for failure in body['failures'] for error in failure['errors']}
    assert (1, 'id', 'not_decimal_integer') in failures
    assert (2, 'id', 'not_decimal_integer') in failures
    assert (3, 'id', 'not_decimal_integer') in failures
    assert (4, 'id', 'integer_out_of_range') in failures
    assert all(
        error['mapping'].startswith(('decimal_int:', 'trim:', 'copy:'))
        for failure in body['failures'] for error in failure['errors']
    )

    # A rejected preview must leave no accepted preview or shadow behind and
    # must not create/replace the formal table.
    state = client.get('/api/state').json()
    assert state['previews'] == []
    assert state['shadow_tables'] == []
    assert state['records'] == []


def test_constant_and_trim_mapping_successfully_creates_first_table(client):
    mappings = {
        'id': {'type': 'constant', 'value': 42},
        'code': {'type': 'constant', 'value': 'ONE'},
        'label': {'type': 'trim', 'source_column': 'raw_name'},
    }
    # Every source row would receive id=42/code=ONE; uniqueness must fail.
    failed = client.post('/api/migrations/preview', json=mappings).json()
    assert failed['ok'] is False
    codes = {(error['field'], error['code']) for error in failed['failures'][1]['errors']}
    assert ('id', 'duplicate') in codes
    assert ('code', 'duplicate') in codes

    preview = preview_valid(client)
    result = commit_valid(client, preview)
    assert result['row_count'] == 3
    assert result['old_version']['row_count'] == 0

    records = client.get('/api/records').json()['rows']
    assert records == [
        {'id': 1, 'code': 'A-001', 'label': 'Alpha', 'legacy_id': 1},
        {'id': 2, 'code': 'B-002', 'label': 'Beta', 'legacy_id': 2},
        {'id': 3, 'code': 'C-003', 'label': 'Gamma', 'legacy_id': 3},
    ]


def test_copy_interruption_leaves_formal_table_and_shadow_cleanup(client):
    assert client.post('/api/test/faults', json={'name': 'preview_copy', 'enabled': True}).status_code == 200
    response = client.post('/api/migrations/preview', json=VALID_MAPPINGS)
    assert response.status_code == 500
    assert 'injected failure' in response.json()['error']['message']

    state = client.get('/api/state').json()
    assert state['records'] == []
    assert state['shadow_tables'] == []
    assert state['previews'] == []


def test_switch_failure_rolls_back_without_half_new_table(client):
    first = commit_valid(client, preview_valid(client))
    assert first['migration_id'] == 1

    client.post('/api/legacy', json={'legacy_id': 4, 'code': 'D-004', 'raw_name': 'Delta', 'note': None})
    second_preview = preview_valid(client)
    assert second_preview['source_revision'] == 4
    assert client.post('/api/test/faults', json={'name': 'commit_switch', 'enabled': True}).status_code == 200

    response = client.post(
        '/api/migrations/commit',
        json={'preview_id': second_preview['preview_id'], 'source_revision': second_preview['source_revision']},
    )
    assert response.status_code == 500
    assert 'injected failure' in response.json()['error']['message']

    state = client.get('/api/state').json()
    # Exactly the old formal table remains; no pending table and no new row.
    assert [row['id'] for row in state['records']] == [1, 2, 3]
    assert state['shadow_tables'] == []
    assert state['previews'] == []
    assert state['history'] == [
        {
            'version_id': 1,
            'kind': 'migration',
            'migration_id': 1,
            'restore_id': None,
            'replaced_table': 'records',
            'source_revision': 3,
            'source_version_id': None,
            'generation': 0,
            'row_count': 0,
            'locked': 1,
            'created_at': state['history'][0]['created_at'],
        }
    ]


def test_stale_preview_is_rejected_after_interleaved_legacy_write(client):
    commit_valid(client, preview_valid(client))
    preview = preview_valid(client)
    revision_before = preview['source_revision']

    interleaved = client.post(
        '/api/legacy',
        json={'legacy_id': 4, 'code': 'D-004', 'raw_name': 'Delta', 'note': 'client B write'},
    )
    assert interleaved.status_code == 200
    assert interleaved.json()['revision'] == 4

    stale = client.post(
        '/api/migrations/commit',
        json={'preview_id': preview['preview_id'], 'source_revision': revision_before},
    )
    assert stale.status_code == 409
    assert stale.json()['error']['code'] == 'stale_preview'

    # The old formal table remains unchanged until a fresh preview is committed.
    state = client.get('/api/state').json()
    assert len(state['records']) == 3
    fresh = preview_valid(client)
    committed = commit_valid(client, fresh)
    assert committed['source_revision'] == 4
    assert committed['row_count'] == 4


def test_two_clients_interleave_with_separate_databases_connections(client):
    # TestClient serializes requests, but the two logical clients exercise the
    # exact optimistic-concurrency protocol a browser pair would use.
    client_a = client
    client_b = client
    first = commit_valid(client_a, preview_valid(client_a))
    assert first['committed_revision'] == 3

    preview_a = client_a.post('/api/migrations/preview', json=VALID_MAPPINGS).json()
    client_b.post('/api/legacy', json={'legacy_id': 4, 'code': 'B-004', 'raw_name': 'Bravo 4', 'note': None})
    client_b.post('/api/legacy', json={'legacy_id': 5, 'code': 'B-005', 'raw_name': 'Bravo 5', 'note': None})

    stale_a = client_a.post(
        '/api/migrations/commit',
        json={'preview_id': preview_a['preview_id'], 'source_revision': preview_a['source_revision']},
    )
    assert stale_a.status_code == 409

    preview_b = client_b.post('/api/migrations/preview', json=VALID_MAPPINGS).json()
    committed_b = commit_valid(client_b, preview_b)
    assert committed_b['source_revision'] == 5
    assert committed_b['row_count'] == 5
    assert committed_b['old_version']['row_count'] == 3

    history = client.get('/api/history').json()['versions']
    assert [(item['migration_id'], item['source_revision'], item['committed_revision'], item['row_count'])
            for item in history] == [(1, 3, 3, 0), (2, 5, 5, 3)]


def test_concurrent_writer_rejects_commit_from_stale_preview(client):
    commit_valid(client, preview_valid(client))
    stale_preview = preview_valid(client)

    import os
    from app.db import default_db_path

    db_path = os.environ.get('MIGRATION_DB') or default_db_path()
    writer_ready = threading.Event()
    writer_finished = threading.Event()

    def concurrent_writer():
        direct = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        direct.execute('PRAGMA busy_timeout=30000')
        direct.execute('BEGIN IMMEDIATE')
        writer_ready.set()
        time.sleep(0.2)
        direct.execute(
            'INSERT INTO legacy_records(legacy_id, code, raw_name, note) VALUES (?, ?, ?, ?)',
            (4, 'D-004', 'Delta', 'concurrent'),
        )
        direct.commit()
        direct.close()
        writer_finished.set()

    worker = threading.Thread(target=concurrent_writer)
    worker.start()
    writer_ready.wait(1)
    # Client B owns the SQLite write lock for a moment.  The commit waits, then
    # re-enters its optimistic revision check through SQLite's serialized lock
    # ordering; it must not switch based on the old snapshot.
    time.sleep(0.05)
    response = client.post(
        '/api/migrations/commit',
        json={
            'preview_id': stale_preview['preview_id'],
            'source_revision': stale_preview['source_revision'],
        },
    )
    worker.join(2)
    writer_finished.wait(1)

    assert response.status_code == 409
    assert response.json()['error']['code'] == 'stale_preview'
    state = client.get('/api/state').json()
    assert state['revision'] == 4
    assert [row['id'] for row in state['records']] == [1, 2, 3]
    assert state['shadow_tables'] == []
    assert state['previews'] == []

    fresh = preview_valid(client)
    committed = commit_valid(client, fresh)
    assert committed['source_revision'] == 4
    assert committed['row_count'] == 4


def test_retained_history_rows_are_readable_and_readonly(client):
    commit_valid(client, preview_valid(client))
    client.post('/api/legacy', json={'legacy_id': 4, 'code': 'D-004', 'raw_name': 'Delta', 'note': None})
    second = commit_valid(client, preview_valid(client))
    version_id = second['old_version']['version_id']
    assert version_id == 2

    rows = client.get(f'/api/history/{version_id}/rows').json()['rows']
    assert rows == [
        {'id': 1, 'code': 'A-001', 'label': 'Alpha', 'legacy_id': 1},
        {'id': 2, 'code': 'B-002', 'label': 'Beta', 'legacy_id': 2},
        {'id': 3, 'code': 'C-003', 'label': 'Gamma', 'legacy_id': 3},
    ]

    import os
    from app.db import default_db_path

    db_path = os.environ.get('MIGRATION_DB') or default_db_path()
    with sqlite3.connect(db_path) as direct:
        with pytest.raises(sqlite3.IntegrityError):
            direct.execute(
                'INSERT INTO record_version_rows(version_id, id, code, label, legacy_id) VALUES (?, ?, ?, ?, ?)',
                (version_id, 99, 'X', 'Hacked insert', 99),
            )
        with pytest.raises(sqlite3.IntegrityError):
            direct.execute('UPDATE record_versions SET row_count=99 WHERE version_id=?', (version_id,))
        with pytest.raises(sqlite3.IntegrityError):
            direct.execute('DELETE FROM record_versions WHERE version_id=?', (version_id,))
        with pytest.raises(sqlite3.IntegrityError):
            direct.execute('UPDATE record_version_rows SET label=? WHERE version_id=?', ('Hacked', version_id))
        with pytest.raises(sqlite3.IntegrityError):
            direct.execute('DELETE FROM record_version_rows WHERE version_id=?', (version_id,))
