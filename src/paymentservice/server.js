// Copyright 2018 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

const path = require('path');
const grpc = require('@grpc/grpc-js');
const protoLoader = require('@grpc/proto-loader');

const charge = require('./charge');
const idem = require('./idempotency');
const catalog = require('./catalog');
const { setAppAttr } = require('./otel');

const { createLogger } = require('./logger');
const logger = createLogger('paymentservice-server');

class HipsterShopServer {
  constructor(protoRoot, port = HipsterShopServer.PORT) {
    this.port = port;

    this.packages = {
      hipsterShop: this.loadProto(path.join(protoRoot, 'demo.proto')),
      health: this.loadProto(path.join(protoRoot, 'grpc/health/v1/health.proto'))
    };

    this.server = new grpc.Server();
    this.loadAllProtos(protoRoot);
  }

  /**
   * Handler for PaymentService.Charge. Never logs the request (it carries card data).
   */
  static async ChargeServiceHandler(call, callback) {
    try {
      logger.info('PaymentService#Charge invoked');
      const response = await charge(call.request);
      callback(null, response);
    } catch (err) {
      callback(HipsterShopServer.toGrpcError(err));
    }
  }

  static async GetChargeHandler(call, callback) {
    try {
      const key = call.request.idempotency_key;
      if (!key || !idem.enabled()) { return callback({ code: grpc.status.FAILED_PRECONDITION, message: 'idempotent charges not configured' }); }
      const r = await idem.lookup(key);
      callback(null, { found: r.found, transaction_id: r.txn || '' });
    } catch (err) {
      callback({ code: grpc.status.UNAVAILABLE, message: 'payment_record_unavailable' });
    }
  }

  /**
   * Handler for PaymentService.Tokenize: catalog ids only. Never logs the request.
   */
  static async TokenizeHandler(call, callback) {
    const card = catalog.entry(String(call.request.catalog_id || ''));
    if (!card) {
      setAppAttr('app.payment.status', 'failed');
      return callback({ code: grpc.status.INVALID_ARGUMENT, message: 'card_not_allowed_demo' });
    }
    if (!idem.enabled()) { return callback({ code: grpc.status.FAILED_PRECONDITION, message: 'tokenization not configured' }); }
    const token = catalog.newToken();
    try {
      await idem.bindToken(token, card.catalog_id);
    } catch (e) {
      return callback({ code: grpc.status.UNAVAILABLE, message: 'payment_record_unavailable' });
    }
    setAppAttr('app.payment.status', 'tokenized');
    setAppAttr('app.payment.provider', 'demo-psp');
    callback(null, { token, brand: card.brand, last4: card.last4, exp_month: card.exp_month, exp_year: card.exp_year });
  }

  // Declined cards are definitive (INVALID_ARGUMENT); everything else is an unknown outcome the
  // caller must resolve with GetCharge before retrying.
  static toGrpcError(err) {
    if (err && err.declined) { return { code: grpc.status.INVALID_ARGUMENT, message: err.message }; }
    if (err && err.failedPrecondition) { return { code: grpc.status.FAILED_PRECONDITION, message: err.message }; }
    if (err && err.unavailable) { return { code: grpc.status.UNAVAILABLE, message: err.message }; }
    return { code: grpc.status.INTERNAL, message: 'internal_error' };
  }

  static CheckHandler(call, callback) {
    callback(null, { status: 'SERVING' });
  }


  listen() {
    const server = this.server 
    const port = this.port
    server.bindAsync(
      `[::]:${port}`,
      grpc.ServerCredentials.createInsecure(),
      function () {
        logger.info(`PaymentService gRPC server started on port ${port}`);
        server.start();
      }
    );
  }

  loadProto(path) {
    const packageDefinition = protoLoader.loadSync(
      path,
      {
        keepCase: true,
        longs: String,
        enums: String,
        defaults: true,
        oneofs: true
      }
    );
    return grpc.loadPackageDefinition(packageDefinition);
  }

  loadAllProtos(protoRoot) {
    const hipsterShopPackage = this.packages.hipsterShop.hipstershop;
    const healthPackage = this.packages.health.grpc.health.v1;

    this.server.addService(
      hipsterShopPackage.PaymentService.service,
      {
        charge: HipsterShopServer.ChargeServiceHandler.bind(this),
        getCharge: HipsterShopServer.GetChargeHandler.bind(this),
        tokenize: HipsterShopServer.TokenizeHandler.bind(this)
      }
    );

    this.server.addService(
      healthPackage.Health.service,
      {
        check: HipsterShopServer.CheckHandler.bind(this)
      }
    );
  }
}

HipsterShopServer.PORT = process.env.PORT;

module.exports = HipsterShopServer;
