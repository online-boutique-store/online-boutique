using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text.Json;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Logging.Abstractions;
using Microsoft.Extensions.Logging.Console;

namespace cartservice;

public sealed class CorrelatedJsonConsoleFormatter : ConsoleFormatter
{
    public const string FormatterName = "correlated-json";

    public CorrelatedJsonConsoleFormatter()
        : base(FormatterName)
    {
    }

    public override void Write<TState>(
        in LogEntry<TState> logEntry,
        IExternalScopeProvider scopeProvider,
        TextWriter textWriter)
    {
        var payload = new Dictionary<string, object>
        {
            ["timestamp"] = DateTimeOffset.UtcNow.ToString("O"),
            ["severity"] = logEntry.LogLevel.ToString(),
            ["message"] = logEntry.Formatter(logEntry.State, logEntry.Exception),
        };

        var pod = Environment.GetEnvironmentVariable("K8S_POD_NAME");
        if (!string.IsNullOrEmpty(pod))
        {
            payload["k8s.pod.name"] = pod;
        }

        var ns = Environment.GetEnvironmentVariable("K8S_NAMESPACE");
        if (!string.IsNullOrEmpty(ns))
        {
            payload["k8s.namespace.name"] = ns;
        }

        var activity = Activity.Current;
        if (activity is { Recorded: true } && activity.TraceId != default)
        {
            payload["trace_id"] = activity.TraceId.ToString();
            payload["span_id"] = activity.SpanId.ToString();
            payload["log_type"] = "application";
        }

        if (logEntry.Exception != null)
        {
            payload["exception"] = logEntry.Exception.ToString();
        }

        textWriter.WriteLine(JsonSerializer.Serialize(payload));
    }
}
