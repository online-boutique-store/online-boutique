import os
import sys
from http.server import HTTPServer

sys.path.insert(0, "/app")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
from fakes import FakeBedrock  # noqa: E402

server = HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), FakeBedrock)
print("fake-bedrock listening", flush=True)
server.serve_forever()
