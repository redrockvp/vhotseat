import inspect

import pytest
from test_platform import _spy

from vhotseat import autostart


def supports_names():
    assert all('name' in inspect.signature(f).parameters for f in
               (autostart.install, autostart.status, autostart.remove)), 'named autostart option is missing'


def test_default_windows_artifacts_are_unchanged(tmp_path):
    supports_names()
    exe = r'C:\Apps\vhotseat.exe'
    calls, run = _spy(fail=('/Create',))
    old = tmp_path / 'old' / 'vhotseat-autostart.vbs'
    new = tmp_path / 'new' / 'vhotseat-autostart.vbs'
    assert autostart.install('web', exe=exe, at='schtasks', run=run, vbs=old) == 0
    old_reg = [x for x in calls if x[:2] == ['reg', 'add']][-1]
    assert autostart.install('web', exe=exe, at='schtasks', run=run, vbs=new, name='vhotseat') == 0
    new_reg = [x for x in calls if x[:2] == ['reg', 'add']][-1]
    assert old.read_bytes() == new.read_bytes()
    assert old_reg[:old_reg.index('/d')] == new_reg[:new_reg.index('/d')]
    assert old_reg[old_reg.index('/d') + 1].replace(str(old), str(new)) == new_reg[new_reg.index('/d') + 1]
    assert new_reg[new_reg.index('/v') + 1] == 'vhotseat'


def test_default_systemd_unit_is_unchanged(tmp_path):
    supports_names()
    calls, run = _spy()
    old, new = tmp_path / 'old', tmp_path / 'new'
    assert autostart.install('web', exe='/bin/vhotseat', at='systemd', run=run, units=old) == 0
    assert autostart.install('web', exe='/bin/vhotseat', at='systemd', run=run, units=new, name='vhotseat') == 0
    assert (old / 'vhotseat.service').read_bytes() == (new / 'vhotseat.service').read_bytes()


def test_named_windows_entries_are_independent(tmp_path, monkeypatch):
    supports_names()
    monkeypatch.setattr(autostart, 'state_dir', lambda: tmp_path)
    calls, run = _spy(fail=('/Create',))
    kw = dict(exe=r'C:\vhotseat.exe', at='schtasks', run=run)
    assert autostart.install('web', **kw) == 0
    assert autostart.install('proxy strata --upstream http://127.0.0.1:8081 --port 8080', name='strata-gate', **kw) == 0
    default, named = tmp_path / 'vhotseat-autostart.vbs', tmp_path / 'vhotseat-strata-gate-autostart.vbs'
    assert default.exists() and named.exists()
    assert any(c[:2] == ['reg', 'add'] and c[c.index('/v') + 1] == 'vhotseat-strata-gate' for c in calls)
    calls.clear()
    autostart.status(at='schtasks', run=run, name='strata-gate')
    assert all('vhotseat' not in c or 'vhotseat-strata-gate' in c for c in calls)
    calls.clear()
    autostart.remove(at='schtasks', run=run, name='strata-gate')
    assert default.exists() and not named.exists()
    assert all('vhotseat' not in c or 'vhotseat-strata-gate' in c for c in calls)


def test_named_systemd_entries_are_independent(tmp_path):
    supports_names()
    calls, run = _spy()
    kw = dict(at='systemd', run=run, units=tmp_path)
    assert autostart.install('web', exe='/bin/vhotseat', **kw) == 0
    assert autostart.install('proxy strata --upstream http://127.0.0.1:8081 --port 8080', exe='/bin/vhotseat', name='strata-gate', **kw) == 0
    default, named = tmp_path / 'vhotseat.service', tmp_path / 'vhotseat-strata-gate.service'
    assert default.exists() and named.exists()
    calls.clear()
    assert autostart.status(name='strata-gate', **kw) == 0
    assert ['systemctl', '--user', 'is-active', named.name] in calls
    calls.clear()
    assert autostart.remove(name='strata-gate', **kw) == 0
    assert default.exists() and not named.exists()
    assert ['systemctl', '--user', 'disable', '--now', named.name] in calls
    assert not any(default.name in c for c in calls)


@pytest.mark.parametrize('name', ['', '../evil', 'Upper', 'a' * 33])
def test_invalid_name_is_refused_by_cli(rig, name, capsys, monkeypatch):
    monkeypatch.setattr(autostart, 'install', lambda *a, **kw: 1)
    try:
        rc = rig.cli('autostart', 'install', '--name', name)
    except SystemExit as exc:
        rc = exc.code
    err = capsys.readouterr().err
    assert rc == 2
    assert 'name' in err.lower() and 'unrecognized arguments' not in err.lower(), 'CLI must validate --name'


def test_cli_passes_named_proxy_command(rig, monkeypatch):
    seen = []
    monkeypatch.setattr(autostart, 'install', lambda *a, **kw: seen.append((a, kw)) or 0)
    try:
        rc = rig.cli('autostart', 'install', '--name', 'strata-gate', 'proxy', 'strata', '--upstream', 'http://127.0.0.1:8081', '--port', '8080')
    except SystemExit as exc:
        rc = exc.code
    assert rc == 0, 'autostart must accept a named arbitrary command'
    assert seen and 'proxy strata --upstream http://127.0.0.1:8081 --port 8080' in str(seen)
    assert 'strata-gate' in str(seen)
