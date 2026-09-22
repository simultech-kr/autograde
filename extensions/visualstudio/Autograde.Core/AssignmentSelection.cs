using System;
using System.Linq;
using Newtonsoft.Json.Linq;

namespace Autograde.Core
{
    public static class AssignmentSelection
    {
        public static string LatestSubmissionId(JObject assignment)
        {
            if (assignment == null) throw new InvalidOperationException("과제를 선택하세요.");
            var latest = assignment["latest_submission"];
            // A missing/null latest submission is a normal state for a new assignment.
            if (latest == null || latest.Type == JTokenType.Null) return null;
            return ServiceClient.Required(latest, "submission_id");
        }

        public static JObject Select(JArray items, string requestedId)
        {
            var assignments = items.OfType<JObject>().Where(a => (string)a["delivery_mode"] == "bundle").ToArray();
            if (requestedId == null) return assignments.FirstOrDefault();
            return assignments.FirstOrDefault(a => (string)a["assignment_id"] == requestedId)
                ?? throw new InvalidOperationException("선택한 과제가 더 이상 목록에 없습니다. 다른 과제의 결과로 전환하지 않습니다. 교수자에게 공개 상태를 확인하세요.");
        }
    }
}
