"""Saving the owner's policy back into their own file: the same comments and the same order,
the change only what they decided, and a bad or stale write that costs them nothing.

The file is the object under test as much as the data: a config the dashboard rewrites is a
config the owner stops trusting, so these tests read the text back rather than only the parsed
result. Every one of them edits a copy of `config.example.yaml` in `tmp_path` - the file that
ships commented is the file the writer has to survive.
"""

import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from vhotseat import config, config_edit, policy
from vhotseat.drivers.base import Context

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config.example.yaml"


def text_of(path) -> str:
    """The file as bytes-decoded text, newlines and all: what the version is a hash of."""
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


@pytest.fixture
def cfg_file(tmp_path):
    """config.example.yaml as the owner's file: ~200 lines, a third of them comments, several of
    those commented-out keys."""
    path = tmp_path / "config.yaml"
    shutil.copy(EXAMPLE, path)
    return path


def save(path, **edits):
    """Save the way the dashboard does: read the file, send its version back with the edits."""
    _, version = config_edit.read(path)
    return config_edit.save(path, version, **edits)


def code_lines(text):
    """The lines that are config rather than comment or blank - what the parser actually sees."""
    return [ln for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def comments(text):
    return [ln.strip() for ln in text.splitlines() if ln.strip().startswith("#")]


def key_order(text, keep_editable=False):
    """Every key in the file, in document order, dotted by where it sits. By default the keys this
    writer may add, change and remove are left out: their order is what the save is for, and what
    has to be proven unchanged is everything else."""
    def editable(dotted):
        return (dotted.split(".")[0] in config_edit.TOP_KEYS
                or dotted.rsplit(".", 1)[-1] in config_edit.WORKLOAD_KEYS)

    def walk(node, prefix=""):
        out = []
        if isinstance(node, dict):
            for k, v in node.items():
                if keep_editable or not editable(prefix + str(k)):
                    out.append(prefix + str(k))
                    out += walk(v, f"{prefix}{k}.")
        elif isinstance(node, list):
            for item in node:
                out += walk(item, prefix)
        return out
    return walk(config.parse(text))


def policy_lines(text):
    """The policy keys as the file writes them, with quotes folded: the writer lets the YAML
    dumper choose whether `*` needs single or double quotes, and both mean one value."""
    return [ln.strip().replace("'", '"') for ln in code_lines(text)
            if ln.strip().split(":")[0] in config_edit.WORKLOAD_KEYS]


STRATA_KEEPS_COMFY_ASKS_CLAIMS = {"comfy": "keep", "claims": "ask", "pressure": "yes"}
COMFY_ASKS_PRESSURE = {"strata": "yes", "claims": "yes", "pressure": "ask"}
ALL_YES = {"comfy": "yes", "claims": "yes", "pressure": "yes"}


def test_a_saved_policy_keeps_every_comment_and_the_key_order(cfg_file):
    """The owner set the policy from the page; the file they open next has the same notes in the
    same places, and the only new lines are the ones that say what they picked. A save that
    reindents, rewraps or reorders the rest of the file - or turns its line endings into something
    else - is a save they can't read a diff in."""
    before = text_of(cfg_file)
    save(cfg_file, priorities=["comfy", "strata"],
         matrix={"strata": STRATA_KEEPS_COMFY_ASKS_CLAIMS, "comfy": COMFY_ASKS_PRESSURE})
    after = text_of(cfg_file)
    assert comments(after) == comments(before)
    assert key_order(after) == key_order(before)
    added = [ln for ln in after.splitlines() if ln not in before.splitlines()]
    assert added and all(ln.strip().split(":")[0] in config_edit.WORKLOAD_KEYS for ln in added)
    assert [ln for ln in before.splitlines() if ln not in after.splitlines()] == []
    written = [ln.strip().replace("'", '"') for ln in after.splitlines()]
    for line in ("priority: 90", "priority: 10", "yields_to: [pressure]",
                 "asks_for: [claims]", 'yields_to: ["*"]', "asks_for: [pressure]"):
        assert line in written, line
    # ...and each one lands inside the block it belongs to, not wherever a round-trip dump's
    # comment bookkeeping would leave it: Strata's keys before the ComfyUI block starts, ComfyUI's
    # before the comments that introduce `patches:`.
    def at(needle, start=0):
        return next(i for i, ln in enumerate(written[start:], start) if ln == needle)

    # the three keys of one row in the order the docs list them, not the order the save happened
    # to be told about them
    assert policy_lines(after)[:3] == ["priority: 90", "yields_to: [pressure]",
                                       "asks_for: [claims]"]
    strata, comfy, patches = at("strata:"), at("comfy:"), at("patches: []")
    assert strata < at("yields_to: [pressure]") < comfy
    for ln in ('yields_to: ["*"]', "asks_for: [pressure]"):
        assert comfy < at(ln) < patches, ln


def test_a_row_that_makes_room_for_everyone_is_written_as_the_star(cfg_file):
    """The page has one choice per pair, so "yes" to every column but one would be written out as
    the list of the names that are yes - a rule that quietly stops covering the fourth workload the
    day one is added. The arbiter's own `*` is what goes in the file, next to the ask that makes the
    row not the default; and a row that's back to yes-everything has both keys taken out, because an
    absent `yields_to:` already says it."""
    save(cfg_file, matrix={"strata": ALL_YES})
    assert policy_lines(text_of(cfg_file)) == []
    save(cfg_file, matrix={"strata": {**ALL_YES, "claims": "keep"}})
    assert policy_lines(text_of(cfg_file)) == ["yields_to: [comfy, pressure]"]
    save(cfg_file, matrix={"strata": {**ALL_YES, "claims": "ask"}})
    assert policy_lines(text_of(cfg_file)) == ['yields_to: ["*"]', "asks_for: [claims]"]
    save(cfg_file, matrix={"strata": ALL_YES})
    assert policy_lines(text_of(cfg_file)) == []


def test_a_key_back_at_its_default_is_taken_out_again(cfg_file):
    """Undoing a change through the page should leave the file as it was, not a block of keys set
    to what they would have been anyway: an owner's config fills with lines that decide nothing and
    have to be maintained forever."""
    save(cfg_file, ask={"timeout_s": 600, "fallback": "release"})
    assert "  timeout_s: 600" in code_lines(text_of(cfg_file))
    save(cfg_file, ask={"timeout_s": 120, "fallback": "keep"})
    after = text_of(cfg_file)
    assert not [ln for ln in code_lines(after)
                if ln.strip().startswith(("ask:", "timeout_s:", "fallback:"))]
    assert comments(after) == comments(text_of(EXAMPLE))


def test_the_pressure_block_is_edited_key_by_key(cfg_file):
    """`pressure:` is the owner's block with their numbers in it: a save that changes when the
    trigger waits must not rewrite the rest of it to defaults, which would undo figures they
    measured for this card."""
    save(cfg_file, pressure={"sustain_s": 60})
    lines = code_lines(text_of(cfg_file))
    assert "pressure:" in lines and "  sustain_s: 60" in lines
    assert not [ln for ln in lines if ln.strip().startswith(("min_free_mib:", "grow_mib:"))]
    save(cfg_file, pressure={"min_free_mib": 250, "sustain_s": 60})
    assert "  min_free_mib: 250" in code_lines(text_of(cfg_file))


def insert_after_line(text, needle, new_lines, nth=1):
    """The owner's file with lines added under the nth block that names `needle`: these tests need
    a key hand-written in a style the example doesn't ship with, in the file's own line endings."""
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in text else "\n"
    seen = 0
    for i, line in enumerate(lines):
        if line.strip() == needle:
            seen += 1
            if seen == nth:
                added = "".join(ln + newline for ln in new_lines)
                return "".join(lines[:i + 1]) + added + "".join(lines[i + 1:])
    raise AssertionError(f"no {nth}th {needle!r}")


def test_resetting_an_annotated_key_to_its_default_still_changes_the_value(cfg_file):
    """The owner wrote why they hold their model; later the page says make room for everyone. That
    answer needs no key - an absent `yields_to:` is the same answer - so the key goes back to what
    being absent means, and the note stays to explain what was decided there before. Leaving the
    old value on the line because the key can't be deleted without taking the note is the failure
    this is about: the page says saved, `status` says the model is safe, and the next handover
    unloads it - or the other way round, with a `[]` nobody chose any more holding the card."""
    hand = insert_after_line(text_of(cfg_file), "allow_restart: true",
                             ["    yields_to: []      # keep my model loaded"], 1)
    hand = insert_after_line(hand, "patches: []",
                             ["ask:", "  timeout_s: 300   # I walk away from the keyboard a lot"], 1)
    cfg_file.write_bytes(hand.encode("utf-8"))
    assert config.load(cfg_file)["workloads"]["strata"]["yields_to"] == []
    save(cfg_file, matrix={"strata": ALL_YES, "comfy": COMFY_ASKS_PRESSURE},
         ask={"timeout_s": 120})
    cfg = config.load(cfg_file)
    assert cfg["workloads"]["strata"]["yields_to"] == ["*"], "the old answer is still in the file"
    assert cfg["ask"]["timeout_s"] == 120
    notes = text_of(cfg_file).splitlines()
    assert any("keep my model loaded" in ln for ln in notes)
    assert any("walk away from the keyboard" in ln for ln in notes)


def test_a_note_at_the_end_of_a_policy_line_survives_the_value_it_describes(cfg_file):
    """The owner wrote why they picked the number next to the number. The page changes the number;
    the note stays where it was rather than drifting a column or going away, because otherwise the
    next save talks over the reason for the last one."""
    hand = insert_after_line(text_of(cfg_file), "allow_restart: true",
                             ["    priority: 50          # idle and cheap to reload"], 1)
    cfg_file.write_bytes(hand.encode("utf-8"))
    save(cfg_file, priorities=["comfy", "strata"])
    line = next(ln for ln in text_of(cfg_file).splitlines()
                if ln.strip().startswith("priority: 90"))
    assert line.startswith("    priority: 90") and line.endswith("# idle and cheap to reload")
    assert config.load(cfg_file)["workloads"]["strata"]["priority"] == 90


def test_a_key_at_its_default_is_left_alone_when_its_note_would_go_with_it(cfg_file):
    """Taking out a key that's back at the default is tidiness; taking out the owner's comment with
    it is a loss they can't get back. A value that already says the default decides nothing either
    way, so the line and its note stay."""
    hand = insert_after_line(text_of(cfg_file), "patches: []",
                             ["ask:", "  timeout_s: 120   # long enough to walk back"], 1)
    cfg_file.write_bytes(hand.encode("utf-8"))
    before = text_of(cfg_file)
    assert config.parse(before)["ask"]["timeout_s"] == 120
    save(cfg_file, ask={"timeout_s": 120, "fallback": "keep"})
    after = text_of(cfg_file)
    assert after == before
    assert any("long enough to walk back" in ln for ln in after.splitlines())


def test_a_block_list_written_at_its_key_s_own_column_is_replaced_whole(cfg_file):
    """`yields_to:` with `- comfy` under it at the *same* column is valid YAML - a block sequence
    needs no extra indent under a mapping key - and it's what someone typing the list gets. Only
    counting lines indented deeper than the key stops at the key: the new list is written, the old
    items stay underneath it, and the file has two answers for one key and stops reading. Same for
    `asks_for:`, and for a top-level `ignore:` under `pressure:`."""
    example = text_of(EXAMPLE)

    def typed(lines):
        cfg_file.write_bytes(insert_after_line(example, "allow_restart: true", lines, 1)
                             .encode("utf-8"))

    typed(["    yields_to:", "    - comfy", "    - claims"])
    assert config.parse(text_of(cfg_file))["workloads"]["strata"]["yields_to"] == ["comfy", "claims"]
    save(cfg_file, matrix={"strata": {"comfy": "keep", "claims": "keep", "pressure": "yes"},
                           "comfy": COMFY_ASKS_PRESSURE})
    written = text_of(cfg_file)
    assert config.parse(written)["workloads"]["strata"]["yields_to"] == ["pressure"]
    assert [ln for ln in code_lines(written) if ln.strip().startswith("-")] == []
    assert comments(written) == comments(example)

    typed(["    yields_to:", "    - comfy", "    - claims"])
    save(cfg_file, matrix={"strata": ALL_YES, "comfy": COMFY_ASKS_PRESSURE})
    written = text_of(cfg_file)
    assert config.parse(written)["workloads"]["strata"].get("yields_to") is None
    assert "- comfy" not in [ln.strip() for ln in code_lines(written)], written
    assert comments(written) == comments(example)

    typed(["    asks_for:", "    - comfy"])
    save(cfg_file, matrix={"strata": STRATA_KEEPS_COMFY_ASKS_CLAIMS, "comfy": COMFY_ASKS_PRESSURE})
    written = text_of(cfg_file)
    assert config.parse(written)["workloads"]["strata"]["asks_for"] == ["claims"]
    assert "- comfy" not in [ln.strip() for ln in code_lines(written)]
    assert comments(written) == comments(example)


def test_an_indentless_list_under_a_top_level_block_is_edited_like_any_other(cfg_file):
    """The example writes `ignore:` on one line; written as a block at its key's own column it has
    nothing indented deeper than that key either, so each of these edits has to come from where the
    parsed list says its items are. A new key goes after the whole list rather than into the middle
    of it, an updated list loses the old names, and a dropped one loses the list with it."""
    example = text_of(EXAMPLE)
    hand = insert_after_line(example, "patches: []",
                             ["pressure:", "  ignore:", "  - vmware-vmx.exe", "  - '*cudaball*'"], 1)
    cfg_file.write_bytes(hand.encode("utf-8"))
    assert config.parse(text_of(cfg_file))["pressure"]["ignore"] == ["vmware-vmx.exe", "*cudaball*"]

    save(cfg_file, pressure={"grow_mib": 5000})          # a key after the list, not inside it
    cfg = config.parse(text_of(cfg_file))["pressure"]
    assert cfg == {"ignore": ["vmware-vmx.exe", "*cudaball*"], "grow_mib": 5000}, cfg

    save(cfg_file, pressure={"ignore": ["thing.exe"], "min_free_mib": 900})
    written = text_of(cfg_file)
    cfg = config.parse(written)["pressure"]
    assert cfg["ignore"] == ["thing.exe"] and cfg["min_free_mib"] == 900, cfg
    hold = [ln.strip() for ln in code_lines(written)]
    assert "- vmware-vmx.exe" not in hold and "- '*cudaball*'" not in hold, written
    assert comments(written) == comments(example)

    save(cfg_file, pressure={"ignore": []})               # back to the default: the list goes too
    written = text_of(cfg_file)
    assert "ignore" not in [ln.strip().split(":")[0] for ln in code_lines(written)]
    assert config.parse(written)["pressure"]["min_free_mib"] == 900
    assert comments(written) == comments(example)


def test_a_policy_key_written_as_a_block_list_is_replaced_whole(cfg_file):
    """`yields_to:` spread over three lines is a valid way to write it. Replacing it leaves one
    line, not a line plus the two list entries that were under it - which would have parsed as
    something else entirely."""
    hand = insert_after_line(text_of(cfg_file), "allow_restart: true",
                             ["    yields_to:", "      - comfy", "      - claims   # the game can have it"], 2)
    cfg_file.write_bytes(hand.encode("utf-8"))
    assert config.load(cfg_file)["workloads"]["comfy"]["yields_to"] == ["comfy", "claims"]
    save(cfg_file, matrix={"comfy": {"strata": "yes", "claims": "ask", "pressure": "yes"}})
    after = text_of(cfg_file)
    assert "- comfy" not in after and "the game can have it" not in after
    cfg = config.load(cfg_file)
    assert cfg["workloads"]["comfy"]["yields_to"] == ["*"]
    assert cfg["workloads"]["comfy"]["asks_for"] == ["claims"]
    assert comments(after) == comments(text_of(EXAMPLE))


def test_a_policy_the_validator_refuses_is_the_message_doctor_prints_and_no_write(cfg_file):
    """The page can post a number the config validator rejects - the two agree on the rules, not on
    a JS copy of them. The owner gets the line `doctor` would have printed, and the file is byte for
    byte what it was: a half-applied policy is a config that acts differently from the one on
    screen."""
    before = text_of(cfg_file)
    with pytest.raises(config.ConfigError) as got:
        save(cfg_file, ask={"timeout_s": 0})
    assert "ask.timeout_s: expected a number of seconds > 0" in str(got.value)
    assert str(cfg_file) in str(got.value)
    assert text_of(cfg_file) == before
    assert list(cfg_file.parent.glob("config.yaml.bak-*")) == []


def test_a_matrix_that_leaves_a_column_blank_is_refused(cfg_file):
    """Half a row is not an answer: filling the missing column from the config the page was showing
    would decide, in the owner's file, what they were about to click."""
    with pytest.raises(config.ConfigError, match="matrix.strata: no choice for claims"):
        save(cfg_file, matrix={"strata": {"comfy": "keep", "pressure": "yes"}})
    with pytest.raises(config.ConfigError, match="matrix.strata.nope"):
        save(cfg_file, matrix={"strata": {**ALL_YES, "nope": "keep"}})
    assert text_of(cfg_file) == text_of(EXAMPLE)


def test_an_order_that_doesnt_name_every_workload_is_refused(cfg_file):
    """The order is the whole list or nothing: a save that reordered two of three workloads and
    left the third at its old number would put one workload in two places at once."""
    with pytest.raises(config.ConfigError, match="says nothing about comfy"):
        save(cfg_file, priorities=["strata"])
    with pytest.raises(config.ConfigError, match="no workload named 'nope'"):
        save(cfg_file, priorities=["comfy", "strata", "nope"])
    with pytest.raises(config.ConfigError, match="named twice"):
        save(cfg_file, priorities=["comfy", "comfy", "strata"])
    assert text_of(cfg_file) == text_of(EXAMPLE)


def test_a_save_against_a_file_that_changed_under_it_writes_nothing(cfg_file):
    """The page was opened before the owner hand-edited the file. Its POST carries the version it
    read, and the save refuses rather than painting over what they typed - and the version now on
    disk comes back with the refusal, so the page can re-read and show them what it says."""
    _, version = config_edit.read(cfg_file)
    hand_edited = text_of(cfg_file).replace("  keep: 500", "  keep: 900")
    cfg_file.write_bytes(hand_edited.encode("utf-8"))
    with pytest.raises(config_edit.Conflict) as got:
        config_edit.save(cfg_file, version, priorities=["comfy", "strata"])
    assert text_of(cfg_file) == hand_edited
    assert got.value.version == config_edit.version_of(hand_edited)
    assert "changed since you read it" in str(got.value)
    assert list(cfg_file.parent.glob("config.yaml.bak-*")) == []


def test_two_saves_that_read_the_file_at_once_take_turns(cfg_file, monkeypatch):
    """Two pages open, or one page and another process: both read the config at the same moment and
    post a different answer. The version check passes for both, because neither has written yet - so
    one of them writes over the other's answer and both are told it saved. Reading, checking and
    replacing are one turn instead: the owner gets one saved and one "the file changed", and only
    one of the two settings is in the file, because the other was refused before it wrote."""
    _, version = config_edit.read(cfg_file)
    slow = config_edit._backup
    monkeypatch.setattr(config_edit, "_backup",
                        lambda path: (slow(path), time.sleep(0.2)))
    started, outcomes = threading.Barrier(2), {}

    def save(who, **edits):
        started.wait()
        try:
            outcomes[who] = config_edit.save(cfg_file, version, **edits)
        except config_edit.Conflict:
            outcomes[who] = "refused"

    both = [threading.Thread(target=save, args=("ask",), kwargs={"ask": {"timeout_s": 300}}),
            threading.Thread(target=save, args=("pressure",),
                             kwargs={"pressure": {"grow_mib": 5000}})]
    for t in both:
        t.start()
    for t in both:
        t.join(timeout=30)
    assert list(outcomes.values()).count("refused") == 1, outcomes
    cfg = config.load(cfg_file)
    saved = [k for k in ("ask", "pressure") if k in cfg]
    assert saved == ["ask"] or saved == ["pressure"], saved


def test_every_save_copies_the_file_it_replaced_and_keeps_the_newest_ten(cfg_file, tmp_path,
                                                                        monkeypatch):
    """A save is the owner's own edit one click away from being lost, so the file it replaced goes
    beside it; the newest ten are enough to walk back an afternoon of trying orders without leaving
    a hundred copies of the same file in the config directory forever. The clock is frozen inside
    one second to make twelve saves happen at once: the stamp is only second resolution, and the
    counter behind it is what keeps the backups in the order they were written, which is what the
    rotation reads when it decides which one is oldest."""
    class OneSecond:
        @staticmethod
        def now():
            return datetime(2026, 10, 24, 8, 15, 0)

    monkeypatch.setattr(config_edit, "datetime", OneSecond)
    was = []
    for i in range(12):
        was.append(text_of(cfg_file))
        save(cfg_file, ask={"timeout_s": 121 + i})
    backups = sorted(tmp_path.glob("config.yaml.bak-*"))
    assert len(backups) == config_edit.BACKUP_KEEP
    assert all(b.name.startswith("config.yaml.bak-20261024-081500") for b in backups)
    assert [text_of(b) for b in backups] == was[-config_edit.BACKUP_KEEP:]


def test_a_save_that_changes_nothing_writes_and_backs_up_nothing(cfg_file, tmp_path):
    """Sending the page's current state when nothing was changed must not be a rewrite: a backup per
    click would push the ones that mean something out of the kept ten, and the file's version would
    change under anyone who had read it."""
    version = save(cfg_file, matrix={"strata": ALL_YES})
    _, now = config_edit.read(cfg_file)
    assert version == now == config_edit.version_of(text_of(EXAMPLE))
    assert list(tmp_path.glob("config.yaml.bak-*")) == []


def test_the_written_lists_are_read_back_as_the_same_policy(cfg_file):
    """What the page wrote and what the arbiter acts on have to be one thing: the file is the
    source, and a value that comes back other than how it went in is a policy the owner didn't
    choose."""
    grid = {"strata": {"comfy": "keep", "claims": "ask", "pressure": "keep"},
            "comfy": {"strata": "ask", "claims": "keep", "pressure": "yes"}}
    save(cfg_file, priorities=["strata", "comfy"], matrix=grid, ask={"timeout_s": 30},
         pressure={"grow_mib": 5000})
    cfg = config.load(cfg_file)
    workloads = {w.name: w for w in config.workloads(cfg, Context(gpu=lambda: {}))}
    for name in ("strata", "comfy"):
        w = workloads[name]
        for column in ("comfy", "strata", "claims", "pressure"):
            if column != name:
                assert policy.choice(w.yields_to, w.asks_for, column) == grid[name][column]
    assert workloads["strata"].priority == 10 and workloads["comfy"].priority == 90
    assert policy.ask_cfg(cfg.get("ask"))["timeout_s"] == 30
    assert cfg["pressure"]["grow_mib"] == 5000


def test_the_order_an_editor_chooses_becomes_priorities_with_room_left():
    """First gives way first, and the arbiter asks the lowest priority first, so first is the
    lowest number. The ends stop at 10 and 90: putting one workload above or below the others
    later shouldn't mean renumbering the whole file, and a middle workload lands on the default,
    which the writer leaves out."""
    assert policy.priorities_for(["comfy", "strata"]) == {"comfy": 10, "strata": 90}
    three = policy.priorities_for(["comfy", "unsloth", "strata"])
    assert three == {"comfy": 10, "unsloth": 50, "strata": 90}
    assert all(policy.PRIORITY_MIN <= v <= policy.PRIORITY_MAX
               for v in policy.priorities_for(list("abcdefgh")).values())


def test_a_new_top_level_block_is_separated_from_the_block_above_it(cfg_file):
    """`pressure:` isn't a real key of the shipped example, so a save that sets it writes one. The
    file keeps a blank line between its top-level blocks, and a new block that lands tight against
    the line above reads like a key of that block - which is the shape the owner has to be able to
    read, not just the parser."""
    before = text_of(cfg_file)
    assert not [ln for ln in before.splitlines() if ln == "pressure:"]
    save(cfg_file, pressure={"grow_mib": 5000}, ask={"timeout_s": 300})
    after = text_of(cfg_file).splitlines()
    for block, key in (("pressure:", "  grow_mib: 5000"), ("ask:", "  timeout_s: 300")):
        at = next(i for i, ln in enumerate(after) if ln == block)
        assert after[at - 1] == "", block
        assert after[at + 1] == key, block
    cfg = config.load(cfg_file)
    assert cfg["pressure"]["grow_mib"] == 5000 and cfg["ask"]["timeout_s"] == 300
