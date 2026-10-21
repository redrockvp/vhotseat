"""The generic `http` driver: a workload described by HTTP requests in the config.

    strata:
      driver: http
      url: http://127.0.0.1:2335
      resident: {get: /health, path: loaded}          # loaded = this field is true
      busy:     {get: /status, path: busy}            # optional; else learned from release
      release:
        pre: {post: http://127.0.0.1:11434/_vhotseat/drain?wait={wait}}   # optional:
                                                      # first, e.g. a proxy stops forwarding
        post: /unload?wait={wait}
        always: true                                  # ask even when it looks unloaded
        busy_when: [{status: 409}, {path: status, in: [busy]}]
        ok_when:   [{path: status, in: [unloaded, not loaded]}]
      confirm: {vram_at_most: 0, timeout: 30}         # default: its VRAM ≤ idle_vram_mib
      load: {post: /load, timeout: 300}               # optional
      model: {get: /health, path: model}              # optional: the model it serves,
                                                      # for admission.by_model
      headers: {Authorization: "Bearer ${SOME_KEY}"}  # optional: sent with every request
                                                      # for this workload ($NAME reads the
                                                      # environment: a key doesn't belong
                                                      # in a file that gets pasted around)
      body: {model_path: "{model}"}                  # optional: JSON body for a POST, with
                                                      # {model} (from `model:` above) and
                                                      # {wait} substituted in its values
      estimate:                                       # optional: ask the server what one of
        from: {get: /settings/last-model}              # its models would cost on the card.
        post: /estimate-memory                         #   `from:` reads the fields that name
        body: {model_path: "{id}", n_ctx: "{ctx}"}      #   the model, {ctx} comes from ctx:
        ctx: 32768
        path: gpu_bytes                                # reported, never enforced

Paths are dotted (`a.b.0.c`; a leading `$.` is allowed). `{wait}` in a URL is
replaced by the seconds the release may wait for running work. A request path
starting with http:// goes to that address instead of `url` (a proxy in front
of the server, say).
"""

from dataclasses import dataclass, field
import os
import re
import time

from .. import util
from ..errors import Busy, SwitchError
from .base import Workload

MISSING = object()
ESTIMATE_TTL_S = 60.0


def lookup(body, path: str):
    """`a.b.0.c` (or `$.a.b[0].c`) in parsed JSON, MISSING if absent."""
    cur = body
    for part in path.removeprefix("$.").replace("[", ".").replace("]", "").split("."):
        if not part:
            continue
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return MISSING
    return cur


def matches(rule: dict, code, body) -> bool:
    """One `*_when` rule: {status: 409}, {path: x}, {path: x, in: [...]} or {path: x, equals: v}."""
    if "status" in rule and code != rule["status"]:
        return False
    if "path" in rule:
        value = lookup(body, rule["path"])
        if "in" in rule:
            return value in rule["in"]
        if "equals" in rule:
            return value == rule["equals"]
        return value is not MISSING and bool(value)
    return True


def _request(spec: dict):
    method = "POST" if "post" in spec else "GET"
    return method, spec.get("post") or spec.get("get")


_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def expand(value: str) -> tuple[str, list[str]]:
    """$NAME / ${NAME} from the environment, and which names were missing. A header can
    name the key without the key itself living in a file that gets shared, pasted and
    committed; if the name isn't in the environment, nothing is invented for it."""
    missing: list[str] = []

    def sub(m):
        name = m.group(1) or m.group(2)
        if name not in os.environ:
            missing.append(name)
        return os.environ.get(name, "")

    return _ENV.sub(sub, value), missing


@dataclass
class HttpWorkload(Workload):
    spec: dict = field(default_factory=dict)

    def __post_init__(self):
        self.health_path = _request(self.spec["resident"])[1]
        self.release_always = bool(self.spec["release"].get("always", False))
        self.headers_missing: list[str] = []
        self._estimate: dict | None = None
        self._estimate_at = 0.0

    def _call(self, spec: dict, timeout: float = 5.0, **fmt):
        method, path = _request(spec)
        body = None
        if method == "POST":
            body = dict(spec.get("body") or {})
            if any("{model}" in str(v) for v in body.values()):
                # {model} is what this workload's `model:` says it's serving: unloading the
                # model it names is safer than one that guesses. Whether an unloaded model
                # should be asked at all is the caller's decision (see release).
                body = {k: (v.replace("{model}", self.model() or "") if isinstance(v, str) else v)
                        for k, v in body.items()}
            body = {k: (v.format(**fmt) if isinstance(v, str) else v) for k, v in body.items()}
        headers, self.headers_missing = self._headers()
        url = path if path.startswith(("http://", "https://")) else self.url + path
        return util.http(method, url.format(**fmt), body, timeout=timeout, headers=headers)

    def _headers(self) -> tuple[dict, list[str]]:
        out, missing = {}, []
        for k, v in (self.spec.get("headers") or {}).items():
            value, miss = expand(str(v))
            out[k], missing = value, missing + miss
        return out, sorted(set(missing))

    def probe(self, gpu: dict) -> dict:
        st = {"service": self.service_active(), "reachable": False, "loaded": False}
        if st["service"]:
            code, body = self._call(self.spec["resident"])
            if code == 200:
                value = lookup(body, self.spec["resident"]["path"])
                st.update(reachable=True, loaded=value is not MISSING and bool(value))
            elif code in (401, 403):
                # Not "not answering": it is answering, and refusing. Blurred together, a
                # key that rotated looks like a server that's down, and the handover
                # starts blaming the wrong thing.
                st["auth"] = self.headers_missing or True
        if self.spec.get("estimate"):
            st["sized"] = self.estimate()
        return st

    def active(self, st: dict, gpu: dict) -> bool:
        # The GPU is the tiebreaker if its API is unreachable but it's on the card.
        return st["loaded"] or gpu[f"{self.name}_mib"] > self.idle_vram_mib

    def describe(self, st: dict) -> str:
        if st.get("auth"):
            miss = st["auth"]
            return ("asks for a key it isn't being sent (401): fix `headers:`"
                    + (f" - ${', $'.join(miss)} isn't in the environment" if isinstance(miss, list) else ""))
        return (super().describe(st) or ("model loaded" if st["loaded"] else "model unloaded")) + self._sized_note(st)

    def _sized_note(self, st: dict) -> str:
        sized = st.get("sized") or {}
        if not sized.get("bytes"):
            return ""
        return f"; {str(sized.get('model') or '').rsplit('/', 1)[-1]} sizes at {sized['bytes'] // 2**20} MiB"

    def model(self) -> str | None:
        spec = self.spec.get("model")
        if not spec or not self.service_active():
            return None
        code, body = self._call(spec)
        value = lookup(body, spec["path"]) if code == 200 else MISSING
        return value if isinstance(value, str) and value else None

    def estimate(self) -> dict | None:
        """What this workload's own API says one of its models would cost on the card, asked
        of the server that knows it: weights, KV cache at a given context, whether the model
        is even downloaded. Reported, never enforced: a server fits a model by shrinking its
        context or spilling into RAM, which is a call its owner sometimes wants to make, and
        the number is what makes that call visible before the weights land. Cached, because
        a sampler asks every few seconds and a sizing question costs a round trip.
        """
        spec = self.spec.get("estimate")
        if not spec or not self.service_active():
            return None
        now = time.monotonic()
        if self._estimate is not None and now - self._estimate_at < ESTIMATE_TTL_S:
            return self._estimate
        self._estimate, self._estimate_at = self._ask_estimate(spec), now
        return self._estimate

    def _ask_estimate(self, spec: dict) -> dict | None:
        """One sizing question: read the fields that name the model (`from:`), fill them into
        the body, ask. A placeholder with nothing behind it drops its key rather than sending
        the placeholder text as a value."""
        fields: dict = {}
        if spec.get("from"):
            code, body = self._call(spec["from"])
            if code == 200 and isinstance(body, dict):
                fields = {k: v for k, v in body.items()
                          if v is None or isinstance(v, (str, int, float, bool))}
        if "ctx" in spec:
            fields["ctx"] = spec["ctx"]
        body = {}
        for field_name, template in (spec.get("body") or {}).items():
            if not isinstance(template, str):
                body[field_name] = template
                continue
            names = re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", template)
            if any(fields.get(n) in (None, "") for n in names):
                continue
            if len(names) == 1 and template.strip() == "{%s}" % names[0] \
                    and isinstance(fields[names[0]], (int, float)):
                body[field_name] = fields[names[0]]   # the field's own type: "32768" is not 32768
                continue
            body[field_name] = re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: str(fields[m.group(1)]), template)
        if not body:
            return None
        code, out = self._call({"post": spec["post"], "body": body})
        if code != 200 or not isinstance(out, dict):
            return None
        value = lookup(out, spec["path"]) if spec.get("path") else MISSING
        return {"model": body.get("model_path") or body.get("model"),
                "bytes": value if isinstance(value, (int, float)) else None,
                "n_ctx": out.get("n_ctx"), "weights_bytes": out.get("weights_bytes"),
                "gpu_bytes": out.get("gpu_bytes"), "available": out.get("available"),
                "reason": out.get("reason")}

    def _vram_limit(self) -> int:
        """The VRAM at or below which this workload counts as holding nothing on the card."""
        return self.spec.get("confirm", {}).get("vram_at_most", self.idle_vram_mib)

    def busy(self) -> str | None:
        spec = self.spec.get("busy")
        if not spec or not self.service_active():
            return None
        code, body = self._call(spec)
        if code != 200:
            # Unknown counts as busy, unless it plainly holds nothing: an API that isn't
            # answering is a workload that isn't running, provided the GPU counters can
            # attribute it (vram.process) and show it off the card.
            if (held := self.vram_mib()) > self._vram_limit():
                return (f"{self.label}'s API isn't answering and it holds {held} MiB on the GPU, "
                        "so whether it's busy is unknown")
            return None
        value = lookup(body, spec["path"])
        return f"{self.label} is busy" if value not in (MISSING, False, 0, None) else None

    def release(self, wait: float, timeout: float) -> list[str]:
        spec = self.spec["release"]
        if not self.reachable():
            # Its API is down. With no service to start it there is nothing to release
            # and nothing to wait for, and stalling on a dead port would leave every
            # other workload unable to take the GPU - closing the program would be
            # punished for a minute. Safe only while the GPU counters can see it: if it
            # is still on the card something is stuck, so name that instead of guessing.
            if (held := self.vram_mib()) > self._vram_limit():
                raise SwitchError(f"{self.label} is not answering and still holds {held} MiB on the GPU; "
                                  "start it so vhotseat can release it, or kill the process")
            util.log(f"{self.label} is not answering and holds nothing on the GPU: nothing to release")
            return []
        if any("{model}" in str(v) for v in (spec.get("body") or {}).values()):
            # A release that names the model it means. With nothing served there is nothing
            # to name, and an empty id is either a 422 or a success that did nothing: either
            # way every handover passing through this workload would die on it.
            if not (self.model() or ""):
                if not self.loaded_now():
                    util.log(f"{self.label} has no model loaded: nothing to release")
                    return []
                raise SwitchError(f"{self.label} reports a model loaded but `model:` can't name it: "
                                  "point `model:` at the field that says which model it is")
        if pre := spec.get("pre"):
            # e.g. a proxy: stop forwarding new requests, let running ones finish (up to wait).
            code, body = self._call(pre, timeout=wait + 30, wait=int(wait))
            if code == 409 or (code is not None and code >= 500 and lookup(body, "status") == "busy"):
                raise Busy(f"{self.label} is still answering (use --wait SECONDS to let it finish)")
            if code != 200:
                raise SwitchError(f"{self.label} {_request(pre)[1].split('?')[0]} failed: {code} {body}")
        code, body = self._call(spec, timeout=wait + spec.get("timeout", 30), wait=int(wait))
        if any(matches(r, code, body) for r in spec.get("busy_when", [])):
            raise Busy(f"{self.label} is answering or loading (use --wait SECONDS to let it finish)")
        ok = spec.get("ok_when") or [{"status": 200}]
        if not any(matches(r, code, body) for r in ok):
            raise SwitchError(f"{self.label} {_request(spec)[1].split('?')[0]} failed: {code} {body}")
        confirm = self.spec.get("confirm", {})
        util.wait_until(lambda: self.vram_mib() <= self._vram_limit(), confirm.get("timeout", 30),
                        f"{self.label}'s engine to leave the GPU")
        return []

    def health_call(self, timeout: float = 5.0):
        """Its own resident probe, with its own headers: a workload that answers 401 to a
        bare GET is not a workload that is down, and reading it that way sends every
        diagnosis off looking for a dead port."""
        return self._call(self.spec["resident"], timeout=timeout)

    def loaded_now(self) -> bool:
        """Whether its own API says something is loaded, asked directly. A release that
        would name nothing is skipped only while this says nothing is loaded too."""
        code, body = self._call(self.spec["resident"])
        if code != 200:
            return False
        # MISSING is a truthy object(): an absent path is "nothing loaded", not "something
        # loaded that we can't name". Reading it as the latter made every handover away from
        # the workload fail, because release() then insisted on naming a model that wasn't there.
        value = lookup(body, self.spec["resident"]["path"])
        return value is not MISSING and bool(value)

    def can_load(self) -> bool:
        return "load" in self.spec

    def load(self, wait: float) -> None:
        spec = self.spec["load"]
        util.log(f"loading {self.label}'s model")
        # Synchronous: answers once it's up (Strata's cold load reads ~33 GB of experts).
        code, body = self._call(spec, timeout=max(spec.get("timeout", 300), wait))
        if code != 200:
            raise SwitchError(f"{self.label} {_request(spec)[1]} failed: {code} {body}")
