using System;
using System.IO;
using System.Net.Http;
using System.Security.Authentication;
using Newtonsoft.Json.Linq;

namespace Autograde.Core
{
    public sealed class DownloadFailure : Exception
    {
        public string Code { get; }
        public DownloadFailure(string code) : base(code) { Code = code; }
    }

    // An in-memory, allowlisted support record. Never serialize exception messages or paths.
    public sealed class DownloadDiagnostic
    {
        public string AttemptId { get; } = Guid.NewGuid().ToString();
        public string Stage { get; set; } = "preflight";
        public string Outcome { get; set; } = "in_progress";
        public string OpenOutcome { get; set; } = "not_attempted";
        public string Code { get; private set; }
        public int? Status { get; private set; }
        public string Delivery { get; set; } = "전달 전";
        int sequence;

        public void Fail(Exception error, bool cancelled)
        {
            Code = Classify(error, cancelled, Stage);
            if (error is ServiceError service) Status = service.Status;
            if (Stage == "opening") { Outcome = "succeeded"; OpenOutcome = cancelled ? "open_cancelled" : "open_failed"; }
            else Outcome = cancelled ? "cancelled" : "failed";
        }
        public void MarkOpened()
        {
            Stage = "opening"; Outcome = "succeeded"; OpenOutcome = "opened";
            Code = null; Status = null;
        }
        // Local UI only: never append this path-bearing text to Payload or Details.
        public static string OpenRecoveryGuidance(string target) =>
            "다운로드한 파일은 보존됩니다. 다시 수락하거나 재다운로드할 필요는 없습니다.\n" +
            "저장 위치: " + target + "\n" +
            "1. 기존 작업을 모두 저장(Ctrl+Shift+S)하고 저장·신뢰 확인창이 열려 있는지 확인하세요. 신뢰할 수 있는 수업 자료만 여세요.\n" +
            "2. ‘과제 폴더 열기’를 다시 누르세요.\n" +
            "3. 계속 실패하면 Visual Studio의 파일 → 열기 → 폴더에서 위 저장 위치를 선택하세요. 상위 폴더가 아닌 main.c/main.cpp 또는 README가 있는 과제 폴더입니다.\n" +
            "4. ‘폴더 관리 · 다시 다운로드’의 제출 폴더가 같은 위치인지 확인하고, 수정 후 모두 저장하여 제출하세요.\n" +
            "계속 열리지 않으면 ‘다운로드 상세 · 문의번호’의 문의번호·오류 코드와 VS 버전을 교수자에게 전달하세요. 비밀번호·수령 코드는 보내지 마세요.";
        public JObject Payload(string version) => new JObject {
            ["schema_version"] = 1, ["attempt_id"] = AttemptId, ["seq"] = sequence++,
            ["stage"] = Stage, ["outcome"] = Outcome, ["open_outcome"] = OpenOutcome,
            ["error_code"] = Code, ["http_status"] = Status,
            ["ide"] = "visualstudio", ["extension_version"] = version,
            ["os"] = "windows", ["remote_kind"] = "none"
        };
        public string Summary => Outcome == "succeeded" ? (OpenOutcome == "open_failed" ? "파일 준비 완료 · IDE 열기 실패" : OpenOutcome == "open_cancelled" ? "파일 준비 완료 · 열기 취소" : "과제 파일 준비 완료") : Outcome == "cancelled" ? "다운로드 취소 · 수락은 유지됩니다." : "다운로드 " + (Outcome == "failed" ? "실패" : "진행 중") + " · " + StageLabel(Stage);
        public string Details => Summary + "\n단계: " + StageLabel(Stage) + "\n오류 코드: " + (Code ?? "없음") +
            (Status.HasValue ? "\nHTTP: " + Status : "") + "\n문의번호: " + AttemptId + "\n진단: " + Delivery + "\n" + Guidance(Code);
        public static string StageLabel(string stage)
        {
            switch (stage) { case "preflight": return "저장 위치 확인"; case "requesting": return "서버 요청·파일 수신";
                case "verifying": return "파일 무결성 검증"; case "installing": return "파일 저장";
                case "files_ready": return "파일 준비 완료"; case "opening": return "IDE 폴더 열기"; default: return "파일 수신"; }
        }
        public static string Classify(Exception error, bool cancelled, string stage)
        {
            string code;
            if (cancelled) code = "USER-CANCELLED";
            else if (stage == "opening") code = "OPEN-WORKSPACE";
            else if (error is DownloadFailure typed) return typed.Code;
            else if (error is ServiceError service) code = service.Status == 401 ? "AUTH-EXPIRED" : service.Status == 403 ? "ACCESS-DENIED" : service.Status == 429 ? "HTTP-RATE-LIMIT" : service.Status >= 500 ? "HTTP-SERVER" : "UNKNOWN";
            else if (error is OperationCanceledException) code = "NETWORK-TIMEOUT";
            else if (error is UnauthorizedAccessException) code = "LOCAL-PERMISSION";
            else if (error is PathTooLongException) code = "LOCAL-PATH";
            else if (error is AuthenticationException || error.InnerException is AuthenticationException) code = "NETWORK-TLS";
            else if (error is HttpRequestException) code = "NETWORK-UNKNOWN";
            else if (error is InvalidDataException) code = "ARCHIVE-INVALID";
            else if (error is IOException io) {
                int native = io.HResult & 0xffff;
                code = native == 112 || native == 39 ? "LOCAL-SPACE" : native == 5 ? "LOCAL-PERMISSION" : native == 80 || native == 183 ? "LOCAL-EXISTS" : "LOCAL-IO";
            }
            else code = "UNKNOWN";
            return "AG-DL-" + code;
        }
        public static string Guidance(string code)
        {
            switch (code) {
                case "AG-DL-LOCAL-PERMISSION": return "선택한 폴더에 저장할 권한이 없습니다. 다른 저장 폴더를 선택하세요. 다시 수락할 필요는 없습니다.";
                case "AG-DL-LOCAL-SPACE": return "디스크 공간을 확보한 뒤 다시 받으세요. 기존 코드는 삭제하지 않습니다.";
                case "AG-DL-LOCAL-EXISTS": return "기존 폴더를 열거나 새 폴더에 받으세요. 기존 코드는 덮어쓰지 않습니다.";
                case "AG-DL-AUTH-EXPIRED": return "로그인이 만료되었습니다. 새 수령 코드로 로그인하세요. 기존 파일은 보존됩니다.";
                case "AG-DL-ACCESS-DENIED": return "과제 접근이 거부됐습니다. 교수자에게 수강·과제 권한 확인을 요청하세요.";
                case "AG-DL-NETWORK-TLS": return "도메인과 인증서를 확인하세요. 인증서 검증을 해제하지 마세요.";
                case "AG-DL-OPEN-WORKSPACE": return "다운로드 완료 · IDE 자동 열기만 실패했습니다. 기존 작업 저장 후 ‘과제 폴더 열기’로 재시도하거나 파일 → 열기 → 폴더에서 저장 위치를 여세요. 아래 안내에서 경로와 복구 순서를 확인하세요.";
                case "AG-DL-USER-CANCELLED": return "취소했습니다. 필요할 때 다시 시도하세요.";
                case "AG-DL-INTEGRITY-HASH": case "AG-DL-INTEGRITY-SIZE": return "받은 파일과 과제 정보가 다릅니다. 과제 정보를 새로고침하고 재시도하거나 문의번호를 알려 주세요.";
                case "AG-DL-RESPONSE-TYPE": return "과제 파일 대신 다른 응답을 받았습니다. API 주소와 서버 프록시 설정을 확인하세요.";
                case "AG-DL-HTTP-RATE-LIMIT": case "AG-DL-HTTP-SERVER": return "서버가 요청을 처리하지 못했습니다. 잠시 후 다시 시도하세요.";
                case "AG-DL-NETWORK-TIMEOUT": case "AG-DL-NETWORK-UNKNOWN": return "서버 연결·응답을 확인할 수 없습니다. API 주소와 네트워크를 확인하고 재시도하세요.";
                case null: return "수락과 다운로드·IDE 열기는 별도 단계입니다.";
                default: return "문의번호와 실패 단계를 교수자에게 알려 주세요. 기존 코드는 덮어쓰지 마세요.";
            }
        }
    }
}
