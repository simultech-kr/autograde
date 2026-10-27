using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
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
    // This client never receives the Autograde access/refresh token and has no control API.
    public sealed class ExamControlClient : IDisposable
    {
        readonly HttpClient http;
        readonly CancellationTokenSource lifetime = new CancellationTokenSource();
        readonly SemaphoreSlim gate = new SemaphoreSlim(1, 1);
        readonly object stateLock = new object();
        string token, examId, seatId;
        bool disposed;
        int generation;
        DateTimeOffset expiry;
        double lifetimeSeconds;
        Stopwatch clock;
        public string BaseUrl { get; }
        public string AutogradeOrigin { get; }
        public string AssignmentId { get; }
        public bool HasSession { get { lock (stateLock) return !disposed && token != null && DateTimeOffset.UtcNow < expiry && clock.Elapsed.TotalSeconds < lifetimeSeconds; } }

        public ExamControlClient(string address, string autogradeOrigin, string assignmentId, HttpMessageHandler handler = null)
        {
            BaseUrl = ServiceClient.NormalizeAddress(address);
            AutogradeOrigin = ServiceClient.NormalizeAddress(autogradeOrigin);
            AssignmentId = ExamStatus.Identifier(assignmentId);
            http = new HttpClient(handler ?? new HttpClientHandler { AllowAutoRedirect = false, UseCookies = false });
            http.Timeout = Timeout.InfiniteTimeSpan;
        }

        static JObject Parse(byte[] bytes)
        {
            try
            {
                using (var reader = new JsonTextReader(new StringReader(new UTF8Encoding(false, true).GetString(bytes))) { MaxDepth = 12, DateParseHandling = DateParseHandling.None })
                {
                    var root = JObject.Load(reader, new JsonLoadSettings { DuplicatePropertyNameHandling = DuplicatePropertyNameHandling.Error });
                    if (reader.Read()) throw new InvalidDataException("시험 서버 응답 뒤에 추가 데이터가 있습니다.");
                    return root;
                }
            }
            catch (Exception ex) when (ex is JsonException || ex is DecoderFallbackException)
            { throw new InvalidDataException("시험 서버 응답 형식을 확인할 수 없습니다."); }
        }

        async Task<JObject> Send(string method, string path, JObject body, string bearer, CancellationToken cancel)
        {
            using (var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancel, lifetime.Token))
            using (var request = new HttpRequestMessage(new HttpMethod(method), BaseUrl + path))
            {
                deadline.CancelAfter(TimeSpan.FromSeconds(10));
                request.Headers.Accept.ParseAdd("application/json");
                request.Headers.Add("Origin", BaseUrl);
                if (bearer != null) request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", bearer);
                if (body != null) request.Content = new StringContent(body.ToString(Formatting.None), Encoding.UTF8, "application/json");
                using (var response = await http.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, deadline.Token).ConfigureAwait(false))
                {
                    // Never follow redirects, echo server error text, or accept an HTML login page.
                    if (!response.IsSuccessStatusCode)
                    {
                        if (response.StatusCode == HttpStatusCode.Unauthorized || response.StatusCode == HttpStatusCode.Forbidden) Clear();
                        throw new ExamControlError((int)response.StatusCode);
                    }
                    if (response.Content.Headers.ContentType?.MediaType != "application/json" || response.Content.Headers.ContentLength > 32768)
                        throw new InvalidDataException("시험 서버 응답 형식 또는 크기를 확인하세요.");
                    using (var stream = await response.Content.ReadAsStreamAsync().ConfigureAwait(false))
                    using (var bytes = new MemoryStream())
                    {
                        var buffer = new byte[4096]; int count;
                        while ((count = await stream.ReadAsync(buffer, 0, buffer.Length, deadline.Token).ConfigureAwait(false)) != 0)
                        {
                            if (bytes.Length + count > 32768) throw new InvalidDataException("시험 서버 응답이 너무 큽니다.");
                            bytes.Write(buffer, 0, count);
                        }
                        deadline.Token.ThrowIfCancellationRequested();
                        var json = Parse(bytes.ToArray());
                        ExamStatus.Keys(json, "ok", "result");
                        if (json["ok"]?.Type != JTokenType.Boolean || (bool)json["ok"] != true || !(json["result"] is JObject result))
                            throw new InvalidDataException("시험 서버 응답이 올바르지 않습니다.");
                        return result;
                    }
                }
            }
        }

        public async Task<ExamStatus> ConnectAsync(string code, CancellationToken cancel)
        {
            if (code == null || !Regex.IsMatch(code, "\\A[0-9]{8}\\z")) throw new ArgumentException("감독자가 안내한 8자리 시험 연결 코드를 입력하세요.");
            await gate.WaitAsync(cancel).ConfigureAwait(false);
            try
            {
                Clear(); int expected; lock (stateLock) { if (disposed) throw new ObjectDisposedException(nameof(ExamControlClient)); expected = generation; }
                var started = Stopwatch.StartNew();
                var result = await Send("POST", "/api/ide/connect", new JObject { ["code"] = code, ["autograde_origin"] = AutogradeOrigin, ["assignment_id"] = AssignmentId }, null, cancel).ConfigureAwait(false);
                ExamStatus.Keys(result, "token", "expires_at", "status");
                string issued = ExamStatus.Text(result, "token", 512);
                if (!Regex.IsMatch(issued, "\\A[A-Za-z0-9_-]{43}\\z")) throw new InvalidDataException("시험 연결 인증 정보 형식 오류");
                var status = ExamStatus.Parse(result["status"], AutogradeOrigin, AssignmentId);
                var expires = ExamStatus.Date(result, "expires_at").Value;
                if (expires != status.ExpiresAt || expires <= status.ServerTime || expires - status.ServerTime > TimeSpan.FromHours(8) || expires <= DateTimeOffset.UtcNow)
                    throw new InvalidDataException("시험 연결 유효 시간을 확인할 수 없습니다.");
                lock (stateLock)
                {
                    cancel.ThrowIfCancellationRequested();
                    if (disposed || generation != expected) throw new OperationCanceledException("시험 연결이 변경되었습니다.");
                    token = issued; examId = status.ExamId; seatId = status.SeatId;
                    expiry = expires; lifetimeSeconds = (expires - status.ServerTime).TotalSeconds; clock = started;
                }
                return status;
            }
            finally { gate.Release(); }
        }

        public async Task<ExamStatus> StatusAsync(CancellationToken cancel)
        {
            await gate.WaitAsync(cancel).ConfigureAwait(false);
            try
            {
                string bearer; int expected;
                lock (stateLock)
                {
                    if (!HasSession) { Clear(); throw new ExamControlError(401); }
                    bearer = token; expected = generation;
                }
                var result = await Send("GET", "/api/ide/status", null, bearer, cancel).ConfigureAwait(false);
                var status = ExamStatus.Parse(result, AutogradeOrigin, AssignmentId);
                lock (stateLock)
                {
                    cancel.ThrowIfCancellationRequested();
                    if (disposed || generation != expected) throw new OperationCanceledException("시험 연결이 변경되었습니다.");
                    if (status.ExamId != examId || status.SeatId != seatId || status.ExpiresAt != expiry || status.ServerTime >= expiry || !HasSession)
                    { Clear(); throw new ExamControlError(401); }
                }
                return status;
            }
            finally { gate.Release(); }
        }

        public async Task DisconnectAsync(CancellationToken cancel)
        {
            string bearer; lock (stateLock) { bearer = token; Clear(); }
            if (bearer == null) return;
            var result = await Send("POST", "/api/ide/disconnect", new JObject(), bearer, cancel).ConfigureAwait(false);
            ExamStatus.Keys(result, "disconnected");
            if (result["disconnected"]?.Type != JTokenType.Boolean || (bool)result["disconnected"] != true)
                throw new InvalidDataException("시험 화면 연결 해제를 확인하지 못했습니다.");
        }

        public void Clear() { lock (stateLock) { token = examId = seatId = null; clock = null; generation++; } }
        public void Dispose()
        {
            lock (stateLock) { if (disposed) return; disposed = true; Clear(); }
            lifetime.Cancel(); http.Dispose(); // CancellationSource retained while async reads unwind.
        }
    }

    public sealed class ExamControlError : Exception
    {
        public int Status { get; }
        public ExamControlError(int status) : base(status == 401 || status == 403 ? "시험 연결 코드가 만료·취소되었거나 좌석 정보가 변경되었습니다. 감독자에게 새 코드를 요청하세요." :
            status == 429 ? "시험 서버 요청이 많습니다. 잠시 후 다시 시도하세요." : "시험 서버 연결 실패. 주소·코드·현재 과제를 확인하세요. (HTTP " + status + ")") { Status = status; }
    }

    public sealed class ExamStatus
    {
        public string ExamId { get; private set; }
        public string Title { get; private set; }
        public string ExamState { get; private set; }
        public string SeatId { get; private set; }
        public string AssignmentId { get; private set; }
        public string Mode { get; private set; }
        public string Backend { get; private set; }
        public string State { get; private set; }
        public bool Online { get; private set; }
        public DateTimeOffset ServerTime { get; private set; }
        public DateTimeOffset ExpiresAt { get; private set; }
        public DateTimeOffset? LastSeen { get; private set; }
        public DateTimeOffset? LeaseExpiresAt { get; private set; }
        public static string Identifier(string value)
        {
            if (value == null || !Regex.IsMatch(value, "\\A[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\\z")) throw new InvalidDataException("시험 또는 과제 식별 정보 형식 오류");
            return value;
        }
        internal static string Text(JObject obj, string field, int max = 256, bool nullable = false)
        {
            var item = obj[field];
            if (nullable && item?.Type == JTokenType.Null) return null;
            if (item?.Type != JTokenType.String || ((string)item).Length == 0 || ((string)item).Length > max || ((string)item).Any(char.IsControl))
                throw new InvalidDataException("시험 응답 필드 오류: " + field);
            return (string)item;
        }
        internal static void Keys(JObject obj, params string[] names)
        {
            if (obj == null || !obj.Properties().Select(p => p.Name).OrderBy(s => s, StringComparer.Ordinal).SequenceEqual(names.OrderBy(s => s, StringComparer.Ordinal)))
                throw new InvalidDataException("시험 응답 구조 오류");
        }
        internal static DateTimeOffset? Date(JObject obj, string field, bool nullable = false)
        {
            var value = obj[field];
            if (nullable && value?.Type == JTokenType.Null) return null;
            if (value?.Type != JTokenType.Integer && value?.Type != JTokenType.Float) throw new InvalidDataException("시험 시간 형식 오류");
            double seconds = (double)value;
            if (double.IsNaN(seconds) || double.IsInfinity(seconds) || seconds < 0 || seconds > 253402300799) throw new InvalidDataException("시험 시간 범위 오류");
            return new DateTimeOffset(1970, 1, 1, 0, 0, 0, TimeSpan.Zero).AddSeconds(seconds);
        }
        public static ExamStatus Parse(JToken value, string origin, string assignmentId)
        {
            var obj = value as JObject;
            Keys(obj, "exam", "seat_id", "assignment_id", "autograde_origin", "server_time", "expires_at", "agent", "permissions");
            var exam = obj["exam"] as JObject; Keys(exam, "id", "title", "status");
            var agent = obj["agent"] as JObject; Keys(agent, "online", "mode", "backend", "state", "last_seen", "lease_expires_at", "verified");
            var permissions = obj["permissions"] as JObject; Keys(permissions, "can_release");
            if (Text(obj, "autograde_origin", 2048) != origin || Identifier(Text(obj, "assignment_id")) != assignmentId ||
                permissions["can_release"]?.Type != JTokenType.Boolean || (bool)permissions["can_release"] ||
                agent["verified"]?.Type != JTokenType.Boolean || (bool)agent["verified"] || agent["online"]?.Type != JTokenType.Boolean)
                throw new InvalidDataException("시험 연결 범위 또는 권한 응답 오류");
            var mode = Text(agent, "mode", 32);
            var state = Text(agent, "state", 32);
            if (!new[] { "simulation", "enforced" }.Contains(mode) || !new[] { "installed", "released", "unknown", "recovery_required", "idle", "simulated_active", "simulated_released", "unsupported" }.Contains(state))
                throw new InvalidDataException("시험 Agent 상태를 확인할 수 없습니다.");
            var result = new ExamStatus { ExamId = Identifier(Text(exam, "id")), Title = Text(exam, "title"), ExamState = Text(exam, "status", 32),
                SeatId = Identifier(Text(obj, "seat_id")), AssignmentId = assignmentId, Mode = mode, Backend = Text(agent, "backend", 64, true), State = state,
                Online = (bool)agent["online"], ServerTime = Date(obj, "server_time").Value, ExpiresAt = Date(obj, "expires_at").Value,
                LastSeen = Date(agent, "last_seen", true), LeaseExpiresAt = Date(agent, "lease_expires_at", true) };
            if (result.ExpiresAt <= result.ServerTime || result.ExpiresAt - result.ServerTime > TimeSpan.FromHours(8)) throw new InvalidDataException("시험 연결 만료 정보 오류");
            if (!new[] { "active", "ended" }.Contains(result.ExamState) || (result.Mode == "enforced" && result.Backend == null) ||
                (result.Mode == "simulation" && result.Backend != null) || result.LastSeen > result.ServerTime.AddSeconds(5)) throw new InvalidDataException("시험 Agent 보고 형식 오류");
            return result;
        }

        public string Label(TimeSpan sinceResponse)
        {
            // Never preserve a green/installed indication after an expired lease, offline report or stale poll.
            var now = ServerTime + sinceResponse;
            if (DateTimeOffset.UtcNow > now) now = DateTimeOffset.UtcNow;
            if (sinceResponse < TimeSpan.Zero || sinceResponse > TimeSpan.FromSeconds(15) || now >= ExpiresAt) return "상태 확인 불가 · 마지막 응답이 오래되었거나 연결 만료";
            if (!Online || !LastSeen.HasValue || now - LastSeen.Value > TimeSpan.FromSeconds(30)) return "상태 확인 불가 · Agent 오프라인 또는 오래된 보고";
            if (Mode == "simulation") return "모의 모드 · 실제 통신 차단 아님 (" + State + ")";
            if (State == "installed" && (!LeaseExpiresAt.HasValue || now >= LeaseExpiresAt)) return "상태 확인 불가 · 제한 기한 만료, 해제 여부 확인 중";
            if (State == "installed") return "제한 설치 보고 · 실제 통신 차단 검증 전";
            if (State == "released") return "제한 해제 보고 · 실제 통신 상태 검증 전";
            if (State == "recovery_required") return "복구 필요 · 감독자에게 문의하세요";
            if (State == "idle") return "제한 대기 · 실제 통신 상태 검증 전";
            return "상태 확인 불가 (" + State + ") · 감독자에게 문의하세요";
        }
    }
}
