using System;
using System.IO;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace Autograde.Core
{
    // Public workspace identity only. Authentication is never restored from disk.
    public sealed class WorkspaceBinding
    {
        public string Root { get; private set; }
        public string Service { get; private set; }
        public string AssignmentId { get; private set; }

        public static WorkspaceBinding Read(string root)
        {
            root = Path.GetFullPath(root);
            Bundle.CheckDirectory(root);
            var directory = Path.Combine(root, ".autograde");
            Bundle.CheckDirectory(directory);
            var path = Path.Combine(directory, "assignment.json");
            if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0 || new FileInfo(path).Length > 8192)
                throw new InvalidDataException("과제 연결 파일 오류. 처음 받은 과제 폴더를 선택하세요.");
            try
            {
                using (var reader = new JsonTextReader(new StringReader(File.ReadAllText(path))) { MaxDepth = 8 })
                {
                    var marker = JObject.Load(reader, new JsonLoadSettings { DuplicatePropertyNameHandling = DuplicatePropertyNameHandling.Error });
                    if (reader.Read() || marker["schemaVersion"]?.Type != JTokenType.Integer || (int?)marker["schemaVersion"] != 1)
                        throw new InvalidDataException("지원하지 않는 과제 연결 파일입니다.");
                    var service = ServiceClient.NormalizeAddress(ServiceClient.Required(marker, "serviceBaseUrl"));
                    var id = ServiceClient.Required(marker, "assignmentId");
                    if (id.Length > 256) throw new InvalidDataException("과제 연결 파일의 과제 번호가 올바르지 않습니다.");
                    return new WorkspaceBinding { Root = root, Service = service, AssignmentId = id };
                }
            }
            catch (JsonException) { throw new InvalidDataException("과제 연결 파일이 손상되었습니다. 원래 과제 폴더를 선택하세요."); }
        }

        public static string FindRoot(string directory)
        {
            if (string.IsNullOrWhiteSpace(directory)) return null;
            Bundle.CheckDirectory(directory);
            for (var current = new DirectoryInfo(Path.GetFullPath(directory)); current != null; current = current.Parent)
                if (Directory.Exists(Path.Combine(current.FullName, ".autograde"))) return current.FullName;
            return null;
        }

        public static WorkspaceBinding FromOpenDirectory(string directory)
        {
            var root = FindRoot(directory);
            return root == null ? null : Read(root);
        }

        public static bool SamePath(string left, string right) => !string.IsNullOrWhiteSpace(left) && !string.IsNullOrWhiteSpace(right) &&
            string.Equals(Path.GetFullPath(left).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar),
                Path.GetFullPath(right).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar),
                System.Runtime.InteropServices.RuntimeInformation.IsOSPlatform(System.Runtime.InteropServices.OSPlatform.Windows)
                    ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal);

        public static string DownloadParent(string chosenDirectory)
        {
            Bundle.CheckDirectory(chosenDirectory);
            // Choosing an old assignment (or its source subfolder) creates a sibling.
            var existing = FindRoot(chosenDirectory);
            if (existing != null)
                for (var parent = Directory.GetParent(existing); parent != null; parent = parent.Parent)
                    if (Directory.Exists(Path.Combine(parent.FullName, ".autograde"))) existing = parent.FullName;
            return existing == null ? Path.GetFullPath(chosenDirectory) :
                Path.GetDirectoryName(existing) ?? throw new InvalidDataException("과제 저장용 상위 폴더를 선택하세요.");
        }

        public static void VerifyActive(string root, string openedDirectory, string service, string assignmentId)
        {
            Bundle.VerifyWorkspace(root, service, assignmentId);
            var opened = FromOpenDirectory(openedDirectory);
            if (opened == null || !SamePath(opened.Root, root))
                throw new InvalidOperationException("현재 IDE에서 연 과제와 제출 폴더가 다릅니다. 제출할 과제를 선택하고 ‘과제 폴더 열기’로 연 뒤 다시 제출하세요.");
        }
    }
}
