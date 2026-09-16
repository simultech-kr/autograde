using System;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json.Linq;

namespace Autograde.Core
{
    public enum GradingWatchEnd { Finished, Expired, Unavailable }

    // Reads results only. A failed read never reverses acceptance or resubmits source.
    public static class GradingPoller
    {
        public static bool IsFinished(string state) => state == "published" || state == "graded" ||
            state == "rejected" || state == "infra_failed" || state == "assessment_failed" || state == "failed";

        public static string Label(string state)
        {
            switch (state)
            {
                case "received": return "접수됨 · 서버 검증 대기";
                case "accepted": case "queued": return "채점 대기";
                case "running": return "채점 중";
                case "graded": return "채점 완료 · 결과 공개 대기";
                case "published": return "결과 공개됨";
                case "rejected": return "제출 검증 거절 · 교수자에게 확인하세요";
                case "infra_failed": return "채점 환경 오류 · 교수자에게 문의하세요";
                case "assessment_failed": case "failed": return "채점 작업 실패 · 교수자에게 문의하세요";
                default: return "상태 확인 필요";
            }
        }

        public static async Task<GradingWatchEnd> WatchAsync(
            Func<CancellationToken, Task<JObject>> fetch, Action<JObject> update,
            Action<string> notice, DateTimeOffset deadline, CancellationToken cancel,
            Func<TimeSpan, CancellationToken, Task> delay = null, Func<DateTimeOffset> now = null)
        {
            delay = delay ?? Task.Delay;
            now = now ?? (() => DateTimeOffset.UtcNow);
            cancel.ThrowIfCancellationRequested();
            var remaining = deadline - now();
            if (remaining <= TimeSpan.Zero) return GradingWatchEnd.Expired;
            using (var window = CancellationTokenSource.CreateLinkedTokenSource(cancel))
            {
                window.CancelAfter(remaining);
                try
                {
                    while (now() < deadline)
                    {
                        var pause = deadline - now();
                        if (pause <= TimeSpan.Zero) break;
                        await delay(pause < TimeSpan.FromSeconds(5) ? pause : TimeSpan.FromSeconds(5), window.Token);
                        window.Token.ThrowIfCancellationRequested();
                        if (now() >= deadline) break;
                        JObject result;
                        try { result = await fetch(window.Token); }
                        catch (Exception ex) when (!window.IsCancellationRequested &&
                            (ex is HttpRequestException || ex is OperationCanceledException ||
                             ex is ServiceError se && (se.Status >= 500 || se.Status == 429)))
                        {
                            notice("접수는 유지됩니다. 결과 연결을 다시 확인하는 중입니다.");
                            continue;
                        }
                        catch (ServiceError) when (!window.IsCancellationRequested)
                        {
                            notice("접수는 유지됩니다. 결과 조회 권한·로그인 상태를 확인하고 최신 결과를 조회하세요.");
                            return GradingWatchEnd.Unavailable;
                        }
                        // A transport may complete after cancellation. Never publish that stale response.
                        window.Token.ThrowIfCancellationRequested();
                        if (now() >= deadline) break;
                        update(result);
                        if (IsFinished((string)result["state"])) return GradingWatchEnd.Finished;
                    }
                }
                catch (OperationCanceledException) when (!cancel.IsCancellationRequested && window.IsCancellationRequested) { }
            }
            cancel.ThrowIfCancellationRequested();
            return GradingWatchEnd.Expired;
        }
    }
}
