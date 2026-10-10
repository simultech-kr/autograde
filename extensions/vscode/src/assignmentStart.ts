export type AssignmentStartAction = "resume" | "choose" | "download";

export interface AssignmentStartActions<T> {
  readonly existing: readonly T[];
  chooseAction(hasExisting: boolean): Promise<AssignmentStartAction | undefined>;
  chooseExisting(candidates: readonly T[]): Promise<T | undefined>;
  selectFolder(): Promise<T | undefined>;
  resume(folder: T): Promise<void>;
  download(): Promise<void>;
  isCurrent(): boolean;
}

/** The same post-claim route handles first use, restart, fresh copies, and cancellation. */
export async function startAssignment<T>(actions: AssignmentStartActions<T>): Promise<void> {
  const action = await actions.chooseAction(actions.existing.length > 0);
  if (!action || !actions.isCurrent()) return;
  if (action === "download") { await actions.download(); return; }
  const folder = action === "choose" ? await actions.selectFolder()
    : actions.existing.length === 1 ? actions.existing[0] : await actions.chooseExisting(actions.existing);
  if (folder === undefined || !actions.isCurrent()) return;
  await actions.resume(folder);
}
