# Building a proxy gate

This is a procedure for a coding agent (or a person) adding a GPU workload to
vhotseat when that workload has **no way to ask before it loads a model**:
Ollama, llama-server (llama.cpp), vLLM, LM Studio, most OpenAI-compatible
servers. vhotseat doesn't ship a proxy: every server differs in how it loads,
streams and unloads, and a proxy nobody here runs would be shipped untested.
You build one for your server, on vhotseat's library API, by following the
steps below. Each step ends with a check; don't go on until it passes.

Read [how-it-works.md](../how-it-works.md) §1 first if you haven't: the safety
rules (never interrupt work; refuse rather than load on top) are the point.

## 0. Do you need a proxy?

| The server… | Gate | Example |
| --- | --- | --- |
| can run a command before it loads a model | **hook**: run `vhotseat gate NAME`, treat non-zero as "don't load" | Strata (`before_load`) |
| has plugins that run before each job | **plugin**: call `vhotseat switch NAME` from one | ComfyUI (the guard node) |
| is only reached through its HTTP API | **proxy** (this guide) | Ollama, llama-server, vLLM |
| isn't a server (a training script) | **hold**: `vhotseat hold NAME -- CMD` | – |

Look for the first two before building a proxy: a hook or plugin covers every
client with no extra process. **Check:** you can name which row your server is
in, and why the rows above it don't apply.

## 1. Map the server's API

Find, from its docs and by trying it, and write down:

| Question | Ollama | llama-server |
| --- | --- | --- |
| Which requests load a model? | `/api/chat`, `/api/generate`, `/api/embed`, `/api/embeddings`, `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings` | In **router mode** (started without `-m`, with `--models-dir DIR`): any request naming a model loads it, unless started with `--no-models-autoload`; then only `POST /models/load {"model": M}`. With a single `-m` model: it's loaded at startup, always. |
| Which don't? (pass straight through) | `/api/tags`, `/api/ps`, `/api/show`, `/api/pull`, `/api/version`, … | `/health`, `/props`, `/v1/models`, `/slots`, … |
| How does a streaming answer end? | the response body closes (NDJSON lines, last has `"done": true`) | the body closes (SSE, last event `data: [DONE]`) |
| Is a model loaded right now? | `GET /api/ps` → `models` non-empty | router mode: `GET /models` → each model's `status.value` (`loaded`, `loading`, `unloaded`, `sleeping`). Single model: always, while the process runs |
| How do you unload it? | `POST /api/generate {"model": M, "keep_alive": 0}` for each model in `/api/ps` | router mode: `POST /models/unload {"model": M}`. Single model: only by stopping the process (`allow_restart` won't do: a restart reloads it) |
| Does it unload on its own? | yes, after `OLLAMA_KEEP_ALIVE` (set it to `-1` so vhotseat decides) | only with `--sleep-idle-seconds` (default off) |
| Which port do clients use? | 11434 | 8080 |

**Check:** you've loaded a model through one of the "loads" requests, seen it in
the "is loaded" answer and in `nvidia-smi`, unloaded it the way you wrote down,
and seen the VRAM go.

## 2. Move the server to an internal port

The proxy takes the port clients already use; the server moves behind it, so
no client changes. Ollama: `OLLAMA_HOST=127.0.0.1:11435` in its service
environment. llama-server: `--port 8081`. Keep the old setting (a `.pre-proxy`
copy of the unit file, or a note) so you can undo it.

**Check:** the server answers on the new port, and nothing answers on the old one
yet.

## 3. Build the proxy

A small HTTP server in Python (stdlib `http.server` with threads is enough for
one user; `aiohttp`/`httpx` if you want async), using `vhotseat.api`:

```python
from vhotseat import api

seat = api.open()          # ~/.config/vhotseat/config.yaml; NAME must be in it (step 4)
NAME = "ollama"
```

Per request:

1. **Not a "loads" request** (step 1's table): forward it unchanged.
2. **Draining** (see 4 below) is on: wait until it's off, up to your hold time
   (120 s is a good default), then answer **503** with `Retry-After: 30` and an
   error body in the server's own format (`{"error": "GPU busy: …"}` for Ollama).
3. **Fast path:** `if seat.owns(NAME)`: forward. While nothing has changed hands
   since it last checked, this is a small file read: no lock, no `nvidia-smi`,
   no HTTP. **On a cache miss it probes every workload, including this one, so it
   calls your own `/_vhotseat/status`**: never call `owns()` (or `acquire()`)
   while holding a lock your status handler needs, and run the HTTP server
   threaded, or a cold request waits on itself.
4. **Slow path:** `seat.acquire(NAME, wait=HOLD)`. It waits (without holding
   vhotseat's lock) for the other workloads' running work to finish, up to
   `wait`, then releases them; their work is never interrupted. On `api.Busy` or
   `api.SwitchError`: answer 503 + `Retry-After` with the exception's message.
   Otherwise forward. It doesn't load your server's model: the request you're
   about to forward does that (or, if your server needs an explicit load, do it
   here, after `acquire` returns).
5. **Count it in flight** from just before you forward until the **last byte** of
   the response has gone to the client (for a stream, until the upstream body
   closes). Use a lock-protected counter.
6. **Stream** the response through as it arrives: don't buffer it, or streaming
   clients see nothing until the end. Request bodies are small: read them whole
   (you need the path anyway, sometimes the `model` field).
7. **With Python's `http.server`:** use `ThreadingHTTPServer`, and set
   `disable_nagle_algorithm = True` on the handler. Without it, keep-alive
   clients wait ~40 ms per request (Nagle against delayed ACK), and step 6.4's
   < 1 ms fails.

Two endpoints of its own, which vhotseat calls (step 4):

- `GET /_vhotseat/status` → `{"in_flight": N, "draining": bool}`, plus
  `"loaded": bool` if your server's own status endpoint can't say it simply (the
  config's `resident:` can then read it from here). Count requests clients send
  straight to the server's internal port too, if the server can tell you (e.g.
  busy slots): they bypass the gate, but must still not be cut off.
- `POST /_vhotseat/drain?wait=S` → set draining (new "loads" requests wait),
  wait until `in_flight == 0` (up to S seconds), unload (if you do it here; see
  step 4), and answer 200. If requests are still running after S: answer **409**
  `{"status": "busy"}`, so vhotseat reports the workload busy instead of
  unloading under a running answer. **Either way, clear draining before you
  answer**: requests that waited then go back through `acquire()`, which waits
  for whoever now holds the GPU. A drain that never clears is a stuck proxy.
- If `vhotseat switch NAME` should leave the model loaded (warm), a
  `POST /_vhotseat/load` that loads it, for the config's `load:` (the `http`
  driver can only send an empty body).

**Check:** with the proxy running in a terminal, a chat request through the old
port works, streams token by token (watch with `curl -N`), and
`GET /_vhotseat/status` shows `in_flight: 1` during it and `0` after.

## 4. Add the workload to config.yaml

```yaml
workloads:
  ollama:
    driver: http
    label: Ollama
    url: http://127.0.0.1:11435           # the server itself, on its internal port
    service: {systemd: ollama}
    resident: {get: /api/ps, path: models.0.name}    # loaded = a model is listed
    busy: {get: http://127.0.0.1:11434/_vhotseat/status, path: in_flight}
    release:
      pre: {post: "http://127.0.0.1:11434/_vhotseat/drain?wait={wait}"}
      post: /api/generate                # see the note below
      always: true                       # see below
      timeout: 90                        # seconds the release request may take, on top of {wait}
      ok_when: [{status: 200}]
    confirm: {vram_at_most: 0, timeout: 30}
    gate:
      kind: proxy                        # doctor checks the proxy answers here
      status: http://127.0.0.1:11434/_vhotseat/status
    allow_restart: true                  # if an unload doesn't free the VRAM
```

**`release.always: true`** is needed for a proxied workload. Without it, when
nothing looks loaded vhotseat skips releasing it, and a request the proxy has
just let through (its model still loading, nothing on the GPU yet) isn't
drained.

Release needs a body with the model name (`{"model": M, "keep_alive": 0}`),
which the `http` driver can't send yet. Two ways:

- **Let the proxy do it:** have `/_vhotseat/drain` also unload (read `/api/ps`,
  send `keep_alive: 0` for each model, wait for `/api/ps` to be empty), and point
  `release.post` at a cheap request that always answers 200 (`/api/version`). The
  proxy knows the server best.
- **Write a Python driver** (a class like `vhotseat.drivers.comfyui.ComfyUI`),
  if the release logic is more than one request.

Then `vhotseat doctor`, and fix whatever it reports.

**Check:** `vhotseat status` lists the workload with the right state while you
load and unload it by hand.

## 5. Run the proxy as a service

A systemd unit, started before the server's clients and restarted on failure:

```ini
[Unit]
Description=vhotseat proxy for Ollama
After=ollama.service

[Service]
ExecStart=/usr/bin/python3 /path/to/ollama_proxy.py
Restart=on-failure

[Install]
WantedBy=default.target
```

**Check:** `systemctl --user status` shows it active, and it survives a restart of
the server behind it.

## 6. Test it

Every item must pass; each one is a real failure seen in this kind of setup.

1. **Exercise:** `vhotseat doctor --exercise NAME` passes (take, load, release,
   VRAM gone).
2. **Never interrupt the LLM:** start a long streamed answer through the proxy,
   then `vhotseat switch comfy --wait 300` in another terminal. The answer
   finishes; then ComfyUI gets the GPU.
3. **Never interrupt a render:** queue a ComfyUI job, then send a request through
   the proxy. It waits; if the render outlasts your hold time, it gets 503 with
   `Retry-After`. It never loads on top.
4. **Fast path:** 100 requests in a row while the workload owns the GPU add < 1 ms
   each (time them with and without the proxy).
5. **Burst:** 10 requests at once while another workload owns the GPU cause **one**
   handover (`events.jsonl` has one `switch` to NAME), not ten.
6. **Proxy killed mid-stream:** the client gets an error; `vhotseat status` is
   sane; the next request through the restarted proxy works.
7. **The full flow:** the round trip you care about (e.g. an agent on this LLM
   driving ComfyUI: LLM → render → LLM → render → LLM) completes.

## 7. Known traps

- **The server's own unload may cut a running answer off.** llama-server's
  `/models/unload` does (tested 2026-10-17). Always drain first.
- **llama-server in router mode** (`--models-dir`), the way to run it under
  vhotseat since it can unload without exiting:
  - start it with `--no-models-autoload`, or any request naming a model loads
    it, and so does any GET with `?autoload=true`: then force `autoload=false` on
    everything the proxy doesn't gate, and add `?autoload=true` to the requests
    it does, after `acquire()`;
  - `/models/load` and `/models/unload` answer 400 when there's nothing to do
    ("already running", "not running"): treat those as success;
  - `/models` says `loading` before any VRAM shows: `release.always` covers it;
  - clients written for a single-model server send no `model` field, which the
    router rejects (400 "model name is missing"): fill in the default model;
  - `/slots` and `/metrics` need `?model=`.
- **A trial config only half applies.** The ComfyUI guard and Strata's hook
  read the live config (`~/.config/vhotseat/config.yaml`); a new workload in
  another file is invisible to them, so a ComfyUI job can load on top of it.
  Test from your copy, but the new workload is only guarded once it's in the
  live config.

- **A client still on the internal port** bypasses the gate entirely. After
  step 2, look for anything configured with the old port number (`grep` the
  configs of your agents and UIs).
- **The server unloads on its own timer** (`OLLAMA_KEEP_ALIVE`): harmless, but
  the model reloads on the next request through the proxy; set it to forever so
  vhotseat decides.
- **Keep-alive connections** opened before a drain can carry a request after it.
  Count in-flight per request, not per connection, and check `draining` on every
  request, not once per connection.
- **Don't buffer responses.** Read request bodies whole; pass response bodies
  through chunk by chunk.
- **Retry-After matters.** Agent harnesses retry 503s with backoff; tell them when.
  Make sure your client's retry window is longer than a typical render (pi: 8
  retries ≤ 30 s apart, ~151 s).

## Appendix: Ollama, worked through

Step 1's Ollama column is the map. Steps 2–5 as above, with the
"let the proxy do it" release. The drain handler:

```python
def drain(wait):
    state.draining = True
    deadline = time.monotonic() + wait
    while state.in_flight and time.monotonic() < deadline:
        time.sleep(0.1)
    if state.in_flight:
        state.draining = False
        return 409, {"status": "busy", "in_flight": state.in_flight}
    for m in get(f"{UPSTREAM}/api/ps")["models"]:
        post(f"{UPSTREAM}/api/generate", {"model": m["name"], "keep_alive": 0})
    wait_until(lambda: not get(f"{UPSTREAM}/api/ps")["models"], 30)
    state.draining = False      # the next request reloads, through acquire()
    return 200, {"in_flight": 0}
```

Open question when this was written: does `keep_alive: 0` wait for a request
Ollama is answering, or cut it short? Draining first makes it moot for traffic
through the proxy, which is all traffic once step 7's first trap is handled.
