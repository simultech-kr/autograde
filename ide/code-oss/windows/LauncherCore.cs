// Shared by the Windows GUI and its dependency-free executable checks.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Web.Script.Serialization;

namespace Autograde.WslLauncher
{
    public sealed class LauncherConfig
    {
        public readonly string Distribution;
        public readonly string User;
        public readonly string InstallRoot;

        private LauncherConfig(string distribution, string user, string installRoot)
        { Distribution = distribution; User = user; InstallRoot = installRoot; }

        public static LauncherConfig Parse(string json)
        {
            if (json == null || json.Length > 16384) throw new InvalidDataException("실행 설정의 크기가 올바르지 않습니다.");
            var serializer = new JavaScriptSerializer { MaxJsonLength = 16384, RecursionLimit = 8 };
            var value = serializer.DeserializeObject(json) as Dictionary<string, object>;
            if (value == null || value.Count != 4 || !value.ContainsKey("schema") ||
                !(value["schema"] is int) || (int)value["schema"] != 1)
                throw new InvalidDataException("실행 설정 형식이 올바르지 않습니다. 담당자에게 재설치를 요청해 주세요.");
            string distribution = StringValue(value, "distribution"), user = StringValue(value, "user"), root = StringValue(value, "installRoot");
            if (!Regex.IsMatch(distribution, @"^[\p{L}\p{N}][\p{L}\p{N} ._-]{0,127}\z") || distribution.EndsWith(" ", StringComparison.Ordinal))
                throw new InvalidDataException("WSL 배포판 이름이 올바르지 않습니다.");
            if (!Regex.IsMatch(user, @"^[a-z_][a-z0-9_-]{0,31}\$?\z") || user == "root")
                throw new InvalidDataException("관리자가 아닌 WSL 실습 사용자 계정을 지정해 주세요.");
            if (root.Length < 2 || root.Length > 1024 || !root.StartsWith("/", StringComparison.Ordinal) ||
                root.StartsWith("/mnt/", StringComparison.Ordinal) || root.IndexOf('\\') >= 0 || Regex.IsMatch(root, @"[\x00-\x1f\x7f]"))
                throw new InvalidDataException("IDE 설치 경로는 WSL의 Linux 파일 시스템 안에 있는 절대 경로여야 합니다.");
            foreach (string component in root.Substring(1).Split('/'))
                if (component.Length == 0 || component == "." || component == "..") throw new InvalidDataException("IDE 설치 경로에 빈 부분이나 상대 경로를 사용할 수 없습니다.");
            return new LauncherConfig(distribution, user, root);
        }

        private static string StringValue(Dictionary<string, object> value, string key)
        {
            object text;
            if (!value.TryGetValue(key, out text) || !(text is string)) throw new InvalidDataException("실행 설정의 " + key + " 항목을 확인해 주세요.");
            return (string)text;
        }

        public string[] Arguments(bool check)
        {
            var args = new List<string> { "--distribution", Distribution, "--user", User, "--exec", InstallRoot + "/bin/autograde-ide" };
            if (check) args.Add("--check");
            return args.ToArray();
        }

        public string MutexName()
        {
            using (var sha = SHA256.Create())
            {
                byte[] digest = sha.ComputeHash(Encoding.UTF8.GetBytes(Distribution + "\0" + User));
                return @"Local\AutogradeWSL-" + BitConverter.ToString(digest).Replace("-", "");
            }
        }
    }

    public static class WindowsArguments
    {
        // ProcessStartInfo.Arguments on .NET Framework uses a command line, not
        // ArgumentList. Quote each literal argument using the Windows CRT rule;
        // never pass through cmd.exe, PowerShell, bash, sh -c or an interpolated script.
        public static string Quote(string value)
        {
            if (value == null || value.IndexOf('\0') >= 0) throw new ArgumentException("Invalid argument");
            var result = new StringBuilder("\"");
            int slashes = 0;
            foreach (char item in value)
            {
                if (item == '\\') { slashes++; continue; }
                if (item == '"') result.Append('\\', slashes * 2 + 1).Append('"');
                else result.Append('\\', slashes).Append(item);
                slashes = 0;
            }
            return result.Append('\\', slashes * 2).Append('"').ToString();
        }

        public static string Join(string[] values)
        {
            var result = new List<string>();
            foreach (var value in values) result.Add(Quote(value));
            return String.Join(" ", result.ToArray());
        }
    }

    public sealed class SessionProtocol
    {
        private readonly bool check;
        private readonly string expectedDistribution;
        public bool Started { get; private set; }
        public bool Cleaned { get; private set; }
        public bool Ready { get; private set; }
        public bool Invalid { get; private set; }
        public string Failure { get; private set; }
        public SessionProtocol(bool checkMode) : this(checkMode, null) { }
        public SessionProtocol(bool checkMode, string distribution) { check = checkMode; expectedDistribution = distribution; }

        private static object Field(Dictionary<string, object> item, string name)
        { object value; return item.TryGetValue(name, out value) ? value : null; }

        public void Accept(string line)
        {
            if (String.IsNullOrWhiteSpace(line)) return;
            if (line.Length > 8192) { Invalid = true; return; }
            try
            {
                var serializer = new JavaScriptSerializer { MaxJsonLength = 8192, RecursionLimit = 8 };
                var item = serializer.DeserializeObject(line) as Dictionary<string, object>;
                object eventValue;
                if (item == null || !item.TryGetValue("event", out eventValue) || !(eventValue is string)) { Invalid = true; return; }
                string eventName = (string)eventValue;
                if (eventName == "ready" && check && !Ready)
                {
                    object schema = Field(item, "schema"), uid = Field(item, "uid"), noSandbox = Field(item, "noSandbox");
                    string distribution = Field(item, "distribution") as string;
                    string architecture = Field(item, "architecture") as string;
                    string gui = Field(item, "gui") as string;
                    string sandbox = Field(item, "sandbox") as string;
                    if (!(schema is int) || (int)schema != 1 || !(uid is int) || (int)uid < 1 ||
                        !(noSandbox is bool) || (bool)noSandbox || !Object.Equals(Field(item, "platform"), "wsl2") ||
                        !Object.Equals(Field(item, "lifetime"), "shared") || String.IsNullOrEmpty(distribution) ||
                        (expectedDistribution != null && distribution != expectedDistribution) ||
                        (architecture != "x64" && architecture != "arm64") || (gui != "x11" && gui != "wayland") ||
                        (sandbox != "user-namespace" && sandbox != "setuid-helper")) { Invalid = true; return; }
                    Ready = true; return;
                }
                if (eventName == "ide-started" && !check && !Started && !Cleaned)
                {
                    object pid;
                    if (!item.TryGetValue("pid", out pid) || !(pid is int) || (int)pid < 1) { Invalid = true; return; }
                    Started = true; return;
                }
                if (eventName == "session-cleaned" && !check && Started && !Cleaned) { Cleaned = true; return; }
                if (eventName == "error")
                {
                    object message;
                    if (!item.TryGetValue("message", out message) || !(message is string) || ((string)message).Length == 0) { Invalid = true; return; }
                    string text = Regex.Replace((string)message, @"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "");
                    Failure = text.Length > 600 ? text.Substring(0, 600) : text;
                    return;
                }
                Invalid = true;
            }
            catch { Invalid = true; }
        }

        public string ResultError(int exitCode)
        {
            if (!String.IsNullOrEmpty(Failure)) return Failure;
            if (exitCode != 0)
                return "WSL 실습 환경을 실행하거나 정리하지 못했습니다. WSL2 배포판·실습 사용자·앱 설치 경로와 WSLg 설치 상태를 담당자에게 확인해 주세요.";
            if (Invalid || (check ? !Ready : (!Started || !Cleaned)))
                return "실습의 실행 또는 임시 파일 정리 완료를 확인하지 못했습니다. 다음 학생에게 자리를 넘기기 전에 담당자에게 확인을 요청해 주세요.";
            return null;
        }
    }
}
