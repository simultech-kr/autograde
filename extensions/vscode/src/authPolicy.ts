/** Return true only when the service definitively rejected stored credentials. */
export function shouldDiscardTokensAfterRefreshError(
  status: number,
  code: string | undefined,
): boolean {
  return (
    (status === 400 || status === 401) &&
    (
      code === "invalid_grant" ||
      code === "invalid_token" ||
      code === "reauthentication_required"
    )
  );
}
