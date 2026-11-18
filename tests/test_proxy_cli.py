import importlib
import socket
import subprocess
import threading

from test_unsloth_proxy import call


def invoke(rig, *args):
    try:
        return rig.cli(*args)
    except SystemExit as exc:
        return exc.code


def require_proxy(rig, capsys):
    rc = invoke(rig, 'proxy', '--help')
    capsys.readouterr()
    assert rc == 0, 'proxy CLI subcommand is missing'


def test_taken_port_reports_pid(rig, monkeypatch, capsys):
    require_proxy(rig, capsys)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        def netstat(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, f'  TCP    127.0.0.1:{port}    0.0.0.0:0    LISTENING    4242\n', '')
        monkeypatch.setattr(subprocess, 'run', netstat)
        rc = invoke(rig, 'proxy', 'strata', '--upstream', rig.strata.url, '--port', str(port))
        err = capsys.readouterr().err.strip()
        assert rc == 2 and '4242' in err and str(port) in err and len(err.splitlines()) == 1


def test_proxy_rejects_self_upstream(rig, capsys):
    require_proxy(rig, capsys)
    rc = invoke(rig, 'proxy', 'strata', '--upstream', 'http://127.0.0.1:38171', '--port', '38171')
    err = capsys.readouterr().err.strip()
    assert rc == 2 and err and len(err.splitlines()) == 1


def test_proxy_rejects_unknown_workload(rig, capsys):
    require_proxy(rig, capsys)
    rc = invoke(rig, 'proxy', 'unknown-workload', '--upstream', rig.strata.url, '--port', '0')
    err = capsys.readouterr().err.strip()
    assert rc == 2 and 'unknown-workload' in err and len(err.splitlines()) == 1


def test_proxy_serves_and_guards_unload(rig, capsys, monkeypatch):
    require_proxy(rig, capsys)
    proxy = importlib.import_module('vhotseat.proxy')
    started = threading.Event()
    instances = []
    original = proxy.Proxy.start
    def start(self):
        result = original(self)
        instances.append(self)
        started.set()
        return result
    monkeypatch.setattr(proxy.Proxy, 'start', start)
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    thread = threading.Thread(target=lambda: invoke(rig, 'proxy', 'strata', '--upstream', rig.strata.url, '--port', str(port), '--hold', '0'), daemon=True)
    thread.start()
    try:
        assert started.wait(5), 'proxy did not start'
        base = f'http://127.0.0.1:{port}'
        assert call(base, 'GET', '/health')[0] == 200
        rig.strata.loaded = True
        entered, release = threading.Event(), threading.Event()
        route = rig.strata.route
        def paused(h, method, url, body):
            if url[0] == '/health':
                entered.set()
                release.wait(5)
            return route(h, method, url, body)
        monkeypatch.setattr(rig.strata, 'route', paused)
        request = threading.Thread(target=lambda: call(base, 'GET', '/health'), daemon=True)
        request.start()
        try:
            assert entered.wait(5)
            assert call(base, 'POST', '/unload?wait=0')[0] == 409
            assert rig.strata.loaded and 'POST /unload' not in rig.strata.calls
        finally:
            release.set()
            request.join(5)
        assert call(base, 'POST', '/unload?wait=0')[0] == 200
        assert not rig.strata.loaded
    finally:
        for instance in instances:
            instance.stop()
