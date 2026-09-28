using System;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using Autograde.Core;
using Newtonsoft.Json.Linq;

internal static class AssignmentDocumentChecks
{
    static JObject Envelope(int revision = 1, string content = "# 설명\r\n\n공백 유지  \n", string assignment = "asn_one")
    {
        var metadata = new JObject { ["revision"] = revision, ["sha256"] = Bundle.Digest(Encoding.UTF8.GetBytes(content)),
            ["updated_at"] = "2026-09-22T12:00:00+00:00", ["change_note"] = "오류 예시 보완" };
        var document = (JObject)metadata.DeepClone();
        document["assignment_id"] = assignment; document["content"] = content;
        document["history"] = new JArray(metadata);
        return new JObject { ["document"] = document };
    }

    public static async Task<int> Run()
    {
        int checks = 0;
        void Check(bool value, string name) { if (!value) throw new Exception(name); checks++; }
        void Reject(Action action, string name)
        {
            try { action(); }
            catch (InvalidDataException) { checks++; return; }
            throw new Exception(name);
        }
        Check(DocumentMetadata.Parse(null) == null, "old assignment metadata is optional");
        Check(DocumentMetadata.Parse(JValue.CreateNull()) == null, "unsupported assignment metadata is null");
        Check(AssignmentDocument.Parse(new JObject(), "asn_one") == null, "old endpoint missing document is graceful");
        Check(AssignmentDocument.Parse(new JObject { ["document"] = JValue.CreateNull() }, "asn_one") == null, "CLI assignment has no online document");
        var response = Envelope();
        var document = AssignmentDocument.Parse(response, "asn_one");
        Check(document.Content == "# 설명\r\n\n공백 유지  \n", "hash and content preserve exact whitespace and newlines");
        Check(document.Metadata.Revision == 1 && document.History.Count == 1, "current metadata and history parsed");
        Check(DocumentMetadata.Parse(document.Metadata.ToJson()).Sha256 == document.Metadata.Sha256, "metadata round trip");
        foreach (var note in new[] { "", new string('a', 500), string.Concat(Enumerable.Repeat("😀", 500)),
            string.Concat(Enumerable.Repeat("😀", 499)) + "가" })
        {
            var metadata = document.Metadata.ToJson(); metadata["change_note"] = note;
            Check(DocumentMetadata.Parse(metadata).ChangeNote == note, "change note allows up to 500 Unicode characters without trimming");
        }
        foreach (var note in new[] { new string('a', 501), string.Concat(Enumerable.Repeat("😀", 501)),
            string.Concat(Enumerable.Repeat("😀", 499)) + "가나", "\ud800", "\udc00", "\ud800x" })
        {
            var metadata = document.Metadata.ToJson(); metadata["change_note"] = note;
            Reject(() => DocumentMetadata.Parse(metadata), "oversized or malformed Unicode change note rejected");
        }
        var invalidHistoryNote = (JObject)response.DeepClone();
        invalidHistoryNote["document"]["history"][0]["change_note"] = new string('a', 501);
        Reject(() => AssignmentDocument.Parse(invalidHistoryNote, "asn_one"), "history change note uses the same 500-character bound");
        var hostile = "<script>alert(1)</script>\n![remote](https://invalid.example/pixel)\n[run](command:delete)";
        Check(AssignmentDocument.Parse(Envelope(content: hostile), "asn_one").Content == hostile, "untrusted markup stays literal text");
        Check(AssignmentDocument.Parse(Envelope(content: ""), "asn_one").Content == "", "empty content hashes without trimming");
        Check(AssignmentDocument.Parse(Envelope(content: new string('a', 20000)), "asn_one").Content.Length == 20000, "20000 character boundary");
        Check(AssignmentDocument.Parse(Envelope(content: string.Concat(Enumerable.Repeat("😀", 20000))), "asn_one").Content.Length == 40000,
            "astral Unicode characters counted as characters, not UTF16 units");
        Reject(() => AssignmentDocument.Parse(Envelope(content: new string('a', 20001)), "asn_one"), "oversized ASCII content rejected");
        Reject(() => AssignmentDocument.Parse(Envelope(content: new string('a', 40001)), "asn_one"), "oversized UTF16 content rejected early");
        Reject(() => AssignmentDocument.Parse(Envelope(content: "\ud800"), "asn_one"), "invalid surrogate rejected");
        Reject(() => AssignmentDocument.Parse(response, "asn_other"), "other assignment content rejected");
        foreach (var revision in new JToken[] { -1, "1", 1.5, (long)int.MaxValue + 1, JValue.CreateNull() })
        {
            var invalid = (JObject)response.DeepClone(); invalid["document"]["revision"] = revision;
            Reject(() => AssignmentDocument.Parse(invalid, "asn_one"), "invalid revision rejected");
        }
        foreach (var field in new[] { "sha256", "updated_at", "change_note", "content", "history" })
        {
            var invalid = (JObject)response.DeepClone(); invalid["document"][field] = JValue.CreateNull();
            Reject(() => AssignmentDocument.Parse(invalid, "asn_one"), "invalid document field rejected: " + field);
        }
        var badHash = (JObject)response.DeepClone(); badHash["document"]["sha256"] = "sha256:" + new string('0', 64);
        Reject(() => AssignmentDocument.Parse(badHash, "asn_one"), "content digest mismatch rejected");
        var badDate = (JObject)response.DeepClone(); badDate["document"]["updated_at"] = "not a date";
        Reject(() => AssignmentDocument.Parse(badDate, "asn_one"), "invalid timestamp rejected");
        var badHistory = (JObject)response.DeepClone(); ((JArray)badHistory["document"]["history"]).Add(document.Metadata.ToJson());
        Reject(() => AssignmentDocument.Parse(badHistory, "asn_one"), "duplicate history revision rejected");
        badHistory = (JObject)response.DeepClone(); badHistory["document"]["history"][0]["revision"] = 2;
        Reject(() => AssignmentDocument.Parse(badHistory, "asn_one"), "future history revision rejected");
        badHistory = (JObject)response.DeepClone(); badHistory["document"]["history"][0] = JValue.CreateNull();
        Reject(() => AssignmentDocument.Parse(badHistory, "asn_one"), "null history rejected");

        var selection = new DocumentSelectionState();
        Check(selection.Label(document.Metadata) == "", "no notice without selected assignment");
        selection.Select("asn_one"); long generation = selection.Generation;
        Check(selection.Label(document.Metadata).Contains("새 설명 있음"), "new revision initially unread");
        Check(selection.MarkRead(generation, document), "selected document marked read");
        Check(selection.Label(document.Metadata) == "설명 v1", "opening latest revision clears new notice");
        var next = AssignmentDocument.Parse(Envelope(2), "asn_one");
        Check(selection.Label(next.Metadata).Contains("새 설명 있음"), "later revision is newly unread");
        var changedHash = AssignmentDocument.Parse(Envelope(content: "changed"), "asn_one");
        Check(selection.Label(changedHash.Metadata).Contains("새 설명 있음"), "same revision different digest not marked read");
        selection.Select("asn_two");
        Check(!selection.MarkRead(generation, document), "cross-assignment delayed document rejected");
        Check(selection.Label(document.Metadata).Contains("새 설명 있음"), "read marker scoped to assignment");
        selection.Select("asn_one");
        Check(!selection.IsCurrent(generation, "asn_one"), "A-B-A selection invalidates original request");
        Check(selection.Label(document.Metadata) == "설명 v1", "read marker survives switching within login");
        generation = selection.Generation; selection.Reset(); selection.Select("asn_one");
        Check(!selection.MarkRead(generation, document), "logout-relogin invalidates original request");
        Check(selection.Label(document.Metadata).Contains("새 설명 있음"), "read state not shared across login");
        Check(selection.Label(AssignmentDocument.Parse(Envelope(0), "asn_one").Metadata) == "설명 v0", "original description is not an update");

        var handler = new DocumentServer { Response = response };
        using var client = new ServiceClient("https://grade.example.edu", handler);
        await client.LoginAsync("AK1-ABCD-EFGH-IJKL", CancellationToken.None);
        Check((await client.DocumentAsync("asn_one", CancellationToken.None)).Content == document.Content, "authenticated document endpoint");
        Check(handler.Authorized, "document request uses bearer on selected assignment endpoint");
        handler.Status = 404;
        Check(await client.DocumentAsync("asn_one", CancellationToken.None) == null && client.HasSession, "old endpoint 404 preserves login");
        handler.Status = 200; handler.Response = new JObject { ["document"] = JValue.CreateNull() };
        Check(await client.DocumentAsync("asn_one", CancellationToken.None) == null, "unsupported CLI document null handled by client");
        handler.Status = 403;
        try { await client.DocumentAsync("asn_one", CancellationToken.None); throw new Exception("403 hidden as unsupported"); }
        catch (ServiceError ex) { Check(ex.Status == 403, "scope denial stays visible"); }
        handler.Status = 401;
        try { await client.DocumentAsync("asn_one", CancellationToken.None); throw new Exception("expired session hidden as unsupported"); }
        catch (ServiceError ex) { Check(ex.Status == 401 && !client.HasSession, "expired login cleared and propagated"); }
        return checks;
    }

    sealed class DocumentServer : HttpMessageHandler
    {
        public JObject Response;
        public int Status = 200;
        public bool Authorized;
        static HttpResponseMessage Json(JObject value, int status = 200) => new HttpResponseMessage((HttpStatusCode)status)
            { Content = new StringContent(value.ToString(), Encoding.UTF8, "application/json") };
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken token)
        {
            var path = request.RequestUri.AbsolutePath;
            if (path == "/v1/device-authorizations") return Task.FromResult(Json(new JObject { ["device_code"] = "device" }));
            if (path == "/v1/assignment-claims/redeem") return Task.FromResult(Json(new JObject { ["assignment_id"] = "asn_one", ["delivery_mode"] = "bundle" }));
            if (path == "/v1/device-authorizations/token") return Task.FromResult(Json(new JObject
                { ["access_token"] = "access", ["refresh_token"] = "refresh", ["expires_in"] = 3600 }));
            if (path == "/v1/tokens/refresh") return Task.FromResult(Json(new JObject { ["error"] = "invalid_grant" }, 401));
            if (path != "/v1/assignments/asn_one/document") throw new Exception("Unexpected document request");
            Authorized = request.Headers.Authorization?.Parameter == "access" && request.Method == HttpMethod.Get &&
                request.RequestUri.GetLeftPart(UriPartial.Authority) == "https://grade.example.edu";
            return Task.FromResult(Json(Status == 200 ? Response : new JObject { ["error"] = "request_failed" }, Status));
        }
    }
}
