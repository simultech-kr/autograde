// This executable must run on Windows; a successful cross-compile is not a pass.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Threading;
using System.Threading.Tasks;

namespace Autograde.NativeWindows
{
    internal static class NativeLauncherChecks
    {
        private static void Assert(bool value, string message) { if (!value) throw new Exception(message); }
        private static bool Blocked(Action action)
        {
            try { action(); return false; }
            catch (IOException) { return true; }
            catch (InvalidOperationException) { return true; }
        }
        private static void ReplaceOwner(string file, IDictionary<string, object> owner) { File.Delete(file); NativePaths.WriteJson(file, owner); }
        private static void MarkStopped(NativeSessionStore store, string session)
        {
            var owner = NativePaths.ReadJson(Path.Combine(session, ".native-owner.json"));
            owner["launcherPid"] = Int32.MaxValue; owner["launcherStarted"] = "0";
            ReplaceOwner(Path.Combine(session, ".native-owner.json"), owner);
            if (session == store.Current) ReplaceOwner(Path.Combine(store.Sessions, ".current-owner.json"), owner);
        }
        private static void CheckRecovery(NativeSessionStore store, string exported)
        {
            string anchor = Path.Combine(store.Sessions, ".current-owner.json");
            string current = store.CreateSession(), oldId = store.SessionId(current);
            Assert(current == Path.Combine(store.Sessions, "current"), "Working directory is not fixed.");
            Assert(Blocked(() => store.CreateSession()), "A new session reused an uncleared working directory.");
            Assert(Blocked(store.Recover) && Directory.Exists(current), "Recovery deleted a live launcher session.");
            store.Cleanup(current);
            string fresh = store.CreateSession(); Assert(fresh == current && store.SessionId(fresh) != oldId, "Fixed path did not receive a fresh session identity.");
            MarkStopped(store, fresh); File.WriteAllText(Path.Combine(fresh, "stale.c"), "synthetic interrupted session");
            string unresolved = Path.Combine(fresh, ".native-launch-pending.json"); NativePaths.WriteJson(unresolved, new { schema = 1 });
            Assert(Blocked(store.Recover) && File.Exists(anchor), "Unconfirmed process containment did not block recovery.");
            File.Delete(unresolved); // Synthetic fixture never launched a child.
            store.Recover(); Assert(!Directory.Exists(fresh) && !File.Exists(anchor) && File.Exists(exported), "Stale recovery retained a session or removed exports.");

            current = store.CreateSession(); MarkStopped(store, current);
            File.Delete(Path.Combine(current, ".native-owner.json"));
            store.Recover(); Assert(!Directory.Exists(current) && !File.Exists(anchor), "Empty owned directory after final-marker removal could not recover.");
            current = store.CreateSession(); MarkStopped(store, current);
            File.Delete(Path.Combine(current, ".native-owner.json")); Directory.Delete(current);
            store.Recover(); Assert(!File.Exists(anchor), "Parent proof was not recovered after directory removal.");

            current = store.CreateSession(); MarkStopped(store, current);
            string savedOwner = File.ReadAllText(Path.Combine(current, ".native-owner.json"));
            File.Delete(Path.Combine(current, ".native-owner.json")); File.WriteAllText(Path.Combine(current, "unmarked.c"), "keep");
            Assert(Blocked(store.Recover) && File.Exists(Path.Combine(current, "unmarked.c")), "A nonempty directory without its child marker was deleted.");
            File.WriteAllText(Path.Combine(current, ".native-owner.json"), savedOwner); store.Recover();

            NativePaths.PrivateDirectory(store.Current, true); File.WriteAllText(Path.Combine(store.Current, "unknown.c"), "keep");
            Assert(Blocked(store.Recover) && File.Exists(Path.Combine(store.Current, "unknown.c")), "An unowned fixed path was adopted or deleted.");
            File.Delete(Path.Combine(store.Current, "unknown.c")); Directory.Delete(store.Current);

            current = store.CreateSession(); MarkStopped(store, current);
            string original = Path.Combine(store.Base, "moved-owned-directory"); Directory.Move(current, original);
            NativePaths.PrivateDirectory(current, true); File.WriteAllText(Path.Combine(current, "replacement.c"), "keep");
            Assert(Blocked(store.Recover) && File.Exists(Path.Combine(current, "replacement.c")), "A replacement directory with another NTFS identity was deleted.");
            File.Delete(Path.Combine(current, "replacement.c")); Directory.Delete(current); Directory.Move(original, current); store.Recover();

            current = store.CreateSession(); MarkStopped(store, current);
            string junction = Path.Combine(current, "export-junction");
            string command = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), @"System32\cmd.exe");
            using (var link = Process.Start(new ProcessStartInfo(command, "/d /c mklink /J " + NativePaths.Quote(junction) + " " + NativePaths.Quote(store.Exports)) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true })) {
                Assert(link.WaitForExit(10000) && link.ExitCode == 0, "Could not create the synthetic NTFS junction fixture.");
            }
            Assert(Blocked(store.Recover) && File.Exists(exported), "Recovery followed a junction outside the session.");
            Directory.Delete(junction, false); store.Recover();

            string legacyId = Guid.NewGuid().ToString("N").Substring(0, 24), legacy = Path.Combine(store.Sessions, "e-" + legacyId);
            NativePaths.PrivateDirectory(legacy, true);
            NativePaths.WriteJson(Path.Combine(legacy, ".native-owner.json"), new { schema = 1, sid = NativePaths.Sid, sessionId = legacyId, root = legacy, launcherPid = Int32.MaxValue, launcherStarted = "0", jobName = NativeSessionStore.JobName(legacyId) });
            File.WriteAllText(Path.Combine(legacy, "old-format.c"), "synthetic migration");
            store.Recover(); Assert(!Directory.Exists(legacy) && File.Exists(exported), "Legacy session migration failed or removed exports.");
            Console.WriteLine("PASS: fixed path, fresh identity, live-process protection, interrupted cleanup, replacement/junction protection and legacy migration");
        }
        private static int Main(string[] args)
        {
            if (args.Length == 2 && args[0] == "--grandchild") { File.WriteAllText(args[1], Process.GetCurrentProcess().Id.ToString()); Thread.Sleep(Timeout.Infinite); return 0; }
            if (args.Length == 2 && args[0] == "--child") { Process.Start(new ProcessStartInfo(Assembly.GetExecutingAssembly().Location, "--grandchild " + NativePaths.Quote(args[1])) { UseShellExecute = false, CreateNoWindow = true }); Thread.Sleep(Timeout.Infinite); return 0; }
            string root = Path.Combine(Path.GetTempPath(), "autograde-native-check-" + Guid.NewGuid().ToString("N"));
            try {
                using (var store = new NativeSessionStore(root)) {
                    string exported = Path.Combine(store.Exports, "kept.txt"); File.WriteAllText(exported, "synthetic export");
                    bool blocked = false; try { using (var second = new NativeSessionStore(root)) { } } catch (IOException) { blocked = true; }
                    Assert(blocked, "A second launcher acquired the active lease.");
                    string session = store.CreateSession(); File.WriteAllText(Path.Combine(session, "answer.c"), "synthetic student file");
                    string held = Path.Combine(session, "held.txt"); File.WriteAllText(held, "locked");
                    using (var handle = new FileStream(held, FileMode.Open, FileAccess.Read, FileShare.None)) {
                        blocked = false; try { store.Cleanup(session); } catch (IOException) { blocked = true; }
                        Assert(blocked && File.Exists(Path.Combine(session, ".native-owner.json")) && File.Exists(Path.Combine(store.Sessions, ".current-owner.json")), "Failed cleanup lost its recovery markers.");
                    }
                    store.Cleanup(session); Assert(!Directory.Exists(session) && File.ReadAllText(exported) == "synthetic export", "Cleanup touched export or retained session.");
                    Console.WriteLine("PASS: ACL, exclusive lease, locked-file recovery marker and export preservation");
                    CheckRecovery(store, exported);
                    string marker = Path.Combine(root, "grandchild-pid.txt");
                    var env = new Dictionary<string, string>(); foreach (System.Collections.DictionaryEntry pair in Environment.GetEnvironmentVariables()) env[(string)pair.Key] = (string)pair.Value;
                    using (var job = new NativeJob(@"Local\AutogradeNativeCheck-" + Guid.NewGuid().ToString("N"))) {
                        var running = Task.Run(() => job.Run(Assembly.GetExecutingAssembly().Location, new[] { "--child", marker }, root, env));
                        for (int i = 0; !File.Exists(marker) && i < 200; i++) Thread.Sleep(50);
                        Assert(File.Exists(marker), "The supervised grandchild did not start."); int pid = Int32.Parse(File.ReadAllText(marker));
                        job.StopAndDrain(); Assert(running.Wait(10000), "Root child did not stop.");
                        bool alive = false; try { using (var process = Process.GetProcessById(pid)) alive = !process.HasExited; } catch (ArgumentException) { }
                        Assert(!alive, "A grandchild escaped the Windows job.");
                    }
                    File.Delete(marker); Console.WriteLine("PASS: suspended start, Windows job process-tree termination and drained cleanup");
                    string temporary = Path.Combine(root, "tmp"); NativePaths.PrivateDirectory(temporary, true);
                    var compilerEnvironment = NativeToolchain.EnvironmentFor(temporary);
                    string compiler = Path.Combine(compilerEnvironment["VCToolsInstallDir"], @"bin\Hostx64\x64\cl.exe");
                    using (var compileJob = new NativeJob(@"Local\AutogradeCompilerCheck-" + Guid.NewGuid().ToString("N"))) {
                        try {
                            foreach (string language in new[] { "c", "cpp" }) {
                                string source = Path.Combine(temporary, "hello." + language), binary = Path.Combine(temporary, "hello-" + language + ".exe");
                                File.WriteAllText(source, language == "c" ? "#include <windows.h>\nint main(void){return GetCurrentProcessId()==0;}\n" : "#include <optional>\n#include <windows.h>\nint main(){std::optional<int> x=0;return *x+(GetCurrentProcessId()==0);}\n");
                                Assert(compileJob.Run(compiler, new[] { "/nologo", language == "c" ? "/TC" : "/TP", language == "c" ? "/std:c17" : "/std:c++17", "/EHsc", source, "/Fe:" + binary }, temporary, compilerEnvironment) == 0, "MSVC compile failed.");
                                Assert(compileJob.Run(binary, new string[0], temporary, compilerEnvironment) == 0, "Compiled native program failed.");
                            }
                        } finally { compileJob.StopAndDrain(); }
                    }
                    Console.WriteLine("PASS: actual MSVC C17/C++17 compile, Windows SDK linking and native program execution");
                }
                Directory.Delete(root, true); return 0;
            } catch (Exception error) { Console.Error.WriteLine(error); return 1; }
        }
    }
}
