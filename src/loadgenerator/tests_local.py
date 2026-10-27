"""Drive the REAL locustfile.AssistantUser against a stub frontend /bot.

The stub walks each conversation through the guided checkout stages and fails the final
place_order when FAIL=1 (standing in for the v2 regression) and records every request's headers/body so
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


FLOW = {}  # conversation id -> index into STAGES (0 = not started)
STAGES = ["identify", "payment", "currency", "confirm", "done"]
EMAILS_SEEN = []
CUSTOMER_TYPES = {}


class Stub(BaseHTTPRequestHandler):
    """Stands in for frontend /bot + the agent's checkout state machine."""

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        cat = self.headers.get("X-Prompt-Category")
        conv = self.headers.get("X-Conversation-ID")
        msg = body["message"]
        SEEN.append({"cat": cat, "msg": msg, "cookie": self.headers.get("Cookie"), "conv": conv})
        outcome, checkout = "ok", None
        low = msg.lower()
        if cat == "checkout_request":
            if "check out" in low and conv not in FLOW:
                FLOW[conv] = 0
            elif conv in FLOW:
                FLOW[conv] += 1
                if "@" in msg:
                    EMAILS_SEEN.append(msg)
                    CUSTOMER_TYPES[conv] = "new" if "new here" in low else (
                        "guest" if "guest" in low else "returning")
            i = min(FLOW.get(conv, 0), len(STAGES) - 1)
            checkout = {"stage": STAGES[i], "next_step": "stub", "customer_type": CUSTOMER_TYPES.get(conv, "unknown")}
            if STAGES[i] == "done" and FAIL:
                outcome = "tool_error"
        elif cat == "failed_checkout_followup" and FAIL:
            outcome = "tool_error"
        out = json.dumps({"message": "stub reply", "outcome": outcome, "checkout": checkout}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


# Import locust (which gevent-monkey-patches the stdlib) BEFORE starting any thread, otherwise the
# patch deadlocks on the import lock while the stub server thread is already running.
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
import gevent
from locust import events
from locust.env import Environment

import locustfile as lf

srv = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=srv.serve_forever, daemon=True).start()

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

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
print("checkout flows started:", len(FLOW), "| identity replies:", len(EMAILS_SEEN))
print("customer types:", sorted(set(CUSTOMER_TYPES.values())), "| pool created:", len(lf.CREATED))
print("no currency in the opening checkout request:", all(
    not any(c in r["msg"].lower() for c in ("yen", "euro", "pound", "dollar", "lira"))
    for r in SEEN if r["msg"].lower().startswith(("i'd like to check out", "i'm ready to check out", "let's check out"))))


# ---- store-scope option (a): every conversation OPENS with a relevant shopping question ----
PRODUCT_WORDS = tuple(lf.ASSISTANT_PRODUCTS)
ok_open, bad = 0, []
for cid, turns in convs.items():
    first = turns[0]
    low = first["msg"].lower()
    good = first["cat"] in lf.OPENING_CATEGORIES and (
        any(w in low for w in PRODUCT_WORDS) or any(p in low for p in ("what do you have", "kitchen", "do you sell")))
    ok_open += good
    if not good:
        bad.append((cid, first["cat"], first["msg"]))
print(f"conversations opening with a product question: {ok_open}/{len(convs)}")
assert not bad, f"conversations that did not open with a shopping question: {bad[:3]}"
if os.environ.get("MIX", "checkout=1") != "checkout=1":
    seen_later = {t["cat"] for ts in convs.values() for t in ts[1:]}
    for wanted in ("off_topic", "prompt_injection", "customer_support"):
        assert wanted in seen_later, f"{wanted} prompts must still be sent (as later turns); saw {sorted(seen_later)}"
    assert not any(ts[0]["cat"] in ("off_topic", "prompt_injection", "customer_support") for ts in convs.values())
print("OPENERS OK")
