"""Drive the REAL locustfile.AssistantUser against a stub frontend /bot.

The stub fails checkouts whose message names a currency in `FAIL_CURRENCIES`
(standing in for the v2 regression) and records every request's headers/body so
we can assert on conversation structure. Think-times are zeroed for speed.
"""
import json
import os
import random
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

for k, v in {"ASSISTANT_THINK_MIN": "0", "ASSISTANT_THINK_MAX": "0", "ASSISTANT_GAP_MIN": "0",
             "ASSISTANT_GAP_MAX": "0"}.items():
    os.environ[k] = v
os.environ["ASSISTANT_SCENARIOS"] = os.environ.get("MIX", "checkout=1")
SEEN = []
FAIL = os.environ.get("FAIL", "1") == "1"


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        cat = self.headers.get("X-Prompt-Category")
        SEEN.append({"cat": cat, "msg": body["message"], "cookie": self.headers.get("Cookie"),
                     "conv": self.headers.get("X-Conversation-ID")})
        outcome = "ok"
        if cat in ("checkout_request", "failed_checkout_followup") and FAIL and any(
                w in body["message"].lower() for w in ("check out", "checkout", "retry", "buy", "again")):
            outcome = "tool_error"
        out = json.dumps({"message": "stub reply", "outcome": outcome}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=srv.serve_forever, daemon=True).start()

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import gevent
from locust import events
from locust.env import Environment

import locustfile as lf

env = Environment(user_classes=[lf.AssistantUser], host=f"http://127.0.0.1:{srv.server_port}", events=events)
runner = env.create_local_runner()
random.seed(int(os.environ.get("SEED", "7")))
runner.start(user_count=3, spawn_rate=3)
gevent.sleep(float(os.environ.get("SECS", "3")))
runner.quit()

convs = {}
for r in SEEN:
    convs.setdefault(r["conv"], []).append(r)
print(f"requests={len(SEEN)} conversations={len(convs)} stats_failures={env.stats.total.num_failures}")
for cid, turns in list(convs.items())[:3]:
    print(f"--- conversation {cid}  customer cookie={turns[0]['cookie'][:30]}...")
    for t in turns:
        print(f"   [{t['cat']:24}] {t['msg']}")
cookies_per_conv = {c: {t['cookie'] for t in ts} for c, ts in convs.items()}
print("single customer per conversation:", all(len(v) == 1 for v in cookies_per_conv.values()))
print("max turns in any conversation:", max(len(v) for v in convs.values()), "(cap", lf.MAX_TURNS, ")")
print("categories seen:", sorted({r['cat'] for r in SEEN}))
