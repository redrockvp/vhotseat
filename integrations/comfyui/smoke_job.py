#!/usr/bin/env python3
"""Queue one tiny ComfyUI job (1-step SDXL ending in PreviewImage, so nothing
is written to output/) and print how it ended. Used by docs/comfyui-updates.md.

    smoke_job.py --ckpt NAME [--no-wait] [--url URL]
"""
import argparse
import json
import os
import time
import urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--no-wait", action="store_true", help="queue and exit")
ap.add_argument("--ckpt", required=True, help="an SDXL checkpoint installed in ComfyUI")
ap.add_argument("--url", default=os.environ.get("COMFY_URL", "http://127.0.0.1:8188"),
                help="ComfyUI's address (default: $COMFY_URL, else http://127.0.0.1:8188)")
args = ap.parse_args()
URL = args.url.rstrip("/")

wf = {
    "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": args.ckpt}},
    "2": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": "a teapot"}},
    "3": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["1", 1], "text": "blurry"}},
    "4": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}},
    "5": {"class_type": "KSampler", "inputs": {
        "model": ["1", 0], "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0],
        "seed": int(time.time()), "steps": 1, "cfg": 5, "sampler_name": "euler",
        "scheduler": "normal", "denoise": 1}},
    "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
    "7": {"class_type": "PreviewImage", "inputs": {"images": ["6", 0]}},
}
req = urllib.request.Request(f"{URL}/prompt", json.dumps({"prompt": wf}).encode(),
                             {"Content-Type": "application/json"})
t0 = time.time()
pid = json.load(urllib.request.urlopen(req))["prompt_id"]
print(f"queued {pid}", flush=True)
while not args.no_wait:
    h = json.load(urllib.request.urlopen(f"{URL}/history/{pid}"))
    if pid in h:
        st = h[pid]["status"]
        err = [m[1].get("exception_message") for m in st.get("messages", []) if m[0] == "execution_error"]
        print(f"{st['status_str']} in {time.time() - t0:.1f}s" + (f": {err[0]}" if err else ""))
        break
    time.sleep(0.5)
