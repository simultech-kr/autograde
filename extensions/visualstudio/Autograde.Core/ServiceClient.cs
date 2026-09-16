using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace Autograde.Core
{
    public sealed class ServiceError : Exception
    {
        public int Status { get; }
        public string Code { get; }
        public ServiceError(int status, string code) : base(Explain(status, code) + " (" + status + ", " + code + ").")
        { Status = status; Code = code; }
        static string Explain(int status, string code)
        {
            if (status == 401) return "인증이 없거나 만료되었습니다. 웹에서 새 수령 코드를 발급받아 로그인하세요";
            if (code == "assignment_closed") return "제출 기한이 지났습니다. 교수자에게 문의하세요";
            if (status == 403) return "과제 접근이 거부되었습니다. 수강 상태와 수령 코드를 확인하세요";
            if (status == 429) return "요청 한도를 초과했습니다. 잠시 기다린 뒤 다시 시도하세요";
            if (status == 413) return "제출 파일이 서버 크기 제한을 초과했습니다";
            if (status >= 500) return "서버가 요청을 처리하지 못했습니다. 제출 여부를 먼저 조회하세요";
            return "서비스 요청 실패";
        }
    }

    // One immutable origin per client. Tokens and pending submission keys never go to disk.
    public sealed class ServiceClient : IDisposable
    {
        readonly HttpClient http;
        readonly SemaphoreSlim gate = new SemaphoreSlim(1, 1);
        readonly SemaphoreSlim submitGate = new SemaphoreSlim(1, 1);
        readonly object stateLock = new object();
        readonly CancellationTokenSource lifetime = new CancellationTokenSource();
        readonly Dictionary<string, string> pending = new Dictionary<string, string>();
        readonly Dictionary<string, JObject> receipts = new Dictionary<string, JObject>();
        bool disposed;
        int sessionVersion;
        string access, refresh;
        DateTime expires;
        public string BaseUrl { get; }
        public bool HasSession { get { lock (stateLock) return !disposed && access != null; } }

        public static string NormalizeAddress(string value)
        {
            value = (value ?? "").Trim();
            if (!value.Contains("://")) value = "https://" + value;
            if (!Uri.TryCreate(value, UriKind.Absolute, out var uri) ||
                (uri.Scheme != "https" && uri.Scheme != "http") ||
                uri.UserInfo != "" || uri.Query != "" || uri.Fragment != "" || uri.AbsolutePath != "/")
                throw new ArgumentException("서버 주소만 입력하세요. 예: https://grade.example.edu:20000");
            bool loopback = uri.Host.Equals("localhost", StringComparison.OrdinalIgnoreCase) ||
                (IPAddress.TryParse(uri.Host.Trim('[', ']'), out var ip) && IPAddress.IsLoopback(ip));
            if (uri.Scheme == "http" && !loopback)
                throw new ArgumentException("외부 접속은 HTTPS가 필요합니다. 인증서 검증은 해제하지 않습니다.");
            return uri.GetLeftPart(UriPartial.Authority);
        }

        public ServiceClient(string address, HttpMessageHandler handler = null)
        {
            BaseUrl = NormalizeAddress(address);
            http = new HttpClient(handler ?? new HttpClientHandler { AllowAutoRedirect = false, UseCookies = false });
            http.Timeout = Timeout.InfiniteTimeSpan;
        }

        static JObject Parse(byte[] bytes)
        {
            try
            {
                using (var reader = new JsonTextReader(new StringReader(new UTF8Encoding(false, true).GetString(bytes))) { MaxDepth = 32, DateParseHandling = DateParseHandling.None })
                {
                    var result = JObject.Load(reader, new JsonLoadSettings { DuplicatePropertyNameHandling = DuplicatePropertyNameHandling.Error });
                    if (reader.Read()) throw new InvalidDataException("서버 JSON 뒤에 추가 데이터가 있습니다.");
                    return result;
                }
            }
            catch (Exception ex) when (ex is JsonException || ex is DecoderFallbackException)
            { throw new InvalidDataException("서버 JSON 응답 형식이 올바르지 않습니다."); }
        }

        public static string Required(JToken value, string key)
        {
            var item = (value as JObject)?[key];
            if (item?.Type != JTokenType.String || string.IsNullOrEmpty((string)item))
                throw new InvalidDataException("서버 응답 필드 오류: " + key);
            return (string)item;
        }

        async Task<byte[]> Send(string method, string path, JObject json, byte[] archive, string token, string key, CancellationToken cancel)
        {
            bool binary = path.EndsWith("/starter", StringComparison.Ordinal) || path.EndsWith("/source", StringComparison.Ordinal);
            using (var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancel, lifetime.Token))
            using (var request = new HttpRequestMessage(new HttpMethod(method), BaseUrl + path))
            {
                timeout.Token.ThrowIfCancellationRequested();
                timeout.CancelAfter(TimeSpan.FromSeconds(archive != null || binary ? 90 : 20));
                request.Headers.Accept.ParseAdd(binary ? "application/gzip" : "application/json");
                if (token != null) request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
                if (key != null) request.Headers.Add("Idempotency-Key", key);
                if (json != null) request.Content = new StringContent(json.ToString(Formatting.None), Encoding.UTF8, "application/json");
                if (archive != null)
                {
                    request.Content = new ByteArrayContent(archive);
                    request.Content.Headers.ContentType = new MediaTypeHeaderValue("application/gzip");
                }
                using (var response = await http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, timeout.Token).ConfigureAwait(false))
                {
                    timeout.Token.ThrowIfCancellationRequested();
                    int limit = response.IsSuccessStatusCode && binary ? Bundle.MaxArchive : 2 * 1024 * 1024;
                    if (response.Content.Headers.ContentLength > limit)
                    {
                        if (!response.IsSuccessStatusCode) throw new ServiceError((int)response.StatusCode, "request_failed");
                        throw new InvalidDataException("서버 응답이 너무 큽니다.");
                    }
                    using (var input = await response.Content.ReadAsStreamAsync().ConfigureAwait(false))
                    using (var output = new MemoryStream())
                    {
                        var buffer = new byte[8192];
                        int count;
                        while ((count = await input.ReadAsync(buffer, 0, buffer.Length, timeout.Token).ConfigureAwait(false)) != 0)
                        {
                            if (output.Length + count > limit)
                            {
                                if (!response.IsSuccessStatusCode) throw new ServiceError((int)response.StatusCode, "request_failed");
                                throw new InvalidDataException("서버 응답이 너무 큽니다.");
                            }
                            output.Write(buffer, 0, count);
                        }
                        var bytes = output.ToArray();
                        if (!response.IsSuccessStatusCode)
                        {
                            string code = "request_failed";
                            try
                            {
                                var body = Parse(bytes);
                                JToken error = body["error"];
                                if (error is JObject nested) error = nested["code"];
                                var candidate = error?.Type == JTokenType.String ? (string)error : null;
                                // Do not echo arbitrary server messages, paths, or credentials into UI/logs.
                                if (candidate != null && Regex.IsMatch(candidate, "^[a-z_]{1,64}$")) code = candidate;
                            }
                            catch (InvalidDataException) { }
                            throw new ServiceError((int)response.StatusCode, code);
                        }
                        string media = response.Content.Headers.ContentType?.MediaType;
                        if (binary && media != "application/gzip" && media != "application/octet-stream" && media != "application/x-gzip")
                            throw new InvalidDataException("과제 파일 응답 형식이 올바르지 않습니다.");
                        if (!binary && response.StatusCode != HttpStatusCode.NoContent && media != "application/json")
                            throw new InvalidDataException("JSON API 대신 다른 페이지가 응답했습니다. 서버 주소를 확인하세요.");
                        timeout.Token.ThrowIfCancellationRequested();
                        return bytes;
                    }
                }
            }
        }

        public async Task HealthAsync(CancellationToken cancel)
        {
            var result = Parse(await Send("GET", "/healthz", null, null, null, null, cancel).ConfigureAwait(false));
            if (Required(result, "status") != "ok") throw new InvalidDataException("서버가 정상 상태를 반환하지 않았습니다.");
        }

        public async Task<string> LoginAsync(string claim, CancellationToken cancel)
        {
            await gate.WaitAsync(cancel).ConfigureAwait(false);
            try
            {
                Clear();
                if (claim == null || claim.Length > 256) throw new ArgumentException("수령 코드 길이가 올바르지 않습니다.");
                claim = claim.Trim().ToUpperInvariant();
                if (!Regex.IsMatch(claim, "^AK1-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$"))
                    throw new ArgumentException("학생 웹에서 받은 AK1-XXXX-XXXX-XXXX 수령 코드를 입력하세요.");
                int version; lock (stateLock) version = sessionVersion;
                var device = Parse(await Send("POST", "/v1/device-authorizations", new JObject
                {
                    ["client"] = "visualstudio-extension",
                    ["extension_version"] = "0.5.0",
                    ["device_name"] = "Visual Studio / Windows",
                    ["claim_code"] = claim
                }, null, null, null, cancel).ConfigureAwait(false));
                var code = Required(device, "device_code");
                var accepted = Parse(await Send("POST", "/v1/assignment-claims/redeem", new JObject
                {
                    ["claim_code"] = claim,
                    ["device_code"] = code
                }, null, null, null, cancel).ConfigureAwait(false));
                if (Required(accepted, "delivery_mode") != "bundle") throw new InvalidDataException("이 확장은 bundle 과제만 지원합니다.");
                var assignment = Required(accepted, "assignment_id");
                for (int attempt = 0; ; attempt++)
                {
                    try
                    {
                        SetTokens(Parse(await Send("POST", "/v1/device-authorizations/token", new JObject { ["device_code"] = code }, null, null, null, cancel).ConfigureAwait(false)), version);
                        return assignment;
                    }
                    catch (ServiceError ex) when (attempt < 5 && (ex.Code == "authorization_pending" || ex.Code == "slow_down"))
                    { await Task.Delay(TimeSpan.FromSeconds(Math.Min(30, Math.Max(5, (int?)device["interval"] ?? 5)) + attempt), cancel).ConfigureAwait(false); }
                }
            }
            catch { Clear(); throw; }
            finally { gate.Release(); }
        }

        void SetTokens(JObject tokens, int expectedVersion)
        {
            var a = Required(tokens, "access_token");
            var r = Required(tokens, "refresh_token");
            int lifetime = (int?)tokens["expires_in"] ?? 0;
            if (lifetime < 1 || lifetime > 86400) throw new InvalidDataException("인증 만료 시간 오류");
            lock (stateLock)
            {
                if (disposed) throw new ObjectDisposedException(nameof(ServiceClient));
                if (sessionVersion != expectedVersion) throw new ServiceError(401, "login_required");
                access = a; refresh = r; expires = DateTime.UtcNow.AddSeconds(lifetime);
            }
        }

        async Task Refresh(CancellationToken cancel)
        {
            int version; lock (stateLock) version = sessionVersion;
            try { SetTokens(Parse(await Send("POST", "/v1/tokens/refresh", new JObject { ["refresh_token"] = refresh }, null, null, null, cancel).ConfigureAwait(false)), version); }
            // Rotation may already have consumed the refresh token even if the response was lost.
            catch { Clear(); throw; }
        }

        async Task<byte[]> Authorized(string method, string path, byte[] archive, string key, CancellationToken cancel, int? expectedSession = null)
        {
            await gate.WaitAsync(cancel).ConfigureAwait(false);
            try
            {
                int version;
                lock (stateLock)
                {
                    if (!HasSession || expectedSession.HasValue && sessionVersion != expectedSession) throw new ServiceError(401, "login_required");
                    version = sessionVersion;
                }
                if (expires <= DateTime.UtcNow.AddSeconds(30)) await Refresh(cancel).ConfigureAwait(false);
                byte[] result;
                try { result = await Send(method, path, null, archive, access, key, cancel).ConfigureAwait(false); }
                catch (ServiceError ex) when (ex.Status == 401)
                {
                    await Refresh(cancel).ConfigureAwait(false);
                    try { result = await Send(method, path, null, archive, access, key, cancel).ConfigureAwait(false); }
                    catch (ServiceError retry) when (retry.Status == 401) { Clear(); throw; }
                }
                lock (stateLock)
                    if (!HasSession || sessionVersion != version) throw new ServiceError(401, "login_required");
                return result;
            }
            finally { gate.Release(); }
        }

        public async Task<JArray> AssignmentsAsync(CancellationToken cancel)
        {
            var value = Parse(await Authorized("GET", "/v1/assignments", null, null, cancel).ConfigureAwait(false));
            return value["assignments"] as JArray ?? throw new InvalidDataException("과제 목록 오류");
        }
        public async Task<JObject> HistoryAsync(string assignment, CancellationToken cancel)
        {
            var value = Parse(await Authorized("GET", "/v1/assignments/" + Uri.EscapeDataString(assignment) + "/history", null, null, cancel).ConfigureAwait(false));
            var rows = value["submissions"] as JArray;
            if (rows == null || rows.Count > 100 || value["has_more"]?.Type != JTokenType.Boolean)
                throw new InvalidDataException("제출 기록 응답 오류");
            var ids = new HashSet<string>(StringComparer.Ordinal);
            foreach (var row in rows)
            {
                var id = Required(row, "submission_id");
                if (!Regex.IsMatch(id, "^bsub_[A-Za-z0-9_-]+$") || !ids.Add(id) || Required(row, "assignment_id") != assignment ||
                    !Regex.IsMatch(Required(row, "source_sha256"), "^sha256:[a-f0-9]{64}$") ||
                    row["source_size_bytes"]?.Type != JTokenType.Integer || (long)row["source_size_bytes"] < 1 || (long)row["source_size_bytes"] > Bundle.MaxArchive ||
                    !DateTimeOffset.TryParse(Required(row, "received_at"), out _))
                    throw new InvalidDataException("제출 기록의 과제 또는 파일 정보 불일치");
                Required(row, "state");
            }
            return value;
        }
        public async Task<byte[]> SourceAsync(JObject submission, CancellationToken cancel)
        {
            var id = Required(submission, "submission_id");
            var digest = Required(submission, "source_sha256");
            var size = (long?)submission["source_size_bytes"];
            var bytes = await Authorized("GET", "/v1/submissions/" + Uri.EscapeDataString(id) + "/source", null, null, cancel).ConfigureAwait(false);
            if (Bundle.Digest(bytes) != digest || bytes.Length != size)
                throw new InvalidDataException("복원 파일 크기 또는 SHA-256 불일치");
            return bytes;
        }

        public async Task<byte[]> StarterAsync(JObject assignment, CancellationToken cancel)
        {
            string id = Required(assignment, "assignment_id");
            var bytes = await Authorized("GET", "/v1/assignments/" + Uri.EscapeDataString(id) + "/starter", null, null, cancel).ConfigureAwait(false);
            if (Bundle.Digest(bytes) != Required(assignment["starter"], "sha256") || bytes.Length != (long?)assignment["starter"]?["size_bytes"])
                throw new InvalidDataException("과제 파일 크기 또는 SHA-256 불일치");
            return bytes;
        }
        public async Task<JObject> SubmitAsync(string assignment, byte[] archive, CancellationToken cancel)
        {
            if (archive == null || archive.Length == 0 || archive.Length > Bundle.MaxArchive) throw new InvalidDataException("제출 압축 파일 크기 오류");
            int version; lock (stateLock) { if (!HasSession) throw new ServiceError(401, "login_required"); version = sessionVersion; }
            archive = (byte[])archive.Clone();
            await submitGate.WaitAsync(cancel).ConfigureAwait(false);
            try
            {
                var fingerprint = assignment + ":" + Bundle.Digest(archive);
                string key;
                lock (stateLock)
                {
                    if (!HasSession || version != sessionVersion) throw new ServiceError(401, "login_required");
                    if (receipts.TryGetValue(fingerprint, out var cached)) return (JObject)cached.DeepClone();
                    if (!pending.TryGetValue(fingerprint, out key))
                    {
                        if (pending.Count + receipts.Count >= 128) throw new InvalidOperationException("세션의 제출 기록 한도입니다. 결과 확인 후 다시 로그인하세요.");
                        pending[fingerprint] = key = Guid.NewGuid().ToString("N");
                    }
                }
                // Retain the key on any ambiguous response; retrying unchanged bytes is safe.
                for (int attempt = 0; ; attempt++)
                {
                    try
                    {
                        var envelope = Parse(await Authorized("POST", "/v1/assignments/" + Uri.EscapeDataString(assignment) + "/submissions", archive, key, cancel, version).ConfigureAwait(false));
                        var result = envelope["submission"] as JObject ?? throw new InvalidDataException("제출 응답 오류");
                        Required(result, "submission_id");
                        if ((string)result["source_sha256"] != Bundle.Digest(archive)) throw new InvalidDataException("제출 영수증 SHA-256 불일치");
                        if (Required(result, "assignment_id") != assignment) throw new InvalidDataException("다른 과제의 제출 영수증입니다.");
                        lock (stateLock)
                        {
                            if (sessionVersion != version || !HasSession) throw new ServiceError(401, "login_required");
                            pending.Remove(fingerprint);
                            receipts[fingerprint] = (JObject)result.DeepClone();
                        }
                        return result;
                    }
                    catch (Exception ex) when (attempt < 2 && !cancel.IsCancellationRequested &&
                        (ex is HttpRequestException || ex is TaskCanceledException || ex is ServiceError se && se.Status >= 500))
                    { await Task.Delay(TimeSpan.FromSeconds(attempt + 1), cancel).ConfigureAwait(false); }
                }
            }
            finally { submitGate.Release(); }
        }
        public async Task<JObject> ResultAsync(string submission, CancellationToken cancel)
        {
            var path = "/v1/submissions/" + Uri.EscapeDataString(submission);
            try
            {
                var response = Parse(await Authorized("GET", path + "/result", null, null, cancel).ConfigureAwait(false));
                var result = response["result"] as JObject ?? throw new InvalidDataException("결과 응답 오류");
                if (Required(result, "submission_id") != submission) throw new InvalidDataException("다른 제출물의 채점 결과입니다.");
                return result;
            }
            catch (ServiceError ex) when (ex.Status == 404 && ex.Code == "result_not_available")
            {
                var response = Parse(await Authorized("GET", path, null, null, cancel).ConfigureAwait(false));
                var result = response["submission"] as JObject ?? throw new InvalidDataException("제출 상태 응답 오류");
                if (Required(result, "submission_id") != submission) throw new InvalidDataException("다른 제출물의 상태입니다.");
                return result;
            }
        }
        public async Task LogoutAsync(CancellationToken cancel)
        {
            try { await gate.WaitAsync(cancel).ConfigureAwait(false); }
            catch { Clear(); throw; }
            try
            {
                if (HasSession)
                {
                    if (expires <= DateTime.UtcNow.AddSeconds(30)) await Refresh(cancel).ConfigureAwait(false);
                    try { await Send("DELETE", "/v1/sessions/current", null, null, access, null, cancel).ConfigureAwait(false); }
                    catch (ServiceError ex) when (ex.Status == 401)
                    {
                        await Refresh(cancel).ConfigureAwait(false);
                        await Send("DELETE", "/v1/sessions/current", null, null, access, null, cancel).ConfigureAwait(false);
                    }
                }
            }
            finally { Clear(); gate.Release(); }
        }
        void Clear() { lock (stateLock) { sessionVersion++; access = null; refresh = null; pending.Clear(); receipts.Clear(); } }
        public void Dispose()
        {
            lock (stateLock) { if (disposed) return; disposed = true; Clear(); }
            lifetime.Cancel(); http.Dispose();
        }
    }
}
