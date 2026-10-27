"""Authoritative account-creation validation contract (server side).

Limits, error codes, messages and test vectors live in account_validation.json next to this file;
this module enforces them. Clients (web form, assistant tool, loadgen) only mirror the contract.
Messages never echo submitted values. Password rules are the pre-existing ones, unchanged.
"""
import json
import os
import re
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "account_validation.json"), encoding="utf-8") as _f:
    CONTRACT = json.load(_f)

LIMITS = CONTRACT["limits"]
MESSAGES = CONTRACT["messages"]
LABELS = CONTRACT["labels"]
US_STATES = frozenset(CONTRACT["us_states"])
SUPPORTED_CURRENCIES = tuple(CONTRACT["supported_currencies"])

_NAME_RE = re.compile(r"^[^\W\d_]+(?:(?:\.\s?|\s|['\u2019-])[^\W\d_]+)*\.?$")
_CITY_RE = _NAME_RE
_ZIP_RE = re.compile(r"^\d{5}(?:-\d{4})?$")
_ZIP9_RE = re.compile(r"^(\d{5})(\d{4})$")
_PO_BOX_RE = re.compile(r"\b(?:P\.?\s?O\.?\s*Box|Post\s+Office\s+Box)\b", re.I)
_US_COUNTRY = {"us", "usa", "u.s.", "u.s.a.", "united states", "united states of america"}
_EXT_RE = re.compile(r"(?i)(?:;ext=|\bext(?:ension)?\.?|\bx|#)\s*\d+\s*$")
_PHONE_CHARS_RE = re.compile(r"^[0-9+()\-.\s]+$")


def message(field, code):
    label = LABELS.get(field, field.replace("_", " "))
    template = MESSAGES.get(f"{field}.{code}") or MESSAGES[code]
    lim = LIMITS.get(field) or {}
    return template.format(label=label, Label=label[:1].upper() + label[1:], max=lim.get("max", ""), min=lim.get("min", ""))


def err(field, code):
    return {"code": code, "message": message(field, code)}


def _collapse(value):
    return " ".join(unicodedata.normalize("NFC", str(value or "")).split())


def _has_control(value):
    return any(unicodedata.category(ch).startswith("C") for ch in value)


# --- names -------------------------------------------------------------------------------------
def validate_name(field, raw):
    """-> (normalized, error|None) for first_name / last_name."""
    value = _collapse(raw)
    if not value:
        return "", err(field, "required")
    if len(value) > LIMITS[field]["max"]:
        return value, err(field, "too_long")
    if not _NAME_RE.match(value):
        return value, err(field, "invalid_characters")
    return value, None


# --- email -------------------------------------------------------------------------------------
def validate_email(raw):
    """-> (display, key, error|None).

    key     lookup/uniqueness key: ASCII-lowercased local part + '@' + lowercase A-label domain.
    display what the customer typed (trimmed); only the domain is normalized. The local part is
            never rewritten for display or outgoing mail.
    """
    from email_validator import EmailNotValidError, validate_email as _validate

    value = str(raw or "").strip()
    if not value:
        return "", "", err("email", "required")
    if any(ch.isspace() for ch in value) or _has_control(value):
        return "", "", err("email", "email_invalid_syntax")
    local_raw, _, domain_raw = value.rpartition("@")
    if len(value) > LIMITS["email"]["max"] or len(local_raw) > LIMITS["email"]["local_max"]:
        return "", "", err("email", "email_too_long")
    if any(len(label) > LIMITS["email"]["label_max"] for label in domain_raw.split(".")):
        return "", "", err("email", "email_too_long")
    try:
        parsed = _validate(value, check_deliverability=False, allow_smtputf8=True)
    except EmailNotValidError as e:
        if "too long" in str(e).lower():
            return "", "", err("email", "email_too_long")
        try:   # which side is wrong? a good domain with the same local part isolates the local part
            _validate(local_raw + "@example.com", check_deliverability=False, allow_smtputf8=True)
            code = "email_invalid_domain"
        except EmailNotValidError:
            code = "email_invalid_syntax"
        return "", "", err("email", code)
    if not parsed.local_part.isascii():
        return "", "", err("email", "email_local_unsupported")
    if len(parsed.ascii_domain.rsplit(".", 1)[-1]) < 2:      # a real domain ending (.com, .edu, ...)
        return "", "", err("email", "email_invalid_domain")
    key = parsed.local_part.lower() + "@" + parsed.ascii_domain.lower()
    if len(key) > LIMITS["email"]["max"]:
        return "", "", err("email", "email_too_long")
    return parsed.local_part + "@" + parsed.domain, key, None


# --- phone (US only) ---------------------------------------------------------------------------
def validate_phone(raw):
    """-> (e164, error|None). US numbers only; extensions are not supported."""
    import phonenumbers

    value = str(raw or "").strip()
    if not value:
        return "", err("phone", "required")
    if _EXT_RE.search(value):
        return "", err("phone", "phone_extension_unsupported")
    if not _PHONE_CHARS_RE.match(value):
        return "", err("phone", "phone_invalid")        # letters/vanity, full-width digits, symbols
    digits = re.sub(r"\D", "", value)
    if len(digits) > 15:
        return "", err("phone", "phone_too_long")
    international = value.startswith("+")
    national = digits[1:] if (digits.startswith("1") and len(digits) == 11) or (international and digits.startswith("1")) else digits
    if international and not digits.startswith("1"):
        return "", err("phone", "phone_country_unsupported")
    if len(national) < 10:
        return "", err("phone", "phone_incomplete")
    try:
        number = phonenumbers.parse("+" + digits if international else value, None if international else "US")
    except phonenumbers.NumberParseException:
        return "", err("phone", "phone_invalid")
    if number.country_code != 1:
        return "", err("phone", "phone_country_unsupported")
    if len(str(number.national_number)) != 10:
        return "", err("phone", "phone_invalid")
    if not phonenumbers.is_valid_number(number):
        return "", err("phone", "phone_invalid")
    if phonenumbers.region_code_for_number(number) != "US":
        return "", err("phone", "phone_country_unsupported")
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164), None


def format_phone(e164):
    if not e164 or len(e164) != 12:
        return ""
    return f"({e164[2:5]}) {e164[5:8]}-{e164[8:]}"


# --- address -----------------------------------------------------------------------------------
def _validate_line(field, raw, required):
    value = _collapse(raw)
    if not value:
        return ("", err(field, "required")) if required else ("", None)
    if len(value) > LIMITS[field]["max"]:
        return value, err(field, "too_long")
    if _has_control(value) or not any(ch.isalnum() for ch in value):
        return value, err(field, "invalid_characters")
    return value, None


def validate_address(fields):
    """-> (address, errors). Keys in: address_line1, address_line2, city, state, zip, country."""
    errors, out = {}, {}
    out["line1"], e = _validate_line("address_line1", fields.get("address_line1"), True)
    if e:
        errors["address_line1"] = e
    out["line2"], e = _validate_line("address_line2", fields.get("address_line2"), False)
    if e:
        errors["address_line2"] = e
    out["line2"] = out["line2"] or None

    city = _collapse(fields.get("city"))
    if not city:
        errors["city"] = err("city", "required")
    elif len(city) > LIMITS["city"]["max"]:
        errors["city"] = err("city", "too_long")
    elif not _CITY_RE.match(city):
        errors["city"] = err("city", "invalid_characters")
    out["city"] = city

    state = _collapse(fields.get("state")).upper()
    if not state:
        errors["state"] = err("state", "required")
    elif state not in US_STATES:
        errors["state"] = err("state", "state_invalid")
    out["state"] = state

    zip_code = _collapse(fields.get("zip"))
    m = _ZIP9_RE.match(zip_code)
    if m:
        zip_code = f"{m.group(1)}-{m.group(2)}"
    if not zip_code:
        errors["zip"] = err("zip", "required")
    elif not _ZIP_RE.match(zip_code):
        errors["zip"] = err("zip", "zip_invalid")
    out["zip"] = zip_code

    country = _collapse(fields.get("country")) or "US"
    if country.lower() in _US_COUNTRY:
        out["country"] = "US"
    else:
        out["country"] = country.upper()[:2]
        errors["country"] = err("country", "country_unsupported")
    out["is_po_box"] = bool(_PO_BOX_RE.search(out["line1"] or "")) or bool(_PO_BOX_RE.search(out["line2"] or ""))
    return out, errors


# --- password (pre-existing rules, unchanged) --------------------------------------------------
def _normalize_email_for_password(email):
    return str(email or "").strip().lower()


def validate_password(password, email=""):
    if not password:
        return "Enter a password."
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if len(password) > 128:
        return "Password must be 128 characters or fewer."
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "Password must include at least one letter and one number."
    if email and password.strip().lower() == _normalize_email_for_password(email):
        return "Password can't be the same as your email."
    return None


_PASSWORD_CODES = {
    "Enter a password.": "password_required",
    "Password must be at least 8 characters.": "password_too_short",
    "Password must be 128 characters or fewer.": "password_too_long",
    "Password must include at least one letter and one number.": "password_needs_letter_and_number",
    "Password can't be the same as your email.": "password_same_as_email",
}


def password_error(password, email=""):
    text = validate_password(password, email)
    return None if text is None else {"code": _PASSWORD_CODES[text], "message": text}


def validate_currency(code):
    code = str(code or "USD").strip().upper()
    return code, (None if code in SUPPORTED_CURRENCIES else err("preferred_currency", "currency_unsupported"))


# --- whole registration / profile --------------------------------------------------------------
def validate_registration(body, require_password=True):
    """-> (profile, password, errors). `profile` is fully normalized when errors is empty."""
    errors = {}
    first, e = validate_name("first_name", body.get("first_name"))
    if e:
        errors["first_name"] = e
    last, e = validate_name("last_name", body.get("last_name"))
    if e:
        errors["last_name"] = e
    display, key, e = validate_email(body.get("email"))
    if e:
        errors["email"] = e
    phone, e = validate_phone(body.get("phone"))
    if e:
        errors["phone"] = e
    address, addr_errors = validate_address(body)
    errors.update(addr_errors)
    currency, e = validate_currency(body.get("preferred_currency"))
    if e:
        errors["preferred_currency"] = e
    password = str(body.get("password") or "")
    if require_password:
        e = password_error(password, key)
        if e:
            errors["password"] = e
        confirm = body.get("confirm_password")
        if confirm is not None and str(confirm) != password and "password" not in errors:
            errors["confirm_password"] = err("confirm_password", "confirm_mismatch")
    profile = {"first_name": first, "last_name": last, "email_display": display, "email": key,
               "phone_e164": phone, "address": address, "preferred_currency": currency,
               "name": f"{first} {last}".strip()}
    return profile, password, errors


def validate_profile_update(body):
    """Partial profile edit (first/last/phone/currency). -> (changes, errors)."""
    changes, errors = {}, {}
    if "first_name" in body:
        changes["first_name"], e = validate_name("first_name", body.get("first_name"))
        if e:
            errors["first_name"] = e
    if "last_name" in body:
        changes["last_name"], e = validate_name("last_name", body.get("last_name"))
        if e:
            errors["last_name"] = e
    if "phone" in body:
        changes["phone_e164"], e = validate_phone(body.get("phone"))
        if e:
            errors["phone"] = e
    if "preferred_currency" in body:
        changes["preferred_currency"], e = validate_currency(body.get("preferred_currency"))
        if e:
            errors["preferred_currency"] = e
    return changes, errors


def missing_profile_fields(customer):
    """Fields a legacy (version 1) customer still has to supply for a complete v2 profile."""
    missing = []
    if not customer.get("first_name"):
        missing.append("first_name")
    if not customer.get("last_name"):
        missing.append("last_name")
    if not customer.get("phone_e164"):
        missing.append("phone")
    if not customer.get("has_address"):
        missing.append("address")
    return missing
