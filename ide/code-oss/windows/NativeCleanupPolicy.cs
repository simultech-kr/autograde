// Policy validation and fallback are pure .NET so they can be checked without Windows.
// Credentials never appear in errors, warnings, diagnostic messages or policy snapshots.
using System;
using System.Collections.Generic;
using System.IO;
using System.Text.RegularExpressions;

namespace Autograde.NativeWindows
{
    public sealed class NativeSeatConfig
    {
        public readonly Uri ApiUrl;
        public readonly string SeatId;
        internal readonly string ReportToken;
        internal NativeSeatConfig(Uri apiUrl, string seatId, string reportToken)
        { ApiUrl = apiUrl; SeatId = seatId; ReportToken = reportToken; }
    }

    public sealed class NativeCleanupPolicy
    {
        public const string NextStart = "next_start", OnExit = "on_exit";
        public readonly string Mode, Source;
        private NativeCleanupPolicy(string mode, string source) { Mode = mode; Source = source; }
        public static bool ValidMode(object value) { return Object.Equals(value, NextStart) || Object.Equals(value, OnExit); }
        public static NativeCleanupPolicy Default(string source) { return new NativeCleanupPolicy(NextStart, source); }
        public string StartupMessage {
            get {
                if (Source == "server") return Mode == OnExit
                    ? "서버 담당자 정책 확인: 종료할 때 실습 코드·자료를 삭제합니다."
                    : "서버 담당자 정책 확인: 다음 IDE 실행 전에 실습 코드·자료를 삭제합니다.";
                return "주의: 서버 정리 정책을 확인하지 못해 기본값을 적용합니다.\n실습 코드·자료는 다음 IDE 실행 전에 삭제합니다.";
            }
        }
        public string RemovalMessage {
            get { return Mode == OnExit ? "이번 실습 코드·자료는 종료할 때 삭제됩니다." : "이번 실습 코드·자료는 다음 IDE 실행 전에 삭제됩니다."; }
        }
        public static NativeSeatConfig ParseConfig(IDictionary<string, object> value)
        {
            object api, seat, token; Uri origin;
            if (value == null || value.Count != 3 || !value.TryGetValue("apiUrl", out api) || !value.TryGetValue("seatId", out seat) || !value.TryGetValue("reportToken", out token) ||
                !(seat is string) || !Regex.IsMatch((string)seat, @"\A[a-z0-9][a-z0-9_-]{0,63}\z") ||
                !(token is string) || !Regex.IsMatch((string)token, @"\A[A-Za-z0-9_-]{43}\z") ||
                !(api is string) || !Uri.TryCreate((string)api, UriKind.Absolute, out origin) || origin.Scheme != "https" ||
                String.IsNullOrEmpty(origin.Host) || origin.UserInfo.Length != 0 || origin.Query.Length != 0 || origin.Fragment.Length != 0 || origin.AbsolutePath != "/")
                throw new InvalidDataException("Invalid operator device configuration.");
            return new NativeSeatConfig(origin, (string)seat, (string)token);
        }
        public static string ParseResponse(IDictionary<string, object> value, string seatId)
        {
            object schema, seat, mode;
            if (value == null || value.Count != 3 || !value.TryGetValue("schema", out schema) || !Object.Equals(schema, 1) ||
                !value.TryGetValue("seat_id", out seat) || !Object.Equals(seat, seatId) || !value.TryGetValue("cleanup_mode", out mode) || !ValidMode(mode))
                throw new InvalidDataException("Invalid operator cleanup policy.");
            return (string)mode;
        }
        // Resolve once at launch; an operator change cannot alter a running session.
        // A missing, unreadable, offline or invalid response explicitly uses next_start.
        public static NativeCleanupPolicy Resolve(Func<NativeSeatConfig> readConfig, Func<NativeSeatConfig, IDictionary<string, object>> fetch)
        {
            NativeSeatConfig config;
            try { config = readConfig(); }
            catch (Exception) { return Default("default-invalid-config"); }
            if (config == null) return Default("default-no-config");
            try { return new NativeCleanupPolicy(ParseResponse(fetch(config), config.SeatId), "server"); }
            catch (Exception) { return Default("default-unconfirmed"); }
        }
    }
}
