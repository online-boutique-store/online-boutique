// Copyright 2020 Google LLC
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

using Microsoft.AspNetCore.Hosting;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Console;
using cartservice;

CreateHostBuilder(args).Build().Run();

static IHostBuilder CreateHostBuilder(string[] args) =>
    Host.CreateDefaultBuilder(args)
        .ConfigureLogging((_, logging) =>
        {
            logging.ClearProviders();
            logging.SetMinimumLevel(LogLevel.Information);

            if (ShouldExportOtlpLogs())
            {
                logging.AddOpenTelemetry(options =>
                {
                    options.IncludeFormattedMessage = true;
                    options.IncludeScopes = true;
                    options.ParseStateValues = true;
                });
            }
            else
            {
                // Stamp trace_id/span_id (from the ambient Activity.Current)
                // onto every stdout log line via CorrelatedJsonConsoleFormatter
                // -- this is what actually reaches Observe today, since
                // OTEL_LOGS_EXPORTER=none in every current deployment makes
                // the AddOpenTelemetry() branch above a no-op.
                logging.AddConsole(options => options.FormatterName = CorrelatedJsonConsoleFormatter.FormatterName)
                    .AddConsoleFormatter<CorrelatedJsonConsoleFormatter, ConsoleFormatterOptions>();
            }
        })
        .ConfigureWebHostDefaults(webBuilder =>
        {
            webBuilder.UseStartup<Startup>();
        });

static bool ShouldExportOtlpLogs() =>
    string.Equals(
        System.Environment.GetEnvironmentVariable("ENABLE_TRACING"),
        "1",
        System.StringComparison.Ordinal)
    && string.Equals(
        System.Environment.GetEnvironmentVariable("OTEL_LOGS_EXPORTER"),
        "otlp",
        System.StringComparison.OrdinalIgnoreCase);
