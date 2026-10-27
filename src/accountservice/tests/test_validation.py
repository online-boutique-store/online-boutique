"""Runs the shared contract vectors (account_validation.json) against validation.py."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import validation as v  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond:
        FAILS.append(name)


def expand(vec, key="in"):
    if "in_repeat" in vec:
        r = vec["in_repeat"]
        return r.get("before", "") + r["unit"] * r["n"] + r.get("after", "")
    return vec[key]


def label(vec, value):
    return (value if len(str(value)) < 40 else f"<{len(value)} chars>")


def run_vectors():
    V = v.CONTRACT["vectors"]
    for vec in V["name"]:
        val = expand(vec)
        norm, e = v.validate_name(vec["field"], val)
        name = f"name[{vec['field']}] {label(vec, val)!r}"
        if vec["ok"]:
            check(name, e is None and ("norm" not in vec or norm == vec["norm"]), (norm, e))
        else:
            check(name, e is not None and e["code"] == vec["code"], e)
    for vec in V["email"]:
        val = expand(vec)
        display, key, e = v.validate_email(val)
        name = f"email {label(vec, val)!r}"
        if vec["ok"]:
            ok = e is None and ("key" not in vec or key == vec["key"]) and ("display" not in vec or display == vec["display"])
            check(name, ok, (display, key, e))
        else:
            check(name, e is not None and e["code"] == vec["code"], e)
    for vec in V["phone"]:
        e164, e = v.validate_phone(vec["in"])
        name = f"phone {vec['in']!r}"
        if vec["ok"]:
            check(name, e is None and e164 == vec["e164"], (e164, e))
        else:
            check(name, e is not None and e["code"] == vec["code"], e)
    for vec in V["address"]:
        if "in_repeat_field" in vec:
            r = vec["in_repeat_field"]
            fields = dict(vec["base"], **{r["field"]: r["unit"] * r["n"]})
            name = f"address {r['field']} x{r['n']}"
        else:
            fields, name = vec["in"], f"address {json.dumps(fields := vec['in'])[:70]}"
        addr, errs = v.validate_address(fields)
        if vec["ok"]:
            out = vec.get("out", {})
            check(name, not errs and all(addr.get(k) == val for k, val in out.items()), (addr, errs))
        else:
            got = {k: e["code"] for k, e in errs.items()}
            check(name, all(got.get(k) == c for k, c in vec["codes"].items()) and set(got) == set(vec["codes"]), got)


def test_messages_never_echo_input():
    secret = "ZzSecretValue"
    _, _, e = v.validate_email(secret + "@@")
    _, e2 = v.validate_phone(secret)
    _, e3 = v.validate_name("first_name", secret + "1")
    check("messages do not echo submitted values",
          all(secret not in x["message"] for x in (e, e2, e3)), (e, e2, e3))


def test_every_code_has_message_and_password_unchanged():
    for code in ("required", "too_long", "email_taken", "phone_invalid", "state_invalid", "zip_invalid"):
        check(f"message exists: {code}", bool(v.message("email", code)))
    cases = [("", "Enter a password."), ("short1", "Password must be at least 8 characters."),
             ("x" * 129 + "1", "Password must be 128 characters or fewer."),
             ("onlyletters", "Password must include at least one letter and one number."),
             ("12345678", "Password must include at least one letter and one number."),
             ("Valid1Passw0rd", None)]
    for pw, expected in cases:
        check(f"password rule unchanged: {pw[:12]!r}", v.validate_password(pw) == expected, v.validate_password(pw))
    check("password == email rejected", v.validate_password("User1234@x.co", "user1234@x.co") == "Password can't be the same as your email.")


def test_registration_and_partial_errors():
    body = {"first_name": "Ada", "last_name": "Lovelace", "email": "Ada+x@Example.com", "phone": "(415) 555-2671",
            "address_line1": "1 Main St", "address_line2": "Apt 2", "city": "Austin", "state": "tx", "zip": "73301",
            "password": "Valid1Passw0rd", "confirm_password": "Valid1Passw0rd"}
    p, pw, errs = v.validate_registration(body)
    check("valid registration", not errs and p["email"] == "ada+x@example.com" and p["email_display"] == "Ada+x@example.com"
          and p["phone_e164"] == "+14155552671" and p["name"] == "Ada Lovelace" and p["address"]["state"] == "TX", (p, errs))
    _, _, errs = v.validate_registration({})
    check("empty registration lists every required field",
          {"first_name", "last_name", "email", "phone", "address_line1", "city", "state", "zip", "password"} <= set(errs), set(errs))
    _, _, errs = v.validate_registration(dict(body, confirm_password="nope"))
    check("confirm mismatch", errs.get("confirm_password", {}).get("code") == "confirm_mismatch", errs)


if __name__ == "__main__":
    run_vectors()
    test_messages_never_echo_input()
    test_every_code_has_message_and_password_unchanged()
    test_registration_and_partial_errors()
    print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
    sys.exit(1 if FAILS else 0)
