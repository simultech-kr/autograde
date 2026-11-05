using System;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;

namespace Autograde.NativeWindows
{
    internal static class NativeDiagnosticLog
    {
        public static string TryWrite(Exception error)
        {
            try {
                return Write(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "AutogradeNativeDiagnostics"), error);
            } catch { return null; } // Reporting must never replace the original failure.
        }

        internal static string Write(string directory, Exception error)
        {
            NativePaths.PrivateDirectory(directory, true);
            string file = Path.Combine(directory, "launch-" + Guid.NewGuid().ToString("N") + ".log");
            // Format deliberately excludes arbitrary exception messages, paths,
            // environment values, stack traces and all student/session contents.
            string report = "Autograde native launcher diagnostics\nUTC: " + DateTime.UtcNow.ToString("o")
                + "\nVersion: " + Assembly.GetExecutingAssembly().GetName().Version
                + "\n\n" + NativeDiagnostics.Format(error) + "\n";
            byte[] bytes = new UTF8Encoding(false).GetBytes(report);
            using (var stream = new FileStream(file, FileMode.CreateNew, FileAccess.Write, FileShare.Read)) {
                stream.Write(bytes, 0, bytes.Length); stream.Flush(true);
            }
            // Keep at most ten closed reports from this logger. Do not traverse
            // subdirectories or touch another program's files or linked paths.
            try {
                var older = Directory.GetFiles(directory, "launch-*.log")
                    .Where(value => value != file && Regex.IsMatch(Path.GetFileName(value), "^launch-[a-f0-9]{32}\\.log$")
                        && (File.GetAttributes(value) & (FileAttributes.ReparsePoint | FileAttributes.Directory)) == 0)
                    .OrderByDescending(File.GetLastWriteTimeUtc).Skip(9);
                foreach (string old in older) { try { File.Delete(old); } catch (IOException) { } catch (UnauthorizedAccessException) { } }
            } catch (IOException) { } catch (UnauthorizedAccessException) { }
            return file;
        }
    }
}
