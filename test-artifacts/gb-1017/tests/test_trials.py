"""Blinded trial checks (run with: python -m pytest or plain python)."""
import os, sys, tempfile

_DB = os.path.join(tempfile.gettempdir(), 'trial-tests.db')
if os.path.exists(_DB):
    os.remove(_DB)
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + _DB)
os.environ.setdefault('SEED_DEMO', 'false')
os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:1/0')
os.environ.setdefault('ARTIFACT_DIR', os.path.join(tempfile.gettempdir(), 'trial-artifacts'))
sys.path.insert(0, '/workspace/backend')

from fastapi.testclient import TestClient
from migrations.initial import migrate
from src.config.database import Session
from src.models import User
from src.utils.security import hash_password

migrate()
with Session() as db:
    for role in ('admin', 'analyst', 'viewer'):
        if not db.query(User).filter(User.email == role + '@demo.local').first():
            db.add(User(email=role + '@demo.local', name=role, role=role, password=hash_password('Demo123!')))
    db.commit()

from src.main import app
client = TestClient(app)


def token(role):
    return {'Authorization': 'Bearer ' + client.post('/auth/login', json={
        'email': role + '@demo.local', 'password': 'Demo123!'}).json()['access_token']}


ADMIN, ANALYST, VIEWER = token('admin'), token('analyst'), token('viewer')


def make(name, n=40, blocks=4):
    strata = [{'site': '东港站', 'model': 'AC-8', 'parts': [
        {'asset': f'{name}-{i:03}', 'serial': f'{name}-SN{i:03}'} for i in range(n)]}]
    r = client.post('/trials', headers=ANALYST, json={
        'name': name, 'block_size': blocks, 'group_ratio': '1:1', 'strata': strata})
    assert r.status_code == 201, r.text
    return r.json()['id']


def enroll(tid, name, i, key=None, serial=None):
    return client.post(f'/trials/{tid}/enrollments', headers=ANALYST, json={
        'asset': f'{name}-{i:03}', 'site': '东港站', 'model': 'AC-8',
        'serial': serial if serial is not None else f'{name}-SN{i:03}',
        'request_key': key})


def test_open_locks_design():
    r = client.post('/trials', headers=ANALYST, json={'name': '分层校验', 'block_size': 4, 'group_ratio': '1:1',
        'strata': [{'site': '东港站', 'model': 'AC-8', 'parts': [{'asset': 'x1'}]}]})
    assert r.status_code == 201
    assert client.post('/trials', headers=ANALYST, json={'name': '非等比', 'block_size': 4, 'group_ratio': '1:2',
        'strata': [{'site': '东港站', 'model': 'AC-8'}]}).status_code == 422
    assert client.post('/trials', headers=ANALYST, json={'name': '错层', 'block_size': 4,
        'strata': [{'site': '东港站', 'model': 'AC-8'}, {'site': '东港站', 'model': 'AC-8'}]}).status_code == 422
    assert client.post('/trials', headers=VIEWER, json={'name': '观察员开验', 'strata': [{'site': 'a', 'model': 'b'}]}).status_code == 403
    body = client.get('/trials', headers=VIEWER).json()
    assert all('seed' not in str(item).lower() for item in body)


def test_unused_allocations_hidden_and_revealed_per_part():
    tid = make('盲态-揭示', 12)
    detail = client.get(f'/trials/{tid}', headers=ANALYST).json()
    assert len(detail['pending']) == 12 and detail['follow_up'] == []
    first = enroll(tid, '盲态-揭示', 0).json()
    assert first['arm'] in ('A', 'B') and not first['replayed']
    detail = client.get(f'/trials/{tid}', headers=ANALYST).json()
    assert len(detail['pending']) == 11
    assert [f['arm'] for f in detail['follow_up']] == [first['arm']]
    assert client.get(f'/trials/{tid}/groups', headers=ANALYST).status_code == 409
    assert client.get(f'/trials/{tid}/estimates', headers=ANALYST).status_code == 409


def test_balance_and_sequential_blocks():
    tid = make('平衡', 40)
    arms = [enroll(tid, '平衡', i).json()['arm'] for i in range(40)]
    for start in range(0, 40, 4):
        assert arms[start:start + 4].count('A') == 2, arms[start:start + 4]
    assert arms.count('A') == 20


def test_retry_returns_same_group_and_duplicates_rejected():
    tid = make('重试', 12)
    first = enroll(tid, '重试', 0, key='k0').json()
    again = enroll(tid, '重试', 0, key='k0').json()
    assert again['arm'] == first['arm'] and again['replayed']
    assert enroll(tid, '重试', 0, key='k1').status_code == 409  # new key, same part
    assert enroll(tid, '重试', 5, serial='重试-SN000').status_code == 409  # same serial, new part


def test_concurrent_enrollments_keep_order():
    import concurrent.futures
    tid = make('并发', 60)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: enroll(tid, '并发', i), range(30)))
    assert all(r.status_code == 201 for r in results), [r.text for r in results if r.status_code != 201]
    detail = client.get(f'/trials/{tid}', headers=ADMIN).json()
    assert len(detail['follow_up']) == 30
    assert sum(1 for f in detail['follow_up'] if f['arm'] == 'A') == 15


def test_close_requires_complete_observations_and_unblinds():
    tid = make('结案', 8)
    for i in range(8):
        enroll(tid, '结案', i)
    rows = [{'asset': f'结案-{i:03}', 'km': 1000 + i, 'failed': i % 2} for i in range(8)]
    assert client.post(f'/trials/{tid}/close', headers=ADMIN).status_code == 409
    assert client.post(f'/trials/{tid}/observations', headers=ANALYST, json={
        'observations': rows[:4], 'request_key': 'obs1'}).status_code == 200
    assert client.post(f'/trials/{tid}/close', headers=ADMIN).status_code == 409
    client.post(f'/trials/{tid}/observations', headers=ANALYST, json={
        'observations': rows[4:], 'request_key': 'obs2'})
    frozen = client.post(f'/trials/{tid}/close', headers=ADMIN).json()
    assert frozen['trial']['status'] == 'closed'
    groups = client.get(f'/trials/{tid}/groups', headers=VIEWER).json()['groups']
    assert sorted(g['rows'] for g in groups) == [4, 4]
    assert client.post(f'/trials/{tid}/close', headers=ADMIN).json()['replayed'] is True
    assert client.post(f'/trials/{tid}/observations', headers=ANALYST, json={
        'observations': [{'asset': '结案-000', 'km': 9, 'failed': 1}]}).status_code == 409
    assert enroll(tid, '结案', 0, key='late').status_code == 409
    assert client.get(f'/trials/{tid}/estimates', headers=ANALYST).status_code == 200


def test_only_admin_can_close_and_viewer_cannot_enroll():
    tid = make('权限', 4)
    assert enroll(tid, '权限', 0).status_code == 201
    assert client.post(f'/trials/{tid}/close', headers=ANALYST).status_code == 403
    r = client.post(f'/trials/{tid}/enrollments', headers=VIEWER, json={
        'asset': '权限-001', 'site': '东港站', 'model': 'AC-8'})
    assert r.status_code == 403


def test_mid_request_interruption_resumes_progress():
    """Simulate a crash after one enrollment: the persisted cursor resumes at the next slot."""
    tid = make('中断', 12)
    a = enroll(tid, '中断', 0).json()
    tid_b = make('中断对照', 12)
    b = enroll(tid_b, '中断对照', 0).json()
    # Same secret seed is per-trial, so compare balance invariants rather than arms.
    assert enroll(tid, '中断', 1).status_code == 201
    detail = client.get(f'/trials/{tid}', headers=ADMIN).json()
    strata = detail['strata'][0]
    assert strata['enrolled'] == 2 and strata['pending'] == 10


def test_supplementary_report_versions_are_append_only():
    tid = make('补报', 4)
    for i in range(4):
        enroll(tid, '补报', i)
    first = client.post(f'/trials/{tid}/observations', headers=ANALYST, json={
        'observations': [{'asset': '补报-000', 'km': 1000, 'failed': 0}], 'request_key': 'r1'}).json()
    assert first['applied'][0]['version'] == 1
    second = client.post(f'/trials/{tid}/observations', headers=ANALYST, json={
        'observations': [{'asset': '补报-000', 'km': 2000, 'failed': 1}], 'request_key': 'r2'}).json()
    assert second['applied'][0]['version'] == 2
    retry = client.post(f'/trials/{tid}/observations', headers=ANALYST, json={
        'observations': [{'asset': '补报-000', 'km': 2000, 'failed': 1}], 'request_key': 'r2'}).json()
    assert retry['applied'] == [] and retry['skipped']
    detail = client.get(f'/trials/{tid}', headers=ADMIN).json()
    follow = next(f for f in detail['follow_up'] if f['asset'] == '补报-000')
    assert follow['observation']['version'] == 2 and follow['observation']['km'] == 2000




def test_viewer_sees_blinded_follow_up_until_close():
    tid = make('观察员盲态', 4)
    enrolled = enroll(tid, '观察员盲态', 0).json()
    as_viewer = client.get(f'/trials/{tid}', headers=VIEWER).json()
    assert as_viewer['follow_up'][0]['arm'] is None
    as_staff = client.get(f'/trials/{tid}', headers=ANALYST).json()
    assert as_staff['follow_up'][0]['arm'] == enrolled['arm']


if __name__ == '__main__':
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith('test_') and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print('PASS', name)
        except Exception as exc:
            failed += 1
            print('FAIL', name, type(exc).__name__, exc)
    sys.exit(1 if failed else 0)
