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

import logging
import os
import sys

from opentelemetry import trace
from pythonjsonlogger import jsonlogger


class CustomJsonFormatter(jsonlogger.JsonFormatter):
  def add_fields(self, log_record, record, message_dict):
    super(CustomJsonFormatter, self).add_fields(log_record, record, message_dict)
    if not log_record.get('timestamp'):
      log_record['timestamp'] = record.created
    if log_record.get('severity'):
      log_record['severity'] = log_record['severity'].upper()
    else:
      log_record['severity'] = record.levelname

    pod = os.environ.get('K8S_POD_NAME')
    if pod:
      log_record['k8s.pod.name'] = pod
    namespace = os.environ.get('K8S_NAMESPACE')
    if namespace:
      log_record['k8s.namespace.name'] = namespace

    span = trace.get_current_span()
    span_context = span.get_span_context()
    if span_context.is_valid:
      log_record['trace_id'] = format(span_context.trace_id, '032x')
      log_record['span_id'] = format(span_context.span_id, '016x')
      log_record['log_type'] = 'application'


def getJSONLogger(name):
  logger = logging.getLogger(name)
  if logger.handlers:
    return logger

  logger.setLevel(logging.INFO)
  logger.propagate = False

  if os.environ.get('OTEL_LOGS_EXPORTER') == 'otlp':
    return logger

  handler = logging.StreamHandler(sys.stdout)
  formatter = CustomJsonFormatter('%(timestamp)s %(severity)s %(name)s %(message)s')
  handler.setFormatter(formatter)
  logger.addHandler(handler)
  return logger
