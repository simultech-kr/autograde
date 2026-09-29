using System;
using Autograde.Core;
using Newtonsoft.Json.Linq;

internal static class ResultPresentationChecks
{
    public static int Run()
    {
        int checks = 0;
        void Check(bool value, string name) { if (!value) throw new Exception(name); checks++; }
        var result = JObject.Parse(@"{""submission_id"":""bsub_one"",""state"":""published"",""score"":7,""max_score"":10,
          ""rubric"":{""compile"":{""score"":2,""max_score"":2},""output"":{""score"":0,""max_score"":3,""feedback"":""newline required""}},
          ""diagnostics"":[{""path"":""main.cpp"",""line"":7,""message"":""check newline""}]} ");
        var model = ResultPresentation.From(result);
        Check(model.Headline == "수정이 필요합니다" && model.Percent == 70, "partial result summary");
        Check(model.Criteria[0].Title == "출력 결과" && model.Criteria[0].NeedsWork, "failed items first");
        Check(model.Criteria[0].Feedback == "newline required" && model.NextStep.Contains("다시 제출"), "actionable feedback");
        Check(model.DiagnosticPreview[0].Contains("main.cpp:7") && model.Details.Contains("bsub_one"), "receipt and diagnostics");
        foreach (var state in new[] { "received", "queued", "running", "graded", "infra_failed", "rejected", "assessment_failed" })
        {
            result["state"] = state;
            model = ResultPresentation.From(result);
            Check(model.Percent == null && model.Score == "점수 미공개" && model.Criteria.Count == 0 &&
                model.DiagnosticPreview.Count == 0 && !model.Details.Contains("check newline"), "no provisional result leak " + state);
        }
        result["state"] = "published"; result["score"] = 10;
        Check(ResultPresentation.From(result).Headline == "결과 확인이 필요합니다", "contradictory full score with failed criterion");
        result["rubric"] = new JObject();
        model = ResultPresentation.From(result);
        Check(model.Headline == "자동채점 총점 기준 충족" && model.NextStep.Contains("별도 요구사항"), "full score not final course completion");
        Check(model.Summary.Contains("항목별 판정은 제공되지"), "score only policy");
        foreach (var score in new JToken[] { JValue.CreateNull(), new JValue("10"), new JValue(-1), new JValue(11), new JValue(double.NaN) })
        {
            result["score"] = score;
            Check(ResultPresentation.From(result).Headline == "결과 확인이 필요합니다", "invalid scores fail closed");
        }
        result["score"] = 0; result["max_score"] = 0;
        Check(ResultPresentation.From(result).Percent == null && ResultPresentation.From(result).Headline == "결과 확인이 필요합니다", "zero maximum not success");
        result["max_score"] = 10;
        Check(ResultPresentation.From(result).Headline == "수정이 필요합니다", "zero score is graded failure not pending");
        result["previous_best"] = new JObject { ["submission_id"] = "bsub_earlier", ["received_at"] = "2026-09-17T00:00:00Z", ["score"] = 8, ["max_score"] = 10 };
        result["score"] = 3;
        model = ResultPresentation.From(result);
        Check(model.Score == "3 / 10점" && model.PreviousBest.Contains("8 / 10점") && model.PreviousBest.Contains("bsub_earlier"), "current score distinct from earlier best");
        result["state"] = "queued";
        model = ResultPresentation.From(result);
        Check(model.Score == "점수 미공개" && model.PreviousBest.Contains("8 / 10점"), "earlier public best does not replace pending current score");

        var detailed = JObject.Parse(@"{""submission_id"":""bsub_details"",""state"":""published"",""score"":3,""max_score"":11,
          ""rubric"":{
            ""compile"":{""status"":""passed"",""score"":2,""max_score"":2,""feedback"":""컴파일 완료""},
            ""output"":{""status"":""blocked"",""score"":0,""max_score"":6,""feedback"":""실행 단계가 끝나지 않아 출력을 검사하지 못했습니다."",""hint"":""실행 문제를 해결한 뒤 다시 제출하세요.""},
            ""execution"":{""status"":""partial"",""score"":1,""max_score"":2,""feedback"":""반환값이 조건을 충족하지 못했습니다."",""hint"":""반환값 조건을 확인하세요."",""path"":""src/main.cpp"",""line"":12,""column"":3},
            ""case_one"":{""status"":""failed"",""score"":0,""max_score"":1,""feedback"":""공개 테스트를 통과하지 못했습니다."",""stdout"":""private stdout"",""expected"":""private expected"",""actual"":""private actual""}},
          ""diagnostics"":[{""message"":""프로그램 실행 시간을 확인하세요.""},{""path"":""src/main.cpp"",""line"":12,""column"":3,""message"":""공개 진단""}]} ");
        model = ResultPresentation.From(detailed);
        Check(model.Criteria[0].Title == "프로그램 실행" && model.Criteria[1].Title == "테스트 one", "partial and failed criteria precede blocked and successful criteria");
        Check(model.Criteria[0].Feedback == "반환값이 조건을 충족하지 못했습니다." && model.Criteria[0].Hint == "반환값 조건을 확인하세요.", "confirmed symptom and correction guidance remain separate");
        Check(model.Criteria[0].SourceLocation == "src/main.cpp:12:3", "criterion source location includes column");
        Check(model.Criteria[0].IsExpanded && model.Criteria[1].IsExpanded && !model.Criteria[3].IsExpanded, "failed and partial expanded successful collapsed");
        Check(model.Criteria[2].IsBlocked && !model.Criteria[2].NeedsWork && !model.Criteria[2].IsPassed && model.Criteria[2].Status == "선행 단계 해결 후 재검사", "blocked means untested after prerequisite failure");
        Check(model.Summary.Contains("2개 수정 필요") && model.Summary.Contains("1개 선행 단계 대기"), "blocked count is separate from failed count");
        Check(model.Criteria[1].Hint == null && model.Criteria[1].SourceLocation == null && model.Criteria[1].Feedback == "공개 테스트를 통과하지 못했습니다.", "no invented correction source location or raw test data");
        Check(model.DiagnosticPreview[0] == "프로그램 실행 시간을 확인하세요." && model.DiagnosticPreview[1] == "src/main.cpp:12:3 공개 진단", "general diagnostic has no leading colon and located diagnostic has column");
        foreach (var state in new[] { "received", "queued", "running", "graded", "infra_failed", "rejected", "assessment_failed" })
        {
            detailed["state"] = state;
            model = ResultPresentation.From(detailed);
            Check(model.Criteria.Count == 0 && model.DiagnosticPreview.Count == 0 && !model.Details.Contains("공개 진단") && !model.Details.Contains("src/main.cpp"), "new detail fields hidden before publication " + state);
        }
        detailed["state"] = "published";

        var direct = JObject.Parse(@"{""state"":""published"",""score"":10,""max_score"":10,
          ""rubric"":{""compile"":{""status"":""passed"",""title"":""컴파일"",""feedback"":""컴파일 완료""},
          ""case_one"":{""status"":""passed"",""score"":10,""max_score"":10}}}");
        model = ResultPresentation.From(direct);
        Check(model.Headline == "자동채점 총점 기준 충족" && model.Criteria[0].IsPassed && !model.Criteria[0].RequiresReview && !model.Criteria[0].IsExpanded, "unscored compile pass with full case score is successful");
        var unscored = (JObject)direct["rubric"]["compile"];
        foreach (var field in new[] { "score", "max_score" })
        {
            unscored[field] = 1;
            model = ResultPresentation.From(direct);
            Check(model.Headline == "결과 확인이 필요합니다" && model.Criteria[0].RequiresReview && !model.Criteria[0].IsPassed, "one-sided score remains a conflict " + field);
            unscored.Remove(field);
        }
        unscored["score"] = JValue.CreateNull(); unscored["max_score"] = JValue.CreateNull();
        Check(ResultPresentation.From(direct).Headline == "결과 확인이 필요합니다", "null score pair is malformed not unscored");

        var criterion = new JObject { ["score"] = 1, ["max_score"] = 1, ["status"] = "passed" };
        var single = new JObject { ["state"] = "published", ["score"] = 1, ["max_score"] = 1, ["rubric"] = new JObject { ["output"] = criterion } };
        foreach (var status in new[] { "failed", "partial", "blocked", "unsupported" })
        {
            criterion["status"] = status;
            model = ResultPresentation.From(single);
            Check(model.Headline == "결과 확인이 필요합니다" && !model.Criteria[0].IsPassed, "full score cannot override adverse or unknown explicit status " + status);
        }
        criterion["status"] = "passed"; criterion["score"] = 0;
        model = ResultPresentation.From(single);
        Check(!model.Criteria[0].IsPassed && model.Criteria[0].NeedsWork && model.Criteria[0].RequiresReview && model.Headline == "결과 확인이 필요합니다", "explicit success cannot override deficient score");
        foreach (var invalid in new JToken[] { JValue.CreateNull(), new JValue("1"), new JValue(-1), new JValue(2), new JValue(double.NaN) })
        {
            criterion["score"] = invalid;
            model = ResultPresentation.From(single);
            Check(!model.Criteria[0].IsPassed && model.Headline == "결과 확인이 필요합니다", "explicit success requires valid supporting score");
        }
        criterion["score"] = 1;
        foreach (var invalid in new JToken[] { new JObject(), new JValue(true), new JValue(1), new JValue("") })
        {
            criterion["status"] = invalid;
            Check(!ResultPresentation.From(single).Criteria[0].IsPassed, "malformed status does not imply success");
        }
        criterion["status"] = "passed"; criterion["path"] = "한글/main.cpp"; criterion["line"] = 8; criterion["column"] = 2;
        Check(ResultPresentation.From(single).Criteria[0].SourceLocation == "한글/main.cpp:8:2", "unicode relative source path");
        foreach (var invalid in new JToken[] { JValue.CreateNull(), new JValue(0), new JValue(-1), new JValue(1.5), new JValue("7"), new JValue(10000001), new JValue(long.MaxValue) })
        {
            criterion["line"] = invalid;
            Check(ResultPresentation.From(single).Criteria[0].SourceLocation == "한글/main.cpp", "invalid line and standalone column omitted");
        }
        criterion["line"] = 8; criterion["column"] = -1;
        Check(ResultPresentation.From(single).Criteria[0].SourceLocation == "한글/main.cpp:8", "invalid column omitted");
        criterion["line"] = 10000000; criterion["column"] = 10000000;
        Check(ResultPresentation.From(single).Criteria[0].SourceLocation == "한글/main.cpp:10000000:10000000", "maximum server position is retained");
        criterion["column"] = 10000001;
        Check(ResultPresentation.From(single).Criteria[0].SourceLocation == "한글/main.cpp:10000000", "oversized column omitted");
        criterion.Remove("line"); criterion.Remove("column");
        criterion["path"] = new string('a', 508) + ".cpp";
        Check(ResultPresentation.From(single).Criteria[0].SourceLocation == (string)criterion["path"], "maximum server path length retained");
        criterion["path"] = "src/😀.cpp";
        Check(ResultPresentation.From(single).Criteria[0].SourceLocation == "src/😀.cpp", "valid supplementary unicode source path retained");
        foreach (var path in new[] { "/tmp/main.cpp", "C:/temp/main.cpp", @"src\main.cpp", "../main.cpp", "src/../main.cpp", "./main.cpp", "src//main.cpp", "main.cpp\n", " /tmp/main.cpp", "main\u202E.cpp", "main\u200D.cpp", "main\uE000.cpp", new string('a', 509) + ".cpp" })
        {
            criterion["path"] = path;
            Check(ResultPresentation.From(single).Criteria[0].SourceLocation == null, "unsafe source location not displayed " + path);
        }
        single["diagnostics"] = new JArray {
            new JObject { ["path"] = "/private/grader/main.cpp", ["line"] = 5, ["message"] = "일반 진단" },
            new JObject { ["line"] = 5, ["message"] = "파일 위치 없음" },
            new JObject { ["path"] = "main.cpp", ["line"] = -1, ["message"] = "행 정보 없음" },
            new JObject { ["path"] = "main.cpp", ["message"] = new JObject() }
        };
        model = ResultPresentation.From(single);
        Check(model.DiagnosticPreview.Count == 3 && model.DiagnosticPreview[0] == "일반 진단" && model.DiagnosticPreview[1] == "파일 위치 없음" && model.DiagnosticPreview[2] == "main.cpp 행 정보 없음", "diagnostic formatting safely handles missing invalid and private locations");
        return checks;
    }
}
