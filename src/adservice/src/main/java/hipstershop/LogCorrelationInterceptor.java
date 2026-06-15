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
import org.apache.logging.log4j.ThreadContext;

/** Sets log_type=application on gRPC request threads for Observe log filtering. */
public final class LogCorrelationInterceptor implements ServerInterceptor {

  private static final String LOG_TYPE = "log_type";
  private static final String APPLICATION = "application";

  @Override
  public <ReqT, RespT> ServerCall.Listener<ReqT> interceptCall(
      ServerCall<ReqT, RespT> call,
      Metadata headers,
      ServerCallHandler<ReqT, RespT> next) {
    ThreadContext.put(LOG_TYPE, APPLICATION);
    ServerCall.Listener<ReqT> listener = next.startCall(call, headers);
    return new ForwardingServerCallListener.SimpleForwardingServerCallListener<ReqT>(listener) {
      @Override
      public void onComplete() {
        ThreadContext.remove(LOG_TYPE);
        super.onComplete();
      }

      @Override
      public void onCancel() {
        ThreadContext.remove(LOG_TYPE);
        super.onCancel();
      }
    };
  }
}
