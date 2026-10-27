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

const cardValidator = require('simple-card-validator');
const { v4: uuidv4 } = require('uuid');
const api = require('@opentelemetry/api');
const { createLogger } = require('./logger');
const { stampAppAttrs, setAppAttr } = require('./otel');
const idem = require('./idempotency');

const logger = createLogger('paymentservice-charge');


class CreditCardError extends Error {
  constructor (message) {
    super(message);
    this.code = 400;
    this.declined = true; // definitive: the card was NOT charged
  }
}

class InvalidCreditCard extends CreditCardError {
  constructor (cardType) {
    super(`Credit card info is invalid`);
  }
}

class UnacceptedCreditCard extends CreditCardError {
  constructor (cardType) {
    super(`Sorry, we cannot process ${cardType} credit cards. Only VISA or MasterCard is accepted.`);
  }
}

class ExpiredCreditCard extends CreditCardError {
  constructor (number, month, year) {
    super(`Your credit card (ending ${number.substr(-4)}) expired on ${month}/${year}`);
  }
}

const catalog = require('./catalog');

/**
 * Verifies the card (or demo token) and (pretend) charges it.
 *
 * With an idempotency key the charge is recorded durably first: a repeat returns the ORIGINAL
 * transaction id with replayed=true and never charges twice. Card numbers, CVVs and tokens are never
 * logged or put on spans.
 */
module.exports = async function charge (request) {
  stampAppAttrs();
  const { amount, credit_card: creditCard, idempotency_key: key, payment_token: token } = request;
  let cardType = 'token';
  let last4 = '';
  if (token) {
    // The token must be one this service issued: it resolves to a catalog entry server-side.
    if (!catalog.TOKEN_RE.test(token) || !idem.enabled()) { throw new InvalidCreditCard(); }
    let catalogId;
    try {
      catalogId = await idem.resolveToken(token);
    } catch (e) {
      const err = new Error('payment_record_unavailable');
      err.unavailable = true;
      throw err;
    }
    const card = catalog.entry(catalogId);
    if (!card) { throw new InvalidCreditCard(); }
    cardType = card.brand;
    last4 = card.last4;
    setAppAttr('app.payment.method_type', 'demo_card_saved');
  } else {
    const cardNumber = creditCard.credit_card_number;
    const cardInfo = cardValidator(cardNumber);
    const details = cardInfo.getCardDetails();
    cardType = details.card_type;
    if (!details.valid) { throw new InvalidCreditCard(); }
    // Only VISA and mastercard is accepted, other card types (AMEX, dinersclub) will
    // throw UnacceptedCreditCard error.
    if (!(cardType === 'visa' || cardType === 'mastercard')) { throw new UnacceptedCreditCard(cardType); }
    const currentMonth = new Date().getMonth() + 1;
    const currentYear = new Date().getFullYear();
    const { credit_card_expiration_year: year, credit_card_expiration_month: month } = creditCard;
    if ((currentYear * 12 + currentMonth) > (year * 12 + month)) { throw new ExpiredCreditCard(cardNumber.replace('-', ''), month, year); }
    last4 = cardNumber.replace(/\D/g, '').slice(-4);
    setAppAttr('app.payment.method_type', 'card_form');
  }

  let transaction_id;
  let replayed = false;
  if (key) {
    if (!idem.enabled()) { const e = new Error('idempotent charges require ORDER_QUEUE_ADDR'); e.unavailable = true; throw e; }
    let rec;
    try {
      rec = await idem.record(key, amount);
    } catch (e) {
      const err = new Error('payment_record_unavailable');
      err.unavailable = true;
      throw err;
    }
    const amt = `${amount.currency_code}:${amount.units}.${amount.nanos}`;
    if (rec.replayed && rec.amount !== amt) {
      const err = new Error('idempotency_mismatch');
      err.failedPrecondition = true;
      throw err;
    }
    transaction_id = rec.txn;
    replayed = rec.replayed;
    if (!replayed && idem.testFault() === 'exit_after_record') { process.exit(1); }
    if (!replayed && idem.testFault().startsWith('slow_after_record')) {
      await new Promise((r) => setTimeout(r, Number(idem.testFault().split(':')[1] || 12000)));
    }
  } else {
    transaction_id = uuidv4();
  }
  logger.info(`Transaction processed: ${cardType}${last4 ? ` ending ${last4}` : ''} replayed=${replayed}`);
  setAppAttr('app.payment.status', 'charged');
  setAppAttr('app.payment.provider', 'demo-psp');
  const span = api.trace.getActiveSpan();
  if (span) { span.setAttribute('app.payment.transaction_id', transaction_id); }
  return { transaction_id, replayed };
};

module.exports.CreditCardError = CreditCardError;
