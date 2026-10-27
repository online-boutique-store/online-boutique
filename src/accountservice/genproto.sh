#!/bin/bash -eu
# Regenerates the Python stubs for accountservice (demo.proto + account.proto).
python -m grpc_tools.protoc -I../../protos --python_out=. --grpc_python_out=. \
  ../../protos/demo.proto ../../protos/account.proto
