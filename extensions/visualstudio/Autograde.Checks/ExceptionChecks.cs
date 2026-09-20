using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Autograde.Core;
using Newtonsoft.Json.Linq;

internal static class ExceptionChecks
{
    static readonly CancellationToken None = CancellationToken.None;
    static HttpResponseMessage Json(string body, int status = 200) => new HttpResponseMessage((HttpStatusCode)status)
    { Content = new StringContent(body, Encoding.UTF8, "application/json") };
    sealed class Handler : HttpMessageHandler
    {
        public Func<HttpRequestMessage, Task<HttpResponseMessage>> Action;
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken token) => Action(request);
    }
    static HttpResponseMessage Auth(string path)
    {
        switch (path)
        {
            case "/v1/device-authorizations": return Json("{\"device_code\":\"device\"}");
            case "/v1/assignment-claims/redeem": return Json("{\"assignment_id\":\"asn_one\",\"delivery_mode\":\"bundle\"}");
            case "/v1/device-authorizations/token": return Json("{\"access_token\":\"old\",\"refresh_token\":\"refresh\",\"expires_in\":3600}");
            default: throw new Exception("Unexpected request " + path);
        }
    }
    static async Task Throws<T>(Func<Task> action) where T : Exception
    { try { await action(); } catch (T) { return; } throw new Exception("Expected " + typeof(T).Name); }
    static void Check(bool value, string message) { if (!value) throw new Exception(message); }

    public static async Task<int> Run()
    {
        var failures = new List<string>(); int passed = 0;
        async Task Case(string name, Func<Task> test)
        { try { await test(); passed++; } catch (Exception ex) { failures.Add(name + ": " + ex.GetType().Name); } }
        await Case("HTML health response is not a healthy API", async () =>
        {
            using var client = new ServiceClient("https://example.edu", new Handler { Action = _ => Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent("<html>login</html>") }) });
            await Throws<InvalidDataException>(() => client.HealthAsync(None));
        });
        await Case("invalid new claim must not retain previous student", async () =>
        {
            using var client = new ServiceClient("https://example.edu", new Handler { Action = request => Task.FromResult(Auth(request.RequestUri.AbsolutePath)) });
            await client.LoginAsync("AK1-ABCD-EFGH-IJKL", None);
            await Throws<ArgumentException>(() => client.LoginAsync("wrong", None));
            Check(!client.HasSession, "old student's session retained");
        });
        await Case("missing selected assignment never falls back to another", () =>
        {
            var items = new JArray(new JObject { ["assignment_id"] = "other", ["delivery_mode"] = "bundle" });
            return Throws<InvalidOperationException>(() => Task.FromResult(AssignmentSelection.Select(items, "removed")));
        });
        await Case("keep explicitly selected assignment", () =>
        {
            var items = new JArray(new JObject { ["assignment_id"] = "first", ["delivery_mode"] = "bundle" },
                new JObject { ["assignment_id"] = "second", ["delivery_mode"] = "bundle" });
            Check((string)AssignmentSelection.Select(items, "second")["assignment_id"] == "second", "selection changed");
            return Task.CompletedTask;
        });
        await Case("cancelled submission stops before reading folder", () =>
            Throws<OperationCanceledException>(() => Task.FromResult(Bundle.CreateSubmission("does-not-exist", new CancellationToken(true)))));
        await Case("cancelled starter stops before writes", () =>
            Throws<OperationCanceledException>(() => { Bundle.ExtractStarter(Array.Empty<byte>(), "not-created", "https://example.edu", "asn", new CancellationToken(true)); return Task.CompletedTask; }));
        await Case("wrong health status", async () =>
        {
            using var client = new ServiceClient("https://example.edu", new Handler { Action = _ => Task.FromResult(Json("{\"status\":\"down\"}")) });
            await Throws<InvalidDataException>(() => client.HealthAsync(None));
        });
        await Case("oversized auth error retains HTTP denial", async () =>
        {
            using var client = new ServiceClient("https://example.edu", new Handler { Action = request =>
            {
                if (request.RequestUri.AbsolutePath == "/v1/assignments" || request.RequestUri.AbsolutePath == "/v1/tokens/refresh")
                { var response = Json("{}", 401); response.Content.Headers.ContentLength = 3 * 1024 * 1024; return Task.FromResult(response); }
                return Task.FromResult(Auth(request.RequestUri.AbsolutePath));
            } });
            await client.LoginAsync("AK1-ABCD-EFGH-IJKL", None);
            await Throws<ServiceError>(() => client.AssignmentsAsync(None));
            Check(!client.HasSession, "oversized unauthorized response retained credentials");
        });
        await Case("trailing JSON response", async () =>
        {
            using var client = new ServiceClient("https://example.edu", new Handler { Action = _ => Task.FromResult(Json("{\"status\":\"ok\"}{}")) });
            await Throws<InvalidDataException>(() => client.HealthAsync(None));
        });
        await Case("malformed 401 body preserves auth error", async () =>
        {
            using var client = new ServiceClient("https://example.edu", new Handler { Action = request => Task.FromResult(
                request.RequestUri.AbsolutePath == "/v1/assignments" || request.RequestUri.AbsolutePath == "/v1/tokens/refresh"
                    ? Json("{\"error\":[]}", 401) : Auth(request.RequestUri.AbsolutePath)) });
            await client.LoginAsync("AK1-ABCD-EFGH-IJKL", None);
            await Throws<ServiceError>(() => client.AssignmentsAsync(None));
            Check(!client.HasSession, "unauthorized credentials retained");
        });
        await Case("lost rotating refresh response clears unusable session", async () =>
        {
            using var client = new ServiceClient("https://example.edu", new Handler { Action = request =>
            {
                string path = request.RequestUri.AbsolutePath;
                if (path == "/v1/tokens/refresh") throw new HttpRequestException("lost response");
                return Task.FromResult(path == "/v1/assignments" ? Json("{}", 401) : Auth(path));
            } });
            await client.LoginAsync("AK1-ABCD-EFGH-IJKL", None);
            await Throws<HttpRequestException>(() => client.AssignmentsAsync(None));
            Check(!client.HasSession, "stale refresh retained");
        });
        await Case("expired access is refreshed for server logout", async () =>
        {
            bool revoked = false;
            using var client = new ServiceClient("https://example.edu", new Handler { Action = request =>
            {
                var path = request.RequestUri.AbsolutePath;
                if (path == "/v1/device-authorizations/token") return Task.FromResult(Json("{\"access_token\":\"old\",\"refresh_token\":\"refresh\",\"expires_in\":1}"));
                if (path == "/v1/tokens/refresh") return Task.FromResult(Json("{\"access_token\":\"new\",\"refresh_token\":\"rotated\",\"expires_in\":3600}"));
                if (path == "/v1/sessions/current") { revoked = request.Headers.Authorization?.Parameter == "new"; return Task.FromResult(Json("", 204)); }
                return Task.FromResult(Auth(path));
            } });
            await client.LoginAsync("AK1-ABCD-EFGH-IJKL", None); await client.LogoutAsync(None);
            Check(revoked && !client.HasSession, "server logout skipped refresh");
        });
        await Case("dispose cannot resurrect a late login", async () =>
        {
            var entered = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            var release = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            var client = new ServiceClient("https://example.edu", new Handler { Action = async request =>
            {
                if (request.RequestUri.AbsolutePath == "/v1/device-authorizations/token") { entered.SetResult(true); await release.Task; }
                return Auth(request.RequestUri.AbsolutePath);
            } });
            var login = client.LoginAsync("AK1-ABCD-EFGH-IJKL", None);
            await entered.Task; client.Dispose(); release.SetResult(true);
            try { await login; } catch (OperationCanceledException) { } catch (ObjectDisposedException) { }
            Check(!client.HasSession, "session resurrected after disposal");
        });
        await Case("new submissions get new keys; transport retry retains its key", async () =>
        {
            var keys = new List<string>(); byte[] archive = Encoding.UTF8.GetBytes("immutable snapshot");
            using var client = new ServiceClient("https://example.edu", new Handler { Action = request =>
            {
                if (!request.RequestUri.AbsolutePath.EndsWith("/submissions")) return Task.FromResult(Auth(request.RequestUri.AbsolutePath));
                keys.Add(request.Headers.GetValues("Idempotency-Key").Single());
                if (keys.Count == 1) throw new HttpRequestException("ambiguous receipt");
                return Task.FromResult(Json(new JObject { ["submission"] = new JObject {
                    ["submission_id"] = "bsub_one", ["assignment_id"] = "asn_one", ["state"] = "queued", ["source_sha256"] = Bundle.Digest(archive) } }.ToString(), 202));
            } });
            await client.LoginAsync("AK1-ABCD-EFGH-IJKL", None);
            await Task.WhenAll(client.SubmitAsync("asn_one", archive, None), client.SubmitAsync("asn_one", archive, None));
            // A subsequent explicit submission is a new attempt, even for unchanged bytes.
            await client.SubmitAsync("asn_one", archive, None);
            Check(keys.Count == 4 && keys[0] == keys[1] && keys.Distinct().Count() == 3, "new attempt versus retry identity");
        });
        await Case("receipt must belong to the submitted assignment", async () =>
        {
            byte[] bytes = Encoding.UTF8.GetBytes("snapshot");
            using var client = new ServiceClient("https://example.edu", new Handler { Action = request => Task.FromResult(
                request.RequestUri.AbsolutePath.EndsWith("/submissions") ? Json(new JObject { ["submission"] = new JObject {
                    ["submission_id"] = "bsub_other", ["assignment_id"] = "asn_other", ["source_sha256"] = Bundle.Digest(bytes) } }.ToString()) : Auth(request.RequestUri.AbsolutePath)) });
            await client.LoginAsync("AK1-ABCD-EFGH-IJKL", None);
            await Throws<InvalidDataException>(() => client.SubmitAsync("asn_one", bytes, None));
        });
        await Case("grade must belong to the requested submission", async () =>
        {
            using var client = new ServiceClient("https://example.edu", new Handler { Action = request => Task.FromResult(
                request.RequestUri.AbsolutePath.EndsWith("/result") ? Json("{\"result\":{\"submission_id\":\"bsub_other\",\"state\":\"published\",\"score\":10}}") : Auth(request.RequestUri.AbsolutePath)) });
            await client.LoginAsync("AK1-ABCD-EFGH-IJKL", None);
            await Throws<InvalidDataException>(() => client.ResultAsync("bsub_one", None));
        });
        await Case("logout discards an in-flight authenticated response", async () =>
        {
            var entered = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            var release = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            using var client = new ServiceClient("https://example.edu", new Handler { Action = async request =>
            {
                if (request.RequestUri.AbsolutePath == "/v1/assignments")
                { entered.SetResult(true); await release.Task; return Json("{\"assignments\":[]}"); }
                return Auth(request.RequestUri.AbsolutePath);
            } });
            await client.LoginAsync("AK1-ABCD-EFGH-IJKL", None);
            var request = client.AssignmentsAsync(None); await entered.Task;
            await Throws<OperationCanceledException>(() => client.LogoutAsync(new CancellationToken(true)));
            release.SetResult(true);
            await Throws<ServiceError>(() => request);
        });
        await Case("queued submission must not migrate to a different login", async () =>
        {
            var entered = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            var release = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            int posts = 0;
            using var client = new ServiceClient("https://example.edu", new Handler { Action = async request =>
            {
                string path = request.RequestUri.AbsolutePath;
                if (path.EndsWith("/submissions")) { posts++; entered.TrySetResult(true); await release.Task; throw new HttpRequestException("lost response"); }
                return path == "/v1/sessions/current" ? Json("", 204) : Auth(path);
            } });
            await client.LoginAsync("AK1-ABCD-EFGH-IJKL", None);
            byte[] bytes = Encoding.UTF8.GetBytes("snapshot");
            var first = client.SubmitAsync("asn_one", bytes, None); await entered.Task;
            var second = client.SubmitAsync("asn_one", bytes, None);
            release.SetResult(true);
            await client.LogoutAsync(None);
            await client.LoginAsync("AK1-ABCD-EFGH-IJKL", None);
            await Throws<ServiceError>(() => first); await Throws<ServiceError>(() => second);
            Check(posts == 1, "old files sent using new login");
        });
        await Case("download diagnostic contains no raw exception or path", () =>
        {
            var diagnostic = new DownloadDiagnostic { Stage = "installing" };
            diagnostic.Fail(new UnauthorizedAccessException("token-secret C:\\Users\\private"), false);
            Check(diagnostic.Code == "AG-DL-LOCAL-PERMISSION", "permission classification");
            var text = diagnostic.Details + diagnostic.Payload("0.5.2").ToString();
            Check(!text.Contains("token-secret") && !text.Contains("C:\\Users"), "raw exception leaked");
            Check((int)diagnostic.Payload("0.5.2")["seq"] == 1, "sequence must increase");
            return Task.CompletedTask;
        });
        await Case("IDE open error preserves download success", () =>
        {
            var diagnostic = new DownloadDiagnostic { Stage = "opening", Outcome = "succeeded" };
            diagnostic.Fail(new InvalidOperationException("unsafe details"), false);
            Check(diagnostic.Outcome == "succeeded" && diagnostic.OpenOutcome == "open_failed", "file readiness lost");
            Check(DownloadDiagnostic.Classify(new OperationCanceledException(), false, "requesting") == "AG-DL-NETWORK-TIMEOUT", "timeout classification");
            Check(DownloadDiagnostic.Classify(new OperationCanceledException(), true, "requesting") == "AG-DL-USER-CANCELLED", "cancel classification");
            return Task.CompletedTask;
        });
        await Case("IDE open recovery guidance retains local path without sending it", () =>
        {
            var diagnostic = new DownloadDiagnostic { Stage = "opening", Outcome = "succeeded" };
            diagnostic.Fail(new InvalidOperationException("private native exception"), false);
            var guidance = DownloadDiagnostic.OpenRecoveryGuidance(@"C:\Users\student\한글 과제");
            Check(guidance.Contains(@"C:\Users\student\한글 과제") && guidance.Contains("파일 → 열기 → 폴더") && guidance.Contains("과제 폴더 열기"), "missing recovery steps or path");
            Check(guidance.Contains("재다운로드할 필요는 없습니다") && guidance.Contains("모두 저장") && guidance.Contains("문의번호"), "missing preservation/support instructions");
            var remote = diagnostic.Payload("0.5.5").ToString() + diagnostic.Details;
            Check(!remote.Contains("C:\\Users") && !remote.Contains("private native exception"), "local recovery information leaked");
            return Task.CompletedTask;
        });
        await Case("successful IDE retry clears previous open failure", () =>
        {
            var diagnostic = new DownloadDiagnostic { Stage = "opening", Outcome = "succeeded" };
            diagnostic.Fail(new InvalidOperationException(), false);
            diagnostic.MarkOpened();
            Check(diagnostic.OpenOutcome == "opened" && diagnostic.Outcome == "succeeded" && diagnostic.Code == null && diagnostic.Status == null, "old failure remains after retry");
            Check(!diagnostic.Details.Contains("IDE 열기 실패"), "old failure summary remains");
            return Task.CompletedTask;
        });
        foreach (int status in new[] { 401, 404, 503 })
        await Case("diagnostic failure does not log out student " + status, async () =>
        {
            using var client = new ServiceClient("https://example.edu", new Handler { Action = request =>
                Task.FromResult(request.RequestUri.AbsolutePath.EndsWith("/download-diagnostics") ? Json("{}", status) : Auth(request.RequestUri.AbsolutePath)) });
            await client.LoginAsync("AK1-ABCD-EFGH-IJKL", None);
            var result = await client.ReportDownloadAsync("asn_one", new DownloadDiagnostic().Payload("0.5.2"), None);
            Check(result != "서버에 전달됨" && client.HasSession, "diagnostics altered session");
        });
        if (failures.Count != 0) throw new Exception(string.Join("\n", failures));
        return passed;
    }
}
