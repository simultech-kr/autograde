using System;
using System.IO;
using Autograde.Core;
using Newtonsoft.Json.Linq;

internal static class AssignmentSelectionChecks
{
    public static int Run()
    {
        int checks = 0;
        void Check(bool value, string name) { if (!value) throw new Exception(name); checks++; }
        var assignment = new JObject { ["assignment_id"] = "asn_new" };
        Check(AssignmentSelection.LatestSubmissionId(assignment) == null, "new assignment has no result to request");
        assignment["latest_submission"] = JValue.CreateNull();
        Check(AssignmentSelection.LatestSubmissionId(assignment) == null, "explicit null submission is a normal empty state");
        assignment["latest_submission"] = new JObject { ["submission_id"] = "bsub_existing", ["state"] = "queued" };
        Check(AssignmentSelection.LatestSubmissionId(assignment) == "bsub_existing", "pending submission still has a result to check");
        foreach (var malformed in new JToken[] { new JObject(), new JArray(), new JObject { ["submission_id"] = 123 } })
        {
            assignment["latest_submission"] = malformed;
            bool rejected = false;
            try { AssignmentSelection.LatestSubmissionId(assignment); }
            catch (InvalidDataException) { rejected = true; }
            Check(rejected, "malformed receipt is not mislabeled as no submission");
        }
        return checks;
    }
}
