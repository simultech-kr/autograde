using System;
using System.Collections.Generic;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using Microsoft.Win32.SafeHandles;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace Autograde.Core
{
    public sealed class SubmissionSnapshot
    {
        public byte[] Archive { get; internal set; }
        public string[] Files { get; internal set; }
        public long SourceBytes { get; internal set; }
    }

    // Deliberately bounded, regular-file-only implementation of autograde.bundle.v1.
    public static class Bundle
    {
        public const int MaxArchive = 25 * 1024 * 1024;
        const int MaxExpanded = 100 * 1024 * 1024, MaxFiles = 5000;
        const string Manifest = "AUTOGRADE-BUNDLE.json";
        static readonly UTF8Encoding Utf8 = new UTF8Encoding(false, true);
        static readonly HashSet<string> Ignored = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        { ".git", ".vs", ".vscode", ".autograde", "build", "out", "bin", "obj", "debug", "release", "x64", "x86", "arm64", "ipch", ".venv", "node_modules" };
        static readonly HashSet<string> SourceExtensions = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        { ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".inl", ".rc", ".txt", ".md", ".cmake" };
        sealed class Entry
        {
            public string Path;
            public byte[] Content;
            public bool Directory, Executable;
        }
        static readonly IComparer<string> Utf8Order = Comparer<string>.Create((a, b) =>
        {
            var left = Utf8.GetBytes(a); var right = Utf8.GetBytes(b);
            for (int i = 0; i < Math.Min(left.Length, right.Length); i++)
                if (left[i] != right[i]) return left[i].CompareTo(right[i]);
            return left.Length.CompareTo(right.Length);
        });

        public static string Digest(byte[] bytes)
        { using (var hash = SHA256.Create()) return "sha256:" + BitConverter.ToString(hash.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant(); }

        public static void ValidatePath(string path)
        {
            if (string.IsNullOrEmpty(path) || Utf8.GetByteCount(path) > 4096 || path.Contains('\\') ||
                path.IndexOfAny(new[] { '\u0085', '\u2028', '\u2029' }) >= 0 || path != path.Normalize(NormalizationForm.FormC))
                throw new InvalidDataException("과제 파일 경로가 안전하지 않습니다.");
            foreach (var part in path.Split('/'))
                if (part == "" || part == "." || part == ".." || part.EndsWith(".") || part.EndsWith(" ") ||
                    Utf8.GetByteCount(part) > 255 || Regex.IsMatch(part, "[<>:\"|?*\\x00-\\x1f\\x7f]") ||
                    Regex.IsMatch(part, @"^(con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])($|\.)", RegexOptions.IgnoreCase))
                    throw new InvalidDataException("Windows에서 안전하지 않은 과제 파일 경로입니다.");
        }

        public static void CheckDirectory(string path)
        {
            var directory = new DirectoryInfo(System.IO.Path.GetFullPath(path));
            for (var current = directory; current != null; current = current.Parent)
                if (!current.Exists || (current.Attributes & FileAttributes.ReparsePoint) != 0)
                    throw new IOException("실제 로컬 폴더를 선택하세요. 링크 또는 junction은 사용할 수 없습니다.");
        }

        static void ValidateEntries(List<Entry> entries)
        {
            if (entries.Count > MaxFiles + 1) throw new InvalidDataException("파일이 너무 많습니다.");
            var paths = new Dictionary<string, Entry>(StringComparer.OrdinalIgnoreCase);
            foreach (var e in entries)
            {
                ValidatePath(e.Path);
                if (paths.ContainsKey(e.Path)) throw new InvalidDataException("중복 또는 대소문자 충돌 경로입니다.");
                paths.Add(e.Path, e);
            }
            var parents = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (var e in entries)
            {
                string parent = e.Path;
                while (parent.LastIndexOf('/') >= 0)
                {
                    parent = parent.Substring(0, parent.LastIndexOf('/'));
                    if (paths.TryGetValue(parent, out var found) && (!found.Directory || found.Path != parent))
                        throw new InvalidDataException("파일/폴더 경로 충돌입니다.");
                    if (parents.TryGetValue(parent, out var spelling) && spelling != parent)
                        throw new InvalidDataException("폴더 대소문자 충돌입니다.");
                    parents[parent] = parent;
                }
            }
        }

        public static SubmissionSnapshot CreateSubmission(string root, CancellationToken cancel = default)
        {
            cancel.ThrowIfCancellationRequested();
            CheckDirectory(root);
            var entries = new List<Entry>();
            long total = 0; int visited = 0;
            Action<string, string> collect = null;
            collect = (directory, relative) =>
            {
                CheckDirectory(directory);
                // Sort only the bounded, selected entries later, not an unbounded directory listing.
                foreach (var path in Directory.EnumerateFileSystemEntries(directory))
                {
                    cancel.ThrowIfCancellationRequested();
                    string name = System.IO.Path.GetFileName(path);
                    if (Ignored.Contains(name) || name.StartsWith(".", StringComparison.Ordinal)) continue;
                    if (++visited > MaxFiles || relative.Count(c => c == '/') > 32) throw new InvalidDataException("제출 폴더의 항목 또는 깊이 제한 초과");
                    var attributes = File.GetAttributes(path);
                    if ((attributes & FileAttributes.ReparsePoint) != 0) throw new IOException("링크 파일은 제출할 수 없습니다.");
                    string next = relative == "" ? name : relative + "/" + name;
                    ValidatePath(next);
                    if ((attributes & FileAttributes.Directory) != 0) { collect(path, next); continue; }
                    if (!SourceExtensions.Contains(System.IO.Path.GetExtension(name))) continue;
                    if (name.Equals(Manifest, StringComparison.OrdinalIgnoreCase)) continue;
                    using (var input = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
                    {
                        if (RuntimeInformation.IsOSPlatform(OSPlatform.Windows) &&
                            (!GetFileInformationByHandle(input.SafeFileHandle, out var info) || info.NumberOfLinks != 1))
                            throw new IOException("파일 링크 상태를 확인할 수 없습니다.");
                        if (input.Length > MaxExpanded || total + input.Length > MaxExpanded || entries.Count >= MaxFiles)
                            throw new InvalidDataException("제출은 5000개 파일, 100 MiB 이하여야 합니다.");
                        var content = new byte[(int)input.Length];
                        ReadExactly(input, content, cancel);
                        if (input.ReadByte() != -1) throw new IOException("제출 중 파일이 변경되었습니다. 저장 후 다시 제출하세요.");
                        total += content.Length;
                        entries.Add(new Entry { Path = next, Content = content });
                    }
                }
            };
            collect(System.IO.Path.GetFullPath(root), "");
            if (!entries.Any(e => Regex.IsMatch(e.Path, @"\.(c|cc|cpp|cxx)$", RegexOptions.IgnoreCase)))
                throw new InvalidDataException("제출 폴더에 C/C++ 소스가 없습니다.");
            entries.Sort((a, b) => Utf8Order.Compare(a.Path, b.Path));
            ValidateEntries(entries);
            var files = entries.Select(e => e.Path).ToArray();
            var manifest = new JObject {
                ["schema"] = "autograde.bundle.v1", ["kind"] = "submission", ["directories"] = new JArray(),
                ["files"] = new JArray(entries.Select(e => new JObject { ["path"] = e.Path, ["size"] = e.Content.Length,
                    ["sha256"] = Digest(e.Content), ["executable"] = false })),
                ["totals"] = new JObject { ["file_count"] = entries.Count, ["expanded_bytes"] = total } };
            var encoded = Utf8.GetBytes(Canonical(manifest).ToString(Formatting.None) + "\n");
            if (total + encoded.Length > MaxExpanded) throw new InvalidDataException("제출 크기 제한 초과");
            entries.Add(new Entry { Path = Manifest, Content = encoded });
            using (var output = new MemoryStream())
            {
                using (var gzip = new GZipStream(output, CompressionLevel.Optimal, true))
                {
                    foreach (var entry in entries) { cancel.ThrowIfCancellationRequested(); WriteEntry(gzip, entry.Path, entry.Content); }
                    gzip.Write(new byte[1024], 0, 1024);
                }
                var bytes = output.ToArray();
                if (bytes.Length > MaxArchive) throw new InvalidDataException("압축 제출 크기는 25 MiB 이하여야 합니다.");
                cancel.ThrowIfCancellationRequested();
                return new SubmissionSnapshot { Archive = bytes, Files = files, SourceBytes = total };
            }
        }

        public static void ExtractStarter(byte[] archive, string target, string service, string assignment, CancellationToken cancel = default)
            => Extract(archive, target, service, assignment, "starter", cancel);

        public static void ExtractSubmission(byte[] archive, string target, string service, string assignment, CancellationToken cancel = default)
            => Extract(archive, target, service, assignment, "submission", cancel);

        static void Extract(byte[] archive, string target, string service, string assignment, string expectedKind, CancellationToken cancel)
        {
            cancel.ThrowIfCancellationRequested();
            var entries = ReadArchive(archive, cancel);
            ValidateEntries(entries);
            var manifest = entries.SingleOrDefault(e => e.Path == Manifest && !e.Directory);
            if (manifest == null || manifest.Content.Length > 16 * 1024 * 1024 || manifest.Executable)
                throw new InvalidDataException("과제 manifest가 없습니다.");
            JObject doc;
            using (var reader = new JsonTextReader(new StringReader(Utf8.GetString(manifest.Content))) { MaxDepth = 32 })
                doc = JObject.Load(reader, new JsonLoadSettings { DuplicatePropertyNameHandling = DuplicatePropertyNameHandling.Error });
            if ((string)doc["schema"] != "autograde.bundle.v1" || (string)doc["kind"] != expectedKind)
                throw new InvalidDataException("요청한 종류의 bundle이 아닙니다.");
            var actual = entries.Where(e => e.Path != Manifest).ToDictionary(e => e.Path, StringComparer.Ordinal);
            var declared = new HashSet<string>(StringComparer.Ordinal);
            long total = 0; int fileCount = 0;
            foreach (var file in doc["files"] as JArray ?? throw new InvalidDataException("manifest files 오류"))
            {
                var path = ServiceClient.Required(file, "path");
                if (!declared.Add(path) || !actual.TryGetValue(path, out var entry) || entry.Directory ||
                    (long?)file["size"] != entry.Content.Length || (string)file["sha256"] != Digest(entry.Content) ||
                    file["executable"]?.Type != JTokenType.Boolean || (bool)file["executable"] != entry.Executable)
                    throw new InvalidDataException("과제 manifest 파일 검증 실패");
                total += entry.Content.Length; fileCount++;
            }
            foreach (var dir in doc["directories"] as JArray ?? throw new InvalidDataException("manifest directories 오류"))
            {
                if (dir.Type != JTokenType.String || !declared.Add((string)dir) || !actual.TryGetValue((string)dir, out var entry) || !entry.Directory)
                    throw new InvalidDataException("과제 manifest 폴더 검증 실패");
            }
            if (declared.Count != actual.Count || total != (long?)doc["totals"]?["expanded_bytes"] || fileCount != (int?)doc["totals"]?["file_count"])
                throw new InvalidDataException("과제 manifest 합계 검증 실패");
            if (actual.Keys.Any(p => p.Split('/').Any(c => c.Equals(".autograde", StringComparison.OrdinalIgnoreCase))))
                throw new InvalidDataException("예약된 과제 메타데이터 경로입니다.");
            target = System.IO.Path.GetFullPath(target);
            if (Directory.Exists(target) || File.Exists(target)) throw new IOException("이미 존재하는 폴더는 덮어쓰지 않습니다.");
            var parent = System.IO.Path.GetDirectoryName(target);
            CheckDirectory(parent);
            var stage = System.IO.Path.Combine(parent, ".autograde-download-" + Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(stage);
            try
            {
                foreach (var entry in actual.Values)
                {
                    cancel.ThrowIfCancellationRequested();
                    string path = System.IO.Path.Combine(stage, entry.Path.Replace('/', System.IO.Path.DirectorySeparatorChar));
                    Directory.CreateDirectory(entry.Directory ? path : System.IO.Path.GetDirectoryName(path));
                    CheckDirectory(entry.Directory ? path : System.IO.Path.GetDirectoryName(path));
                    if (!entry.Directory)
                        using (var file = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                            file.Write(entry.Content, 0, entry.Content.Length);
                }
                Directory.CreateDirectory(System.IO.Path.Combine(stage, ".autograde"));
                File.WriteAllText(System.IO.Path.Combine(stage, ".autograde", "assignment.json"), new JObject {
                    ["schemaVersion"] = 1, ["serviceBaseUrl"] = service, ["assignmentId"] = assignment }.ToString(), Utf8);
                CheckDirectory(parent);
                cancel.ThrowIfCancellationRequested();
                Directory.Move(stage, target);
            }
            finally { if (Directory.Exists(stage)) Directory.Delete(stage, true); }
        }

        public static void VerifyWorkspace(string root, string service, string assignment)
        {
            CheckDirectory(root);
            var directory = System.IO.Path.Combine(root, ".autograde");
            CheckDirectory(directory);
            var path = System.IO.Path.Combine(directory, "assignment.json");
            if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0 || new FileInfo(path).Length > 8192)
                throw new InvalidDataException("과제 연결 파일 오류");
            var marker = JObject.Parse(File.ReadAllText(path));
            if ((int?)marker["schemaVersion"] != 1 || (string)marker["serviceBaseUrl"] != service || (string)marker["assignmentId"] != assignment)
                throw new InvalidDataException("선택한 폴더의 서버 또는 과제가 다릅니다.");
        }

        static JToken Canonical(JToken value)
        {
            if (value is JObject obj) return new JObject(obj.Properties().OrderBy(p => p.Name, StringComparer.Ordinal).Select(p => new JProperty(p.Name, Canonical(p.Value))));
            if (value is JArray array) return new JArray(array.Select(Canonical));
            return value.DeepClone();
        }

        static List<Entry> ReadArchive(byte[] archive, CancellationToken cancel)
        {
            if (archive.Length == 0 || archive.Length > MaxArchive) throw new InvalidDataException("압축 파일 크기 제한 초과");
            using (var gzip = new GZipStream(new MemoryStream(archive), CompressionMode.Decompress))
            using (var output = new MemoryStream())
            {
                var buffer = new byte[8192]; int count;
                while ((count = gzip.Read(buffer, 0, buffer.Length)) != 0)
                {
                    cancel.ThrowIfCancellationRequested();
                    if (output.Length + count > MaxExpanded + 8 * 1024 * 1024) throw new InvalidDataException("압축 해제 크기 제한 초과");
                    output.Write(buffer, 0, count);
                }
                output.Position = 0;
                var entries = new List<Entry>(); string pax = null; long expanded = 0;
                while (output.Position + 512 <= output.Length)
                {
                    cancel.ThrowIfCancellationRequested();
                    var header = new byte[512]; ReadExactly(output, header);
                    if (header.All(b => b == 0))
                    {
                        if (pax != null || output.Length - output.Position < 512) throw new InvalidDataException("tar 종료 오류");
                        while (output.Position < output.Length) if (output.ReadByte() != 0) throw new InvalidDataException("tar 후행 데이터 오류");
                        return entries;
                    }
                    long checksum = Octal(header, 148, 8);
                    long sum = header.Select((b, i) => i >= 148 && i < 156 ? 32L : b).Sum();
                    if (checksum != sum || Text(header, 257, 5) != "ustar") throw new InvalidDataException("tar header 오류");
                    long size = Octal(header, 124, 12);
                    if (size > MaxExpanded || size < 0 || output.Position + ((size + 511) / 512) * 512 > output.Length)
                        throw new InvalidDataException("tar 크기 오류");
                    var content = new byte[(int)size]; ReadExactly(output, content);
                    output.Position += (512 - size % 512) % 512;
                    char type = (char)header[156];
                    if (type == 'x')
                    {
                        if (pax != null || size > 8192) throw new InvalidDataException("PAX header 오류");
                        pax = ReadPax(content); continue;
                    }
                    if (type != '\0' && type != '0' && type != '5') throw new InvalidDataException("링크 또는 지원하지 않는 tar 항목입니다.");
                    var path = Text(header, 0, 100);
                    var prefix = Text(header, 345, 155);
                    if (prefix != "") path = prefix + "/" + path;
                    path = pax ?? path; pax = null;
                    bool directory = type == '5';
                    if (directory && path.EndsWith("/")) path = path.Substring(0, path.Length - 1);
                    if (directory && size != 0) throw new InvalidDataException("tar 폴더 크기 오류");
                    expanded += size;
                    if (expanded > MaxExpanded || entries.Count >= MaxFiles + 1) throw new InvalidDataException("bundle 제한 초과");
                    entries.Add(new Entry { Path = path, Directory = directory, Content = content, Executable = (Octal(header, 100, 8) & 73) != 0 });
                }
                throw new InvalidDataException("잘린 tar 파일입니다.");
            }
        }

        static string ReadPax(byte[] bytes)
        {
            string path = null; int offset = 0;
            while (offset < bytes.Length)
            {
                int space = Array.IndexOf(bytes, (byte)' ', offset);
                if (space < 0 || !int.TryParse(Encoding.ASCII.GetString(bytes, offset, space - offset), out int length) ||
                    length <= space - offset + 2 || length > bytes.Length - offset || bytes[offset + length - 1] != 10)
                    throw new InvalidDataException("PAX 레코드 오류");
                var field = Utf8.GetString(bytes, space + 1, offset + length - space - 2);
                if (!field.StartsWith("path=", StringComparison.Ordinal) || path != null) throw new InvalidDataException("지원하지 않는 PAX 필드입니다.");
                path = field.Substring(5); offset += length;
            }
            return path ?? throw new InvalidDataException("PAX 경로 누락");
        }
        static string Text(byte[] bytes, int start, int length)
        { int end = start; while (end < start + length && bytes[end] != 0) end++; return Utf8.GetString(bytes, start, end - start); }
        static long Octal(byte[] bytes, int start, int length)
        {
            var value = Text(bytes, start, length).Trim();
            if (value == "") return 0;
            if (!Regex.IsMatch(value, "^[0-7]+$")) throw new InvalidDataException("tar 숫자 오류");
            return Convert.ToInt64(value, 8);
        }
        static void ReadExactly(Stream stream, byte[] buffer, CancellationToken cancel = default)
        {
            int offset = 0;
            while (offset < buffer.Length)
            {
                cancel.ThrowIfCancellationRequested();
                int count = stream.Read(buffer, offset, Math.Min(65536, buffer.Length - offset));
                if (count == 0) throw new EndOfStreamException();
                offset += count;
            }
        }
        static void Put(byte[] header, int offset, string value) { var bytes = Encoding.ASCII.GetBytes(value); Array.Copy(bytes, 0, header, offset, bytes.Length); }
        static void WriteEntry(Stream output, string path, byte[] content)
        {
            if (Utf8.GetByteCount(path) > 100 || path.Any(c => c > 127))
            {
                string body = " path=" + path + "\n"; int length = Utf8.GetByteCount(body) + 1;
                while (length != Utf8.GetByteCount(body) + length.ToString().Length) length = Utf8.GetByteCount(body) + length.ToString().Length;
                WriteRaw(output, "PaxHeader", Utf8.GetBytes(length + body), 'x'); path = "PaxFile";
            }
            WriteRaw(output, path, content, '0');
        }
        static void WriteRaw(Stream output, string path, byte[] content, char type)
        {
            var header = new byte[512]; Put(header, 0, path); Put(header, 100, "0000644\0"); Put(header, 108, "0000000\0"); Put(header, 116, "0000000\0");
            Put(header, 124, Convert.ToString(content.Length, 8).PadLeft(11, '0') + "\0"); Put(header, 136, "00000000000\0");
            Put(header, 148, "        "); header[156] = (byte)type; Put(header, 257, "ustar\0"); Put(header, 263, "00");
            Put(header, 148, Convert.ToString(header.Sum(b => (int)b), 8).PadLeft(6, '0') + "\0 ");
            output.Write(header, 0, 512); output.Write(content, 0, content.Length);
            var padding = new byte[(512 - content.Length % 512) % 512]; output.Write(padding, 0, padding.Length);
        }

        [StructLayout(LayoutKind.Sequential)]
        struct FileInformation
        {
            public uint Attributes; public System.Runtime.InteropServices.ComTypes.FILETIME Creation, Access, Write;
            public uint Volume, SizeHigh, SizeLow, NumberOfLinks, IndexHigh, IndexLow;
        }
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        static extern bool GetFileInformationByHandle(SafeFileHandle handle, out FileInformation information);
    }
}
