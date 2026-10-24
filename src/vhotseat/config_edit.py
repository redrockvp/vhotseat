"""Changing the owner's policy in their own config file: two lines of diff, not a rewrite.

The file is the owner's. It is hand-written and commented, and the comments are the notes they
wrote for themselves - `config.example.yaml` ships with a third of its lines explaining a key
nobody has set. So a save has to be the smallest change that says what the owner just decided:
only the policy keys (`priority`, `yields_to`, `asks_for`, `ask`, `pressure`), the comments and
key order where they were, the resulting whole file validated before anything touches the disk,
a timestamped backup kept beside it, and an out-of-date version refused instead of quietly
overwritten. Someone who opens the file after a save should be able to read what changed.

`ruamel.yaml` in round-trip mode reads it, because it is the only parser here that knows where a
key is and what comment sits next to it. What it *writes* is another matter: dumping the document
re-renders flow styles it didn't write (the first `{…}` of a flow sequence loses its braces) and
puts an appended item wherever its comment bookkeeping leaves it - in this file, the notes
introducing `patches:` appearing above a workload's new keys. So the tree locates and the write
replaces only the lines of the keys being edited; every other line comes back byte for byte. The
one comment a save can cost is a note written inside the value of the very key being replaced.

Nothing here decides what the policy means: `policy.py` is the vocabulary and `config.py` is the
validator. What this module adds is the arithmetic that turns an editor's grid into the two lists
per workload, and the care around the write.
"""

import hashlib
import io
import os
import tempfile
from datetime import datetime
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedSeq

from . import policy, pressure
from .config import ConfigError, _check, parse

# Backups kept beside the config file: enough that "what was my order before I started fiddling"
# reaches back over a session of saving, few enough that a year of dashboards doesn't leave a
# hundred copies of a 100-line file behind.
BACKUP_KEEP = 10
# A key the edit removes rather than writes: what it would hold is the default, and an absent key
# says the default without filling the file with lines that say nothing.
DROP = object()
# The per-workload keys this module will touch. Everything else in a workload block - its url, its
# gate, its release requests - isn't a policy key and is never written from here.
WORKLOAD_KEYS = ("priority", "yields_to", "asks_for")
TOP_KEYS = ("ask", "pressure")
BLOCK_KEYS = {"ask": tuple(policy.ASK_DEFAULTS), "pressure": pressure.KEYS}


def version_of(text: str) -> str:
    """What a reader sends back to prove the file is still the one it read."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Conflict(Exception):
    """The file isn't the one that was read: hand-edited since, or saved by someone else first.
    Nothing was written, and the version now on disk comes with the message, so the caller
    re-reads instead of guessing."""

    def __init__(self, message: str, version: str):
        super().__init__(message)
        self.version = version


def read(path) -> tuple[str, str]:
    """(the file's text, its version) - the text with its own line endings, untranslated: a
    config written on Windows is CRLF, and a save that turned every ending into LF would show the
    owner a diff of the whole file for one changed key, and change the version of a file nobody
    edited."""
    try:
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
    except UnicodeDecodeError:
        raise ConfigError(f"{path}: not utf-8 text") from None
    except OSError as e:
        raise ConfigError(f"{path}: {e.strerror or e}") from None
    return text, version_of(text)


def policy_edits(cfg: dict, priorities=None, matrix=None, ask=None, pressure_cfg=None) -> dict:
    """What to write, as config-key paths, from what an editor posted.

    `priorities` is the unload order, first gives way first; `matrix` is the grid, one row per
    workload that can hold the card and one choice per column; `ask` and `pressure` are the two
    top-level blocks. Each is skipped when it's None - a page that sends only the matrix must not
    reset the pressure numbers it was never shown.

    Two things are settled here rather than by the page, so that what lands in the file is the
    owner's answer and not the grid's: a row that makes room for everything is written
    `yields_to: ["*"]`, which is the same answer the arbiter reads and the one that survives a
    workload being added later; and a key back at its default is dropped rather than written out.
    A row that asks anything writes both keys, because `["*"]` beside `asks_for:` is the pair that
    means "everyone, except a game, which asks first".
    """
    wls = cfg.get("workloads") or {}
    edits: list[list] = []
    if priorities is not None:
        _check(isinstance(priorities, list), "priorities",
               "expected the unload order: every workload name once, first gives way first")
        twice = sorted({n for n in priorities if list(priorities).count(n) > 1})
        _check(not twice, "priorities", "named twice: " + ", ".join(map(repr, twice)))
        unknown = sorted(n for n in priorities if n not in wls)
        _check(not unknown, "priorities",
               "no workload named " + ", ".join(map(repr, unknown)) + "; configured: "
               + (", ".join(sorted(wls)) or "none"))
        missing = sorted(n for n in wls if n not in priorities)
        _check(not missing, "priorities", "says nothing about " + ", ".join(missing)
               + ": name every workload once, or send no priorities at all")
        for name, value in policy.priorities_for(priorities).items():
            edits.append([["workloads", name, "priority"],
                          DROP if value == policy.DEFAULT_PRIORITY else value])
    if matrix is not None:
        _check(isinstance(matrix, dict), "matrix", "expected {workload: {requester: choice}}")
        for name, row in matrix.items():
            _check(name in wls, "matrix", f"no workload named {name!r}; configured: "
                                          f"{', '.join(sorted(wls)) or 'none'}")
            _check(isinstance(row, dict) and row, f"matrix.{name}", "expected a choice for each "
                                                                    "of the other workloads, "
                                                                    "claims and pressure")
            for key, value in _grid_row(row, name, wls).items():
                edits.append([["workloads", name, key], value])
    # In the order the file writes them: the pressure numbers, then the ask, which is the order
    # the example explains them.
    for top, values, defaults in (("pressure", pressure_cfg, pressure.DEFAULTS),
                                  ("ask", ask, policy.ASK_DEFAULTS)):
        if values is None:
            continue
        _check(isinstance(values, dict), top, f"expected some of {', '.join(sorted(defaults))}")
        keys = {}
        for k, v in values.items():
            _check(k in defaults, f"{top}.{k}", "unknown key; expected some of "
                                                + ", ".join(sorted(defaults)))
            keys[k] = DROP if _same(v, defaults[k]) else v
        edits.append([[top], keys])
    return {"edits": edits}


def _same(value, default) -> bool:
    """Equality against a default, with `[]` and `()` the same answer: `ignore: []` says what no
    `ignore:` says."""
    if isinstance(value, (list, tuple)) and isinstance(default, (list, tuple)):
        return list(value) == list(default)
    return value == default


def _grid_row(row: dict, name: str, wls: dict) -> dict:
    """One grid row as the two config keys it says. The columns are every other workload plus
    claims and pressure, and a missing cell is refused rather than filled in from the current
    config: that would write an answer for a column the owner was about to click."""
    columns = [n for n in wls if n != name] + [policy.CLAIMS, policy.PRESSURE]
    choices: dict[str, str] = {}
    for col, choice in row.items():
        _check(col != name, f"matrix.{name}", "a workload doesn't unload itself: there is no "
                                              f"{name} column here")
        _check(col in columns, f"matrix.{name}.{col}",
               "expected one of " + ", ".join(columns))
        if isinstance(choice, dict):
            choice = choice.get("choice")
        _check(choice in policy.CHOICES, f"matrix.{name}.{col}",
               f"expected one of {', '.join(policy.CHOICES)}, got {choice!r}")
        choices[col] = choice
    missing = [c for c in columns if c not in choices]
    _check(not missing, f"matrix.{name}", "no choice for " + ", ".join(missing))
    order = [c for c in wls if c != name] + [policy.CLAIMS, policy.PRESSURE]
    yes = [c for c in order if choices[c] == "yes"]
    asks = [c for c in order if choices[c] == "ask"]
    keeps = [c for c in order if choices[c] == "keep"]
    if not asks and not keeps:
        return {"yields_to": DROP, "asks_for": DROP}
    return {"yields_to": ["*"] if not keeps else yes,
            "asks_for": asks or DROP}


def apply(text: str, edits: dict) -> str:
    """The file with the policy keys changed and every other line left exactly as it was.

    The result is parsed and validated as a whole config before it's returned, so what's checked
    is the file as it would be rather than the keys in isolation, and the error is the line
    `doctor` prints.
    """
    doc = _Doc(text)
    for path, value in edits["edits"]:
        _check(len(path) >= 1, "edits", "expected a config key path")
        if path[0] == "workloads":
            _check(len(path) == 3 and path[2] in WORKLOAD_KEYS, ".".join(path),
                   f"not a policy key; this writes {'/'.join(WORKLOAD_KEYS)} of a workload only")
        else:
            _check(path[0] in TOP_KEYS, ".".join(path),
                   f"not a policy block; this writes {', '.join(TOP_KEYS)}")
            if len(path) > 1:
                _check(path[1] in BLOCK_KEYS[path[0]], ".".join(path),
                       "unknown key; expected some of " + ", ".join(BLOCK_KEYS[path[0]]))
        if len(path) == 1 and isinstance(value, dict):
            doc.set_block(path, value)
        else:
            doc.set(path, value)
    out = doc.text()
    try:
        parse(out)
    except ConfigError as e:
        raise ConfigError(str(e)) from None
    return out


def save(path, version: str, *, priorities=None, matrix=None, ask=None, pressure=None) -> str:
    """Apply an editor's changes to the config file and return the new version.

    Nothing is written unless the whole resulting file is a config, and nothing is written when
    `version` isn't what's on disk now: a page opened before the owner hand-edited the file must
    not paint over what they typed. The file being replaced is copied beside it first, newest ten
    kept.

    Raises Conflict (carrying the version now on disk) or ConfigError (carrying the message
    doctor prints).
    """
    path = Path(path)
    text, current = read(path)
    if version != current:
        raise Conflict(f"{path}: changed since you read it - nothing was written", current)
    cfg = parse(text)
    edits = policy_edits(cfg, priorities, matrix, ask, pressure)
    try:
        new_text = apply(text, edits)
    except ConfigError as e:
        # The line `doctor` prints for this file, and nothing written: the owner's own
        # validation, at the moment they'd otherwise lose their config.
        raise ConfigError(f"{path}: {e}") from None
    if new_text != text:
        _backup(path)
        _write(path, new_text)
    return version_of(new_text)


def _backup(path: Path) -> None:
    """Copy the file to `config.yaml.bak-YYYYMMDD-HHMMSS` beside it, keeping the newest ten."""
    target = _next_backup_name(path, datetime.now().strftime("%Y%m%d-%H%M%S"))
    target.write_bytes(path.read_bytes())
    for old in sorted(path.parent.glob(f"{path.name}.bak-*"))[:-BACKUP_KEEP]:
        old.unlink(missing_ok=True)


def _next_backup_name(path: Path, stamp: str) -> Path:
    """An unused backup name for this second: the plain stamp, then `.01`, `.02`, - padded, and
    one past the highest already there. Counting or taking the first free slot would be wrong,
    because the rotation deletes the oldest first: a freed slot at the front of the sequence would
    be handed to the newest save, and the name is the write order - what the rotation keeps the
    newest of is sorted by it, so a save that sorts out of order is a save whose backup gets
    deleted instead of the one it was meant to replace."""
    taken = {p.name for p in path.parent.glob(f"{path.name}.bak-{stamp}*")}
    if not taken:
        return path.with_name(f"{path.name}.bak-{stamp}")
    prefix = f"{path.name}.bak-{stamp}"
    n = max(_suffix(name, prefix) for name in taken) + 1
    name = f"{prefix}.{n:02}"
    while name in taken:
        n += 1
        name = f"{prefix}.{n:02}"
    return path.with_name(name)


def _suffix(name: str, prefix: str) -> int:
    """The counter on a backup name: 0 for the plain stamp, 1 for `.01`."""
    tail = name[len(prefix):]
    return int(tail[1:]) if tail.startswith(".") else 0


def _write(path: Path, text: str) -> None:
    """Replace the file atomically: the owner's editor, or a `watch` reloading it, must never see
    half a config, which is a config error and a refused load."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# --- writing the lines --------------------------------------------------------------

class _Doc:
    """The file as text, plus a ruamel round-trip read of it for where each key is and in what
    order they sit. A value is written back over the lines of the value it replaces."""

    def __init__(self, text: str):
        self.newline = "\r\n" if "\r\n" in text else "\n"
        self.lines = text.splitlines()
        self.data = _yaml().load(text)
        self.patches: list[tuple[int, int, list[str]]] = []

    def node(self, path: list[str]):
        out = self.data
        for i, key in enumerate(path):
            _check(isinstance(out, dict) and key in out, ".".join(path),
                   f"no {'.'.join(path[:i + 1])} in the config as it stands now")
            out = out[key]
        return out

    def set(self, path: list[str], value) -> None:
        """One key of the file: over its own line when it has one, otherwise at the end of the
        block it belongs to."""
        parent, key = self.node(path[:-1]), path[-1]
        _check(isinstance(parent, dict), ".".join(path[:-1]) or "config", "expected a mapping")
        where = _where(parent)
        if key in where:
            line, col = int(where[key][0]), int(where[key][1])
            if value is DROP:
                # A key whose note the owner wrote stays: dropping the key would drop their
                # comment with it, and a value already at the default decides nothing either way.
                if _eol_comment(self.lines[line]):
                    return
                self.patches.append((line, _extent(self.lines, line, col) + 1, []))
                return
            self.patches.append((line, _extent(self.lines, line, col) + 1,
                                 _value_lines(key, value, col, self.lines[line])))
            return
        if value is DROP or (isinstance(value, dict) and not
                             [v for v in value.values() if v is not DROP]):
            return
        after, col = self._new_key_spot(path[:-1])
        lines = _value_lines(key, value, col)
        if col == 0 and after >= 0 and self.lines[after].strip():
            # A new top-level block gets the blank line the file puts between its blocks: one
            # written tight against the line above reads as another key of that block, and the
            # owner finds out when they open the file to check what the page saved.
            lines = [""] + lines
        self.patches.append((after + 1, after + 1, lines))

    def set_block(self, path: list[str], values: dict) -> None:
        """A whole block: its keys one at a time, so the ones this save isn't changing keep their
        lines and their comments. When every key of an existing block falls back to its default and
        the edit covers all of them, the block heading goes with them rather than being left as a
        heading over nothing - unless one of those keys carries a note the owner wrote, in which
        case the lines stay and the heading has to stay above them."""
        if path[-1] not in _where(self.node(path[:-1])):
            if any(v is not DROP for v in values.values()):
                self.set(path, {k: v for k, v in values.items() if v is not DROP})
            return
        where = _where(self.node(path))
        annotated = [k for k, pos in where.items() if _eol_comment(self.lines[int(pos[0])])]
        if all(v is DROP for v in values.values()) and set(where) <= set(values) and not annotated:
            self.set(path, DROP)
            return
        for k, v in values.items():
            self.set(path + [k], v)

    def _new_key_spot(self, path: list[str]) -> tuple[int, int]:
        """(last line of the block, column for a new key) for the mapping at `path`: after its
        last key and everything that key's value spans, so a new key lands inside the block and
        never on top of the comments that introduce whatever follows it."""
        node = self.node(path)
        where = _where(node)
        if where:
            last = max(where.values(), key=lambda v: int(v[0]))
            col = min(int(v[1]) for v in where.values())
            return _extent(self.lines, int(last[0]), int(last[1])), col
        # An empty mapping: the line the mapping itself was written on, or the top of the file.
        parent = self.node(path[:-1]) if path else self.data
        own = _where(parent).get(path[-1]) if path else None
        if own is None:
            return len(self.lines) - 1, 0
        return _extent(self.lines, int(own[0]), int(own[1])), int(own[1]) + 2

    def text(self) -> str:
        out = list(self.lines)
        # Two keys new to the same block land at the same spot: they go in the order they were
        # asked for (priority, then yields_to, then asks_for), not in the order the edits happen to
        # have been listed, and the whole set is applied from the bottom of the file up so the
        # earlier line numbers still point where they did.
        for start, end, lines in _merge(self.patches):
            out[start:end] = lines
        return self.newline.join(out) + self.newline


def _merge(patches: list[tuple[int, int, list[str]]]) -> list[tuple[int, int, list[str]]]:
    """Patches that replace the same span, joined in the order they were made, sorted by where
    they go, last first."""
    merged: dict[tuple[int, int], list[str]] = {}
    for start, end, lines in patches:
        merged.setdefault((start, end), []).extend(lines)
    return [(start, end, lines) for (start, end), lines in
            sorted(merged.items(), key=lambda kv: -kv[0][0])]


def _yaml() -> YAML:
    y = YAML(typ="rt")
    # The file's comments run long; the default 80-column dump would fold them and still call
    # that preserving the comment.
    y.width = 4092
    return y


def _where(node) -> dict:
    """{key: [line, column, …]} for a round-trip mapping: where its keys are in the file."""
    lc = getattr(node, "lc", None)
    return getattr(lc, "data", {}) or {}


def _extent(lines: list[str], start: int, col: int) -> int:
    """The last line of the value that begins on `start`: every following line indented deeper
    than the key. A line at the key's column or shallower starts something else, and blank lines
    in between belong to neither side."""
    end, i = start, start + 1
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        if _col(lines[i]) > col:
            end, i = i, i + 1
            continue
        break
    return end


def _col(line: str) -> int:
    return len(line) - len(line.lstrip())


def _eol_comment(line: str) -> str:
    """The `# …` at the end of a line, or "": the owner's note about the key, which a value
    change keeps. The scan respects quotes so a `#` inside a URL or a string stays part of it."""
    stripped = line.rstrip()
    quoted = ""
    for i, ch in enumerate(stripped):
        if quoted:
            if ch == quoted:
                quoted = ""
        elif ch in "\"'":
            quoted = ch
        elif ch == "#" and (i == 0 or stripped[i - 1] in " \t"):
            return stripped[i:]
    return ""


def _dump(value) -> str:
    """A value as YAML text on one line - `[comfy, claims]`, `["*"]`, `120`, `keep` - written by
    the same round-trip dumper that read the file, so the quoting a `*` or a `yes` needs is
    whatever the parser wants rather than a guess made here. A list is written the way the owner
    writes one: on a line, in brackets."""
    y = _yaml()
    if isinstance(value, list):
        seq = CommentedSeq(list(value))
        seq.fa.set_flow_style()
        value = seq
    buf = io.StringIO()
    y.dump({"v": value}, buf)
    text = buf.getvalue().strip()
    _check(text.startswith("v: "), "value", f"can't write {value!r} on one line")
    return text[len("v: "):]


def _value_lines(key: str, value, col: int, original: str = "") -> list[str]:
    """The line(s) for `key` at column `col`. A note the owner left at the end of the line being
    replaced stays: at its own column while the new value doesn't reach it, one space past the
    value when it does, because a comment that drifts under the wrong key is worse than one that
    isn't lined up."""
    if value is DROP:
        return []
    indent = " " * col
    if isinstance(value, dict):
        out = [f"{indent}{key}:"]
        for k, v in value.items():
            out += _value_lines(k, v, col + 2)
        return out
    line = f"{indent}{key}: {_dump(list(value) if isinstance(value, list) else value)}"
    note = _eol_comment(original)
    if note:
        was = original.index(note)
        line += " " * (max(2, was - len(line)) if was > len(line) else 2) + note
    return [line]
