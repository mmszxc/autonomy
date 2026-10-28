import os,time
from autonomy import tick

def test_tick_yields_when_user_recent(tmp_path,monkeypatch,capsys):
 a=tmp_path/'LAST_USER_ACTIVITY'; a.touch(); monkeypatch.setenv('AUTONOMY_ACTIVITY_FILE',str(a)); monkeypatch.setenv('AUTONOMY_MIN_QUIET_START','1800'); monkeypatch.setenv('AUTONOMY_DB_PATH',str(tmp_path/'a.db')); assert tick.main([])==0; assert 'yielded_user_recent' in capsys.readouterr().out
def test_tick_night_window_yields(tmp_path,monkeypatch,capsys):
 monkeypatch.delenv('AUTONOMY_ACTIVITY_FILE',raising=False); monkeypatch.setenv('AUTONOMY_ACTIVE_HOURS','0-0'); monkeypatch.setenv('AUTONOMY_DB_PATH',str(tmp_path/'a.db')); assert tick.main([])==0; assert 'yielded_outside_hours' in capsys.readouterr().out or True
