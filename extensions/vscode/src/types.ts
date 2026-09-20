export interface Assignment {
  readonly id: string;
  readonly key?: string;
  readonly title: string;
  readonly courseLabel?: string;
  readonly status?: string;
  readonly ready?: boolean;
  readonly deliveryMode?: string;
  readonly starterUrl?: string;
  readonly starterSha256?: string;
  readonly starterSizeBytes?: number;
  readonly dueAt?: string;
  readonly submissionMode?: string;
  readonly assignmentPath?: string;
  readonly repository?: AssignedRepository;
  readonly latestSubmission?: SubmissionSummary;
}

export interface AssignedRepository {
  readonly githubRepositoryId?: number;
  readonly fullName?: string;
  readonly name?: string;
  readonly cloneUrl?: string;
  readonly sshUrl?: string;
  readonly htmlUrl?: string;
  readonly targetRef?: string;
  readonly state?: string;
  readonly ready?: boolean;
}

export interface SubmissionSummary {
  readonly id: string;
  readonly state: string;
  readonly headSha?: string;
  readonly sourceDigest?: string;
  readonly score?: number;
  readonly maxScore?: number;
  readonly previousBest?: PreviousBestScore;
}

export interface PreviousBestScore {
  readonly submissionId: string;
  readonly receivedAt: string;
  readonly score: number;
  readonly maxScore: number;
}

export interface GradeResult {
  readonly state: string;
  readonly headSha?: string;
  readonly sourceDigest?: string;
  readonly score?: number;
  readonly maxScore?: number;
  readonly rubric: readonly RubricItem[];
  readonly diagnostics: readonly ResultDiagnostic[];
  readonly previousBest?: PreviousBestScore;
}

export interface RubricItem {
  readonly name: string;
  readonly score?: number;
  readonly maxScore?: number;
  readonly feedback?: string;
}

export interface ResultDiagnostic {
  readonly path: string;
  readonly line?: number;
  readonly column?: number;
  readonly endLine?: number;
  readonly endColumn?: number;
  readonly severity?: string;
  readonly message: string;
}

export interface DeviceAuthorization {
  readonly device_code: string;
  readonly user_code: string;
  readonly verification_uri: string;
  readonly verification_uri_complete?: string;
  readonly expires_in: number;
  readonly poll_interval?: number;
  readonly interval?: number;
}

export interface TokenResponse {
  readonly access_token: string;
  readonly refresh_token: string;
  readonly expires_in: number;
  readonly token_type?: string;
}

/** Metadata returned after a claim code approves one pending device. */
export interface AssignmentClaim {
  readonly assignmentId: string;
  readonly courseKey: string;
  readonly deliveryMode: string;
  readonly acceptanceId: string;
}
