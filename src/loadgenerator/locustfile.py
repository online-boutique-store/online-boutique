#!/usr/bin/python
#
# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import random
from locust import FastHttpUser, TaskSet, between
from faker import Faker
import datetime
fake = Faker()

products = [
    '0PUK6V6EV0',
    '1YMWWN1N4O',
    '2ZYFJ3GM2N',
    '66VCHSJNUP',
    '6E92ZMYYFZ',
    '9SIQT8TOJO',
    'L9ECAV7KIM',
    'LS4PSXUNUM',
    'OLJCESPC7Z']

# Fixed roster of customer identities used for the Observe reference-table demo.
# Each simulated user is pinned (via the shop_session-id cookie, see on_start
# below) to one of these UUIDs for its whole lifetime, so app.customer.id in
# telemetry always matches a row in the customer_records reference table
# instead of a fresh random UUID every run. Keep this list in sync with
# deploy/observe-skaffold/customer_records.csv.
CUSTOMER_UUIDS = [
    "c1a1f2b0-0001-4a4a-9c1a-1a2b3c4d5e01",
    "c1a1f2b0-0002-4a4a-9c1a-1a2b3c4d5e02",
    "c1a1f2b0-0003-4a4a-9c1a-1a2b3c4d5e03",
    "c1a1f2b0-0004-4a4a-9c1a-1a2b3c4d5e04",
    "c1a1f2b0-0005-4a4a-9c1a-1a2b3c4d5e05",
    "c1a1f2b0-0006-4a4a-9c1a-1a2b3c4d5e06",
    "c1a1f2b0-0007-4a4a-9c1a-1a2b3c4d5e07",
    "c1a1f2b0-0008-4a4a-9c1a-1a2b3c4d5e08",
    "c1a1f2b0-0009-4a4a-9c1a-1a2b3c4d5e09",
    "c1a1f2b0-0010-4a4a-9c1a-1a2b3c4d5e10",
    "c1a1f2b0-0011-4a4a-9c1a-1a2b3c4d5e11",
    "c1a1f2b0-0012-4a4a-9c1a-1a2b3c4d5e12",
    "c1a1f2b0-0013-4a4a-9c1a-1a2b3c4d5e13",
    "c1a1f2b0-0014-4a4a-9c1a-1a2b3c4d5e14",
    "c1a1f2b0-0015-4a4a-9c1a-1a2b3c4d5e15",
    "c1a1f2b0-0016-4a4a-9c1a-1a2b3c4d5e16",
    "c1a1f2b0-0017-4a4a-9c1a-1a2b3c4d5e17",
    "c1a1f2b0-0018-4a4a-9c1a-1a2b3c4d5e18",
    "c1a1f2b0-0019-4a4a-9c1a-1a2b3c4d5e19",
    "c1a1f2b0-0020-4a4a-9c1a-1a2b3c4d5e20",
]

def customer_headers(l):
    # Sending our own Cookie header (rather than letting the frontend mint one)
    # means the very first request already carries the pinned identity, so the
    # server never generates a random shop_session-id for this simulated user.
    return {"Cookie": f"shop_session-id={l.customer_id}"}

def index(l):
    l.client.get("/", headers=customer_headers(l))

def setCurrency(l):
    currencies = ['EUR', 'USD', 'JPY', 'CAD', 'GBP', 'TRY']
    l.client.post("/setCurrency",
        {'currency_code': random.choice(currencies)},
        headers=customer_headers(l))

def browseProduct(l):
    l.client.get("/product/" + random.choice(products), headers=customer_headers(l))

def viewCart(l):
    l.client.get("/cart", headers=customer_headers(l))

def addToCart(l):
    product = random.choice(products)
    l.client.get("/product/" + product, headers=customer_headers(l))
    l.client.post("/cart", {
        'product_id': product,
        'quantity': random.randint(1,10)},
        headers=customer_headers(l))
    
def empty_cart(l):
    l.client.post('/cart/empty', headers=customer_headers(l))

def checkout(l):
    addToCart(l)
    current_year = datetime.datetime.now().year+1
    l.client.post("/cart/checkout", {
        'email': fake.email(),
        'street_address': fake.street_address(),
        'zip_code': fake.zipcode(),
        'city': fake.city(),
        'state': fake.state_abbr(),
        'country': fake.country(),
        'credit_card_number': fake.credit_card_number(card_type="visa"),
        'credit_card_expiration_month': random.randint(1, 12),
        'credit_card_expiration_year': random.randint(current_year, current_year + 70),
        'credit_card_cvv': f"{random.randint(100, 999)}",
    }, headers=customer_headers(l))
    
def logout(l):
    l.client.get('/logout', headers=customer_headers(l))  


class UserBehavior(TaskSet):

    def on_start(self):
        # Pin this simulated user to one identity from the fixed roster for
        # its entire session, so app.customer.id in Observe telemetry lines
        # up with the customer_records reference table.
        self.customer_id = random.choice(CUSTOMER_UUIDS)
        index(self)

    tasks = {index: 1,
        setCurrency: 2,
        browseProduct: 10,
        addToCart: 2,
        viewCart: 3,
        checkout: 1}

class WebsiteUser(FastHttpUser):
    tasks = [UserBehavior]
    wait_time = between(1, 10)
