using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Autograde.Core;
using Newtonsoft.Json.Linq;

static class ExamControlChecks
{
    static int checks;
    const string Origin = "https://grade.example.edu:20000", Control = "https://exam.example.edu:20443", Assignment = "asn_one";
    const string IdeToken = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG";
    static readonly CancellationToken None = CancellationToken.None;
    static void Check(bool value, string name) { if (!value) throw new Exception("Exam mode: " + name); checks++; }
    static async Task Reject(Func<Task> action, string name)
    {
        try { await action(); }
        catch (Exception ex) when (ex is InvalidDataException || ex is ArgumentException || ex is ExamControlError || ex is OperationCanceledException) { checks++; return; }
        throw new Exception("Exam mode accepted: " + name);
    }
    static JObject Status()
    {
        double now = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;
        return new JObject {
            ["exam"] = new JObject { ["id"] = "exam_1", ["title"] = "시스템 프로그래밍 중간고사", ["status"] = "active" },
            ["seat_id"] = "A-01", ["assignment_id"] = Assignment, ["autograde_origin"] = Origin,
            ["server_time"] = now, ["expires_at"] = now + 3600,
            ["agent"] = new JObject { ["online"] = true, ["mode"] = "enforced", ["backend"] = "windows-wfp-hyperv", ["state"] = "installed", ["last_seen"] = now, ["lease_expires_at"] = now + 180, ["verified"] = false },
            ["permissions"] = new JObject { ["can_release"] = false }
        };
    }
    static JObject Connected(JObject status) => new JObject { ["token"] = IdeToken, ["expires_at"] = status["expires_at"], ["status"] = status };
    static HttpResponseMessage Json(JObject result) => new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(new JObject { ["ok"] = true, ["result"] = result }.ToString(), Encoding.UTF8, "application/json") };
    sealed class Handler : HttpMessageHandler
    {
        public Func<HttpRequestMessage, CancellationToken, Task<HttpResponseMessage>> Reply;
        public int Calls;
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        { Calls++; return Reply(request, cancellationToken); }
    }
    static ExamControlClient Client(Handler handler) => new ExamControlClient(Control, Origin, Assignment, handler);
    public static async Task<int> Run()
    {
        checks = 0;
        var state = Status();
        var calls = new List<string>();
        var handler = new Handler { Reply = async (request, ct) => {
            Check(request.RequestUri.GetLeftPart(UriPartial.Authority) == Control, "origin fixed");
            Check(string.Join("", request.Headers.GetValues("Origin")) == Control, "same-origin header");
            var path = request.RequestUri.AbsolutePath; calls.Add(path);
            if (path == "/api/ide/connect") {
                Check(request.Headers.Authorization == null, "no Autograde credential on connect");
                var body = JObject.Parse(await request.Content.ReadAsStringAsync(ct));
                Check(body.Count == 3 && (string)body["code"] == "12345678" && (string)body["assignment_id"] == Assignment && (string)body["autograde_origin"] == Origin, "scoped code payload only");
                return Json(Connected(state));
            }
            Check(request.Headers.Authorization?.Parameter == IdeToken, "only IDE bearer sent");
            if (path == "/api/ide/status") return Json(state);
            Check(path == "/api/ide/disconnect" && request.Method == HttpMethod.Post && await request.Content.ReadAsStringAsync(ct) == "{}", "disconnect not Agent release");
            return Json(new JObject { ["disconnected"] = true });
        } };
        using (var client = Client(handler))
        {
            var status = await client.ConnectAsync("12345678", None);
            Check(client.HasSession && status.SeatId == "A-01", "memory session");
            Check(status.Label(TimeSpan.Zero).Contains("검증 전"), "installed is not verified");
            Check(status.Label(TimeSpan.FromSeconds(16)).Contains("확인 불가"), "stale cannot show installed");
            Check((await client.StatusAsync(None)).AssignmentId == Assignment, "status scope");
            await client.DisconnectAsync(None); Check(!client.HasSession, "disconnect clears memory");
            await Reject(() => client.StatusAsync(None), "status after disconnect");
        }
        Check(calls.Count == 3, "no unauthorized control request");
        foreach (var code in new[] { "", "123456", "123456789", "１２３４５６７８", "12345678\n", "AK1-ABCD-EFGH-IJKL" })
        {
            var h = new Handler(); using var c = Client(h);
            await Reject(() => c.ConnectAsync(code, None), "invalid code"); Check(h.Calls == 0, "invalid code local only");
        }
        foreach (var edit in new Action<JObject>[] {
            s => s["assignment_id"] = "another", s => s["autograde_origin"] = "https://evil.example",
            s => s["permissions"]["can_release"] = true, s => s["agent"]["verified"] = true,
            s => s["agent"]["online"] = 1, s => s["agent"]["mode"] = "untrusted", s => s["agent"]["state"] = "all_clear",
            s => s["agent"]["last_seen"] = "2026-10-23", s => s["agent"]["backend"] = null,
            s => s["expires_at"] = "not-time", s => s["expires_at"] = double.NaN,
            s => s["expires_at"] = (double)s["server_time"] - 1, s => s["extra"] = true,
            s => s["exam"]["status"] = "maybe", s => s["exam"]["title"] = "title\nspoof",
        })
        {
            var invalid = Status(); edit(invalid);
            using var c = Client(new Handler { Reply = (_, __) => Task.FromResult(Json(Connected(invalid))) });
            await Reject(() => c.ConnectAsync("12345678", None), "invalid status schema"); Check(!c.HasSession, "invalid schema never stores token");
        }
        var offline = Status(); offline["agent"]["online"] = false;
        Check(ExamStatus.Parse(offline, Origin, Assignment).Label(TimeSpan.Zero).Contains("확인 불가"), "offline degraded");
        var old = Status(); old["agent"]["last_seen"] = (double)old["server_time"] - 60;
        Check(ExamStatus.Parse(old, Origin, Assignment).Label(TimeSpan.Zero).Contains("확인 불가"), "heartbeat stale degraded");
        var expired = Status(); expired["agent"]["lease_expires_at"] = (double)expired["server_time"] - 1;
        Check(ExamStatus.Parse(expired, Origin, Assignment).Label(TimeSpan.Zero).Contains("기한 만료"), "lease expires separately");
        var simulation = Status(); simulation["agent"]["mode"] = "simulation"; simulation["agent"]["backend"] = null; simulation["agent"]["state"] = "simulated_active";
        Check(ExamStatus.Parse(simulation, Origin, Assignment).Label(TimeSpan.Zero).Contains("실제 통신 차단 아님"), "simulation explicit");
        foreach (var response in new[] {
            new HttpResponseMessage(HttpStatusCode.Found) { Headers = { Location = new Uri("https://evil.example/") } },
            new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent("<html>login</html>", Encoding.UTF8, "text/html") },
            new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(new string('x', 32769), Encoding.UTF8, "application/json") },
            new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent("{\"ok\":true,\"ok\":true,\"result\":{}}", Encoding.UTF8, "application/json") },
            new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent("{\"ok\":true,\"result\":{}} trailing", Encoding.UTF8, "application/json") }
        })
        {
            using var c = Client(new Handler { Reply = (_, __) => Task.FromResult(response) });
            await Reject(() => c.ConnectAsync("12345678", None), "bad HTTP response"); Check(!c.HasSession, "bad response no token");
        }
        var responseGate = new TaskCompletionSource<HttpResponseMessage>(TaskCreationOptions.RunContinuationsAsynchronously);
        using (var c = Client(new Handler { Reply = (_, __) => responseGate.Task }))
        {
            var connect = c.ConnectAsync("12345678", None); c.Clear(); responseGate.SetResult(Json(Connected(Status())));
            await Reject(() => connect, "late connect cannot restore cleared session"); Check(!c.HasSession, "generation barrier");
        }
        using (var c = Client(new Handler { Reply = async (_, ct) => { await Task.Delay(Timeout.Infinite, ct); return null; } }))
        using (var cancel = new CancellationTokenSource(25))
            await Reject(() => c.ConnectAsync("12345678", cancel.Token), "connect cancellation");
        var revoked = new Handler { Reply = (_, __) => Task.FromResult(Json(Connected(Status()))) };
        using (var c = Client(revoked))
        {
            await c.ConnectAsync("12345678", None);
            revoked.Reply = (_, __) => Task.FromResult(new HttpResponseMessage(HttpStatusCode.Unauthorized));
            await Reject(() => c.StatusAsync(None), "revoked IDE token"); Check(!c.HasSession, "401 clears session");
        }
        foreach (var url in new[] { "http://exam.example.edu", "https://user:secret@example.edu", "https://exam.example.edu/api", "https://exam.example.edu/?token=x" })
            await Reject(() => { using var c = new ExamControlClient(url, Origin, Assignment); return Task.CompletedTask; }, "unsafe management address");
        var driftState = Status(); var driftHandler = new Handler { Reply = (_, __) => Task.FromResult(Json(Connected(driftState))) };
        using (var c = Client(driftHandler))
        {
            await c.ConnectAsync("12345678", None); driftState["seat_id"] = "B-02";
            driftHandler.Reply = (_, __) => Task.FromResult(Json(driftState));
            await Reject(() => c.StatusAsync(None), "seat scope changed"); Check(!c.HasSession, "seat drift revokes locally");
        }
        var disconnectFailure = new Handler { Reply = (_, __) => Task.FromResult(Json(Connected(Status()))) };
        using (var c = Client(disconnectFailure))
        {
            await c.ConnectAsync("12345678", None);
            disconnectFailure.Reply = (_, __) => Task.FromResult(new HttpResponseMessage(HttpStatusCode.ServiceUnavailable));
            await Reject(() => c.DisconnectAsync(None), "offline revoke"); Check(!c.HasSession, "offline disconnect clears locally");
        }
        var lateStatusState = Status(); var lateHandler = new Handler { Reply = (_, __) => Task.FromResult(Json(Connected(lateStatusState))) };
        using (var c = Client(lateHandler))
        {
            await c.ConnectAsync("12345678", None);
            var delayed = new TaskCompletionSource<HttpResponseMessage>(TaskCreationOptions.RunContinuationsAsynchronously);
            lateHandler.Reply = (_, __) => delayed.Task;
            var read = c.StatusAsync(None); c.Clear(); delayed.SetResult(Json(lateStatusState));
            await Reject(() => read, "late status after logout"); Check(!c.HasSession, "late status never resurrects token");
        }
        return checks;
    }
}
