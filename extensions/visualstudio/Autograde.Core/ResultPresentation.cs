using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text;
using Newtonsoft.Json.Linq;

namespace Autograde.Core
{
    public sealed class CriterionPresentation
    {
        public string Title { get; set; }
        public string Status { get; set; }
        public string Score { get; set; }
        public string Feedback { get; set; }
        public string Hint { get; set; }
        public string SourceLocation { get; set; }
        public bool NeedsWork { get; set; }
        public bool IsBlocked { get; set; }
        public bool RequiresReview { get; set; }
        public bool IsPassed { get; set; }
        public bool IsExpanded => NeedsWork || IsBlocked || RequiresReview;
    }

    // Display policy, not a new grading policy: no invented pass threshold.
    public sealed class ResultPresentation
    {
        public string Headline { get; private set; }
        public string Score { get; private set; } = "점수 미공개";
        public string Summary { get; private set; }
        public string NextStep { get; private set; }
        public string Details { get; private set; }
        public string PreviousBest { get; private set; } = "이전 최고점 정보 없음";
        public double? Percent { get; private set; }
        public IReadOnlyList<CriterionPresentation> Criteria { get; private set; } = new List<CriterionPresentation>();
        public IReadOnlyList<string> DiagnosticPreview { get; private set; } = new List<string>();

        static decimal? Number(JToken token)
        {
            if (token == null || (token.Type != JTokenType.Integer && token.Type != JTokenType.Float)) return null;
            try { return token.Value<decimal>(); }
            catch (Exception ex) when (ex is OverflowException || ex is FormatException || ex is InvalidCastException) { return null; }
        }
        static bool Valid(decimal? score, decimal? max) => score.HasValue && max.HasValue && max > 0 && score >= 0 && score <= max;
        static string Format(decimal value) => value.ToString("0.##", CultureInfo.InvariantCulture);
        static string TextValue(JToken token) => token?.Type == JTokenType.String && !string.IsNullOrWhiteSpace((string)token) ? (string)token : null;
        static int? PositiveInteger(JToken token)
        {
            if (token?.Type != JTokenType.Integer) return null;
            try { var value = token.Value<int>(); return value > 0 && value <= 10000000 ? (int?)value : null; }
            catch (Exception ex) when (ex is OverflowException || ex is FormatException || ex is InvalidCastException) { return null; }
        }
        static bool UnsafePathCharacters(string value)
        {
            int characters = 0;
            for (int index = 0; index < value.Length; index++)
            {
                if (++characters > 512) return true;
                switch (CharUnicodeInfo.GetUnicodeCategory(value, index))
                {
                    case UnicodeCategory.Control:
                    case UnicodeCategory.Format:
                    case UnicodeCategory.Surrogate:
                    case UnicodeCategory.PrivateUse:
                    case UnicodeCategory.OtherNotAssigned:
                        return true;
                }
                if (char.IsHighSurrogate(value[index]) && index + 1 < value.Length && char.IsLowSurrogate(value[index + 1])) index++;
            }
            return false;
        }
        static string Location(JObject value)
        {
            var path = TextValue(value["path"]);
            // Locations describe submitted files, never server paths or navigable URLs.
            if (path == null || path != path.Trim() || path.StartsWith("/", StringComparison.Ordinal) || path.Contains("\\") || path.Contains(":") ||
                UnsafePathCharacters(path) || path.Split('/').Any(part => part.Length == 0 || part == "." || part == "..")) return null;
            var line = PositiveInteger(value["line"]); var column = PositiveInteger(value["column"]);
            return path + (line.HasValue ? ":" + line.Value.ToString(CultureInfo.InvariantCulture) +
                (column.HasValue ? ":" + column.Value.ToString(CultureInfo.InvariantCulture) : "") : "");
        }
        static CriterionPresentation Criterion(string key, JObject value)
        {
            var earned = Number(value["score"]); var possible = Number(value["max_score"]);
            bool comparable = Valid(earned, possible);
            var status = comparable ? (earned == possible ? "passed" : earned == 0 ? "failed" : "partial") : null;
            var explicitStatus = TextValue(value["status"]);
            bool requiresReview = false;
            if (value["status"] != null && value["status"].Type != JTokenType.Null && explicitStatus == null)
            {
                status = null; requiresReview = true;
            }
            if (explicitStatus != null)
            {
                switch (explicitStatus)
                {
                    case "passed":
                        // Informational stages may omit both score fields. When a
                        // score is supplied, a success label must agree with it.
                        bool unscored = value.Property("score") == null && value.Property("max_score") == null;
                        requiresReview = !unscored && (!comparable || earned != possible);
                        if (unscored) status = "passed";
                        else if (!comparable) status = null;
                        break;
                    case "partial":
                    case "failed":
                        status = explicitStatus;
                        requiresReview = comparable && earned == possible;
                        break;
                    case "blocked":
                        status = explicitStatus;
                        requiresReview = comparable && earned > 0;
                        break;
                    default:
                        status = null; requiresReview = true;
                        break;
                }
            }
            bool needsWork = status == "failed" || status == "partial", blocked = status == "blocked";
            return new CriterionPresentation
            {
                Title = TextValue(value["title"]) ?? Title(key),
                Status = blocked ? "선행 단계 해결 후 재검사" : needsWork ? (status == "partial" ? "일부 충족 · 수정 필요" : "수정 필요") :
                    status == "passed" ? "충족" : requiresReview ? "판정 확인 필요" : "참고 · 판정 없음",
                Score = comparable ? Format(earned.Value) + " / " + Format(possible.Value) : "배점 정보 없음",
                Feedback = TextValue(value["feedback"]) ?? "공개된 상세 피드백이 없습니다.",
                Hint = TextValue(value["hint"]), SourceLocation = Location(value), NeedsWork = needsWork,
                IsBlocked = blocked, RequiresReview = requiresReview, IsPassed = status == "passed"
            };
        }
        static string Title(string key)
        {
            switch (key)
            {
                case "compile": return "컴파일";
                case "execution": return "프로그램 실행";
                case "output": return "출력 결과";
                case "correctness": return "정확성";
                default: return key.StartsWith("case_", StringComparison.Ordinal) ? "테스트 " + key.Substring(5) : key;
            }
        }
        public static ResultPresentation From(JObject result)
        {
            var model = new ResultPresentation();
            if (result.Property("previous_best") != null) model.PreviousBest = "이전 공개 점수 없음";
            if (result["previous_best"] is JObject best && Valid(Number(best["score"]), Number(best["max_score"])))
                model.PreviousBest = "이전 최고점: " + Format(Number(best["score"]).Value) + " / " + Format(Number(best["max_score"]).Value) +
                    "점 · " + (string)best["received_at"] + "\n접수번호: " + (string)best["submission_id"];
            var state = (string)result["state"];
            model.Headline = GradingPoller.Label(state);
            model.Summary = "아직 완성 여부를 판단할 수 없습니다.";
            model.NextStep = state == "graded" ? "교수자가 결과를 공개한 뒤 다시 확인하세요. 다시 제출할 필요는 없습니다." :
                GradingPoller.IsFinished(state) ? "접수번호와 함께 교수자에게 문의하세요. 코드 오류인지 채점 환경 오류인지 먼저 확인하세요." :
                "접수된 제출은 서버에서 처리됩니다. 잠시 뒤 최신 결과를 확인하세요.";
            var details = new StringBuilder("접수번호: " + (string)result["submission_id"] + "\n상태: " + state + "\n");
            if (state == "published")
            {
                var score = Number(result["score"]); var max = Number(result["max_score"]);
                bool valid = Valid(score, max);
                if (valid) { model.Score = Format(score.Value) + " / " + Format(max.Value) + "점"; model.Percent = (double)(score.Value / max.Value * 100); }
                else model.Score = "점수 정보 확인 필요";
                var criteria = new List<CriterionPresentation>();
                if (result["rubric"] is JObject rubric)
                    foreach (var item in rubric.Properties())
                    {
                        if (!(item.Value is JObject value)) continue;
                        criteria.Add(Criterion(item.Name, value));
                    }
                model.Criteria = criteria.OrderByDescending(item => item.NeedsWork).ThenByDescending(item => item.IsBlocked)
                    .ThenByDescending(item => item.RequiresReview).ToList();
                bool inconsistent = criteria.Any(item => item.RequiresReview) || valid && score == max && criteria.Any(item => item.NeedsWork || item.IsBlocked);
                if (valid && !inconsistent)
                {
                    model.Headline = score == max ? "자동채점 총점 기준 충족" : "수정이 필요합니다";
                    model.NextStep = score == max ? "자동채점은 만점입니다. 보고서·설계 설명 등 별도 요구사항과 마감 전에 제출한 코드가 최종본인지 확인하세요." :
                        "수정 필요 항목과 공개된 피드백을 확인하고 코드 수정 → 모두 저장 → 다시 제출하세요. 상세 피드백이 없다면 교수자에게 문의하세요.";
                }
                else
                {
                    model.Headline = "결과 확인이 필요합니다";
                    model.NextStep = "점수 또는 항목별 결과가 완성 여부를 판단하기에 충분하지 않습니다. 교수자에게 확인하세요.";
                }
                var scored = criteria.Count(item => item.IsPassed || item.NeedsWork || item.IsBlocked || item.RequiresReview);
                model.Summary = scored > 0 ? $"공개된 채점 항목 {scored}개 · {criteria.Count(item => item.IsPassed)}개 충족 · {criteria.Count(item => item.NeedsWork)}개 수정 필요" +
                    (criteria.Any(item => item.IsBlocked) ? $" · {criteria.Count(item => item.IsBlocked)}개 선행 단계 대기" : "") +
                    (criteria.Any(item => item.RequiresReview) ? " · 항목 판정 확인 필요" : "") :
                    "항목별 판정은 제공되지 않았습니다. 총점만으로 표시하며 교수자의 최종 평가를 대신하지 않습니다.";
                if (result["diagnostics"] is JArray diagnostics)
                {
                    var preview = new List<string>();
                    foreach (var item in diagnostics.OfType<JObject>())
                    {
                        var message = TextValue(item["message"]);
                        if (message == null) continue;
                        var location = Location(item);
                        var line = (location == null ? "" : location + " ") + message;
                        details.AppendLine(line);
                        if (preview.Count < 5) preview.Add(line);
                    }
                    model.DiagnosticPreview = preview;
                }
            }
            else if (state == "rejected") model.NextStep = "제출 형식·필수 파일·마감 등 과제 안내를 확인하고 교수자에게 문의하세요. 채점 통과로 처리되지 않았습니다.";
            // Never display provisional score/rubric/diagnostics before publication.
            details.AppendLine("소스 식별값: " + ((string)result["source_sha256"] ?? (string)result["head_sha"] ?? "제공되지 않음"));
            details.AppendLine("공개 시각: " + ((string)result["published_at"] ?? "미공개"));
            model.Details = details.ToString();
            return model;
        }
    }
}
