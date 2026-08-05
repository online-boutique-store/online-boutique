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

using System.Diagnostics;

namespace cartservice
{
    public static class OtelAttrs
    {
        /// <summary>
        /// Copies every app.* baggage entry onto the current activity (span).
        /// Call at the entry of every gRPC handler so each span carries all
        /// attributes that exist at that point in the request flow.
        /// </summary>
        public static void StampAppAttrs()
        {
            var activity = Activity.Current;
            if (activity == null)
            {
                return;
            }

            foreach (var kv in OpenTelemetry.Baggage.Current.GetBaggage())
            {
                if (kv.Key != null && kv.Key.StartsWith("app."))
                {
                    activity.SetTag(kv.Key, kv.Value);
                }
            }
        }
    }
}
