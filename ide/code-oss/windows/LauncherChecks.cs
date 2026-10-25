// Run this compiled console program on Windows. The quote test launches this
// executable itself, so it verifies real Windows/.NET argument parsing.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Web.Script.Serialization;

namespace Autograde.WslLauncher
{
    internal static class LauncherChecks
    {
        private static int passed;
        private const string Ready = "{\"event\":\"ready\",\"schema\":1,\"platform\":\"wsl2\",\"architecture\":\"x64\",\"extensionVersion\":\"0.5.12\",\"distribution\":\"Ubuntu-24.04\",\"uid\":1000,\"gui\":\"x11\",\"sandbox\":\"user-namespace\",\"lifetime\":\"shared\",\"noSandbox\":false}";
        private static string Config(string distribution, string user, string root)
        { return new JavaScriptSerializer().Serialize(new { schema = 1, distribution = distribution, user = user, installRoot = root }); }
        private static void Assert(bool condition, string label)
        { if (!condition) throw new Exception(label); }
        private static void Check(string label, Action body)
        { body(); passed++; Console.WriteLine("PASS: " + label); }
        private static void Denied(string json)
        {
            bool denied = false;
            try { LauncherConfig.Parse(json); } catch { denied = true; }
            Assert(denied, "unsafe configuration accepted");
        }

        private static void QuoteRoundTrip()
        {
            string[] expected = { "", "plain", "Ubuntu Course 01", "학교 배포판", @"C:\folder\", "quote\"inside", "space and \\\"quote", "/opt/course; echo NOT_EXECUTED", "$(not-executed)", "&|<>%PATH%", "ends-with-\\\\" };
            var arguments = new List<string> { "--echo" }; arguments.AddRange(expected);
            using (var process = new Process())
            {
                process.StartInfo = new ProcessStartInfo {
                    FileName = Assembly.GetExecutingAssembly().Location,
                    Arguments = WindowsArguments.Join(arguments.ToArray()), UseShellExecute = false,
                    CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true,
                    StandardOutputEncoding = new UTF8Encoding(false), StandardErrorEncoding = new UTF8Encoding(false)
                };
                process.Start();
                var output = process.StandardOutput.ReadToEndAsync();
                var errors = process.StandardError.ReadToEndAsync();
                if (!process.WaitForExit(10000)) { process.Kill(); process.WaitForExit(); throw new Exception("argument echo timed out"); }
                process.WaitForExit();
                Assert(process.ExitCode == 0, "argument echo failed: " + errors.Result);
                var actual = new JavaScriptSerializer().Deserialize<string[]>(output.Result);
                Assert(actual.Length == expected.Length, "argument count changed");
                for (int i = 0; i < expected.Length; i++) Assert(actual[i] == expected[i], "argument " + i + " changed");
            }
        }

        private static int Main(string[] args)
        {
            Console.OutputEncoding = new UTF8Encoding(false);
            if (args.Length > 0 && args[0] == "--echo")
            {
                var values = new List<string>(args); values.RemoveAt(0);
                Console.WriteLine(new JavaScriptSerializer().Serialize(values.ToArray())); return 0;
            }
            try
            {
                Check("literal arguments survive actual Windows process parsing", QuoteRoundTrip);
                Check("exact WSL distro/user/exec argv; no shell", delegate {
                    var config = LauncherConfig.Parse(Config("Ubuntu Course 01", "student", "/opt/학교 실습"));
                    var actual = config.Arguments(false);
                    Assert(actual.Length == 6 && actual[0] == "--distribution" && actual[1] == "Ubuntu Course 01" &&
                        actual[2] == "--user" && actual[3] == "student" && actual[4] == "--exec" && actual[5] == "/opt/학교 실습/bin/autograde-ide", "argv mismatch");
                    Assert(config.Arguments(true)[6] == "--check", "check argument missing");
                });
                Check("root account rejected", delegate { Denied(Config("Ubuntu", "root", "/opt/autograde")); });
                Check("trailing newline never bypasses distro or root-user validation", delegate {
                    Denied(Config("Ubuntu\n", "student", "/opt/autograde"));
                    Denied(Config("Ubuntu", "root\n", "/opt/autograde"));
                    Denied(Config("Ubuntu", "student\n", "/opt/autograde"));
                });
                Check("distribution flags cannot become options", delegate { Denied(Config("--terminate Ubuntu", "student", "/opt/autograde")); });
                Check("Windows mount, relative and traversal paths rejected", delegate {
                    foreach (string root in new string[] { "/mnt/c/app", "relative/app", "/opt/../app", "/opt//app", "/", "/opt/app/", "/opt/line\napp", @"C:\app" }) Denied(Config("Ubuntu", "student", root));
                });
                Check("unknown config keys are rejected", delegate { Denied("{\"schema\":1,\"distribution\":\"Ubuntu\",\"user\":\"student\",\"installRoot\":\"/opt/app\",\"arguments\":\"--no-sandbox\"}"); });
                Check("updates and copied launchers share the same seat mutex", delegate {
                    Assert(LauncherConfig.Parse(Config("Ubuntu", "student", "/opt/version1")).MutexName() == LauncherConfig.Parse(Config("Ubuntu", "student", "/opt/version2")).MutexName(), "different version bypasses mutex");
                    Assert(LauncherConfig.Parse(Config("Ubuntu", "student", "/opt/app")).MutexName() != LauncherConfig.Parse(Config("Ubuntu", "other", "/opt/app")).MutexName(), "users share mutex");
                });
                Check("empty success cannot claim cleanup", delegate { Assert(new SessionProtocol(false).ResultError(0) != null, "empty success"); });
                Check("start without cleanup is failure", delegate {
                    var p = new SessionProtocol(false); p.Accept("{\"event\":\"ide-started\",\"pid\":123}"); Assert(p.ResultError(0) != null, "missing cleanup");
                });
                Check("cleanup without recorded start is failure", delegate {
                    var p = new SessionProtocol(false); p.Accept("{\"event\":\"session-cleaned\"}"); Assert(p.ResultError(0) != null, "missing start");
                });
                Check("complete session succeeds only after cleanup", delegate {
                    var p = new SessionProtocol(false); p.Accept("{\"event\":\"ide-started\",\"pid\":123}"); p.Accept("{\"event\":\"session-cleaned\"}"); Assert(p.ResultError(0) == null, "valid session rejected");
                    Assert(p.ResultError(3) != null, "crashed process reported success");
                });
                Check("failure message survives cleanup event", delegate {
                    var p = new SessionProtocol(false); p.Accept("{\"event\":\"ide-started\",\"pid\":123}"); p.Accept("{\"event\":\"error\",\"message\":\"정리 실패\"}"); p.Accept("{\"event\":\"session-cleaned\"}"); Assert(p.ResultError(0) == "정리 실패", "failure lost");
                });
                Check("valid WSL2 sandboxed preflight accepted", delegate {
                    var p = new SessionProtocol(true, "Ubuntu-24.04"); p.Accept(Ready); Assert(p.ResultError(0) == null, "ready rejected");
                });
                Check("bare ready or wrong distribution cannot create shortcut", delegate {
                    var p = new SessionProtocol(true, "other"); p.Accept(Ready); Assert(p.ResultError(0) != null, "wrong distribution accepted");
                    p = new SessionProtocol(true); p.Accept("{\"event\":\"ready\"}"); Assert(p.ResultError(0) != null, "bare ready accepted");
                });
                Check("root, WSL1, unsafe sandbox and persistent ready are rejected", delegate {
                    foreach (var pair in new string[][] { new[] { "\"uid\":1000", "\"uid\":0" }, new[] { "\"platform\":\"wsl2\"", "\"platform\":\"wsl1\"" }, new[] { "\"noSandbox\":false", "\"noSandbox\":true" }, new[] { "\"lifetime\":\"shared\"", "\"lifetime\":\"persistent\"" } })
                    { var p = new SessionProtocol(true); p.Accept(Ready.Replace(pair[0], pair[1])); Assert(p.ResultError(0) != null, "unsafe ready accepted"); }
                });
                Check("invalid JSON, oversized lines and unrecognized events fail closed", delegate {
                    foreach (string line in new[] { "unexpected log", new string('x', 9000), "{\"event\":\"done\"}" })
                    { var p = new SessionProtocol(true); p.Accept(line); p.Accept(Ready); Assert(p.ResultError(0) != null, "invalid protocol accepted"); }
                });
                Console.WriteLine("Windows launcher checks: " + passed + " passed"); return 0;
            }
            catch (Exception error) { Console.Error.WriteLine("FAIL: " + error.Message); return 1; }
        }
    }
}
