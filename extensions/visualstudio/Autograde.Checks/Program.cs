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

internal static class Program
{
    static int checks;
    static readonly CancellationToken None = CancellationToken.None;
    static void Check(bool condition, string name) { if (!condition) throw new Exception(name); checks++; }
    static void Reject(Action action, string name)
    { try { action(); } catch (Exception ex) when (ex is ArgumentException || ex is IOException || ex is InvalidDataException) { checks++; return; } throw new Exception(name); }
    static async Task Main(string[] args)
    {
        if (args.Length > 0 && args[0] == "pack")
        { File.WriteAllBytes(args[2], Bundle.CreateSubmission(args[1]).Archive); return; }
        if (args.Length > 0 && args[0] == "extract")
        { Bundle.ExtractStarter(File.ReadAllBytes(args[1]), args[2], "http://127.0.0.1:20000", "asn_test"); return; }
        if (args.Length > 0 && args[0] == "live")
        {
            // Test-only IPC: synthetic claim is passed on stdin, never a command argument/log.
            var setup = JObject.Parse(Console.ReadLine());
            using var client = new ServiceClient((string)setup["url"]);
            await client.HealthAsync(None);
            var id = await client.LoginAsync((string)setup["claim"], None);
            var assignment = (JObject)(await client.AssignmentsAsync(None)).Single(a => (string)a["assignment_id"] == id);
            var root = (string)setup["target"];
            Bundle.ExtractStarter(await client.StarterAsync(assignment, None), root, client.BaseUrl, id);
            Bundle.VerifyWorkspace(root, client.BaseUrl, id);
            var diagnostic = new DownloadDiagnostic { Stage = "files_ready", Outcome = "succeeded" };
            Check(await client.ReportDownloadAsync(id, diagnostic.Payload("0.5.2"), None) == "서버에 전달됨", "live download report");
            File.WriteAllText(Path.Combine(root, "main.c"), "#include <stdio.h>\nint main(void){puts(\"Hello, World!\");return 0;}\n");
            File.WriteAllText(Path.Combine(root, "answer.txt"), "my solution");
            var receipt = await client.SubmitAsync(id, Bundle.CreateSubmission(root).Archive, None);
            Console.WriteLine(receipt.ToString(Newtonsoft.Json.Formatting.None));
            Console.ReadLine(); // Python test processes the accepted submission, then unblocks result retrieval.
            var result = await client.ResultAsync((string)receipt["submission_id"], None);
            Check((double?)result["score"] == 10, "live score");
            var history = await client.HistoryAsync(id, None);
            var version = (JObject)((JArray)history["submissions"]).Single();
            Check((string)version["submission_id"] == (string)receipt["submission_id"], "live receipt in history");
            var restored = root + "-restored";
            Bundle.ExtractSubmission(await client.SourceAsync(version, None), restored, client.BaseUrl, id);
            Bundle.VerifyWorkspace(restored, client.BaseUrl, id);
            Check(File.ReadAllText(Path.Combine(restored, "main.c")) == File.ReadAllText(Path.Combine(root, "main.c")), "live original restored");
            await client.LogoutAsync(None);
            Check(!client.HasSession, "live logout");
            Console.WriteLine("LIVE PASS"); return;
        }
        await SelfTest();
        checks += await ExceptionChecks.Run();
        checks += await GradingPollerChecks.Run();
        checks += ResultPresentationChecks.Run();
        Console.WriteLine(checks + " checks passed");
    }

    static async Task SelfTest()
    {
        Check(ServiceClient.NormalizeAddress(" grade.example.edu:20000 ") == "https://grade.example.edu:20000", "bare address");
        Check(ServiceClient.NormalizeAddress("http://127.0.0.1:20000/") == "http://127.0.0.1:20000", "loopback");
        Check(ServiceClient.NormalizeAddress("http://[::1]:20000") == "http://[::1]:20000", "IPv6 loopback");
        foreach (var url in new[] { "http://192.168.1.5:20000", "http://grade.example.edu", "https://user:pw@example.edu", "https://example.edu/api", "https://example.edu?q=token", "https://example.edu/#secret", "file:///tmp/a", "http://localhost.example.edu" })
            Reject(() => ServiceClient.NormalizeAddress(url), "unsafe URL: " + url);
        foreach (var path in new[] { "../a.c", "/main.c", "a\\b.c", "a//b.c", "CON.txt", "dir/LPT1", "a:ads.c", "a./x.c", "a /x.c", "a\0.c", "AUX", "com¹.h", "e\u0301.c" })
            Reject(() => Bundle.ValidatePath(path), "unsafe path");
        Bundle.ValidatePath("src/한글.c"); checks++;
        var root = Path.Combine(OperatingSystem.IsMacOS() ? "/private/tmp" : Path.GetTempPath(), "autograde-checks-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try
        {
            File.WriteAllText(Path.Combine(root, "main.c"), "int main(void){return 0;}\n");
            foreach (var dir in new[] { ".vs", "x64", "Debug", "build", ".autograde" })
            { Directory.CreateDirectory(Path.Combine(root, dir)); File.WriteAllText(Path.Combine(root, dir, "secret.c"), "not submitted"); }
            File.WriteAllText(Path.Combine(root, "hello.exe"), "binary");
            File.WriteAllText(Path.Combine(root, "student_roster.csv"), "never submitted");
            var snapshot = Bundle.CreateSubmission(root);
            Check(snapshot.Files.SequenceEqual(new[] { "main.c" }), "output/credential exclusions");
            Check(snapshot.Archive.SequenceEqual(Bundle.CreateSubmission(root).Archive), "deterministic snapshots");
            Reject(() => Bundle.ExtractStarter(snapshot.Archive, Path.Combine(root, "bad"), "https://a", "b"), "submission must not be starter");
            Check(!Directory.Exists(Path.Combine(root, "bad")), "validate before write");
            var restored = Path.Combine(root, "restored");
            Bundle.ExtractSubmission(snapshot.Archive, restored, "https://grade.example.edu", "asn_one");
            Bundle.VerifyWorkspace(restored, "https://grade.example.edu", "asn_one");
            Check(File.ReadAllText(Path.Combine(restored, "main.c")) == File.ReadAllText(Path.Combine(root, "main.c")), "restore original code");
            Reject(() => Bundle.ExtractSubmission(snapshot.Archive, restored, "https://grade.example.edu", "asn_one"), "restore never overwrites");
            Directory.Delete(restored, true);
            if (!OperatingSystem.IsWindows())
            {
                File.CreateSymbolicLink(Path.Combine(root, "link.c"), Path.Combine(root, "main.c"));
                Reject(() => Bundle.CreateSubmission(root), "no symlink upload");
                File.Delete(Path.Combine(root, "link.c"));
            }
            var handler = new FakeServer(snapshot.Archive);
            using var client = new ServiceClient("https://grade.example.edu:20000", handler);
            await client.HealthAsync(None);
            Check(await client.LoginAsync("AK1-ABCD-EFGH-IJKL", None) == "asn_one", "claim workflow");
            Check(client.HasSession, "memory session");
            var assignment = (JObject)(await client.AssignmentsAsync(None))[0];
            Check(handler.RefreshCount == 1, "401 refresh exactly once");
            Check((await client.StarterAsync(assignment, None)).SequenceEqual(snapshot.Archive), "starter digest verified");
            assignment["starter"]["sha256"] = "sha256:" + new string('0', 64);
            try { await client.StarterAsync(assignment, None); throw new Exception("digest mismatch accepted"); }
            catch (DownloadFailure error) when (error.Code == "AG-DL-INTEGRITY-HASH") { checks++; }
            var receipt = await client.SubmitAsync("asn_one", snapshot.Archive, None);
            Check((string)receipt["submission_id"] == "bsub_one", "submission envelope");
            Check(handler.Keys.Count == 2 && handler.Keys.Distinct().Count() == 1, "retry same idempotency key");
            Check((string)(await client.ResultAsync("bsub_one", None))["state"] == "queued", "result pending fallback");
            Check((int?)(await client.ResultAsync("bsub_one", None))["score"] == 10, "published result envelope");
            await client.LogoutAsync(None);
            Check(!client.HasSession, "logout clears session");
            try { await client.AssignmentsAsync(None); throw new Exception("logout ineffective"); }
            catch (ServiceError ex) { Check(ex.Status == 401, "logged out denied"); }
            Check(handler.OriginViolation == false, "origin bound credentials");
            using var revoked = new ServiceClient("https://grade.example.edu:20000", new FakeServer(snapshot.Archive) { RevokeRefresh = true });
            await revoked.LoginAsync("AK1-ABCD-EFGH-IJKL", None);
            try { await revoked.AssignmentsAsync(None); throw new Exception("revoked refresh accepted"); }
            catch (ServiceError) { Check(!revoked.HasSession, "revoked refresh clears credentials"); }
            using var offlineLogout = new ServiceClient("https://grade.example.edu:20000", new FakeServer(snapshot.Archive) { LogoutFails = true });
            await offlineLogout.LoginAsync("AK1-ABCD-EFGH-IJKL", None);
            try { await offlineLogout.LogoutAsync(None); throw new Exception("logout failure expected"); }
            catch (HttpRequestException) { Check(!offlineLogout.HasSession, "offline logout clears credentials"); }
            using var delayed = new ServiceClient("https://grade.example.edu", new DelayHandler());
            using var cancel = new CancellationTokenSource(20);
            try { await delayed.HealthAsync(cancel.Token); throw new Exception("request not cancelled"); }
            catch (OperationCanceledException) { checks++; }
            Reject(() => Bundle.ExtractStarter(new byte[Bundle.MaxArchive + 1], Path.Combine(root, "large"), "https://a", "b"), "oversized archive");
            using var redirect = new ServiceClient("https://grade.example.edu", new RedirectHandler());
            try { await redirect.HealthAsync(None); throw new Exception("redirect accepted"); }
            catch (ServiceError ex) { Check(ex.Status == 302, "redirect denied"); }
        }
        finally { Directory.Delete(root, true); }
    }

    sealed class RedirectHandler : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken token) => Task.FromResult(new HttpResponseMessage(HttpStatusCode.Found) {
            Content = new StringContent("{}"), Headers = { Location = new Uri("https://untrusted.example/") } });
    }
    sealed class DelayHandler : HttpMessageHandler
    {
        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken token)
        { await Task.Delay(Timeout.Infinite, token); throw new Exception("unreachable"); }
    }
    sealed class FakeServer : HttpMessageHandler
    {
        readonly byte[] archive;
        bool expired = true, resultPending = true;
        public int RefreshCount;
        public bool OriginViolation;
        public bool RevokeRefresh, LogoutFails;
        public List<string> Keys = new List<string>();
        public FakeServer(byte[] archive) { this.archive = archive; }
        static HttpResponseMessage Json(string body, int status = 200) => new HttpResponseMessage((HttpStatusCode)status) { Content = new StringContent(body, Encoding.UTF8, "application/json") };
        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken token)
        {
            OriginViolation |= request.RequestUri.GetLeftPart(UriPartial.Authority) != "https://grade.example.edu:20000";
            string path = request.RequestUri.AbsolutePath;
            string body = request.Content == null ? "" : await request.Content.ReadAsStringAsync(token);
            switch (path)
            {
                case "/healthz": return Json("{\"status\":\"ok\"}");
                case "/v1/device-authorizations":
                    Check((string)JObject.Parse(body)["client"] == "visualstudio-extension", "client identity");
                    Check(body.Contains("AK1-"), "claim routing");
                    return Json("{\"device_code\":\"device\",\"interval\":5}", 201);
                case "/v1/assignment-claims/redeem": return Json("{\"assignment_id\":\"asn_one\",\"delivery_mode\":\"bundle\"}");
                case "/v1/device-authorizations/token": return Json("{\"access_token\":\"old\",\"refresh_token\":\"refresh\",\"expires_in\":3600}");
                case "/v1/tokens/refresh":
                    if (RevokeRefresh) return Json("{\"error\":{\"code\":\"invalid_grant\"}}", 401);
                    Check((string)JObject.Parse(body)["refresh_token"] == "refresh", "refresh payload");
                    RefreshCount++; return Json("{\"access_token\":\"new\",\"refresh_token\":\"rotated\",\"expires_in\":3600}");
                case "/v1/assignments":
                    if (expired) { expired = false; return Json("{\"error\":{\"code\":\"invalid_token\"}}", 401); }
                    Check(request.Headers.Authorization?.Parameter == "new", "refreshed bearer");
                    return Json(new JObject { ["assignments"] = new JArray(new JObject { ["assignment_id"] = "asn_one", ["starter"] = new JObject { ["sha256"] = Bundle.Digest(archive), ["size_bytes"] = archive.Length } }) }.ToString());
                case "/v1/assignments/asn_one/starter":
                    var response = new HttpResponseMessage(HttpStatusCode.OK) { Content = new ByteArrayContent(archive) };
                    response.Content.Headers.ContentType = new System.Net.Http.Headers.MediaTypeHeaderValue("application/gzip"); return response;
                case "/v1/assignments/asn_one/submissions":
                    Keys.Add(request.Headers.GetValues("Idempotency-Key").Single());
                    if (Keys.Count == 1) throw new HttpRequestException("simulated lost response");
                    return Json(new JObject { ["submission"] = new JObject { ["submission_id"] = "bsub_one", ["assignment_id"] = "asn_one", ["state"] = "queued", ["source_sha256"] = Bundle.Digest(archive) } }.ToString(), 202);
                case "/v1/submissions/bsub_one/result":
                    if (resultPending) { resultPending = false; return Json("{\"error\":{\"code\":\"result_not_available\"}}", 404); }
                    return Json("{\"result\":{\"submission_id\":\"bsub_one\",\"state\":\"published\",\"score\":10,\"max_score\":10}}");
                case "/v1/submissions/bsub_one": return Json("{\"submission\":{\"submission_id\":\"bsub_one\",\"state\":\"queued\"}}");
                case "/v1/sessions/current":
                    if (LogoutFails) throw new HttpRequestException("simulated offline logout");
                    return Json("", 204);
                default: throw new Exception("Unexpected request: " + path);
            }
        }
    }
}
