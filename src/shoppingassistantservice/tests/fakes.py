"""Local end-to-end test harness. NOT a Cortex test.

Fake pieces (all labelled, none of this validates Cortex or the real services):
  - FakeCortex: an OpenAI chat-completions server with scripted tool-calling.
  - Fake gRPC backends: catalog / cart / currency / checkout built from the same
    generated stubs. The fake currency + checkout reproduce the REAL contract we
    verified in src/currencyservice (INVALID_ARGUMENT on unsupported code).

What it does validate: tool wrappers, agent loop, span tree + parenting across
Flask -> agent -> gRPC server, baggage propagation, fault-injection labelling,
metrics, and that backend failures are reported honestly.
"""
import json
import os
import sys
import threading
from concurrent import futures
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import grpc

import demo_pb2 as pb
import demo_pb2_grpc as pbg

SUPPORTED = {"USD", "EUR", "JPY", "GBP", "CAD", "TRY"}
PRODUCTS = {
    "6E92ZMYYFZ": ("Mug", 8, ["kitchen"]),
    "OLJCESPC7Z": ("Sunglasses", 19, ["accessories"]),
    "1YMWWN1N4O": ("Watch", 109, ["accessories"]),
}
CARTS = {}
_SERVERS = []
SEEN_BAGGAGE = []  # baggage observed by the fake backends, to prove propagation


# --------------------------------------------------------------- fake gRPC backends
class Catalog(pbg.ProductCatalogServiceServicer):
    def _p(self, pid):
        n, price, cats = PRODUCTS[pid]
        return pb.Product(id=pid, name=n, description=f"A {n.lower()}", price_usd=pb.Money(currency_code="USD", units=price), categories=cats)

    def SearchProducts(self, req, ctx):
        from opentelemetry import baggage
        SEEN_BAGGAGE.append(dict(baggage.get_all()))
        q = req.query.lower()
        return pb.SearchProductsResponse(results=[self._p(i) for i, (n, _, c) in PRODUCTS.items() if q in n.lower() or q in " ".join(c)])

    def GetProduct(self, req, ctx):
        if req.id not in PRODUCTS:
            ctx.abort(grpc.StatusCode.NOT_FOUND, "no product with ID " + req.id)
        return self._p(req.id)


class Cart(pbg.CartServiceServicer):
    def AddItem(self, req, ctx):
        CARTS.setdefault(req.user_id, {})
        CARTS[req.user_id][req.item.product_id] = CARTS[req.user_id].get(req.item.product_id, 0) + req.item.quantity
        return pb.Empty()

    def GetCart(self, req, ctx):
        return pb.Cart(user_id=req.user_id, items=[pb.CartItem(product_id=p, quantity=q) for p, q in CARTS.get(req.user_id, {}).items()])


class Currency(pbg.CurrencyServiceServicer):
    def GetSupportedCurrencies(self, req, ctx):
        return pb.GetSupportedCurrenciesResponse(currency_codes=sorted(SUPPORTED))

    def Convert(self, req, ctx):
        # Mirrors the validation added to src/currencyservice/server.js.
        for code in (getattr(req, "from").currency_code, req.to_code):
            if code not in SUPPORTED:
                ctx.abort(grpc.StatusCode.INVALID_ARGUMENT, f"unsupported currency code: {json.dumps(code)}")
        return pb.Money(currency_code=req.to_code, units=getattr(req, "from").units * 100, nanos=0)


class Checkout(pbg.CheckoutServiceServicer):
    def PlaceOrder(self, req, ctx):
        # Real checkout converts every item via the currency service and wraps any
        # failure as codes.Internal ("failed to convert price ...").
        cur = pbg.CurrencyServiceStub(grpc.insecure_channel(os.environ["CURRENCY_SERVICE_ADDR"]))
        try:
            cur.Convert(pb.CurrencyConversionRequest(**{"from": pb.Money(currency_code="USD", units=1)}, to_code=req.user_currency))
        except grpc.RpcError as e:
            ctx.abort(grpc.StatusCode.INTERNAL, f"failed to convert price of item to {req.user_currency}: {e.details()}")
        CARTS[req.user_id] = {}
        return pb.PlaceOrderResponse(order=pb.OrderResult(order_id="ord-123", shipping_tracking_id="trk-1",
                                                           shipping_cost=pb.Money(currency_code=req.user_currency, units=5)))


def start_backends():
    from opentelemetry.instrumentation.grpc import GrpcInstrumentorServer
    GrpcInstrumentorServer().instrument()
    addrs = {}
    for name, add, impl in (("CATALOG", pbg.add_ProductCatalogServiceServicer_to_server, Catalog()),
                            ("CART", pbg.add_CartServiceServicer_to_server, Cart()),
                            ("CURRENCY", pbg.add_CurrencyServiceServicer_to_server, Currency()),
                            ("CHECKOUT", pbg.add_CheckoutServiceServicer_to_server, Checkout())):
        s = grpc.server(futures.ThreadPoolExecutor(4))
        add(impl, s)
        port = s.add_insecure_port("127.0.0.1:0")
        s.start()
        _SERVERS.append(s)  # grpc stops a server that gets garbage-collected
        addrs[name] = f"127.0.0.1:{port}"
    os.environ["PRODUCT_CATALOG_SERVICE_ADDR"] = addrs["CATALOG"]
    os.environ["CART_SERVICE_ADDR"] = addrs["CART"]
    os.environ["CURRENCY_SERVICE_ADDR"] = addrs["CURRENCY"]
    os.environ["CHECKOUT_SERVICE_ADDR"] = addrs["CHECKOUT"]


# --------------------------------------------------------------------- fake Cortex
class FakeCortex(BaseHTTPRequestHandler):
    """Scripted 'model'. Decides tool calls from the latest user text and the
    tool schema it was given, so v1/v2 prompt text really changes its argument -
    but this is a SCRIPT standing in for a model, not evidence about Cortex."""
    requests = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeCortex.requests.append(body)
        msgs = body["messages"]
        user = [m for m in msgs if m["role"] == "user"][-1]["content"].lower()
        last = msgs[-1]
        tools = {t["function"]["name"]: t["function"] for t in body.get("tools", [])}

        def call(name, args, cid):
            return {"role": "assistant", "content": None, "tool_calls": [{"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}, "tool_calls"

        if last["role"] == "tool":
            data = json.loads(last["content"])
            if "error" in data:
                msg, fr = {"role": "assistant", "content": "Sorry, that failed: " + data["error"]}, "stop"
            elif "order_id" in data:
                msg, fr = {"role": "assistant", "content": f"Order {data['order_id']} placed."}, "stop"
            elif "results" in data:
                msg, fr = {"role": "assistant", "content": "I found Mug [6E92ZMYYFZ]."}, "stop"
            else:
                msg, fr = {"role": "assistant", "content": "Done."}, "stop"
        elif "check out" in user or "checkout" in user:
            v2 = "written the way the customer said it" in tools["place_order"]["parameters"]["properties"]["currency_code"]["description"]
            code = "Japanese Yen" if v2 else "JPY"   # scripted model behaviour per prompt version
            msg, fr = call("place_order", {"currency_code": code}, "call_co")
        elif "add" in user:
            msg, fr = call("add_to_cart", {"product_id": "6E92ZMYYFZ", "quantity": 2}, "call_add")
        else:
            msg, fr = call("search_products", {"query": "mug"}, "call_s")
        out = {"id": "chatcmpl-fake", "object": "chat.completion", "created": 1, "model": body["model"],
               "choices": [{"index": 0, "message": msg, "finish_reason": fr}],
               "usage": {"prompt_tokens": 120, "completion_tokens": 20, "total_tokens": 140}}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_fake_cortex():
    srv = HTTPServer(("127.0.0.1", 0), FakeCortex)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["CORTEX_BASE_URL"] = f"http://127.0.0.1:{srv.server_port}/v1"
    os.environ["CORTEX_PAT"] = "fake-token-not-a-secret"
