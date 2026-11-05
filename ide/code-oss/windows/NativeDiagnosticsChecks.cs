// Compile with NativeDiagnostics.cs only. No Win32 calls or student session access.
using System;
using System.ComponentModel;
using System.IO;

namespace Autograde.NativeWindows
{
    internal static class NativeDiagnosticsChecks
    {
        private static int count;
        private static void Assert(bool condition, string message) { count++; if (!condition) throw new Exception(message); }
        private static Exception Capture(Action action)
        {
            try { action(); }
            catch (Exception error) { return error; }
            throw new Exception("Expected an exception.");
        }
        private static void Main()
        {
            Assert(NativeDiagnostics.Run("toolchain", delegate { return 42; }) == 42, "Run changed the return value.");
            bool ran = false;
            NativeDiagnostics.Run("toolchain", delegate { ran = true; });
            Assert(ran, "Action was not run.");

            var original = new InvalidDataException("Unmarked working directory is not empty.");
            Exception failure = Capture(delegate { NativeDiagnostics.Run("session-recovery", delegate { throw original; }); });
            var marked = failure as NativeStageException;
            Assert(marked != null && marked.Stage == "session-recovery", "Failing stage was lost.");
            Assert(Object.ReferenceEquals(marked.InnerException, original), "Original exception was lost.");
            Exception nested = Capture(delegate { NativeDiagnostics.Run("runtime", delegate { throw failure; }); });
            Assert(Object.ReferenceEquals(nested, failure), "Outer operation overwrote the failure stage.");
            string diagnostic = NativeDiagnostics.Format(nested);
            Assert(diagnostic.Contains("native.session-recovery.owner-record"), "Ownership reason was not classified.");
            Assert(diagnostic.Contains("System.IO.InvalidDataException") && diagnostic.Contains("HResult: 0x"), "Exception metadata is missing.");

            bool cleaned = false;
            int value = NativeDiagnostics.WithCleanup(delegate { return 3; }, delegate { cleaned = true; });
            Assert(value == 3 && cleaned, "Successful work did not clean up.");
            cleaned = false;
            Exception single = Capture(delegate {
                NativeDiagnostics.WithCleanup<int>(delegate { throw failure; }, delegate { cleaned = true; });
            });
            Assert(cleaned && Object.ReferenceEquals(single, failure), "Cleanup changed the original failure.");
            var cleanup = new IOException("private-file-name-secret");
            Exception dual = Capture(delegate {
                NativeDiagnostics.WithCleanup<int>(delegate { throw failure; }, delegate {
                    NativeDiagnostics.Run("session-cleanup", delegate { throw cleanup; });
                });
            });
            var aggregate = dual as AggregateException;
            Assert(aggregate != null && aggregate.InnerExceptions.Count == 2, "Dual failures were not retained.");
            Assert(Object.ReferenceEquals(aggregate.InnerExceptions[0], failure), "Primary failure ordering changed.");
            Assert(Object.ReferenceEquals(aggregate.InnerExceptions[1].InnerException, cleanup), "Cleanup failure was lost.");
            diagnostic = NativeDiagnostics.Format(dual);
            Assert(diagnostic.IndexOf("[session-recovery]", StringComparison.Ordinal) < diagnostic.IndexOf("[session-cleanup]", StringComparison.Ordinal), "Diagnostic changed failure ordering.");
            Assert(!diagnostic.Contains("private-file-name-secret"), "Raw cleanup message was exposed.");
            Exception cleanupOnly = Capture(delegate {
                NativeDiagnostics.WithCleanup(delegate { return 1; }, delegate { throw cleanup; });
            });
            Assert(Object.ReferenceEquals(cleanupOnly, cleanup), "Cleanup-only error was hidden.");

            var native = new Win32Exception(5, "private windows message");
            diagnostic = NativeDiagnostics.Format(new NativeStageException("runtime",
                new InvalidOperationException("Cannot start the bundled runtime.", native)));
            Assert(diagnostic.Contains("Windows 오류 번호: 5"), "Native error code was lost inside the cause.");
            Assert(diagnostic.Contains("native.runtime.runtime-start"), "Known runtime error was not classified.");
            Assert(!diagnostic.Contains("private windows message"), "Native error message was exposed.");

            const string secret = "AK1-TEST-SECRET password=learner C:\\Users\\student\\answer.cpp";
            var arbitrary = new NativeStageException(secret, new FileNotFoundException(secret, secret, new Exception(secret)));
            diagnostic = NativeDiagnostics.Format(arbitrary);
            Assert(diagnostic.Contains("[unknown]"), "Unknown stage was not normalized.");
            Assert(!diagnostic.Contains("AK1-") && !diagnostic.Contains("learner") && !diagnostic.Contains("answer.cpp"), "Student data was exposed.");
            Assert(!diagnostic.Contains(" at ") && !diagnostic.Contains(" 위치:"), "Stack traces were exposed.");

            foreach (string stage in new[] { "package-node", "package-bootstrap", "package-manifest" }) {
                diagnostic = NativeDiagnostics.Format(new NativeStageException(stage, new FileNotFoundException(secret)));
                Assert(diagnostic.Contains("[" + stage + "]"), "Package component stage was lost.");
                Assert(!diagnostic.Contains(secret), "File-not-found path was exposed.");
            }
            var many = new Exception[100];
            for (int i = 0; i < many.Length; i++) many[i] = new NativeStageException("runtime", new Exception(secret));
            diagnostic = NativeDiagnostics.Format(new AggregateException(many));
            Assert(diagnostic.Split(new[] { "진단 코드:" }, StringSplitOptions.None).Length - 1 == 8, "Failure count is not bounded.");
            Assert(diagnostic.Contains("길이 제한"), "Truncation was not reported.");
            Exception deep = new Exception(secret);
            for (int i = 0; i < 100; i++) deep = new NativeStageException("runtime", deep);
            diagnostic = NativeDiagnostics.Format(deep);
            Assert(diagnostic.Contains("길이 제한") && diagnostic.Length < 500, "Depth was not bounded.");
            Console.WriteLine("PASS: native diagnostics (" + count + " assertions): stages, primary/cleanup failures, Win32 codes, privacy, bounded output");
        }
    }
}
