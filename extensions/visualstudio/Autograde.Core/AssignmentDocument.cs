using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;
using Newtonsoft.Json.Linq;

namespace Autograde.Core
{
    public sealed class DocumentMetadata
    {
        public int Revision { get; private set; }
        public string Sha256 { get; private set; }
        public string UpdatedAt { get; private set; }
        public string ChangeNote { get; private set; }

        public static DocumentMetadata Parse(JToken value)
        {
            // Absent metadata is an older server or an unsupported assignment, not an error.
            if (value == null || value.Type == JTokenType.Null) return null;
            var revision = (value as JObject)?["revision"];
            if (revision?.Type != JTokenType.Integer || !int.TryParse(revision.ToString(), out var number) || number < 0)
                throw new InvalidDataException("과제 설명 버전 정보가 올바르지 않습니다.");
            var hash = ServiceClient.Required(value, "sha256");
            var updated = ServiceClient.Required(value, "updated_at");
            var note = value["change_note"];
            if (!Regex.IsMatch(hash, "^sha256:[a-f0-9]{64}$") || updated.Length > 100 ||
                !DateTimeOffset.TryParse(updated, CultureInfo.InvariantCulture, DateTimeStyles.None, out _) ||
                note?.Type != JTokenType.String || ((string)note).Length > 1000)
                throw new InvalidDataException("과제 설명 메타정보가 올바르지 않습니다.");
            // The server permits 500 Unicode characters, including supplementary characters.
            // Reject malformed surrogate sequences before counting pairs as one character.
            try { new UTF8Encoding(false, true).GetByteCount((string)note); }
            catch (EncoderFallbackException) { throw new InvalidDataException("과제 설명 변경 안내 문자 인코딩 오류입니다."); }
            if (((string)note).Length - ((string)note).Count(char.IsHighSurrogate) > 500)
                throw new InvalidDataException("과제 설명 변경 안내가 500자를 초과했습니다.");
            return new DocumentMetadata { Revision = number, Sha256 = hash, UpdatedAt = updated, ChangeNote = (string)note };
        }

        public JObject ToJson() => new JObject { ["revision"] = Revision, ["sha256"] = Sha256,
            ["updated_at"] = UpdatedAt, ["change_note"] = ChangeNote };
    }

    public sealed class AssignmentDocument
    {
        public string AssignmentId { get; private set; }
        public string Content { get; private set; }
        public DocumentMetadata Metadata { get; private set; }
        public IReadOnlyList<DocumentMetadata> History { get; private set; }

        public static AssignmentDocument Parse(JObject response, string expectedAssignment)
        {
            var value = response?["document"];
            if (value == null || value.Type == JTokenType.Null) return null;
            var id = ServiceClient.Required(value, "assignment_id");
            var metadata = DocumentMetadata.Parse(value);
            var content = value["content"];
            // .NET strings use UTF-16: 20,000 Unicode characters can occupy 40,000 code units.
            if (id != expectedAssignment || content?.Type != JTokenType.String || ((string)content).Length > 40000 ||
                !(value["history"] is JArray history) || history.Count > 50)
                throw new InvalidDataException("선택 과제의 설명 응답이 올바르지 않습니다.");
            byte[] bytes;
            try { bytes = new UTF8Encoding(false, true).GetBytes((string)content); }
            catch (EncoderFallbackException) { throw new InvalidDataException("과제 설명 문자 인코딩 오류입니다."); }
            if (((string)content).Length - ((string)content).Count(char.IsHighSurrogate) > 20000)
                throw new InvalidDataException("과제 설명이 허용 길이를 초과했습니다.");
            if (Bundle.Digest(bytes) != metadata.Sha256)
                throw new InvalidDataException("과제 설명 SHA-256이 일치하지 않습니다. 과제 새로고침 후 다시 여세요.");
            var versions = history.Select(DocumentMetadata.Parse).ToArray();
            if (versions.Any(item => item == null || item.Revision > metadata.Revision) ||
                versions.Select(item => item.Revision).Distinct().Count() != versions.Length)
                throw new InvalidDataException("과제 설명 변경 이력이 올바르지 않습니다.");
            return new AssignmentDocument { AssignmentId = id, Content = (string)content, Metadata = metadata, History = versions };
        }
    }

    // Read markers are scoped to this login only and never written into the student's workspace.
    // The generation also rejects A -> B -> A and logout/relogin response races.
    public sealed class DocumentSelectionState
    {
        readonly Dictionary<string, string> read = new Dictionary<string, string>(StringComparer.Ordinal);
        public long Generation { get; private set; }
        public string AssignmentId { get; private set; }
        public void Select(string assignmentId) { AssignmentId = assignmentId; Generation++; }
        public void Reset() { read.Clear(); Select(null); }
        public bool IsCurrent(long generation, string assignmentId) => generation == Generation &&
            assignmentId != null && assignmentId == AssignmentId;
        static string Key(DocumentMetadata metadata) => metadata.Revision + ":" + metadata.Sha256;
        public string Label(DocumentMetadata metadata)
        {
            if (AssignmentId == null || metadata == null) return "";
            bool unseen = metadata.Revision > 0 && (!read.TryGetValue(AssignmentId, out var previous) || previous != Key(metadata));
            return "설명 v" + metadata.Revision + (unseen ? " · 새 설명 있음" : "");
        }
        public bool MarkRead(long generation, AssignmentDocument document)
        {
            if (document == null || !IsCurrent(generation, document.AssignmentId)) return false;
            read[document.AssignmentId] = Key(document.Metadata);
            return true;
        }
    }
}
