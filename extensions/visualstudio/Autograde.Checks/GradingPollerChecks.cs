using System;
using System.Collections.Generic;
using System.IO;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using Autograde.Core;
using Newtonsoft.Json.Linq;

internal static class GradingPollerChecks
{
    static JObject Result(string state, string id = "sub_one") => new JObject { ["state"] = state, ["submission_id"] = id };
    public static async Task<int> Run()
    {
        int checks = 0;
        void Check(bool value, string name) { if (!value) throw new Exception(name); checks++; }
        var now = DateTimeOffset.UtcNow;
        Task Delay(TimeSpan span, CancellationToken ct) { ct.ThrowIfCancellationRequested(); now += span; return Task.CompletedTask; }
        var sequence = new Queue<string>(new[] { "received", "accepted", "queued", "running", "published" });
        var seen = new List<string>();
        var end = await GradingPoller.WatchAsync(_ => Task.FromResult(Result(sequence.Dequeue())),
            r => seen.Add((string)r["state"]), _ => throw new Exception("unexpected notice"), now.AddMinutes(2), CancellationToken.None, Delay, () => now);
        Check(end == GradingWatchEnd.Finished && seen.Count == 5 && sequence.Count == 0, "queued through published");
        foreach (var state in new[] { "published", "graded", "rejected", "infra_failed", "assessment_failed", "failed" })
        {
            int calls = 0;
            end = await GradingPoller.WatchAsync(_ => { calls++; return Task.FromResult(Result(state)); },
                _ => { }, _ => { }, now.AddMinutes(2), CancellationToken.None, Delay, () => now);
            Check(end == GradingWatchEnd.Finished && calls == 1 && GradingPoller.Label(state) != "상태 확인 필요", "terminal " + state);
        }
        Check(!GradingPoller.IsFinished("new_server_state") && GradingPoller.Label("new_server_state") == "상태 확인 필요", "unknown state not success");
        int fetches = 0;
        end = await GradingPoller.WatchAsync(_ => { fetches++; return Task.FromResult(Result("queued")); }, _ => { }, _ => { },
            now, CancellationToken.None, Delay, () => now);
        Check(end == GradingWatchEnd.Expired && fetches == 0, "expired before request");
        end = await GradingPoller.WatchAsync(_ => { fetches++; return Task.FromResult(Result("queued")); }, _ => { }, _ => { },
            now.AddSeconds(11), CancellationToken.None, Delay, () => now);
        Check(end == GradingWatchEnd.Expired && fetches == 2, "bounded queued wait");
        foreach (var failure in new Exception[] { new HttpRequestException(), new TaskCanceledException(), new ServiceError(503, "busy"), new ServiceError(429, "limit") })
        {
            int attempts = 0, notices = 0;
            end = await GradingPoller.WatchAsync(_ => ++attempts == 1 ? Task.FromException<JObject>(failure) : Task.FromResult(Result("published")),
                _ => { }, message => { if (message.Contains("접수는 유지")) notices++; }, now.AddMinutes(2), CancellationToken.None, Delay, () => now);
            Check(end == GradingWatchEnd.Finished && attempts == 2 && notices == 1, "transient " + failure.GetType().Name);
        }
        foreach (var status in new[] { 401, 403, 404 })
        {
            int attempts = 0, updates = 0;
            end = await GradingPoller.WatchAsync(_ => { attempts++; throw new ServiceError(status, "denied"); }, _ => updates++, _ => { },
                now.AddMinutes(2), CancellationToken.None, Delay, () => now);
            Check(end == GradingWatchEnd.Unavailable && attempts == 1 && updates == 0, "do not retry access failure " + status);
        }
        foreach (var failure in new Exception[] {
            new ServiceError(401, "invalid_refresh_token"), new HttpRequestException(),
            new TaskCanceledException(), new InvalidDataException("invalid refresh response") })
        {
            bool authenticated = true;
            int attempts = 0, updates = 0, notices = 0;
            end = await GradingPoller.WatchAsync(_ => {
                    attempts++; authenticated = false;
                    return Task.FromException<JObject>(failure);
                }, _ => updates++, _ => notices++, now.AddMinutes(2), CancellationToken.None, Delay, () => now,
                hasSession: () => authenticated);
            Check(end == GradingWatchEnd.SessionExpired && attempts == 1 && updates == 0 && notices == 0,
                "lost session immediately requests login instead of retrying " + failure.GetType().Name);
        }
        using (var cancel = new CancellationTokenSource())
        {
            int updates = 0; var late = new TaskCompletionSource<JObject>();
            var waiting = GradingPoller.WatchAsync(_ => late.Task, _ => updates++, _ => { }, now.AddMinutes(2), cancel.Token, Delay, () => now);
            Check(!waiting.IsCompleted, "grading pending independently of acceptance");
            cancel.Cancel();
            late.SetResult(Result("published", "old_student_submission"));
            bool cancelled = false;
            try { await waiting; } catch (OperationCanceledException) { cancelled = true; }
            Check(cancelled && updates == 0, "logout/replacement ignores transport completing after cancellation");
        }
        using (var cancel = new CancellationTokenSource())
        {
            int updates = 0, calls = 0;
            var waiting = GradingPoller.WatchAsync(_ => { calls++; return Task.FromResult(Result("published")); }, _ => updates++, _ => { },
                DateTimeOffset.UtcNow.AddMinutes(2), cancel.Token, (_, ct) => Task.Delay(Timeout.Infinite, ct));
            cancel.Cancel();
            try { await waiting; } catch (OperationCanceledException) { }
            Check(calls == 0 && updates == 0, "cancel during polling interval");
        }
        int lateUpdates = 0;
        end = await GradingPoller.WatchAsync(_ => { now += TimeSpan.FromMinutes(3); return Task.FromResult(Result("published")); }, _ => lateUpdates++, _ => { },
            now.AddMinutes(2), CancellationToken.None, Delay, () => now);
        Check(end == GradingWatchEnd.Expired && lateUpdates == 0, "late result outside watch deadline");
        return checks;
    }
}
