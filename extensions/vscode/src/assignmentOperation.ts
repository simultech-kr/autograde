/** Keep related pickers from replacing a claim input or a folder choice in flight. */
export class AssignmentOperationGate {
  private active = false;

  public async run(action: () => Promise<unknown>, onBusy: () => void): Promise<void> {
    if (this.active) {
      onBusy();
      return;
    }
    this.active = true;
    try {
      await action();
    } finally {
      // Cancellation and failure must permit a deliberate retry; never queue an old action.
      this.active = false;
    }
  }
}

/** A confirmation must not authorize a folder switch in a replacement login session. */
export async function confirmWorkspaceSwitch(actions: {
  isCurrent(): boolean;
  validate(): Promise<unknown>;
  confirm(): Promise<boolean>;
  open(): Promise<unknown>;
}): Promise<void> {
  if (!actions.isCurrent()) return;
  await actions.validate();
  if (!actions.isCurrent()) return;
  if (!await actions.confirm() || !actions.isCurrent()) return;
  // The folder can be moved or removed while the confirmation is open.
  await actions.validate();
  if (!actions.isCurrent()) return;
  await actions.open();
}
