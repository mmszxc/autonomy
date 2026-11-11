"""Cross-profile seed sharing is off by default, including stale peer files."""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'plugins'))
from autonomy import runner


def make_runner(tmp_path):
    instance = object.__new__(runner.Runner)
    instance.hermes_home = str(tmp_path / 'xiaozhi')
    instance.peer_share_enabled = False
    instance.db = None
    return instance


def test_default_export_creates_no_shared_directory_or_files(tmp_path, monkeypatch):
    instance = make_runner(tmp_path)
    monkeypatch.setattr(runner.Runner, '_SHARED_KNOWLEDGE_DIR', str(tmp_path / 'shared'))
    assert instance._export_shared_seeds('tick-1') == {'exported': 0, 'reason': 'disabled'}
    assert not (tmp_path / 'shared').exists()


def test_default_peer_load_ignores_stale_other_profile_file(tmp_path, monkeypatch):
    instance = make_runner(tmp_path)
    shared = tmp_path / 'shared'
    shared.mkdir()
    (shared / 'top_seeds_xiaoai.jsonl').write_text(
        json.dumps({'summary': 'STALE_PEER_SECRET', 'importance': .99}) + '\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(runner.Runner, '_SHARED_KNOWLEDGE_DIR', str(shared))
    assert instance._load_peer_shared_seeds() == ''
    assert 'TEAM_RECALL' not in instance._load_peer_shared_seeds()


def test_prefetch_render_never_injects_stale_peer_content(tmp_path, monkeypatch):
    instance = make_runner(tmp_path)
    seed_dir = Path(instance.hermes_home) / 'seed_data' / 'seeds'
    seed_dir.mkdir(parents=True)
    db = sqlite3.connect(seed_dir / 'seeds.db')
    db.execute('create table seeds (id text primary key, summary text)')
    db.execute('insert into seeds values (?, ?)', ('seed-1', 'my own memory'))
    db.commit()
    db.close()
    shared = tmp_path / 'shared'
    shared.mkdir()
    (shared / 'top_seeds_xiaoai.jsonl').write_text(
        json.dumps({'summary': 'STALE_PEER_SECRET', 'importance': .99}) + '\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(runner.Runner, '_SHARED_KNOWLEDGE_DIR', str(shared))
    rendered = instance._render_three_layers('tick-3', ['seed-1'])
    assert 'my own memory' in rendered
    assert 'STALE_PEER_SECRET' not in rendered
    assert 'TEAM_RECALL' not in rendered


def test_explicit_disabled_setting_still_skips_both_methods(tmp_path, monkeypatch):
    instance = make_runner(tmp_path)
    instance.peer_share_enabled = False
    monkeypatch.setattr(runner.Runner, '_SHARED_KNOWLEDGE_DIR', str(tmp_path / 'shared'))
    assert instance._export_shared_seeds('tick-2')['reason'] == 'disabled'
    assert instance._load_peer_shared_seeds() == ''


def test_explicit_enabled_setting_loads_peer_content(tmp_path, monkeypatch):
    instance = make_runner(tmp_path)
    instance.peer_share_enabled = True
    shared = tmp_path / 'shared'
    shared.mkdir()
    (shared / 'top_seeds_xiaoai.jsonl').write_text(
        json.dumps({'summary': 'peer memory', 'importance': .99}) + '\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(runner.Runner, '_SHARED_KNOWLEDGE_DIR', str(shared))

    assert instance._load_peer_shared_seeds() == '[[TEAM_RECALL]] 对方记忆: peer memory'


def test_explicit_enabled_setting_exports_shared_content(tmp_path, monkeypatch):
    instance = make_runner(tmp_path)
    instance.peer_share_enabled = True
    seed_db = tmp_path / 'seeds.db'
    db = sqlite3.connect(seed_db)
    db.execute('create table seeds (id text primary key, summary text, importance real)')
    db.execute('insert into seeds values (?, ?, ?)', ('seed-1', 'share me', .9))
    db.commit()
    db.close()
    shared = tmp_path / 'shared'
    monkeypatch.setattr(runner.Runner, '_SHARED_KNOWLEDGE_DIR', str(shared))
    monkeypatch.setattr(instance, '_seed_db_path', lambda: seed_db)

    result = instance._export_shared_seeds('tick-3')

    assert result['exported'] == 1
    exported = shared / 'top_seeds_xiaozhi.jsonl'
    assert json.loads(exported.read_text(encoding='utf-8')) == {
        'id': 'seed-1', 'summary': 'share me', 'importance': .9,
    }
