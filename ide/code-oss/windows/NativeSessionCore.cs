// Windows-specific lifecycle. Never uses POSIX process groups or chmod as ACLs.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using Microsoft.Win32.SafeHandles;

namespace Autograde.NativeWindows
{
    public static class NativePaths
    {
        public static readonly string Sid = WindowsIdentity.GetCurrent().User.Value;
        public static bool Exists(string file)
        {
            try { File.GetAttributes(file); return true; }
            catch (FileNotFoundException) { return false; }
            catch (DirectoryNotFoundException) { return false; }
        }
        public static string DirectoryIdentity(string directory)
        {
            NoLinks(directory);
            // Open the directory itself, never a reparse target. NTFS file IDs
            // survive child deletion, unlike timestamps or the directory name.
            using (var handle = CreateFile(directory, 0, 1 | 2 | 4, IntPtr.Zero, 3, 0x02000000 | 0x00200000, IntPtr.Zero)) {
                FileInformation info;
                if (handle.IsInvalid || !GetFileInformationByHandle(handle, out info) || (info.Attributes & 0x10) == 0 || (info.Attributes & 0x400) != 0) throw new InvalidDataException("Cannot identify the managed directory.");
                return info.Volume.ToString("x8") + ":" + info.IndexHigh.ToString("x8") + info.IndexLow.ToString("x8");
            }
        }
        [StructLayout(LayoutKind.Sequential)] struct FileInformation {
            public uint Attributes, CreatedLow, CreatedHigh, AccessedLow, AccessedHigh, WrittenLow, WrittenHigh, Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
        }
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern SafeFileHandle CreateFile(string name, uint access, uint sharing, IntPtr security, uint creation, uint flags, IntPtr template);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool GetFileInformationByHandle(SafeFileHandle file, out FileInformation info);
        public static void NoLinks(string value)
        {
            string current = Path.GetFullPath(value);
            for (;;) {
                if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0) throw new InvalidDataException("Linked path is not allowed.");
                string parent = Path.GetDirectoryName(current); if (String.IsNullOrEmpty(parent) || parent == current) break; current = parent;
            }
        }
        public static void PrivateDirectory(string value, bool create)
        {
            if (!Directory.Exists(value)) {
                if (!create) throw new InvalidDataException("Private directory is missing.");
                NoLinks(Path.GetDirectoryName(value));
                var acl = new DirectorySecurity(); var sid = new SecurityIdentifier(Sid);
                acl.SetOwner(sid); acl.SetAccessRuleProtection(true, false);
                acl.AddAccessRule(new FileSystemAccessRule(sid, FileSystemRights.FullControl, InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
                Directory.CreateDirectory(value, acl);
            }
            NoLinks(value);
            var actual = Directory.GetAccessControl(value);
            if (actual.GetOwner(typeof(SecurityIdentifier)).Value != Sid) throw new InvalidDataException("Session directory owner changed.");
            bool full = false;
            foreach (FileSystemAccessRule rule in actual.GetAccessRules(true, true, typeof(SecurityIdentifier))) {
                if (rule.AccessControlType == AccessControlType.Allow && rule.IdentityReference.Value != Sid && rule.IdentityReference.Value != "S-1-5-18") throw new InvalidDataException("Session directory grants another user access.");
                if (rule.AccessControlType == AccessControlType.Allow && rule.IdentityReference.Value == Sid && (rule.FileSystemRights & FileSystemRights.FullControl) == FileSystemRights.FullControl) full = true;
            }
            if (!full) throw new InvalidDataException("Session directory lacks private control.");
        }
        public static string Quote(string value)
        {
            if (value == null || value.IndexOf('\0') >= 0) throw new ArgumentException("Invalid argument");
            var text = new StringBuilder("\""); int slashes = 0;
            foreach (char c in value) { if (c == '\\') { slashes++; continue; } if (c == '"') text.Append('\\', slashes * 2 + 1).Append(c); else text.Append('\\', slashes).Append(c); slashes = 0; }
            return text.Append('\\', slashes * 2).Append('"').ToString();
        }
        public static void WriteJson(string file, object value)
        {
            byte[] data = new UTF8Encoding(false).GetBytes(new JavaScriptSerializer().Serialize(value));
            using (var stream = new FileStream(file, FileMode.CreateNew, FileAccess.Write, FileShare.None)) { stream.Write(data, 0, data.Length); stream.Flush(true); }
        }
        public static Dictionary<string, object> ReadJson(string file)
        {
            NoLinks(file); if (new FileInfo(file).Length > 16384) throw new InvalidDataException("Invalid ownership marker.");
            var result = new JavaScriptSerializer { MaxJsonLength = 16384 }.DeserializeObject(File.ReadAllText(file, Encoding.UTF8)) as Dictionary<string, object>;
            if (result == null) throw new InvalidDataException("Invalid ownership marker."); return result;
        }
    }

    public sealed class NativeJob : IDisposable
    {
        private IntPtr handle; private bool uncontainedProcess;
        public NativeJob(string name)
        {
            handle = CreateJobObject(IntPtr.Zero, name);
            if (handle == IntPtr.Zero || Marshal.GetLastWin32Error() == 183) { Dispose(); throw new InvalidOperationException("Cannot reserve a private job."); }
            var limits = new ExtendedLimit(); limits.BasicLimitInformation.LimitFlags = 0x2000; // KILL_ON_JOB_CLOSE, never BREAKAWAY_OK
            int size = Marshal.SizeOf(limits); IntPtr memory = Marshal.AllocHGlobal(size);
            try { Marshal.StructureToPtr(limits, memory, false); if (!SetInformationJobObject(handle, 9, memory, (uint)size)) throw new InvalidOperationException("Cannot contain the Windows session."); }
            catch { Dispose(); throw; } finally { Marshal.FreeHGlobal(memory); }
        }
        private NativeJob(IntPtr existing) { handle = existing; }
        public static void DrainPrevious(string name)
        {
            IntPtr previous = OpenJobObject(0x0004 | 0x0008, false, name); // QUERY | TERMINATE
            if (previous == IntPtr.Zero) { if (Marshal.GetLastWin32Error() == 2) return; throw new InvalidOperationException("Cannot inspect a previous job."); }
            using (var job = new NativeJob(previous)) job.StopAndDrain();
        }
        public int Run(string executable, string[] arguments, string cwd, IDictionary<string, string> environment, string containmentMarker = null)
        {
            NativePaths.NoLinks(executable); NativePaths.NoLinks(cwd);
            var command = new StringBuilder(NativePaths.Quote(executable)); foreach (var arg in arguments) command.Append(' ').Append(NativePaths.Quote(arg));
            var block = new StringBuilder(); foreach (var pair in environment.OrderBy(p => p.Key, StringComparer.OrdinalIgnoreCase)) { if (pair.Key.IndexOf('=') >= 0 || pair.Key.IndexOf('\0') >= 0 || pair.Value.IndexOf('\0') >= 0) throw new InvalidDataException("Invalid environment."); block.Append(pair.Key).Append('=').Append(pair.Value).Append('\0'); } block.Append('\0');
            IntPtr env = Marshal.StringToHGlobalUni(block.ToString()); var start = new StartupInfo(); start.cb = Marshal.SizeOf(start); ProcessInfo process;
            try {
                if (containmentMarker != null) NativePaths.WriteJson(containmentMarker, new { schema = 1, phase = "process-not-yet-contained" });
                // The child cannot run user code before the job assignment succeeds.
                if (!CreateProcess(executable, command, IntPtr.Zero, IntPtr.Zero, false, 0x4 | 0x400 | 0x08000000, env, cwd, ref start, out process)) throw new InvalidOperationException("Cannot start the bundled runtime.");
            } finally { Marshal.FreeHGlobal(env); }
            try {
                if (!AssignProcessToJobObject(handle, process.hProcess)) {
                    uncontainedProcess = true;
                    if (TerminateProcess(process.hProcess, 1) && WaitForSingleObject(process.hProcess, 30000) == 0) { uncontainedProcess = false; if (containmentMarker != null) File.Delete(containmentMarker); }
                    throw new InvalidOperationException("Cannot supervise the Windows session.");
                }
                if (containmentMarker != null) File.Delete(containmentMarker);
                if (ResumeThread(process.hThread) == UInt32.MaxValue) throw new InvalidOperationException("Cannot resume the contained runtime.");
                if (WaitForSingleObject(process.hProcess, UInt32.MaxValue) != 0) throw new InvalidOperationException("Cannot wait for the session.");
                uint code; if (!GetExitCodeProcess(process.hProcess, out code)) throw new InvalidOperationException("Cannot read the session result."); return (int)code;
            } finally { CloseHandle(process.hThread); CloseHandle(process.hProcess); }
        }
        public void StopAndDrain()
        {
            if (handle == IntPtr.Zero || uncontainedProcess) throw new InvalidOperationException("An uncontained process could still exist; operator recovery is required.");
            if (!TerminateJobObject(handle, 1)) throw new InvalidOperationException("Cannot stop session processes.");
            for (int i = 0; i < 300; i++) { var data = new Accounting(); uint returned; if (!QueryInformationJobObject(handle, 1, ref data, (uint)Marshal.SizeOf(data), out returned)) throw new InvalidOperationException("Cannot verify stopped processes."); if (data.ActiveProcesses == 0) return; Thread.Sleep(100); }
            throw new InvalidOperationException("Session processes are still stopping; cleanup is blocked.");
        }
        public void Dispose() { if (handle != IntPtr.Zero) { CloseHandle(handle); handle = IntPtr.Zero; } }
        [StructLayout(LayoutKind.Sequential)] struct BasicLimit { public long PerProcessUserTimeLimit, PerJobUserTimeLimit; public uint LimitFlags; public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize; public uint ActiveProcessLimit; public UIntPtr Affinity; public uint PriorityClass, SchedulingClass; }
        [StructLayout(LayoutKind.Sequential)] struct IoCounters { public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount, ReadTransferCount, WriteTransferCount, OtherTransferCount; }
        [StructLayout(LayoutKind.Sequential)] struct ExtendedLimit { public BasicLimit BasicLimitInformation; public IoCounters IoInfo; public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed; }
        [StructLayout(LayoutKind.Sequential)] struct Accounting { public long TotalUserTime, TotalKernelTime, ThisPeriodTotalUserTime, ThisPeriodTotalKernelTime; public uint TotalPageFaultCount, TotalProcesses, ActiveProcesses, TotalTerminatedProcesses; }
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)] struct StartupInfo { public int cb; public string reserved, desktop, title; public uint x, y, xSize, ySize, xCountChars, yCountChars, fillAttribute, flags; public short showWindow, reserved2; public IntPtr reservedPtr, input, output, error; }
        [StructLayout(LayoutKind.Sequential)] struct ProcessInfo { public IntPtr hProcess, hThread; public uint processId, threadId; }
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern IntPtr CreateJobObject(IntPtr attributes, string name);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern IntPtr OpenJobObject(uint access, bool inherit, string name);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool SetInformationJobObject(IntPtr job, int kind, IntPtr data, uint size);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool TerminateJobObject(IntPtr job, uint exitCode);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool QueryInformationJobObject(IntPtr job, int kind, ref Accounting data, uint size, out uint returned);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern bool CreateProcess(string application, StringBuilder commandLine, IntPtr processAttributes, IntPtr threadAttributes, bool inherit, uint flags, IntPtr environment, string cwd, ref StartupInfo startup, out ProcessInfo process);
        [DllImport("kernel32.dll", SetLastError = true)] static extern uint ResumeThread(IntPtr thread);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool TerminateProcess(IntPtr process, uint code);
        [DllImport("kernel32.dll", SetLastError = true)] static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);
        [DllImport("kernel32.dll", SetLastError = true)] static extern bool GetExitCodeProcess(IntPtr process, out uint code);
        [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    }

    public sealed class NativeSessionStore : IDisposable
    {
        public readonly string Base, Sessions, Exports, Current; private FileStream lease;
        private string CurrentAnchor { get { return Path.Combine(Sessions, ".current-owner.json"); } }
        public NativeSessionStore(string directory)
        {
            Base = Path.GetFullPath(directory); string drive = Path.GetPathRoot(Base);
            if (Base.StartsWith(@"\\") || new DriveInfo(drive).DriveFormat != "NTFS") throw new InvalidDataException("Native sessions require a local NTFS directory.");
            bool exists = Directory.Exists(Base); NativePaths.PrivateDirectory(Base, true);
            string ownership = Path.Combine(Base, ".autograde-native-root.json");
            if (!exists) NativePaths.WriteJson(ownership, new { schema = 1, sid = NativePaths.Sid });
            var owner = NativePaths.ReadJson(ownership);
            if (!Object.Equals(owner["schema"], 1) || !Object.Equals(owner["sid"], NativePaths.Sid)) throw new InvalidDataException("Native runtime ownership changed.");
            string leaseFile = Path.Combine(Base, ".lease"); if (File.Exists(leaseFile)) NativePaths.NoLinks(leaseFile);
            lease = new FileStream(leaseFile, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
            try { Sessions = Path.Combine(Base, "sessions"); Exports = Path.Combine(Base, "exports"); Current = Path.Combine(Sessions, "current"); NativePaths.PrivateDirectory(Sessions, true); NativePaths.PrivateDirectory(Exports, true); Recover(); }
            catch { lease.Dispose(); throw; }
        }
        public static string JobName(string id) { return @"Local\AutogradeNative-" + NativePaths.Sid + "-" + id; }
        public string CreateSession()
        {
            string id = Guid.NewGuid().ToString("N").Substring(0, 24), root = Current;
            if (NativePaths.Exists(root) || NativePaths.Exists(CurrentAnchor)) throw new InvalidOperationException("Previous working directory cleanup is incomplete.");
            NativePaths.PrivateDirectory(root, true);
            using (var process = Process.GetCurrentProcess()) {
                var owner = new { schema = 1, sid = NativePaths.Sid, sessionId = id, root, rootIdentity = NativePaths.DirectoryIdentity(root), launcherPid = process.Id,
                    launcherStarted = process.StartTime.ToUniversalTime().Ticks.ToString(), jobName = JobName(id) };
                // Parent proof outlives the payload and its last child marker.
                NativePaths.WriteJson(CurrentAnchor, owner);
                NativePaths.WriteJson(Path.Combine(root, ".native-owner.json"), owner);
            }
            return root;
        }
        public string SessionId(string root) { return (string)Owner(root)["sessionId"]; }
        private Dictionary<string, object> ValidateOwner(string root, Dictionary<string, object> value)
        {
            string name = Path.GetFileName(root);
            if ((name != "current" && !Regex.IsMatch(name, "^e-[a-f0-9]{24}$")) || Path.GetDirectoryName(root) != Sessions) throw new InvalidDataException("Unexpected session directory.");
            object rawId;
            string id = value.TryGetValue("sessionId", out rawId) ? rawId as string : null;
            if (id == null || !Regex.IsMatch(id, "^[a-f0-9]{24}$") || (name != "current" && name != "e-" + id) ||
                !Object.Equals(value["schema"], 1) || !Object.Equals(value["sid"], NativePaths.Sid) || !Object.Equals(value["root"], root) || !Object.Equals(value["jobName"], JobName(id)) ||
                !(value["launcherPid"] is int) || (int)value["launcherPid"] <= 0 || !(value["launcherStarted"] is string) || !Regex.IsMatch((string)value["launcherStarted"], "^[0-9]{1,19}$")) throw new InvalidDataException("Session ownership changed.");
            if (name == "current" && (!value.ContainsKey("rootIdentity") || !(value["rootIdentity"] is string) || !Regex.IsMatch((string)value["rootIdentity"], "^[a-f0-9]{8}:[a-f0-9]{16}$"))) throw new InvalidDataException("Fixed working directory identity is missing.");
            return value;
        }
        private Dictionary<string, object> Owner(string root)
        {
            NativePaths.PrivateDirectory(root, false);
            string marker = Path.Combine(root, ".native-owner.json");
            if (root != Current) return ValidateOwner(root, NativePaths.ReadJson(marker));
            var anchor = ValidateOwner(root, NativePaths.ReadJson(CurrentAnchor));
            if (!Object.Equals(anchor["rootIdentity"], NativePaths.DirectoryIdentity(root))) throw new InvalidDataException("Fixed working directory was replaced.");
            if (!NativePaths.Exists(marker)) {
                if (Directory.GetFileSystemEntries(root).Length != 0) throw new InvalidDataException("Unmarked working directory is not empty.");
                return anchor; // Interrupted after the final child marker deletion.
            }
            var value = ValidateOwner(root, NativePaths.ReadJson(marker));
            foreach (string field in new[] { "schema", "sid", "sessionId", "root", "rootIdentity", "launcherPid", "launcherStarted", "jobName" })
                if (!Object.Equals(value[field], anchor[field])) throw new InvalidDataException("Fixed working directory marker differs from its parent proof.");
            return value;
        }
        private static void RequireStopped(Dictionary<string, object> owner)
        {
            int pid = (int)owner["launcherPid"]; bool alive = false;
            try { using (var process = Process.GetProcessById(pid)) alive = !process.HasExited && process.StartTime.ToUniversalTime().Ticks.ToString() == (string)owner["launcherStarted"]; }
            catch (ArgumentException) { }
            if (alive) throw new InvalidOperationException("A previous native launcher is still running.");
            NativeJob.DrainPrevious((string)owner["jobName"]);
        }
        public void Recover()
        {
            if (NativePaths.Exists(CurrentAnchor) && !NativePaths.Exists(Current)) {
                var owner = ValidateOwner(Current, NativePaths.ReadJson(CurrentAnchor));
                RequireStopped(owner); File.Delete(CurrentAnchor); // Directory removal finished before parent proof removal.
            }
            foreach (string item in Directory.GetFileSystemEntries(Sessions)) {
                if (item == CurrentAnchor) continue;
                var owner = Owner(item);
                if (NativePaths.Exists(Path.Combine(item, ".native-launch-pending.json"))) throw new InvalidOperationException("A previous launch stopped before process containment was confirmed; operator recovery is required.");
                RequireStopped(owner); Cleanup(item);
            }
        }
        public void Cleanup(string root)
        {
            Owner(root); CheckTree(root);
            foreach (string item in Directory.GetFileSystemEntries(root)) {
                if (Path.GetFileName(item) == ".native-owner.json") continue;
                NativePaths.NoLinks(item); if (Directory.Exists(item)) DeleteTree(item); else File.Delete(item);
            }
            // Keep recovery evidence until every payload file has been removed.
            Owner(root); File.Delete(Path.Combine(root, ".native-owner.json")); Directory.Delete(root, false);
            if (Directory.Exists(root)) throw new IOException("Session cleanup was not completed.");
            if (root == Current) File.Delete(CurrentAnchor);
        }
        private static void CheckTree(string directory)
        {
            NativePaths.PrivateDirectory(directory, false);
            foreach (string item in Directory.GetFileSystemEntries(directory)) { NativePaths.NoLinks(item); if (Directory.Exists(item)) CheckTree(item); }
        }
        private static void DeleteTree(string directory)
        {
            NativePaths.NoLinks(directory);
            foreach (string item in Directory.GetFileSystemEntries(directory)) { NativePaths.NoLinks(item); if (Directory.Exists(item)) DeleteTree(item); else File.Delete(item); }
            Directory.Delete(directory, false);
        }
        public void Dispose() { if (lease != null) { lease.Dispose(); lease = null; } }
    }

    public static class NativeToolchain
    {
        public static Dictionary<string, string> EnvironmentFor(string temporary)
        {
            var env = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (string key in new[] { "SystemRoot", "WINDIR", "SystemDrive", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "COMSPEC", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE" }) { string value = Environment.GetEnvironmentVariable(key); if (!String.IsNullOrEmpty(value)) env[key] = value; }
            string windows = Environment.GetFolderPath(Environment.SpecialFolder.Windows);
            env["PATH"] = Path.Combine(windows, "System32") + ";" + windows + ";" + Path.Combine(windows, @"System32\Wbem"); env["TEMP"] = temporary; env["TMP"] = temporary;
            string vswhere = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86), @"Microsoft Visual Studio\Installer\vswhere.exe");
            if (!File.Exists(vswhere)) throw new InvalidDataException("MSVC C++ 도구와 Windows SDK가 필요합니다. 담당자가 Visual Studio Build Tools의 C++ 데스크톱 개발을 먼저 설치해 주세요.");
            string install = Capture(vswhere, "-latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath", env).Trim();
            if (String.IsNullOrEmpty(install) || Regex.IsMatch(install, "[\r\n\"&|<>^%!]") || !Path.IsPathRooted(install)) throw new InvalidDataException("MSVC x64 도구 설치를 확인해 주세요.");
            string setup = Path.Combine(install, @"VC\Auxiliary\Build\vcvarsall.bat"); NativePaths.NoLinks(setup);
            // Only this discovered Microsoft setup script uses cmd; no learner
            // input, workspace text or arbitrary arguments become shell code.
            string output = Capture(Path.Combine(windows, @"System32\cmd.exe"), "/d /s /c \"\"" + setup + "\" x64 >nul && set\"", env);
            foreach (string line in output.Split(new[] { '\r', '\n' }, StringSplitOptions.RemoveEmptyEntries)) { int equals = line.IndexOf('='); if (equals > 0) env[line.Substring(0, equals)] = line.Substring(equals + 1); }
            string tools, sdk, version;
            if (!env.TryGetValue("VCToolsInstallDir", out tools) || !env.TryGetValue("WindowsSdkDir", out sdk) || !env.TryGetValue("WindowsSDKVersion", out version) ||
                !File.Exists(Path.Combine(tools, @"bin\Hostx64\x64\cl.exe")) || !File.Exists(Path.Combine(sdk, "Include", version.TrimEnd('\\'), @"um\Windows.h")) || !File.Exists(Path.Combine(sdk, "Lib", version.TrimEnd('\\'), @"um\x64\kernel32.lib"))) throw new InvalidDataException("MSVC와 Windows SDK 헤더·라이브러리 설치가 완료되지 않았습니다. 담당자에게 확인해 주세요.");
            string git = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), @"Git\cmd");
            if (File.Exists(Path.Combine(git, "git.exe"))) env["PATH"] += ";" + git;
            return env;
        }
        private static string Capture(string program, string arguments, IDictionary<string, string> environment)
        {
            NativePaths.NoLinks(program);
            var info = new ProcessStartInfo(program, arguments) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
            info.EnvironmentVariables.Clear(); foreach (var pair in environment) info.EnvironmentVariables[pair.Key] = pair.Value;
            using (var child = Process.Start(info)) { var output = child.StandardOutput.ReadToEndAsync(); var error = child.StandardError.ReadToEndAsync(); if (!child.WaitForExit(60000)) { child.Kill(); child.WaitForExit(); throw new InvalidOperationException("Toolchain discovery timed out."); } child.WaitForExit(); if (child.ExitCode != 0) throw new InvalidDataException("Windows 개발 도구 환경을 준비하지 못했습니다. 담당자에게 확인해 주세요."); return output.Result; }
        }
    }
}
