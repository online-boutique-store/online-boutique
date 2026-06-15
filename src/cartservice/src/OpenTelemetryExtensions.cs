using System;
using System.Reflection;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using OpenTelemetry.Logs;
using OpenTelemetry.Metrics;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;

namespace cartservice;

public static class OpenTelemetryExtensions
{
    public static IServiceCollection AddAppTelemetry(
        this IServiceCollection services,
        IConfiguration configuration)
    {
        var serviceName =
            configuration["OTEL_SERVICE_NAME"] ??
            Assembly.GetEntryAssembly()?.GetName().Name ??
            "cartservice";

        var redisPeerService = GetRedisPeerService(configuration["REDIS_ADDR"]);

        services.AddOpenTelemetry()
            .ConfigureResource(resource => resource
                .AddService(serviceName)
                .AddEnvironmentVariableDetector())
            .WithTracing(tracing =>
            {
                tracing
                    .AddAspNetCoreInstrumentation(options => options.RecordException = true)
                    .AddGrpcClientInstrumentation()
                    .AddOtlpExporter();

                if (!string.IsNullOrEmpty(redisPeerService))
                {
                    tracing.AddRedisInstrumentation(options =>
                    {
                        options.FlushInterval = TimeSpan.FromSeconds(1);
                        options.SetVerboseDatabaseStatements = true;
                        options.Enrich = (activity, _) =>
                        {
                            activity.SetTag("peer.service", redisPeerService);
                            activity.SetTag("server.address", redisPeerService);
                            activity.SetTag("db.name", redisPeerService);
                        };
                    });
                }
            })
            .WithMetrics(metrics => metrics
                .AddRuntimeInstrumentation()
                .AddProcessInstrumentation()
                .AddAspNetCoreInstrumentation()
                .AddOtlpExporter())
            .WithLogging(logging => logging.AddOtlpExporter());

        return services;
    }

    private static string GetRedisPeerService(string redisAddress)
    {
        if (string.IsNullOrEmpty(redisAddress))
        {
            return null;
        }

        var host = redisAddress.Split(':')[0];
        return string.IsNullOrEmpty(host) ? "redis-cart" : host;
    }
}
