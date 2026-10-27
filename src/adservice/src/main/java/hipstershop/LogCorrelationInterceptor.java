/*
 * Copyright 2018 Google LLC.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package hipstershop;

import io.grpc.ForwardingServerCallListener;
import io.grpc.Metadata;
import io.grpc.ServerCall;
import io.grpc.ServerCallHandler;
import io.grpc.ServerInterceptor;
import io.opentelemetry.api.trace.Span;
import io.opentelemetry.api.trace.SpanContext;
import org.apache.logging.log4j.ThreadContext;

/**
 * Sets log_type=application on gRPC request threads for Observe log
 * filtering, and explicitly stamps trace_id/span_id into Log4j2's
 * ThreadContext (MDC) so ObserveLogLayout.json's mdc resolver can pick them
 * up. This is done explicitly here -- reading Span.current() from the
 * OTel Java API, which the javaagent's zero-code gRPC instrumentation has
 * already bound to this thread by the time this interceptor runs -- rather
 * than relying on the agent's own log4j-context-data auto-instrumentation
 * module, so trace/log correlation doesn't depend on undocumented default
 * behavior for whatever agent version happens to be pinned.
 */
public final class LogCorrelationInterceptor implements ServerInterceptor {

  private static final String LOG_TYPE = "log_type";
  private static final String APPLICATION = "application";
  private static final String TRACE_ID = "trace_id";
  private static final String SPAN_ID = "span_id";

  @Override
  public <ReqT, RespT> ServerCall.Listener<ReqT> interceptCall(
      ServerCall<ReqT, RespT> call,
      Metadata headers,
      ServerCallHandler<ReqT, RespT> next) {
    ThreadContext.put(LOG_TYPE, APPLICATION);

    SpanContext spanContext = Span.current().getSpanContext();
    if (spanContext.isValid()) {
      ThreadContext.put(TRACE_ID, spanContext.getTraceId());
      ThreadContext.put(SPAN_ID, spanContext.getSpanId());
    }

    ServerCall.Listener<ReqT> listener = next.startCall(call, headers);
    return new ForwardingServerCallListener.SimpleForwardingServerCallListener<ReqT>(listener) {
      @Override
      public void onComplete() {
        ThreadContext.remove(LOG_TYPE);
        ThreadContext.remove(TRACE_ID);
        ThreadContext.remove(SPAN_ID);
        super.onComplete();
      }

      @Override
      public void onCancel() {
        ThreadContext.remove(LOG_TYPE);
        ThreadContext.remove(TRACE_ID);
        ThreadContext.remove(SPAN_ID);
        super.onCancel();
      }
    };
  }
}
