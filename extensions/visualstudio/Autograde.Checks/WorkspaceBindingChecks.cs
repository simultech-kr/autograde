using System;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Autograde.Core;
using Newtonsoft.Json.Linq;

internal static class WorkspaceBindingChecks
{
    public static async Task<int> Run()
    {
        int checks = 0;
        void Check(bool value, string name) { if (!value) throw new Exception(name); checks++; }
        void Reject(Action action, string name)
        {
            try { action(); }
            catch (Exception ex) when (ex is IOException || ex is InvalidDataException || ex is ArgumentException || ex is InvalidOperationException) { checks++; return; }
            throw new Exception(name);
        }
        var root = Path.Combine(OperatingSystem.IsMacOS() ? "/private/tmp" : Path.GetTempPath(), "autograde-workspaces-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try
        {
            var source = Path.Combine(root, "source"); Directory.CreateDirectory(source);
            File.WriteAllText(Path.Combine(source, "main.c"), "int main(void){return 0;}\n");
            var archive = Bundle.CreateSubmission(source).Archive;
            var first = Path.Combine(root, "first"); var second = Path.Combine(root, "second");
            Bundle.ExtractSubmission(archive, first, "https://grade.example.edu", "asn_one");
            Bundle.ExtractSubmission(archive, second, "https://grade.example.edu", "asn_two");
            var solution = Path.Combine(first, "solution"); Directory.CreateDirectory(solution);
            File.WriteAllText(Path.Combine(first, "main.c"), "// student's saved answer\nint main(){return 1;}\n");
            var before = Bundle.CreateSubmission(first).Archive;
            var reopened = WorkspaceBinding.FromOpenDirectory(solution);
            Check(reopened.Root == first && reopened.AssignmentId == "asn_one", "restart recognizes root from nested solution directory");
            Bundle.VerifyWorkspace(reopened.Root, "https://grade.example.edu/", "asn_one");
            Check(Bundle.Digest(Bundle.CreateSubmission(first).Archive) == Bundle.Digest(before), "reattaching never rewrites saved work");
            WorkspaceBinding.VerifyActive(first, solution, "https://grade.example.edu", "asn_one"); checks++;
            Reject(() => WorkspaceBinding.VerifyActive(first, second, "https://grade.example.edu", "asn_one"), "cannot submit old target while other task open");
            Reject(() => WorkspaceBinding.VerifyActive(first, null, "https://grade.example.edu", "asn_one"), "no open IDE task cannot silently submit remembered path");
            Reject(() => Bundle.VerifyWorkspace(first, "https://other.example.edu", "asn_one"), "server mismatch rejected");
            Reject(() => Bundle.VerifyWorkspace(first, "https://grade.example.edu", "asn_two"), "assignment mismatch rejected");
            Check(WorkspaceBinding.DownloadParent(solution) == root, "new download from old solution becomes a sibling");
            Check(WorkspaceBinding.DownloadParent(source) == source, "ordinary parent preserved");
            var nested = Path.Combine(first, "old-nested");
            Bundle.ExtractSubmission(archive, nested, "https://grade.example.edu", "asn_two");
            Check(WorkspaceBinding.DownloadParent(nested) == root, "previously nested workspace also escapes outer assignment");
            Reject(() => Bundle.CreateSubmission(first), "nested assignment files never included in another submission");
            Directory.Delete(nested, true);
            var moved = Path.Combine(root, "moved"); Directory.Move(first, moved);
            Bundle.VerifyWorkspace(moved, "https://grade.example.edu", "asn_one"); checks++;
            Reject(() => Bundle.VerifyWorkspace(first, "https://grade.example.edu", "asn_one"), "missing remembered path requires attach at new location");
            var marker = Path.Combine(moved, ".autograde", "assignment.json");
            File.WriteAllText(marker, "{\"schemaVersion\":1,\"schemaVersion\":1,\"serviceBaseUrl\":\"https://grade.example.edu\",\"assignmentId\":\"asn_one\"}");
            Reject(() => WorkspaceBinding.Read(moved), "ambiguous duplicate marker rejected");
            File.WriteAllText(marker, "{}");
            Reject(() => WorkspaceBinding.Read(moved), "invalid marker does not become fresh download authorization");
        }
        finally { Directory.Delete(root, true); }

        var server = new CatalogServer();
        using var client = new ServiceClient("https://grade.example.edu", server);
        await client.LoginAsync("AK1-ABCD-EFGH-IJKL", CancellationToken.None);
        Check(client.ClaimRedeemed, "successful claim records replacement of former session");
        var catalog = await client.AcceptedAssignmentsAsync(CancellationToken.None);
        Check(catalog.Count == 2 && (bool)catalog[1]["current_session_authorized"] == false, "history contains previous assignment without authorizing it");
        Check(server.CatalogAuthorized, "catalog requires current bearer authorization");
        server.Malformed = true;
        try { await client.AcceptedAssignmentsAsync(CancellationToken.None); throw new Exception("unauthorized catalog submit permission accepted"); }
        catch (InvalidDataException) { checks++; }
        server.Malformed = false;
        server.Status = 404;
        Check(await client.AcceptedAssignmentsAsync(CancellationToken.None) == null && client.HasSession, "old server catalog fallback preserves current login");
        server.Status = 403;
        try { await client.AcceptedAssignmentsAsync(CancellationToken.None); throw new Exception("scope failure hidden"); }
        catch (ServiceError error) { Check(error.Status == 403, "catalog scope denial remains visible"); }
        using var tokenFailure = new ServiceClient("https://grade.example.edu", new CatalogServer { TokenFails = true });
        try { await tokenFailure.LoginAsync("AK1-ABCD-EFGH-IJKL", CancellationToken.None); throw new Exception("token failure hidden"); }
        catch (ServiceError) { Check(tokenFailure.ClaimRedeemed && !tokenFailure.HasSession, "post-redeem failure cannot restore revoked previous authentication"); }
        return checks;
    }

    sealed class CatalogServer : HttpMessageHandler
    {
        public int Status = 200;
        public bool CatalogAuthorized, TokenFails, Malformed;
        static HttpResponseMessage Json(JObject body, int status = 200) => new HttpResponseMessage((HttpStatusCode)status)
            { Content = new StringContent(body.ToString(), Encoding.UTF8, "application/json") };
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancel)
        {
            var path = request.RequestUri.AbsolutePath;
            if (path == "/v1/device-authorizations") return Task.FromResult(Json(new JObject { ["device_code"] = "device" }));
            if (path == "/v1/assignment-claims/redeem") return Task.FromResult(Json(new JObject { ["assignment_id"] = "asn_one", ["delivery_mode"] = "bundle" }));
            if (path == "/v1/device-authorizations/token") return Task.FromResult(TokenFails ? Json(new JObject { ["error"] = "invalid_grant" }, 401) :
                Json(new JObject { ["access_token"] = "access", ["refresh_token"] = "refresh", ["expires_in"] = 3600 }));
            if (path != "/v1/accepted-assignments/catalog") throw new Exception("Unexpected request: " + path);
            CatalogAuthorized = request.Headers.Authorization?.Parameter == "access";
            var rows = new JArray();
            foreach (var id in new[] { "asn_one", "asn_two" }) rows.Add(new JObject { ["assignment_id"] = id,
                ["title"] = id, ["course_key"] = "course", ["status"] = "available", ["current_session_authorized"] = id == "asn_one",
                ["accepted_at"] = "2026-09-29T00:00:00Z", ["can_reconnect"] = true, ["can_submit"] = Malformed || id == "asn_one" });
            return Task.FromResult(Json(new JObject { ["assignments"] = rows }, Status));
        }
    }
}
