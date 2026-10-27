"""Stub gRPC backends (cart, catalog, currency, email) and a process supervisor for the real
checkoutservice / shippingservice / paymentservice binaries used by the durability tests."""
import os
import socket
import subprocess
import tempfile
import threading
import time
from concurrent import futures
from decimal import Decimal

import grpc

import demo_pb2 as dpb
import demo_pb2_grpc as dpbg

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.abspath(os.path.join(HERE, "..", ".."))
RATES = {"USD": Decimal(1), "EUR": Decimal("0.9"), "JPY": Decimal(150), "CAD": Decimal("1.3")}
PRODUCTS = {
    "P1": ("Canary Mug", ["kitchen"], 8, 990000000),
    "P2": ("Sunglasses", ["accessories"], 19, 500000000),
}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _money(code, amount):
    cents = (Decimal(amount) * 100).quantize(Decimal(1))
    total_nanos = int(cents) * 10_000_000
    return dpb.Money(currency_code=code, units=total_nanos // 1_000_000_000, nanos=total_nanos % 1_000_000_000)


class Stubs:
    """One gRPC server hosting all four stub services; call counters are asserted by tests."""

    def __init__(self):
        self.carts, self.emails, self.empty_calls, self.quote_calls = {}, [], [], 0
        outer = self

        class Cart(dpbg.CartServiceServicer):
            def GetCart(self, request, context):
                return dpb.Cart(user_id=request.user_id, items=[dpb.CartItem(product_id=p, quantity=q) for p, q in outer.carts.get(request.user_id, [])])

            def AddItem(self, request, context):
                items = outer.carts.setdefault(request.user_id, [])
                for i, (pid, q) in enumerate(items):
                    if pid == request.item.product_id:
                        items[i] = (pid, q + request.item.quantity)
                        break
                else:
                    items.append((request.item.product_id, request.item.quantity))
                return dpb.Empty()

            def EmptyCart(self, request, context):
                outer.empty_calls.append(request.user_id)
                outer.carts.pop(request.user_id, None)
                return dpb.Empty()

        class Catalog(dpbg.ProductCatalogServiceServicer):
            def GetProduct(self, request, context):
                if request.id not in PRODUCTS:
                    context.abort(grpc.StatusCode.NOT_FOUND, "no such product")
                name, cats, u, n = PRODUCTS[request.id]
                return dpb.Product(id=request.id, name=name, categories=cats, price_usd=dpb.Money(currency_code="USD", units=u, nanos=n))

        def _product(pid):
            name, cats, u, n = PRODUCTS[pid]
            return dpb.Product(id=pid, name=name, categories=cats, price_usd=dpb.Money(currency_code="USD", units=u, nanos=n))

        class Catalog2(Catalog):
            def ListProducts(self, request, context):
                return dpb.ListProductsResponse(products=[_product(p) for p in PRODUCTS])

        class Reco(dpbg.RecommendationServiceServicer):
            def ListRecommendations(self, request, context):
                return dpb.ListRecommendationsResponse(product_ids=[])

        class Ads(dpbg.AdServiceServicer):
            def GetAds(self, request, context):
                return dpb.AdResponse(ads=[dpb.Ad(redirect_url="/product/P1", text="Demo ad")])

        class Currency(dpbg.CurrencyServiceServicer):
            def GetSupportedCurrencies(self, request, context):
                return dpb.GetSupportedCurrenciesResponse(currency_codes=list(RATES))

            def Convert(self, request, context):
                m = getattr(request, "from")
                amount = Decimal(m.units) + Decimal(m.nanos) / Decimal(10 ** 9)
                amount = amount / RATES[m.currency_code] * RATES[request.to_code]
                return _money(request.to_code, amount)

        class Email(dpbg.EmailServiceServicer):
            def SendOrderConfirmation(self, request, context):
                outer.emails.append(request.order.order_id)
                return dpb.Empty()

        self.server = grpc.server(futures.ThreadPoolExecutor(8))
        dpbg.add_CartServiceServicer_to_server(Cart(), self.server)
        dpbg.add_ProductCatalogServiceServicer_to_server(Catalog2(), self.server)
        dpbg.add_RecommendationServiceServicer_to_server(Reco(), self.server)
        dpbg.add_AdServiceServicer_to_server(Ads(), self.server)
        dpbg.add_CurrencyServiceServicer_to_server(Currency(), self.server)
        dpbg.add_EmailServiceServicer_to_server(Email(), self.server)
        self.port = self.server.add_insecure_port("127.0.0.1:0")
        self.server.start()
        self.addr = f"127.0.0.1:{self.port}"

    def stop(self):
        self.server.stop(0)


class Proc:
    """A real service binary; restartable with a different environment (e.g. without a crash fault)."""

    def __init__(self, name, argv, cwd, port, base_env):
        self.name, self.argv, self.cwd, self.port, self.base_env = name, argv, cwd, port, {k: str(v) for k, v in base_env.items()}
        self.proc, self.log = None, tempfile.NamedTemporaryFile(prefix=name, suffix=".log", delete=False)

    def start(self, **env):
        e = dict(os.environ)
        e.update(self.base_env)
        e.update({k: str(v) for k, v in env.items() if v is not None})
        for k, v in env.items():
            if v is None:
                e.pop(k, None)
        self.proc = subprocess.Popen(self.argv, cwd=self.cwd, env=e, stdout=self.log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        self.wait_port()
        return self

    def wait_port(self, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.3).close()
                return
            except OSError:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"{self.name} exited early: " + self.tail())
                time.sleep(0.1)
        raise RuntimeError(f"{self.name} did not open port: " + self.tail())

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def wait_dead(self, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            if not self.alive():
                return True
            time.sleep(0.05)
        return False

    def stop(self):
        if self.alive():
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

    def kill9(self):
        if self.alive():
            self.proc.kill()
            self.proc.wait()

    def restart(self, **env):
        self.stop()
        return self.start(**env)

    def tail(self, n=800):
        try:
            return open(self.log.name).read()[-n:]
        except OSError:
            return ""

    def log_text(self):
        try:
            return open(self.log.name).read()
        except OSError:
            return ""


def ensure_binaries():
    """Builds the Go services and installs node deps if missing (no-ops when up to date)."""
    env = dict(os.environ, PATH="/opt/homebrew/bin:/usr/local/go/bin:" + os.environ.get("PATH", ""))
    for svc, out in (("checkoutservice", "/tmp/checkout_bin"), ("shippingservice", "/tmp/shipping_bin")):
        subprocess.run(["go", "build", "-o", out, "."], cwd=os.path.join(SRC, svc), env=env, check=True, capture_output=True)
    if not os.path.isdir(os.path.join(SRC, "paymentservice", "node_modules", "redis")):
        subprocess.run(["npm", "ci", "--ignore-scripts", "--silent"], cwd=os.path.join(SRC, "paymentservice"), env=env, check=True, capture_output=True)
