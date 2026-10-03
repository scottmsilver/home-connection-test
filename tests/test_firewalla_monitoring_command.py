import importlib.util
import io
import json
import threading
from http.client import HTTPException, IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "connection_monitoring/firewalla_gate.py"
FIBER = "11111111-1111-4111-8111-111111111111"
BACKUP = "22222222-2222-4222-8222-222222222222"


@pytest.fixture
def command_module():
    spec = importlib.util.spec_from_file_location("firewalla_monitoring_command", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def allowlist_file(tmp_path):
    path = tmp_path / "allowlist.json"
    path.write_text(json.dumps({"wans": [FIBER, BACKUP], "quality_target": "198.51.100.1"}))
    return path


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


def response_payload(uuid=FIBER, **result_overrides):
    result = {"success": True, "manual": True, "uuid": uuid}
    result.update(result_overrides)
    return json.dumps({"code": 200, "message": "success", "data": {"result": result}}).encode()


def test_manual_speedtest_posts_exact_fireapi_request(command_module, allowlist_file):
    calls = []

    def opener(request, *, timeout):
        calls.append((request, timeout))
        return FakeResponse(response_payload())

    command_module.dispatch(
        f"run-speedtest {FIBER}",
        opener=opener,
        request_id_factory=lambda: "request-123",
        allowlist_path=allowlist_file,
    )

    request, timeout = calls[0]
    parsed = urlsplit(request.full_url)
    assert request.get_method() == "POST"
    assert (parsed.scheme, parsed.hostname, parsed.port, parsed.path) == (
        "http",
        "127.0.0.1",
        8834,
        "/v1/encipher/simple",
    )
    assert parse_qs(parsed.query) == {
        "command": ["cmd"],
        "item": ["runInternetSpeedtest"],
        "id": ["request-123"],
    }
    assert json.loads(request.data) == {"wanUUID": FIBER, "vendor": "ookla"}
    assert request.headers["Content-type"] == "application/json"
    assert timeout == 120


def test_manual_speedtest_uses_real_urlopen_transport(command_module, allowlist_file):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            calls.append((self.command, self.path, body))
            payload = response_payload()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        command_module.FIREAPI_URL = f"http://127.0.0.1:{server.server_port}/v1/encipher/simple"
        command_module.dispatch(
            f"run-speedtest {FIBER}",
            request_id_factory=lambda: "real-request",
            allowlist_path=allowlist_file,
        )
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    method, path, body = calls[0]
    assert method == "POST"
    assert path == ("/v1/encipher/simple?command=cmd&item=runInternetSpeedtest&id=real-request")
    assert json.loads(body) == {"wanUUID": FIBER, "vendor": "ookla"}


@pytest.mark.parametrize("wan_uuid", [FIBER, BACKUP])
def test_both_allowed_wans_accept_success_envelope(command_module, wan_uuid, allowlist_file):
    command_module.dispatch(
        f"run-speedtest {wan_uuid}",
        opener=lambda _request, *, timeout: FakeResponse(response_payload(wan_uuid)),
        allowlist_path=allowlist_file,
    )


def test_empty_command_execs_read_only_export_without_shell(command_module, allowlist_file):
    exec_calls = []

    def fail_opener(*_args, **_kwargs):
        raise AssertionError("HTTP must not be called")

    command_module.dispatch(
        "",
        opener=fail_opener,
        execv=lambda *args: exec_calls.append(args),
        allowlist_path=allowlist_file,
    )

    assert exec_calls == [
        (
            "/usr/bin/redis-cli",
            ["/usr/bin/redis-cli", "--raw", "zrange", "internet_speedtest_results", "0", "-1"],
        )
    ]


@pytest.mark.parametrize(
    "original_command",
    [
        "run-speedtest unknown-uuid",
        f"other-verb {FIBER}",
        f" run-speedtest {FIBER}",
        f"run-speedtest {FIBER} ",
        f"run-speedtest  {FIBER}",
        f"run-speedtest\t{FIBER}",
        f"run-speedtest {FIBER} extra",
        f"run-speedtest {FIBER}; whoami",
        f"run-speedtest {FIBER} | cat",
        f"run-speedtest $(echo {FIBER})",
        f"run-speedtest {FIBER}\nwhoami",
        f"run-speedtest\xa0{FIBER}",
        " export-network-quality",
        "export-network-quality ",
        "export-network-quality; whoami",
        "export-network-quality extra",
        "export-network-quality\nwhoami",
    ],
)
def test_malformed_commands_are_rejected_before_process_or_http_call(command_module, original_command, allowlist_file):
    calls = []

    with pytest.raises(command_module.CommandRejected, match="^rejected command$"):
        command_module.dispatch(
            original_command,
            opener=lambda *_args, **_kwargs: calls.append("http"),
            execv=lambda *_args, **_kwargs: calls.append("process"),
            allowlist_path=allowlist_file,
        )

    assert calls == []


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (json.dumps({"code": 500, "message": "secret input"}).encode(), "FireApi returned an error"),
        (json.dumps({"code": 200, "data": {}}).encode(), "invalid FireApi response"),
        (b"not-json", "invalid FireApi response"),
        (response_payload(success=False), "speed test was not accepted"),
        (response_payload(manual=False), "speed test was not accepted"),
        (response_payload(uuid=BACKUP), "speed test was not accepted"),
    ],
)
def test_api_and_result_failures_have_fixed_errors(command_module, payload, message, allowlist_file):
    with pytest.raises(command_module.CommandFailure, match=f"^{message}$"):
        command_module.dispatch(
            f"run-speedtest {FIBER}",
            opener=lambda _request, *, timeout: FakeResponse(payload),
            allowlist_path=allowlist_file,
        )


@pytest.mark.parametrize(
    "error",
    [
        HTTPError("http://127.0.0.1/", 503, "details", {}, io.BytesIO(b"sensitive")),
        TimeoutError("sensitive timeout detail"),
        TypeError("sensitive signature detail"),
        HTTPException("sensitive httpexception detail"),
    ],
)
def test_transport_failures_have_fixed_error(command_module, error, allowlist_file):
    def opener(_request, *, timeout):
        raise error

    with pytest.raises(command_module.CommandFailure, match="^FireApi request failed$") as failure:
        command_module.dispatch(f"run-speedtest {FIBER}", opener=opener, allowlist_path=allowlist_file)

    assert failure.value.exit_code == 69


def test_main_rejects_with_exit_64_and_does_not_echo_input(command_module, monkeypatch, capsys, allowlist_file):
    # A valid allowlist is loaded here on purpose: without one, every command
    # rejects for the trivial reason (nothing to check against) and this test
    # would stay green even if the actual lookup logic were deleted. With a
    # real allowlist present, the rejection below is the code deciding
    # "unknown-command sensitive-value" isn't one of its permitted commands.
    hostile = "unknown-command sensitive-value"
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", hostile)

    assert command_module.main(["--allowlist", str(allowlist_file)]) == 64
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "rejected command\n"
    assert hostile not in captured.err


def test_main_does_not_leak_a_traceback_when_execv_fails(command_module, monkeypatch, capsys, allowlist_file):
    # The empty-command branch execs a fixed argv with no allowlist involved.
    # If the target binary is ever missing or not executable, os.execv raises
    # OSError with the offending path baked into its message — exactly the
    # kind of detail this forced-command gate must never hand back to the
    # key holder.
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "")

    def raiser(*_args):
        raise OSError(2, "No such file or directory: /usr/bin/redis-cli")

    monkeypatch.setattr(command_module.os, "execv", raiser)

    assert command_module.main(["--allowlist", str(allowlist_file)]) == 71
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "internal error\n"
    assert "/usr/bin/redis-cli" not in captured.err
    assert "Traceback" not in captured.err


def test_main_does_not_leak_a_traceback_when_the_speedtest_transport_raises_httpexception(
    command_module, monkeypatch, capsys, allowlist_file
):
    # trigger_speedtest's except tuple used to omit HTTPException while its
    # sibling export_network_quality already caught it — this pins that the
    # asymmetry is closed. Routed through main(), not dispatch() directly, so
    # this also proves the failure surfaces as the ordinary CommandFailure
    # path rather than reaching (or needing) main()'s catch-all.
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", f"run-speedtest {FIBER}")

    def opener(_request, *, timeout):
        raise HTTPException("sensitive httpexception detail")

    command_module.urlopen = opener

    assert command_module.main(["--allowlist", str(allowlist_file)]) == 69
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "FireApi request failed\n"
    assert "sensitive httpexception detail" not in captured.err


def test_main_does_not_leak_a_traceback_when_json_decoding_recurses_too_deep(
    command_module, monkeypatch, capsys, allowlist_file
):
    # A pathologically nested FireApi response makes json.loads raise a real
    # RecursionError, which neither trigger_speedtest's nor
    # export_network_quality's except tuple names. Before main() had a
    # catch-all, this reached the top of the script as a raw traceback. Uses
    # an actual deeply-nested payload rather than monkeypatching json.loads
    # directly — json.load() (used by load_allowlist for the allowlist file)
    # is implemented in terms of json.loads() in CPython, so patching the
    # module-level name would also break allowlist loading and reject the
    # command for the wrong reason before this path is even reached.
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", f"run-speedtest {FIBER}")
    deeply_nested = ("[" * 20000 + "]" * 20000).encode()
    command_module.urlopen = lambda _request, *, timeout: FakeResponse(deeply_nested)

    assert command_module.main(["--allowlist", str(allowlist_file)]) == 71
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "internal error\n"
    assert "recursion" not in captured.err.lower()
    assert "Traceback" not in captured.err


def test_export_network_quality_gets_exact_request_and_sanitizes_history(command_module, capsys, allowlist_file):
    payload = {
        "code": 200,
        "data": {
            f"metric:monitor:raw:ping:198.51.100.1:{FIBER}": {
                "1700000000": {
                    "stat": {
                        "min": 1.0,
                        "max": 3,
                        "median": 2,
                        "mean": 2.1,
                        "lossrate": 0,
                        "samples": [1, 2, 3],
                    },
                    "extra": "discarded",
                },
                "bad-timestamp": {"stat": {"mean": 2}},
                "1700000001": {"stat": {"mean": True}},
            },
            f"metric:monitor:raw:ping:198.51.100.1:{BACKUP}": {
                "1700000002": {"stat": {"mean": 4.5, "unknown": "discarded"}}
            },
            f"metric:monitor:raw:ping:192.0.2.1:{FIBER}": {"1699999998": {"stat": {"mean": 0.5}}},
            f"metric:monitor:raw:ping:203.0.113.1:{BACKUP}": {"1699999999": {"stat": {"mean": 8.5}}},
            f"metric:monitor:raw:dns:198.51.100.1:{FIBER}": {"1700000003": {"stat": {"mean": 7}}},
            "metric:monitor:raw:ping:198.51.100.1:unknown-uuid": {"1700000004": {"stat": {"mean": 7}}},
            "device:monitor:raw:ping:198.51.100.1:" + FIBER: {"1700000005": {"stat": {"mean": 7}}},
        },
    }
    calls = []

    def opener(request, *, timeout):
        calls.append((request, timeout))
        return FakeResponse(json.dumps(payload).encode())

    command_module.dispatch("export-network-quality", opener=opener, allowlist_path=allowlist_file)

    request, timeout = calls[0]
    parsed = urlsplit(request.full_url)
    assert request.get_method() == "GET"
    assert request.data is None
    assert (parsed.scheme, parsed.hostname, parsed.port, parsed.path) == (
        "http",
        "127.0.0.1",
        8834,
        "/v1/encipher/simple",
    )
    assert parse_qs(parsed.query) == {
        "command": ["get"],
        "item": ["networkMonitorData"],
        "target": ["0.0.0.0"],
    }
    assert timeout == 10
    assert json.loads(capsys.readouterr().out) == {
        "code": 200,
        "data": {
            f"metric:monitor:raw:ping:198.51.100.1:{FIBER}": {
                "1700000000": {"stat": {"min": 1.0, "max": 3, "median": 2, "mean": 2.1, "lossrate": 0}}
            },
            f"metric:monitor:raw:ping:198.51.100.1:{BACKUP}": {"1700000002": {"stat": {"mean": 4.5}}},
        },
    }


def test_export_network_quality_uses_real_urlopen_transport(command_module, capsys, allowlist_file):
    calls = []
    payload = json.dumps({"code": 200, "data": {}}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.command, self.path))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        command_module.FIREAPI_URL = f"http://127.0.0.1:{server.server_port}/v1/encipher/simple"
        command_module.dispatch("export-network-quality", allowlist_path=allowlist_file)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert calls == [("GET", "/v1/encipher/simple?command=get&item=networkMonitorData&target=0.0.0.0")]
    assert json.loads(capsys.readouterr().out) == {"code": 200, "data": {}}


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        json.dumps({"code": 500, "data": {}, "message": "sensitive"}).encode(),
        json.dumps({"code": 200, "data": []}).encode(),
    ],
)
def test_export_network_quality_rejects_malformed_envelopes_without_echoing_them(
    command_module, payload, monkeypatch, capsys, allowlist_file
):
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "export-network-quality")
    command_module.urlopen = lambda _request, *, timeout: FakeResponse(payload)

    assert command_module.main(["--allowlist", str(allowlist_file)]) == 70
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "invalid FireApi response\n"
    assert payload.decode("utf-8", "ignore") not in captured.err


def test_export_network_quality_sanitizes_transport_errors(command_module, monkeypatch, capsys, allowlist_file):
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "export-network-quality")

    def opener(_request, *, timeout):
        raise TimeoutError("sensitive timeout detail")

    command_module.urlopen = opener
    assert command_module.main(["--allowlist", str(allowlist_file)]) == 69
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "FireApi request failed\n"
    assert "sensitive timeout detail" not in captured.err


def test_export_network_quality_sanitizes_incomplete_response_reads(
    command_module, monkeypatch, capsys, allowlist_file
):
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "export-network-quality")

    class IncompleteResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            raise IncompleteRead(b"sensitive partial response", 100)

    command_module.urlopen = lambda _request, *, timeout: IncompleteResponse()

    assert command_module.main(["--allowlist", str(allowlist_file)]) == 69
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "FireApi request failed\n"
    assert "sensitive partial response" not in captured.err


def test_export_network_quality_drops_malformed_records_fail_closed(command_module, capsys, allowlist_file):
    payload = {
        "code": 200,
        "data": {
            f"metric:monitor:raw:ping:198.51.100.1:{FIBER}": {
                "1700000000": {"stat": {"mean": "not-numeric"}},
                "1700000001": {"stat": {"mean": False}},
                "1700000002": {"not-stat": {"mean": 2}},
            }
        },
    }

    command_module.dispatch(
        "export-network-quality",
        opener=lambda _request, *, timeout: FakeResponse(json.dumps(payload).encode()),
        allowlist_path=allowlist_file,
    )

    assert json.loads(capsys.readouterr().out) == {"code": 200, "data": {}}


ALLOWLIST = {
    "wans": ["33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444"],
    "quality_target": "198.51.100.1",
}


def _write_allowlist(tmp_path, data):
    path = tmp_path / "allowlist.json"
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return path


def test_the_allowlist_comes_from_a_file_not_from_hard_coded_uuids(command_module, tmp_path):
    # One gate for the deployment. A per-site copy of this file would give two
    # sites two divergent pieces of security-sensitive code.
    allowlist = command_module.load_allowlist(_write_allowlist(tmp_path, ALLOWLIST))

    assert allowlist.commands == {
        "run-speedtest 33333333-3333-4333-8333-333333333333": "33333333-3333-4333-8333-333333333333",
        "run-speedtest 44444444-4444-4444-8444-444444444444": "44444444-4444-4444-8444-444444444444",
    }
    assert allowlist.quality_keys == frozenset(
        {
            "metric:monitor:raw:ping:198.51.100.1:33333333-3333-4333-8333-333333333333",
            "metric:monitor:raw:ping:198.51.100.1:44444444-4444-4444-8444-444444444444",
        }
    )


def test_a_missing_allowlist_rejects_every_command(command_module, tmp_path):
    with pytest.raises(command_module.CommandRejected):
        command_module.dispatch(
            "run-speedtest 33333333-3333-4333-8333-333333333333",
            allowlist_path=tmp_path / "does-not-exist.json",
        )


@pytest.mark.parametrize(
    "bad",
    [
        "not json at all",
        "[]",
        '{"wans": "not-a-list", "quality_target": "198.51.100.1"}',
        '{"wans": [], "quality_target": "198.51.100.1"}',
        '{"wans": ["33333333-3333-4333-8333-333333333333"]}',
        "[" * 20000 + "]" * 20000,  # raises RecursionError from json.load, not OSError/ValueError
    ],
)
def test_a_malformed_allowlist_rejects_every_command(command_module, tmp_path, bad):
    # Fail closed. A gate that falls back to a default on a bad file is a gate
    # that can be opened by corrupting the file.
    with pytest.raises(command_module.CommandRejected):
        command_module.dispatch(
            "run-speedtest 33333333-3333-4333-8333-333333333333",
            allowlist_path=_write_allowlist(tmp_path, bad),
        )


@pytest.mark.parametrize(
    "bad",
    [
        '{"wans": [123], "quality_target": "198.51.100.1"}',
        '{"wans": ["*"], "quality_target": "198.51.100.1"}',
    ],
)
def test_a_uuid_character_set_violation_yields_no_permitted_commands(command_module, tmp_path, bad):
    # These two payloads pass the shape checks (a non-empty "wans" list, a
    # non-empty "quality_target" string) and are only rejected by the UUID
    # character-set validation. Asserting via dispatch of an unrelated fixed
    # UUID would pass for the wrong reason — that UUID is absent from
    # commands regardless of whether the validation ran, because the built
    # command key is "run-speedtest *" or "run-speedtest 123". Assert
    # directly on load_allowlist instead, so the validation itself is pinned.
    allowlist = command_module.load_allowlist(_write_allowlist(tmp_path, bad))
    assert allowlist.commands == {}
    assert allowlist.quality_keys == frozenset()


def test_export_network_quality_is_rejected_with_no_allowlist(command_module, tmp_path):
    # `dispatch` guards the quality export with `if not allowlist.quality_keys:
    # raise CommandRejected(...)` before ever calling export_network_quality.
    # Every other test dispatching export-network-quality uses the good
    # allowlist_file fixture, so nothing exercises this guard directly —
    # deleting it changes nothing else the suite observes.
    with pytest.raises(command_module.CommandRejected):
        command_module.dispatch(
            "export-network-quality",
            allowlist_path=tmp_path / "does-not-exist.json",
        )


def test_a_uuid_outside_the_allowlist_is_still_rejected(command_module, tmp_path):
    with pytest.raises(command_module.CommandRejected):
        command_module.dispatch(
            "run-speedtest 22222222-2222-4222-8222-222222222222",
            allowlist_path=_write_allowlist(tmp_path, ALLOWLIST),
        )


def test_the_empty_command_still_execs_the_read_only_export_without_an_allowlist(command_module, tmp_path):
    # The read-only redis export takes no argument and reveals nothing
    # site-specific, so it must keep working even if the allowlist is missing —
    # otherwise a bad allowlist file takes the speed importer down too.
    exec_calls = []
    command_module.dispatch(
        "",
        opener=None,
        execv=lambda *args: exec_calls.append(args),
        allowlist_path=tmp_path / "does-not-exist.json",
    )
    assert exec_calls == [(command_module.EXPORT_ARGV[0], command_module.EXPORT_ARGV)]


def test_state_export_filters_native_network_hash(command_module, monkeypatch, capsys):
    raw = {'eth0': {'uuid':FIBER,'type':'wan','ready':True,'active':True,'ip4':'secret'}, 'eth2':{'uuid':BACKUP,'type':'wan','ready':False,'active':False}, 'lan':{'type':'lan','ready':True}}
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return type('Result', (), {'stdout': '\n'.join(v for k,r in raw.items() for v in (k,json.dumps(r)))})()
    monkeypatch.setattr(command_module.subprocess, 'run', run)
    command_module.export_network_state([FIBER, BACKUP], now=lambda:1000)
    result=json.loads(capsys.readouterr().out)
    assert calls == [['/usr/bin/redis-cli','--raw','hgetall','sys:network:info']]
    assert result == {'time':1000,'data':{FIBER:{'ready':True,'active':True},BACKUP:{'ready':False,'active':False}}}
    assert 'secret' not in json.dumps(result)


@pytest.mark.parametrize('record', [{}, {'type':'lan','ready':False,'active':False}, {'type':'wan','ready':0,'active':False}, {'type':'wan','ready':True,'active':None}])
def test_state_export_rejects_unusable_native_state(command_module, monkeypatch, record):
    monkeypatch.setattr(command_module.subprocess,'run',lambda *a,**k:type('Result',(),{'stdout':'eth0\n'+json.dumps(dict(record,uuid=FIBER))})())
    with pytest.raises(command_module.CommandFailure):command_module.export_network_state([FIBER])


def test_state_dispatch_requires_valid_allowlist(command_module, allowlist_file, monkeypatch):
    calls=[]
    monkeypatch.setattr(command_module,'export_network_state',lambda uuids:calls.append(set(uuids)))
    command_module.dispatch('export-network-state',allowlist_path=allowlist_file)
    assert calls == [{FIBER,BACKUP}]
    with pytest.raises(command_module.CommandRejected):command_module.dispatch('export-network-state',allowlist_path='/nonexistent')


def test_state_export_rejects_duplicate_wan_identity(command_module, monkeypatch):
    state=json.dumps({'uuid':FIBER,'type':'wan','ready':True,'active':True})
    monkeypatch.setattr(command_module.subprocess,'run',lambda *a,**k:type('Result',(),{'stdout':'eth0\n'+state+'\neth2\n'+state})())
    with pytest.raises(command_module.CommandFailure):command_module.export_network_state([FIBER])


def test_cli_requires_explicit_allowlist_before_effects(command_module, monkeypatch, capsys):
    monkeypatch.setenv('SSH_ORIGINAL_COMMAND', '')
    monkeypatch.setattr(command_module.os, 'execv', lambda *args: pytest.fail('unexpected process'))
    assert command_module.main([]) == 64
    assert capsys.readouterr().err == 'rejected command\n'
