#!/usr/bin/env python3
import json
import sys
import time
import uuid
sys.path.insert(0, "/Users/pwalsh/online-boutique/src/shoppingassistantservice/tests")
from integration_local import Browser

BASE = "http://127.0.0.1:18080"
PW = "Sup3rPassw0rd!"

def check(value, label):
    if not value:
        raise SystemExit("FAIL " + label)
    print("PASS " + label)

def form_checkout(browser, email):
    check(browser.req("POST", "/cart", form={"product_id": "6E92ZMYYFZ", "quantity": "1"})[0] == 302, "add item")
    code, _, body = browser.req("POST", "/cart/checkout", form={
        "email": email, "street_address": "1600 Amphitheatre Parkway", "zip_code": "94043",
        "city": "Mountain View", "state": "CA", "country": "United States",
        "credit_card_number": "4432801561520454", "credit_card_expiration_month": "1",
        "credit_card_expiration_year": "2030", "credit_card_cvv": "672"})
    check(code == 200 and "order" in body.lower(), "storefront checkout renders confirmation")

# Web account, login/logout, profile update.
web = Browser(BASE)
WEB_EMAIL = f"kind.web.{uuid.uuid4().hex[:8]}@example.com"
CHAT_EMAIL = f"kind.chat.{uuid.uuid4().hex[:8]}@example.com"
check(web.req("GET", "/account/create")[0] == 200, "account create page")
csrf = web.csrf("/account/create")
code, headers, _ = web.req("POST", "/account/create", form={
    "csrf": csrf, "name": "Kind Web Shopper", "email": WEB_EMAIL, "password": PW,
    "street_address": "1 Main St", "city": "Austin", "state": "TX", "zip_code": "73301",
    "country": "United States", "preferred_currency": ""})
check(code == 302 and web.cookie("shop_account-session"), "web registration")
check(web.req("GET", "/account/profile")[0] == 200, "web profile")
csrf = web.csrf("/account/profile")
code, _, _ = web.req("POST", "/account/profile", form={
    "csrf": csrf, "name": "Kind Web Shopper Updated", "street_address": "2 Main St",
    "city": "Dallas", "state": "TX", "zip_code": "75001", "country": "United States",
    "preferred_currency": "EUR"})
check(code == 302, "profile update")
web.req("GET", "/logout")
check(not web.cookie("shop_account-session"), "logout clears auth cookie")
csrf = web.csrf("/login")
code, _, _ = web.req("POST", "/login", form={"csrf": csrf, "email": WEB_EMAIL, "password": PW})
check(code == 302 and web.cookie("shop_account-session"), "login")

# Guest storefront checkout.
guest = Browser(BASE)
guest.req("GET", "/")
form_checkout(guest, "kind.guest@example.com")

# Assistant-created account, activation, replay and authenticated storefront checkout.
chat = Browser(BASE)
chat.req("GET", "/")
for message in ["find me a mug", "add two to my cart", "I'd like to check out", f"my email is {CHAT_EMAIL}"]:
    code, result = chat.chat(message, "checkout_request")
    check(code == 200, "chat turn " + message[:18])
check(result.get("complete_setup") is True, "chat account requests setup")
activation_code = ""
code, _, html = chat.req("GET", "/account/activation")
check(code == 200 and CHAT_EMAIL in html, "activation page shows server-resolved email")
check(not any(name.startswith("shop_activation_") for name in chat.cookies()), "activation has no browser code cookie")
csrf = chat.csrf("/account/activation")
code, headers, _ = chat.req("POST", "/account/activation", form={"csrf": csrf, "password": PW})
check(code == 302 and chat.cookie("shop_account-session"), "same browser activation")
check(chat.req("GET", "/account/profile")[0] == 200, "activated profile")
check(chat.req("POST", "/account/activation", form={"csrf": chat.csrf("/account/activation"), "password": "Other1Passw0rd"})[0] != 302, "activation replay rejected")
form_checkout(chat, CHAT_EMAIL)

print("LOCAL_FUNCTIONAL_SMOKE_PASS")
