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
3. **Fast path:** `if seat.owns(NAME)`: forward. This is a small file read: no
   lock, no `nvidia-smi`, no HTTP. It stays true until anything else takes the
   GPU.
4. **Slow path:** `seat.acquire(NAME, wait=HOLD)`. It releases the other
   workloads (never interrupting their work; it waits up to `wait` for them) and
   returns. On `api.Busy` or `api.SwitchError`: answer 503 + `Retry-After` with
   the exception's message. Otherwise forward.
5. **Count it in flight** from just before you forward until the **last byte** of
   the response has gone to the client (for a stream, until the upstream body
   closes). Use a lock-protected counter.
6. **Stream** the response through as it arrives: don't buffer it, or streaming
   clients see nothing until the end. Request bodies are small: read them whole
   (you need the path anyway, sometimes the `model` field).

Two endpoints of its own, which vhotseat calls (step 4):

- `GET /_vhotseat/status` → `{"in_flight": N, "draining": bool}`.
- `POST /_vhotseat/drain?wait=S` → set draining (new "loads" requests wait),
  wait until `in_flight == 0` (up to S seconds), and answer 200 `{"in_flight": 0}`.
  If requests are still running after S: clear draining and answer **409**
  `{"status": "busy"}`: vhotseat then reports the workload busy instead of
  unloading under a running answer. Clear draining again when the model has been
  unloaded and the next request comes (or after a short timeout): a drained proxy
  that never reopens is a stuck proxy.

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
      ok_when: [{status: 200}]
    confirm: {vram_at_most: 0, timeout: 30}
    gate: {kind: hold}                   # the proxy is outside vhotseat; doctor can't check it
    allow_restart: true                  # if an unload doesn't free the VRAM
```

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
