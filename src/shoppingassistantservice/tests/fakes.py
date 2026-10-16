"""Local end-to-end test harness. NOT a Bedrock test.

Fake pieces (all labelled, none of this validates Bedrock or the real services):
  - FakeBedrock: a Bedrock Converse server with scripted tool use.
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


# -------------------------------------------------------------------- fake Bedrock
class FakeBedrock(BaseHTTPRequestHandler):
    """Scripted 'model' speaking the Bedrock Converse wire format. Decides tool calls
    from the latest user text and the tool schema it was given, so v1/v2 prompt text
    really changes its argument - but this is a SCRIPT standing in for a model, not
    evidence about Bedrock or Claude."""
    requests = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeBedrock.requests.append(body)
        msgs = body["messages"]
        last = msgs[-1]["content"]
        user_texts = [c["text"] for m in msgs if m["role"] == "user" for c in m["content"] if "text" in c]
        user = user_texts[-1].lower()
        tools = {t["toolSpec"]["name"]: t["toolSpec"] for t in body.get("toolConfig", {}).get("tools", [])}

        def call(name, args, cid):
            return [{"toolUse": {"toolUseId": cid, "name": name, "input": args}}], "tool_use"

        results = [c["toolResult"] for c in last if "toolResult" in c]
        if results:
            data = results[0]["content"][0]["json"]
            if "error" in data:
                content, stop = [{"text": "Sorry, that failed: " + data["error"]}], "end_turn"
            elif "order_id" in data:
                content, stop = [{"text": f"Order {data['order_id']} placed."}], "end_turn"
            elif "results" in data:
                content, stop = [{"text": "I found Mug [6E92ZMYYFZ]."}], "end_turn"
            else:
                content, stop = [{"text": "Done."}], "end_turn"
        elif "check out" in user or "checkout" in user:
            desc = tools["place_order"]["inputSchema"]["json"]["properties"]["currency_code"]["description"]
            code = "Japanese Yen" if "written the way the customer said it" in desc else "JPY"
            content, stop = call("place_order", {"currency_code": code}, "tu_co")
        elif "add" in user:
            content, stop = call("add_to_cart", {"product_id": "6E92ZMYYFZ", "quantity": 2}, "tu_add")
        else:
            content, stop = call("search_products", {"query": "mug"}, "tu_s")
        out = {"output": {"message": {"role": "assistant", "content": content}}, "stopReason": stop,
               "usage": {"inputTokens": 120, "outputTokens": 20, "totalTokens": 140},
               "metrics": {"latencyMs": 3}}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start_fake_bedrock():
    srv = HTTPServer(("127.0.0.1", 0), FakeBedrock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ["BEDROCK_ENDPOINT_URL"] = f"http://127.0.0.1:{srv.server_port}"
    os.environ["AWS_REGION"] = "us-west-2"
    # Dummy, non-secret values so botocore can sign requests to the local fake.
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ.pop("AWS_PROFILE", None)
