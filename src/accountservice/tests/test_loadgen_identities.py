"""Every load-generator identity must satisfy the authoritative account contract, and the generator's
state list must equal the contract's. The identities are produced by the real locustfile (run in the
loadgenerator's own venv) and validated here by accountservice's validation module."""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]
import validation as v  # noqa: E402

LG_DIR = os.path.join(os.path.dirname(os.path.dirname(HERE)), "loadgenerator")
LG_PY = os.environ.get("LOADGEN_PY", "/tmp/lgvenv2/bin/python")
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {str(detail)[:300]}"))
    if not cond:
        FAILS.append(name)


def main():
    if not os.path.exists(LG_PY):
        print("SKIP loadgen identities: no loadgenerator venv at", LG_PY)
        return
    code = "import json, locustfile as l; print(json.dumps({'ids': l.IDENTITIES, 'states': l._CONTRACT_STATES}))"
    out = subprocess.run([LG_PY, "-c", code], cwd=LG_DIR, capture_output=True, text=True, timeout=120,
                         env=dict(os.environ, ASSISTANT_SCENARIOS="checkout=1"))
    data = json.loads(out.stdout.strip().splitlines()[-1])
    ids, states = data["ids"], data["states"]
    check("loadgen state list equals the contract's 50 states + DC", sorted(states) == sorted(v.US_STATES), set(states) ^ set(v.US_STATES))
    bad = []
    for p in ids:
        body = {"first_name": p["first"], "last_name": p["last"], "email": p["email"], "phone": p["phone"], "address_line1": p["street"],
                "address_line2": p["line2"], "city": p["city"], "state": p["state"], "zip": p["zip"], "country": p["country"],
                "password": "Valid1Passw0rd", "preferred_currency": "USD"}
        _, _, errors = v.validate_registration(body)
        if errors:
            bad.append((p["email"], {k: e["code"] for k, e in errors.items()}))
    check(f"all {len(ids)} load-generator identities pass the account contract", not bad, bad[:3])
    check("phones are unique per identity block", len({p["phone"] for p in ids}) >= min(len(ids), 50))
    print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
