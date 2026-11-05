// Pure policy checks: no Windows APIs, network, real tokens or learner data.
using System;
using System.Collections.Generic;
using System.IO;

namespace Autograde.NativeWindows
{
    internal static class NativeCleanupPolicyChecks
    {
        private static int assertions;
        private static void Assert(bool value, string message) { assertions++; if (!value) throw new Exception(message); }
        private static IDictionary<string, object> Config()
        { return new Dictionary<string, object> { { "apiUrl", "https://example.edu:20000" }, { "seatId", "lab-01" }, { "reportToken", new string('a', 43) } }; }
        private static IDictionary<string, object> Response(string mode)
        { return new Dictionary<string, object> { { "schema", 1 }, { "seat_id", "lab-01" }, { "cleanup_mode", mode } }; }
        private static bool Invalid(Action action)
        { try { action(); return false; } catch (InvalidDataException) { return true; } }
        private static void Defaulted(NativeCleanupPolicy policy, string source)
        {
            Assert(policy.Mode == NativeCleanupPolicy.NextStart && policy.Source == source, "Fallback did not choose the explicit next_start default.");
            Assert(policy.StartupMessage.Contains("주의") && policy.StartupMessage.Contains("기본값") && policy.StartupMessage.Contains("다음 IDE 실행"), "Fallback warning was not explicit.");
        }
        private static int Main()
        {
            try {
                var config = NativeCleanupPolicy.ParseConfig(Config());
                Assert(config.SeatId == "lab-01" && config.ApiUrl.Scheme == "https", "Valid private config was rejected.");
                foreach (string url in new[] { "http://example.edu", "https://user:password@example.edu", "https://example.edu/path", "https://example.edu/?query=1", "https://example.edu/#fragment", "file:///tmp/operator", "relative", "" }) {
                    var bad = Config(); bad["apiUrl"] = url;
                    Assert(Invalid(() => NativeCleanupPolicy.ParseConfig(bad)), "An unsafe API URL was accepted.");
                }
                foreach (object seat in new object[] { "../lab", "LAB", "lab\n", "lab/01", "-lab", new string('a', 65), "", null, 1 }) {
                    var bad = Config(); bad["seatId"] = seat;
                    Assert(Invalid(() => NativeCleanupPolicy.ParseConfig(bad)), "An invalid seat ID was accepted.");
                }
                foreach (object token in new object[] { new string('a', 42), new string('a', 44), new string('a', 43) + "\n", new string('/', 43), "", null, 1 }) {
                    var bad = Config(); bad["reportToken"] = token;
                    Assert(Invalid(() => NativeCleanupPolicy.ParseConfig(bad)), "An invalid device token was accepted.");
                }
                var extra = Config(); extra["cleanup_mode"] = "on_exit";
                Assert(Invalid(() => NativeCleanupPolicy.ParseConfig(extra)), "Device config accepted a local policy override.");
                var missing = Config(); missing.Remove("reportToken");
                Assert(Invalid(() => NativeCleanupPolicy.ParseConfig(missing)), "Missing token was accepted.");
                Assert(Invalid(() => NativeCleanupPolicy.ParseConfig(null)), "Null config was accepted.");
                foreach (string mode in new[] { NativeCleanupPolicy.NextStart, NativeCleanupPolicy.OnExit }) {
                    var policy = NativeCleanupPolicy.Resolve(() => config, supplied => {
                        Assert(Object.ReferenceEquals(supplied, config), "Fetcher received different credentials.");
                        return Response(mode);
                    });
                    Assert(policy.Mode == mode && policy.Source == "server", "Server policy was not selected.");
                    Assert(policy.StartupMessage.Contains("서버 담당자 정책 확인") && !policy.StartupMessage.Contains("기본값"), "Confirmed policy appeared as a fallback.");
                    Assert(policy.RemovalMessage.Contains(mode == NativeCleanupPolicy.OnExit ? "종료할 때" : "다음 IDE 실행"), "Exit warning contradicts the selected mode.");
                }
                foreach (object mode in new object[] { "exit", "on_exit\n", "next_start\n", "never", null, 1 }) {
                    var bad = Response("next_start"); bad["cleanup_mode"] = mode;
                    Assert(Invalid(() => NativeCleanupPolicy.ParseResponse(bad, "lab-01")), "Invalid mode was accepted.");
                    Defaulted(NativeCleanupPolicy.Resolve(() => config, supplied => bad), "default-unconfirmed");
                }
                foreach (object schema in new object[] { "1", 1.0, 2, null }) {
                    var bad = Response("on_exit"); bad["schema"] = schema;
                    Assert(Invalid(() => NativeCleanupPolicy.ParseResponse(bad, "lab-01")), "Invalid schema was accepted.");
                }
                Assert(Invalid(() => NativeCleanupPolicy.ParseResponse(Response("on_exit"), "another-seat")), "Another seat's policy was accepted.");
                var extended = Response("on_exit"); extended["unexpected"] = true;
                Assert(Invalid(() => NativeCleanupPolicy.ParseResponse(extended, "lab-01")), "Unexpected policy payload was accepted.");
                Assert(Invalid(() => NativeCleanupPolicy.ParseResponse(null, "lab-01")), "Null policy was accepted.");
                bool fetched = false;
                Defaulted(NativeCleanupPolicy.Resolve(() => null, supplied => { fetched = true; return Response("on_exit"); }), "default-no-config");
                Assert(!fetched, "Missing config triggered a network request.");
                Defaulted(NativeCleanupPolicy.Resolve(() => { throw new IOException("synthetic-private-secret"); }, supplied => { fetched = true; return Response("on_exit"); }), "default-invalid-config");
                Assert(!fetched, "Unreadable config triggered a network request.");
                foreach (Exception failure in new Exception[] { new TimeoutException("synthetic-private-secret"), new IOException("synthetic-private-secret"), new InvalidDataException("synthetic-private-secret") }) {
                    var policy = NativeCleanupPolicy.Resolve(() => config, supplied => { throw failure; });
                    Defaulted(policy, "default-unconfirmed");
                    Assert(!policy.StartupMessage.Contains("synthetic-private-secret") && !policy.StartupMessage.Contains(config.ReportToken), "Fallback exposed exception data or a token.");
                }
                var mutable = Response("on_exit");
                var snapshot = NativeCleanupPolicy.Resolve(() => config, supplied => mutable);
                mutable["cleanup_mode"] = "next_start";
                Assert(snapshot.Mode == "on_exit", "A response change mutated an active policy snapshot.");
                Console.WriteLine("PASS: " + assertions + " cleanup-policy assertions (validation, injected client, fallback, immutable snapshot and secret-free warnings)");
                return 0;
            } catch (Exception error) { Console.Error.WriteLine(error); return 1; }
        }
    }
}
