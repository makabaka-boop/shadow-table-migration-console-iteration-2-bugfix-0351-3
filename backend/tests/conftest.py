import os
from pathlib import Path

import pytest

# Must be set before importing the FastAPI application so startup and API test
# controls use the temporary database.
TEST_DB = Path('/tmp/sqlite-shadow-migration-pytest.db')
os.environ['MIGRATION_DB'] = str(TEST_DB)
os.environ['ALLOW_FAULT_INJECTION'] = '1'

from fastapi.testclient import TestClient

from app.main import app
from app.db import init_db


VALID_MAPPINGS = {
    'id': {'type': 'copy', 'source_column': 'legacy_id'},
    'code': {'type': 'copy', 'source_column': 'code'},
    'label': {'type': 'trim', 'source_column': 'raw_name'},
}


@pytest.fixture()
def client():
    for suffix in ('', '-wal', '-shm'):
        Path(str(TEST_DB) + suffix).unlink(missing_ok=True)
    init_db(str(TEST_DB), reset=True)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def preview_valid(client):
    response = client.post('/api/migrations/preview', json=VALID_MAPPINGS)
    assert response.status_code == 200
    body = response.json()
    assert body['ok'] is True
    return body


def commit_valid(client, preview):
    response = client.post(
        '/api/migrations/commit',
        json={'preview_id': preview['preview_id'], 'source_revision': preview['source_revision']},
    )
    assert response.status_code == 200, response.text
    return response.json()
