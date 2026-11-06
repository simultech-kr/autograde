/** A server-confirmed student key used verbatim as one portable directory component. */
export function studentWorkspaceKey(value: unknown): string {
  if (typeof value !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(value)
    || value.endsWith(".") || /^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)/i.test(value)) {
    throw new Error("서버에서 실습폴더에 사용할 학번을 확인하지 못했습니다. 교수자에게 학번 등록 상태를 확인해 주세요.");
  }
  return value;
}
