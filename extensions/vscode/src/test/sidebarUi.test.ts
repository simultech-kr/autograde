import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import * as path from "node:path";
import test from "node:test";

import type { Assignment } from "../types";

interface ModuleLoader {
  _load(request: string, parent: unknown, isMain: boolean): unknown;
}

class TreeItem {
  public id?: string;
  public description?: string;
  public tooltip?: unknown;
  public contextValue?: string;
  public iconPath?: unknown;
  public command?: {
    command: string;
    title: string;
    arguments?: unknown[];
  };

  public constructor(
    public readonly label: string,
    public readonly collapsibleState: number,
  ) {}
}

class EventEmitter<T> {
  public readonly event = (): void => {};

  public fire(_value: T): void {}
}

class ThemeIcon {
  public constructor(public readonly id: string) {}
}

class MarkdownString {
  public constructor(public readonly value: string) {}
}

const moduleLoader = require("node:module") as ModuleLoader;
const originalLoad = moduleLoader._load;
moduleLoader._load = function loadWithVscodeStub(
  request: string,
  parent: unknown,
  isMain: boolean,
): unknown {
  if (request === "vscode") {
    return {
      EventEmitter,
      MarkdownString,
      ThemeIcon,
      TreeItem,
      TreeItemCollapsibleState: { None: 0, Collapsed: 1 },
    };
  }
  return originalLoad.call(this, request, parent, isMain);
};
const { AssignmentTreeItem, AssignmentsTreeProvider } = require("../tree") as typeof import("../tree");
moduleLoader._load = originalLoad;

interface MenuContribution {
  readonly command: string;
  readonly when?: string;
  readonly group?: string;
}

interface WelcomeContribution {
  readonly view: string;
  readonly contents: string;
  readonly when?: string;
}

interface ExtensionManifest {
  readonly contributes?: {
    readonly menus?: {
      readonly "view/title"?: readonly MenuContribution[];
      readonly "view/item/context"?: readonly MenuContribution[];
    };
    readonly viewsWelcome?: readonly WelcomeContribution[];
  };
}

test("sidebar title exposes direct signed-out and signed-in actions", async () => {
  const manifest = await readManifest();
  const titleMenus = manifest.contributes?.menus?.["view/title"] ?? [];

  const signIn = titleMenus.find((item) => item.command === "autograde.signIn");
  assert.ok(signIn, "the Assignments title must provide a visible Sign In action");
  assert.match(signIn.when ?? "", /view\s*==\s*autograde\.assignments/);
  assert.match(signIn.when ?? "", /!autograde\.authenticated/);
  assert.match(signIn.group ?? "", /^navigation/);

  for (const command of [
    "autograde.signOut",
    "autograde.refreshAssignments",
    "autograde.cloneAssignment",
  ]) {
    const menu = titleMenus.find((item) => item.command === command);
    assert.ok(menu, `${command} must be reachable from the Assignments title`);
    assert.match(menu.when ?? "", /view\s*==\s*autograde\.assignments/);
    assert.match(menu.when ?? "", /autograde\.authenticated/);
    assert.match(menu.group ?? "", /^navigation/);
  }

  const download = titleMenus.find((item) => item.command === "autograde.cloneAssignment");
  assert.match(download?.when ?? "", /isWorkspaceTrusted/);
  assert.match(download?.when ?? "", /autograde\.hasDownloadableAssignments/);

  const itemMenus = manifest.contributes?.menus?.["view/item/context"] ?? [];
  assert.ok(
    itemMenus.some((item) =>
      item.command === "autograde.cloneAssignment" && (item.group ?? "").startsWith("inline")
    ),
    "the existing one-click download action on each assignment row must remain available",
  );
});

test("sidebar welcome content offers authentication-aware clickable controls", async () => {
  const manifest = await readManifest();
  const welcome = (manifest.contributes?.viewsWelcome ?? [])
    .filter((item) => item.view === "autograde.assignments");
  const signedOut = welcome.find((item) => item.when?.includes("!autograde.authenticated"));
  const signedIn = welcome.find((item) =>
    item.when?.includes("autograde.authenticated") && !item.when.includes("!autograde.authenticated")
  );

  assert.ok(signedOut, "signed-out students need a dedicated welcome state");
  assert.match(signedOut.contents, /\[[^\]]*(?:Sign In|로그인)[^\]]*\]\(command:autograde\.signIn\)/i);

  assert.ok(signedIn, "signed-in students need a dedicated welcome state");
  assert.match(signedIn.contents, /\[[^\]]+\]\(command:autograde\.refreshAssignments\)/);
  assert.doesNotMatch(
    signedIn.contents,
    /command:autograde\.cloneAssignment/,
    "an empty assignment list must not offer a download action that is guaranteed to fail",
  );
});

test("extension keeps authentication and download availability contexts in sync", async () => {
  const source = await readFile(path.resolve(__dirname, "../../src/extension.ts"), "utf8");
  const authenticationStart = source.indexOf("const updateAuthenticationUI = async");
  const refreshStart = source.indexOf("const refreshAssignments = async", authenticationStart);
  const authControllerStart = source.indexOf("const auth = new AuthenticationController", refreshStart);

  assert.ok(authenticationStart >= 0 && refreshStart > authenticationStart);
  assert.ok(authControllerStart > refreshStart);
  const authenticationWiring = source.slice(authenticationStart, refreshStart);
  assert.match(
    authenticationWiring,
    /"setContext",\s*"autograde\.authenticated",\s*authenticated/,
  );
  assert.match(
    authenticationWiring,
    /"setContext",\s*"autograde\.hasDownloadableAssignments",\s*false/,
  );

  const refreshWiring = source.slice(refreshStart, authControllerStart);
  assert.match(
    refreshWiring,
    /"setContext",\s*"autograde\.hasDownloadableAssignments",\s*assignments\.some\(isAssignmentDownloadable\)/,
  );
  assert.match(source, /void updateAuthenticationUI\(false\)/);
});

test("expanding an assignment exposes a visible file-download child action", () => {
  const assignments: Assignment[] = [
    {
      id: "observer-cpp",
      title: "Observer C++",
      deliveryMode: "bundle",
      status: "open",
      ready: true,
      starterUrl: "/v1/assignments/observer-cpp/starter",
    },
    {
      id: "observer-repository",
      title: "Observer Repository",
      repository: {
        githubRepositoryId: 42,
        fullName: "classroom/observer-student",
        cloneUrl: "https://github.com/classroom/observer-student.git",
        state: "ready",
      },
    },
  ];
  const provider = new AssignmentsTreeProvider();
  provider.setAssignments(assignments);
  const roots = provider.getChildren();

  assert.equal(roots.length, assignments.length);
  for (const root of roots) {
    assert.ok(root instanceof AssignmentTreeItem);
    const download = provider.getChildren(root).find((child) => child.label === "과제 파일 다운로드");
    assert.ok(download, "every assignment's details must include a labelled download action");
    assert.equal(download.command?.command, "autograde.cloneAssignment");
    assert.deepEqual(download.command?.arguments, [root]);
  }
});

async function readManifest(): Promise<ExtensionManifest> {
  const raw = await readFile(path.resolve(__dirname, "../../package.json"), "utf8");
  return JSON.parse(raw) as ExtensionManifest;
}
