import os
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app.db import default_db_path, init_db
from app.main import app
from app.restore import RestoreCommitPayload, commit_restore
from tests.conftest import VALID_MAPPINGS, commit_valid, preview_valid


MAPPING_PREVIEW_PATH = '/api/migrations/preview'
MAPPING_COMMIT_PATH = '/api/migrations/commit'
RESTORE_PREVIEW_PATH = '/api/restores/preview'
RESTORE_COMMIT_PATH = '/api/restores/commit'


def _db_path():
    return os.environ.get('MIGRATION_DB') or default_db_path()


def _migrate_twice(client):
    """Generation 1 holds three seeded rows; generation 2 holds a fourth row."""
    commit_valid(client, preview_valid(client))
    inserted = client.post(
        '/api/legacy',
        json={'legacy_id': 4, 'code': 'D-004', 'raw_name': 'Delta', 'note': None},
    )
    assert inserted.status_code == 200
    commit_valid(client, preview_valid(client))


def restore_preview(client, version_id):
    response = client.post(RESTORE_PREVIEW_PATH, json={'version_id': version_id})
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


def _history_summary(client):
    return [
        (
            row['version_id'],
            row['kind'],
            row['generation'],
            row['source_version_id'],
            row['row_count'],
            row['locked'],
        )
        for row in client.get('/api/state').json()['history']
    ]


def test_restore_preview_is_read_through_and_shows_rows_constraints_and_diff(client):
    _migrate_twice(client)

    # Version 2 sealed the generation-1 formal table (rows 1..3).
    preview = restore_preview(client, 2)
    assert preview['row_count'] == 3
    assert preview['records_generation'] == 2
    assert preview['source'] == {
        'version_id': 2,
        'kind': 'migration',
        'migration_id': 2,
        'restore_id': None,
        'source_version_id': None,
        'generation': 1,
        'source_revision': 4,
        'row_count': 3,
    }
    assert [row['id'] for row in preview['candidate_rows']] == [1, 2, 3]
    assert preview['diff']['formal_exists'] is True
    assert preview['diff']['added'] == []
    assert preview['diff']['removed'] == [{'id': 4}]
    assert preview['diff']['changed'] == []
    assert preview['diff']['removed_count'] == 1

    state = client.get('/api/state').json()
    # Dry run only: formal table, generation and sealed history are untouched;
    # the candidate is parked under its own name.
    assert state['records_generation'] == 2
    assert _ids(state) == [1, 2, 3, 4]
    assert len(state['history']) == 2
    assert state['restore_previews'] == [
        {
            'preview_id': preview['preview_id'],
            'source_version_id': 2,
            'records_generation': 2,
            'row_count': 3,
            'diff_summary_json': state['restore_previews'][0]['diff_summary_json'],
            'created_at': state['restore_previews'][0]['created_at'],
        }
    ]
    assert state['shadow_tables'] == [
        f'records_restore_candidate_{preview["preview_id"]}'
    ]

    # The candidate carries the final constraints directly.
    with sqlite3.connect(_db_path()) as direct:
        with pytest.raises(sqlite3.IntegrityError):
            direct.execute(
                f'INSERT INTO records_restore_candidate_{preview["preview_id"]}'
                '(id, code, label, legacy_id) VALUES (9, ?, ?, ?)',
                ('A-001', 'duplicate code', 99),
            )


def test_restore_commit_seals_current_table_switches_candidate_and_advances_generation(client):
    _migrate_twice(client)
    preview = restore_preview(client, 2)

    response = restore_commit(client, preview)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['base_generation'] == 2
    assert result['new_generation'] == 3
    assert result['source_version_id'] == 2
    assert result['row_count'] == 3
    assert result['archived_version'] == {'version_id': 3, 'row_count': 4}

    state = client.get('/api/state').json()
    assert state['records_generation'] == 3
    assert _ids(state) == [1, 2, 3]
    assert state['records'] == [
        {'id': 1, 'code': 'A-001', 'label': 'Alpha', 'legacy_id': 1},
        {'id': 2, 'code': 'B-002', 'label': 'Beta', 'legacy_id': 2},
        {'id': 3, 'code': 'C-003', 'label': 'Gamma', 'legacy_id': 3},
    ]
    # Version 3 archives the pre-restore formal table for later investigation;
    # version ids form their own lineage independent of migration ids.
    assert _history_summary(client) == [
        (1, 'migration', 0, None, 0, 1),
        (2, 'migration', 1, None, 3, 1),
        (3, 'restore', 2, 2, 4, 1),
    ]
    assert state['restore_previews'] == []
    assert state['previews'] == []
    assert state['shadow_tables'] == []

    rows = client.get('/api/history/3/rows').json()['rows']
    assert [row['id'] for row in rows] == [1, 2, 3, 4]

    with sqlite3.connect(_db_path()) as direct:
        direct.execute('PRAGMA foreign_keys=ON')
        restore_row = direct.execute(
            'SELECT restore_id, source_version_id, base_generation, new_generation, '
            'archived_version_id, row_count FROM restores'
        ).fetchone()
        assert restore_row == (1, 2, 2, 3, 3, 3)


def test_migration_preview_interleaved_with_restore_cannot_overwrite_restored_table(client):
    _migrate_twice(client)

    # Client A prepares a field migration against generation 2.
    migration_preview = client.post(MAPPING_PREVIEW_PATH, json=VALID_MAPPINGS).json()
    assert migration_preview['records_generation'] == 2

    # Client B restores generation-1 content while A is still holding its preview.
    restore = restore_preview(client, 2)
    assert restore_commit(client, restore).status_code == 200

    stale = client.post(
        MAPPING_COMMIT_PATH,
        json={
            'preview_id': migration_preview['preview_id'],
            'source_revision': migration_preview['source_revision'],
            'records_generation': migration_preview['records_generation'],
        },
    )
    assert stale.status_code in (404, 409)
    assert stale.json()['error']['code'] in ('preview_not_found', 'stale_preview')

    state = client.get('/api/state').json()
    assert state['records_generation'] == 3
    assert _ids(state) == [1, 2, 3]
    assert state['shadow_tables'] == []

    # A fresh migration preview binds to generation 3 and can commit afterwards.
    # Its archive becomes version 4: version ids are independent of migration
    # ids (the migration itself is #3).
    fresh = client.post(MAPPING_PREVIEW_PATH, json=VALID_MAPPINGS).json()
    assert fresh['records_generation'] == 3
    committed = commit_valid(client, fresh)
    assert committed['new_generation'] == 4
    assert committed['migration_id'] == 3
    assert committed['old_version']['version_id'] == 4
    assert _ids(client.get('/api/state').json()) == [1, 2, 3, 4]


def test_restore_preview_interleaved_with_migration_is_rejected(client):
    _migrate_twice(client)

    stale_restore = restore_preview(client, 2)
    migration = client.post(MAPPING_PREVIEW_PATH, json=VALID_MAPPINGS).json()
    assert commit_valid(client, migration)['new_generation'] == 3

    rejected = restore_commit(client, stale_restore, generation=2)
    assert rejected.status_code in (404, 409)
    assert rejected.json()['error']['code'] in ('preview_not_found', 'stale_preview')

    state = client.get('/api/state').json()
    # The migration's four-row table stays in place; no archive was created.
    assert state['records_generation'] == 3
    assert _ids(state) == [1, 2, 3, 4]
    assert [row[0] for row in _history_summary(client)] == [1, 2, 3]


def test_two_competing_restores_one_wins_and_loser_is_not_replayable(client):
    _migrate_twice(client)
    empty_version = restore_preview(client, 1)
    three_row_version = restore_preview(client, 2)
    assert empty_version['records_generation'] == 2
    assert three_row_version['records_generation'] == 2

    outcome = {}
    barrier = threading.Barrier(2)

    def worker(name, preview_id):
        barrier.wait()
        try:
            outcome[name] = commit_restore(
                RestoreCommitPayload(preview_id=preview_id, records_generation=2),
                _db_path(),
            )
        except Exception as exc:  # ServiceError on the losing side
            outcome[name] = exc

    first = threading.Thread(target=worker, args=('empty', empty_version['preview_id']))
    second = threading.Thread(target=worker, args=('three', three_row_version['preview_id']))
    first.start()
    second.start()
    first.join(5)
    second.join(5)

    winners = [name for name, value in outcome.items() if not isinstance(value, Exception)]
    losers = [name for name, value in outcome.items() if isinstance(value, Exception)]
    assert winners == ['empty'] or winners == ['three']
    assert len(losers) == 1
    loser_error = outcome[losers[0]]
    assert loser_error.status_code == 409
    assert loser_error.code == 'stale_preview'

    winner = outcome[winners[0]]
    assert winner['new_generation'] == 3
    expected_rows = 0 if winners[0] == 'empty' else 3
    assert winner['row_count'] == expected_rows

    state = client.get('/api/state').json()
    assert state['records_generation'] == 3
    assert len(_ids(state)) == expected_rows
    # Exactly one archive version was committed; the loser rolled back entirely.
    assert [row[0] for row in _history_summary(client)] == [1, 2, 3]
    assert state['shadow_tables'] == []
    assert state['restore_previews'] == []
    assert state['previews'] == []

    # The loser preview can never be submitted again.
    loser_preview = empty_version if losers[0] == 'empty' else three_row_version
    replay = restore_commit(client, loser_preview, generation=2)
    assert replay.status_code in (404, 409)
    assert replay.json()['error']['code'] in ('preview_not_found', 'stale_preview')
    assert _ids(client.get('/api/state').json()) == list(range(1, expected_rows + 1))


def test_history_and_generation_survive_process_restart(client):
    _migrate_twice(client)
    restore = restore_preview(client, 2)
    assert restore_commit(client, restore).status_code == 200
    before = client.get('/api/state').json()
    assert before['records_generation'] == 3
    assert _ids(before) == [1, 2, 3]
    version_rows = {
        version_id: client.get(f'/api/history/{version_id}/rows').json()['rows']
        for version_id in (1, 2, 3)
    }

    # A brand-new application instance reopens the same database file.
    with TestClient(app, raise_server_exceptions=False) as restarted:
        after = restarted.get('/api/state').json()
        assert after['records_generation'] == 3
        assert _ids(after) == [1, 2, 3]
        assert _history_summary(restarted) == [
            (1, 'migration', 0, None, 0, 1),
            (2, 'migration', 1, None, 3, 1),
            (3, 'restore', 2, 2, 4, 1),
        ]
        history = restarted.get('/api/history').json()
        assert history['records_generation'] == 3
        for version_id, rows in version_rows.items():
            assert restarted.get(f'/api/history/{version_id}/rows').json()['rows'] == rows

        # The lineage remains usable after restart: restore the sealed
        # generation-2 archive and reach generation 4.
        again = restore_preview(restarted, 3)
        assert again['records_generation'] == 3
        committed = restore_commit(restarted, again)
        assert committed.status_code == 200
        assert committed.json()['new_generation'] == 4
        assert _ids(restarted.get('/api/state').json()) == [1, 2, 3, 4]


def test_restore_switch_failure_rolls_back_the_whole_transaction(client):
    _migrate_twice(client)
    preview = restore_preview(client, 2)

    enabled = client.post(
        '/api/test/faults',
        json={'name': 'restore_switch', 'enabled': True},
    )
    assert enabled.status_code == 200
    failed = restore_commit(client, preview)
    assert failed.status_code == 500
    assert 'injected failure' in failed.json()['error']['message']

    state = client.get('/api/state').json()
    assert state['records_generation'] == 2
    assert _ids(state) == [1, 2, 3, 4]
    # No half formal table, no orphan archive version or restore ledger row.
    assert [row[0] for row in _history_summary(client)] == [1, 2]
    assert state['shadow_tables'] == []
    assert state['restore_previews'] == []
    with sqlite3.connect(_db_path()) as direct:
        assert direct.execute('SELECT COUNT(*) FROM restores').fetchone()[0] == 0
        assert direct.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='records'"
        ).fetchone() is not None

    client.post('/api/test/faults', json={'name': 'restore_switch', 'enabled': False})
    fresh = restore_preview(client, 2)
    assert restore_commit(client, fresh).status_code == 200
    state = client.get('/api/state').json()
    assert state['records_generation'] == 3
    assert _ids(state) == [1, 2, 3]


def test_successful_restore_cannot_be_submitted_twice(client):
    _migrate_twice(client)
    preview = restore_preview(client, 2)
    first = restore_commit(client, preview)
    assert first.status_code == 200

    replay = restore_commit(client, preview, generation=2)
    assert replay.status_code in (404, 409)
    assert replay.json()['error']['code'] in ('preview_not_found', 'stale_preview')
    assert client.get('/api/state').json()['records_generation'] == 3


def test_restore_preview_unknown_version_is_rejected_without_side_effects(client):
    _migrate_twice(client)
    response = client.post(RESTORE_PREVIEW_PATH, json={'version_id': 999})
    assert response.status_code == 404
    assert response.json()['error']['code'] == 'version_not_found'

    state = client.get('/api/state').json()
    assert state['records_generation'] == 2
    assert state['restore_previews'] == []
    assert state['shadow_tables'] == []


def test_restore_created_versions_are_read_only(client):
    _migrate_twice(client)
    committed = restore_commit(client, restore_preview(client, 2))
    archived_version = committed.json()['archived_version']['version_id']

    with sqlite3.connect(_db_path()) as direct:
        direct.execute('PRAGMA foreign_keys=ON')
        with pytest.raises(sqlite3.IntegrityError):
            direct.execute(
                'INSERT INTO record_version_rows(version_id, id, code, label, legacy_id) '
                'VALUES (?, ?, ?, ?, ?)',
                (archived_version, 99, 'X', 'Hacked insert', 99),
            )
        with pytest.raises(sqlite3.IntegrityError):
            direct.execute(
                'UPDATE record_version_rows SET label=? WHERE version_id=?',
                ('Hacked', archived_version),
            )
        with pytest.raises(sqlite3.IntegrityError):
            direct.execute(
                'DELETE FROM record_version_rows WHERE version_id=?',
                (archived_version,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            direct.execute(
                'UPDATE record_versions SET row_count=99 WHERE version_id=?',
                (archived_version,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            direct.execute(
                'DELETE FROM record_versions WHERE version_id=?',
                (archived_version,),
            )


def test_restore_chains_from_restore_created_versions(client):
    _migrate_twice(client)                       # gen 2, rows 1..4
    assert restore_commit(client, restore_preview(client, 2)).status_code == 200  # gen 3, rows 1..3
    chained = restore_preview(client, 3)        # version 3 is itself a restore archive
    assert chained['source']['kind'] == 'restore'
    assert chained['source']['source_version_id'] == 2
    result = restore_commit(client, chained)
    assert result.status_code == 200
    body = result.json()
    assert body['new_generation'] == 4
    assert body['source_version_id'] == 3
    state = client.get('/api/state').json()
    assert _ids(state) == [1, 2, 3, 4]
    assert _history_summary(client)[-1] == (4, 'restore', 3, 3, 3, 1)


def test_restore_diff_reports_changed_columns_and_restore_then_reproduces_them(client):
    commit_valid(client, preview_valid(client))  # gen 1: label Alpha/Beta/Gamma

    with sqlite3.connect(_db_path()) as direct:
        direct.execute('PRAGMA busy_timeout=30000')
        direct.execute("UPDATE legacy_records SET raw_name='  Alpha II  ' WHERE legacy_id=1")

    # Second migration reaches gen 2 with a changed label for id 1; its archive
    # version 2 holds the generation-1 labels.
    commit_valid(client, preview_valid(client))
    assert client.get('/api/records').json()['rows'][0]['label'] == 'Alpha II'

    preview = restore_preview(client, 2)
    assert preview['diff']['changed_count'] == 1
    change = preview['diff']['changed'][0]
    assert change['id'] == 1
    assert change['columns'] == ['label']
    assert change['current'] == {'code': 'A-001', 'label': 'Alpha II', 'legacy_id': 1}
    assert change['candidate'] == {'code': 'A-001', 'label': 'Alpha', 'legacy_id': 1}
    assert preview['diff']['added_count'] == 0
    assert preview['diff']['removed_count'] == 0

    assert restore_commit(client, preview).status_code == 200
    assert client.get('/api/records').json()['rows'][0]['label'] == 'Alpha'
    assert client.get('/api/state').json()['records_generation'] == 3


def test_commit_without_generation_field_still_works_and_server_generation_is_authority(client):
    # Older client payload: only preview_id + source_revision.
    first = preview_valid(client)
    assert 'records_generation' in first
    response = client.post(
        MAPPING_COMMIT_PATH,
        json={'preview_id': first['preview_id'], 'source_revision': first['source_revision']},
    )
    assert response.status_code == 200
    assert response.json()['new_generation'] == 1

    # A mismatched client-echoed generation cannot force a commit through.
    second = preview_valid(client)
    bad_echo = client.post(
        MAPPING_COMMIT_PATH,
        json={
            'preview_id': second['preview_id'],
            'source_revision': second['source_revision'],
            'records_generation': second['records_generation'] + 1,
        },
    )
    assert bad_echo.status_code == 409
    assert bad_echo.json()['error']['code'] == 'stale_preview'
    # The rejected preview is discarded and cannot be replayed with the echo
    # corrected.
    replay = client.post(
        MAPPING_COMMIT_PATH,
        json={
            'preview_id': second['preview_id'],
            'source_revision': second['source_revision'],
            'records_generation': second['records_generation'],
        },
    )
    assert replay.status_code in (404, 409)
