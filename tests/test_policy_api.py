"""The policy through the dashboard's API: the grid it shows, the file it writes, the requests it
refuses, and the reload that makes a save take effect in the processes already running.

Nothing here touches the card: the tests are about the config file and the answers. The file is
always a copy of `config.example.yaml` in `tmp_path` pointed at the fakes - the dashboard writes
the owner's config, so a test that let it write the real one would be the bug.
"""

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from vhotseat import api, config, config_edit, policy, web

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def served(rig, tmp_path):
    """The rig, with the config it was built from written out beside it, and a dashboard serving
    it: the file the editor saves and the file the running processes reload from."""
    path = tmp_path / "config.yaml"
    path.write_text(rig_config_text(rig), encoding="utf-8")
    rig.arb.config_path = path
    dash = web.Dashboard(rig.arb)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dash.handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def rig_config_text(rig) -> str:
    """config.example.yaml as the owner's file, with the fakes' addresses in it, so a reload swaps
    in the same workload objects the rig is probing rather than the real servers."""
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    return (text.replace("url: http://127.0.0.1:2335", f"url: {rig.strata.url}")
                .replace("url: http://127.0.0.1:8188", f"url: {rig.comfy.url}"))


def get(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, json.loads(r.read()), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), dict(e.headers)


def post(url, body, origin=None, kind="application/json", edit="1"):
    """POST the way the page does. `edit=None` / `kind="text/plain"` is the way a page on somebody
    else's machine does it."""
    headers = {"Content-Type": kind}
    if edit is not None:
        headers[web.EDIT_HEADER] = edit
    if origin is not None:
        headers["Origin"] = origin
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


GRID = {"strata": {"comfy": "keep", "claims": "ask", "pressure": "yes"},
        "comfy": {"strata": "yes", "claims": "keep", "pressure": "ask"}}


def read_policy(path) -> tuple[dict, str]:
    """What the file says, and its version."""
    text, version = config_edit.read(path)
    return config.parse(text), version


def on_disk(rig) -> str:
    """The version of the file as it is now, by the same hash the API sends."""
    return config_edit.read(rig.arb.config_path)[1]


def numbers_in(path) -> dict:
    """What the file's `priority:` numbers are, per workload (None where it wrote none)."""
    cfg, _ = read_policy(path)
    return {n: (w or {}).get("priority") for n, w in cfg["workloads"].items()}


# --- what the page is shown ----------------------------------------------------------


def test_the_dashboard_shows_the_grid_the_arbiter_acts_on(served, rig):
    """Every cell of the grid comes from the same two keys the handover reads, with the sentence
    saying what it would do: an owner choosing between Make room and Keep loaded is choosing between
    two consequences they have been told, and the page never has a rule of its own to get out of
    step with the arbiter."""
    code, body, _ = get(served + "/api/policy")
    assert code == 200
    _, version = config_edit.read(rig.arb.config_path)
    assert body["version"] == version and body["path"] == str(rig.arb.config_path)
    assert [w["workload"] for w in body["order"]] == ["strata", "comfy"]
    assert body["order"][0] == {"workload": "strata", "label": "Strata", "priority": 50}
    assert [r["id"] for r in body["requesters"]] == ["strata", "comfy", "claims", "pressure"]
    assert body["matrix"]["strata"]["comfy"] == {"choice": "yes",
                                                 "says": "Strata unloads once idle"}
    assert body["matrix"]["strata"]["claims"]["says"] == "Strata unloads once idle"
    assert body["matrix"]["comfy"]["claims"] == {"choice": "yes",
                                                 "says": "ComfyUI unloads once idle"}
    assert "comfy" not in body["matrix"]["comfy"]          # nothing unloads itself
    assert body["ask"] == {"timeout_s": 120, "fallback": "keep"}
    assert body["pressure"]["min_free_mib"] == 512


def test_the_order_is_the_one_the_arbiter_would_unload_in(served, rig):
    """The page's list is the order a handover asks in, not the order the config happens to type
    them: priorities decide, and a tie keeps the config's order."""
    rig.arb.workloads["comfy"].priority = 10
    code, body, _ = get(served + "/api/policy")
    assert [w["workload"] for w in body["order"]] == ["comfy", "strata"]


def test_the_api_answers_the_page_and_nobody_else(served):
    """The policy page is served from this port only, and the server sends no CORS headers at all:
    a page on another origin can't read the config, let alone write it."""
    _, _, headers = get(served + "/api/policy")
    assert not [h for h in headers if h.lower().startswith("access-control")]
    assert get(served + "/api/nope")[0] == 404


# --- saving it ----------------------------------------------------------------------


def test_saving_the_policy_writes_the_config_and_the_running_rules(served, rig):
    """One save does both halves of what the owner expects: the file says what they picked, and the
    dashboard and watcher on this box are acting on it without a restart. A save that only did the
    first would leave `watch` unloading something the owner had just protected."""
    before = get(served + "/api/policy")[1]["version"]
    code, body = post(served + "/api/policy", {"version": before,
                                               "priorities": ["comfy", "strata"], "matrix": GRID})
    assert code == 200 and set(body) == {"version"} and body["version"] != before
    assert body["version"] == on_disk(rig)
    cfg, _ = read_policy(rig.arb.config_path)
    assert cfg["workloads"]["strata"]["yields_to"] == ["pressure"]
    assert cfg["workloads"]["strata"]["asks_for"] == ["claims"]
    assert cfg["workloads"]["comfy"]["yields_to"] == ["strata"]
    assert cfg["workloads"]["comfy"]["asks_for"] == ["pressure"]
    assert cfg["workloads"]["comfy"]["priority"] == 10
    assert cfg["workloads"]["strata"]["priority"] == 90
    w = rig.arb.workloads
    assert policy.choice(w["strata"].yields_to, w["strata"].asks_for, "comfy") == "keep"
    assert policy.choice(w["strata"].yields_to, w["strata"].asks_for, "claims") == "ask"
    assert w["comfy"].priority == 10


def test_reading_the_order_and_posting_it_back_writes_the_same_numbers(served, rig):
    """The page reads the order - lowest priority first, because that is who the arbiter asks to
    let go first - and sends it back as `priorities:`. So the direction has to match, and an order
    that is only re-read and re-saved must not move anybody: an editor that inverted it on every
    save would flip every pair in the file each time the page was opened, and the next handover
    would unload the workload the owner had just put last."""
    code, body = post(served + "/api/policy", {"version": get(served + "/api/policy")[1]["version"],
                                               "priorities": ["comfy", "strata"]})
    assert code == 200 and numbers_in(rig.arb.config_path) == {"strata": 90, "comfy": 10}
    _, shown, _ = get(served + "/api/policy")
    order = [w["workload"] for w in shown["order"]]
    assert order == ["comfy", "strata"]            # the order it just wrote, lowest first
    code, again = post(served + "/api/policy",
                       {"version": shown["version"], "priorities": order})
    assert code == 200
    assert numbers_in(rig.arb.config_path) == {"strata": 90, "comfy": 10}
    _, after, _ = get(served + "/api/policy")
    assert after["order"] == shown["order"]


def test_the_saved_grid_is_shown_back_on_the_next_read(served, rig):
    """The page reads the file, not its own last post: after a save the grid says what the file
    says, which is what makes the version in the answer usable for the next save."""
    version = get(served + "/api/policy")[1]["version"]
    code, body = post(served + "/api/policy", {"version": version, "matrix": GRID,
                                               "ask": {"timeout_s": 600}})
    assert code == 200
    code, shown, _ = get(served + "/api/policy")
    assert shown["version"] == body["version"]
    assert shown["matrix"]["strata"]["comfy"]["choice"] == "keep"
    assert shown["ask"] == {"timeout_s": 600, "fallback": "keep"}


def test_a_stale_version_is_refused_and_says_what_the_file_says_now(served, rig):
    """The owner hand-edited the file after opening the page. Their save carries the version they
    read, and the answer is 409 with the version that's there now: nothing is written, so what they
    typed is still there for the page to re-read instead of being painted over."""
    version = get(served + "/api/policy")[1]["version"]
    text = config_edit.read(rig.arb.config_path)[0]
    rig.arb.config_path.write_text(text.replace("  keep: 500", "  keep: 900"), encoding="utf-8",
                                   newline="")
    code, body = post(served + "/api/policy", {"version": version, "matrix": GRID})
    assert code == 409 and body["version"] == on_disk(rig)
    assert "changed since you read it" in body["error"]
    cfg, _ = read_policy(rig.arb.config_path)
    assert cfg["events"]["keep"] == 900 and "asks_for" not in cfg["workloads"]["strata"]
    assert on_disk(rig) != version                # the file really did change under them


@pytest.mark.parametrize("why,kwargs", [
    (web.EDIT_HEADER, {"edit": None}),
    ("Content-Type", {"kind": "text/plain"}),
    ("Origin", {"origin": "http://evil.example:8765"}),
])
def test_a_request_that_isnt_this_pages_is_refused_before_it_writes(served, rig, why, kwargs):
    """Three doors shut on a POST that didn't come from this dashboard's page: no custom header, a
    content type a form can send, and an Origin that isn't this port. Each is refused, and the
    config is what it was - a cross-site request that half-applies is a config the owner didn't
    write."""
    version = get(served + "/api/policy")[1]["version"]
    before = rig.arb.config_path.read_text(encoding="utf-8")
    code, body = post(served + "/api/policy", {"version": version, "matrix": GRID}, **kwargs)
    assert code == 403 and why in body["error"]
    assert rig.arb.config_path.read_text(encoding="utf-8") == before
    assert list(rig.arb.config_path.parent.glob("config.yaml.bak-*")) == []


def test_a_save_the_config_validator_refuses_is_the_message_doctor_prints(served, rig):
    """The page and `doctor` check the same rules, from the same code: a POST that puts 0 in
    `ask.timeout_s` gets back the line doctor would print, and no backup, no write."""
    version = get(served + "/api/policy")[1]["version"]
    before = rig.arb.config_path.read_text(encoding="utf-8")
    code, body = post(served + "/api/policy", {"version": version, "ask": {"timeout_s": 0}})
    assert code == 400
    assert "ask.timeout_s: expected a number of seconds > 0" in body["error"]
    assert rig.arb.config_path.read_text(encoding="utf-8") == before


def test_a_dashboard_that_wasn_t_given_its_config_file_says_so(rig):
    """A caller that hands in a config dict rather than letting the CLI read the file gets a
    dashboard that shows the policy and won't write one: the only file it could write is the real
    one, which nobody asked it to touch."""
    dash = web.Dashboard(rig.arb)
    assert dash.policy()["path"] is None and dash.policy()["version"] is None
    assert dash.save_policy(b'{"version": ""}') == (
        400, {"error": "this dashboard isn't reading a config file it can write back"})
    assert rig.arb.config_path is None


def test_the_body_has_to_be_json_it_can_t_be_a_form(served, rig):
    """The content type isn't theatre: a form post with a bogus body gets the parse error, and the
    config is untouched."""
    code, body = post(served + "/api/policy", "not-an-object")
    assert code == 400 and "expected an object" in body["error"]


# --- the reload ---------------------------------------------------------------------


def write(rig, *, after=None, lines=(), append=None):
    """The owner editing their own file: rewrite the copy with an extra policy key set - by hand,
    not through the API, which is the point of these tests."""
    text = rig_config_text(rig)
    ends = "\r\n" if "\r\n" in text else "\n"
    if after:
        at = text.index(after)
        end = text.index("\n", at) + 1     # insert after the anchor's whole line, not inside it
        text = text[:end] + ends.join(lines) + ends + text[end:]
    if append:
        text = text.rstrip("\r\n") + ends + append.replace("\n", ends)
    rig.arb.config_path.write_text(text, encoding="utf-8", newline="")
    return text


def test_a_file_saved_elsewhere_is_in_force_on_the_next_poll(served, rig):
    """The save went through the CLI, another dashboard, or an editor: the watcher and the page
    notice by the file's mtime and put the new rules in force without a restart. The claim release
    reads its targets from the workloads, so a watcher on old rules is a watcher that unloads the
    wrong thing."""
    write(rig, after="    allow_restart: true",
          lines=["    yields_to: [comfy]", "    asks_for: [claims]"])
    assert rig.arb.workloads["strata"].yields_to == ("*",)      # not yet: nothing has looked
    web.watch(rig.arb, polls=1)
    assert rig.arb.workloads["strata"].yields_to == ("comfy",)
    assert rig.arb.workloads["strata"].asks_for == ("claims",)
    assert rig.arb.workloads["comfy"].yields_to == ("*",)


def test_the_pressure_and_ask_numbers_come_in_with_them(served, rig):
    """The same file, the same moment: the trigger's numbers and the ask's are read together with
    the policy, because a policy that changed and a `pressure:` that didn't is a config nobody
    wrote."""
    write(rig, append="pressure:\n  min_free_mib: 1200\nask:\n  timeout_s: 30\n"
                      "  fallback: release\n")
    get(served + "/api/status")
    assert rig.arb.pressure_cfg["min_free_mib"] == 1200
    assert rig.arb.ask == {"timeout_s": 30, "fallback": "release"}
    _, body, _ = get(served + "/api/policy")
    assert body["pressure"]["min_free_mib"] == 1200 and body["ask"]["fallback"] == "release"


def test_a_hand_edit_that_broke_the_config_keeps_the_old_rules_and_says_so(served, rig, capsys):
    """A half-typed edit is the mistake an owner actually makes. The rules that were running keep
    running - a config that doesn't read is not a reason to unload anything - and the dashboard
    says what's wrong and that it's the old rules, because a page that quietly keeps showing the
    previous policy is how an owner believes a protection is in force when it isn't."""
    write(rig, after="    driver: comfyui", lines=["    yields_to: [strata"])
    code, body, _ = get(served + "/api/status")
    assert code == 200 and "yields_to" in body["config_error"]
    assert rig.arb.workloads["strata"].yields_to == ("*",)
    assert "the rules it had before are still running" in capsys.readouterr().err
    # and it says it once rather than every poll
    get(served + "/api/status")
    assert "no longer reads" not in capsys.readouterr().err


def test_the_old_rules_come_back_when_the_file_is_fixed(served, rig):
    """Fixing the file is a reload too: the error goes, and what's in force is what the file says
    now - in the watcher, which is the process that would otherwise unload the wrong thing."""
    write(rig, after="    driver: comfyui", lines=["    yields_to: [strata"])
    get(served + "/api/status")
    assert rig.arb.config_error
    write(rig, after="    driver: comfyui", lines=["    yields_to: [strata]"])
    web.watch(rig.arb, polls=1)          # one poll of the watcher, no page involved
    assert rig.arb.config_error is None
    assert rig.arb.workloads["comfy"].yields_to == ("strata",)


def test_a_seat_opened_once_obeys_a_policy_saved_afterwards(rig, tmp_path):
    """A proxy's gate is the one policy reader that isn't a fresh process: `api.open()` runs once
    and the Seat answers every request after it. So a save has to reach it - otherwise the owner's
    new rule sits on the dashboard while the gate goes on refusing and allowing exactly as it
    always did, and the fix is a proxy restart nobody thought of."""
    path = tmp_path / "config.yaml"
    text = rig_config_text(rig)
    path.write_text(text.replace("    allow_restart: true",
                                 "    allow_restart: true\n    yields_to: [comfy]", 1),
                    encoding="utf-8", newline="")
    rig.arb.config_path = path
    seat = api.Seat(rig.arb)
    assert rig.arb.workloads["strata"].yields_to == ("*",)     # as it was when the Seat opened
    path.write_text(text.replace("    allow_restart: true",
                                 "    allow_restart: true\n    yields_to: [pressure]", 1),
                    encoding="utf-8", newline="")
    seat.acquire("comfy")               # what the proxy does before it forwards a request
    assert rig.arb.workloads["strata"].yields_to == ("pressure",)


def test_a_running_process_that_never_reads_a_file_is_unaffected(rig):
    """`web` and `watch` reload because they hold one arbiter for hours. Nothing else needs to: an
    arbiter built from a dict has no file to watch, and a gate or guard is a fresh process that
    read the config already."""
    assert rig.arb.reload_policy() is None
    assert rig.arb.reload_policy(force=True) is None
