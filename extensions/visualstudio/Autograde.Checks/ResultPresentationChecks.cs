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
        return checks;
    }
}
