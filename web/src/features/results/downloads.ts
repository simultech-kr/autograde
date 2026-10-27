// Match the instructor download routes. Reject unsafe segments instead
// of turning untrusted IDs into a different route with encoding or traversal.
const assignmentID = /^[A-Za-z0-9_-]+$/;
const submissionID = /^bsub_[A-Za-z0-9_-]+$/;
const courseID = /^[a-z0-9_-]{1,96}$/;
const matchesWhole = (pattern: RegExp, value: string) => pattern.exec(value)?.[0] === value;

export function submissionDownloadURL(course: string, submission: string | null): string | null {
  if (!matchesWhole(courseID, course) || !submission || !matchesWhole(submissionID, submission)) return null;
  return `/courses/${course}/instructor/submissions/${submission}/download.zip`;
}

export function assignmentDownloadsURL(course: string, assignment: string): string | null {
  if (!matchesWhole(courseID, course) || !matchesWhole(assignmentID, assignment)) return null;
  return `/courses/${course}/instructor/assignments/${assignment}/downloads`;
}
