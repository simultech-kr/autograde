import test from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";
import { act, createElement, type ComponentType } from "react";
import ts from "typescript";
import { sampleBootstrap, sampleResults } from "./fixtures.ts";

function loadDashboard(): ComponentType {
  const require = createRequire(import.meta.url), cache = new Map<string, { exports: unknown }>();
  const sourceRoot = fileURLToPath(new URL("../src/", import.meta.url));
  function load(path: string): unknown {
    assert.ok(path.startsWith(sourceRoot));
    if (path.endsWith(".css")) return { __esModule: true, default: new Proxy({}, { get: (_target, key) => key }) };
    if (cache.has(path)) return cache.get(path)!.exports;
    const module = { exports: {} }; cache.set(path, module);
    const javascript = ts.transpileModule(readFileSync(path, "utf8"), {
      compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
    }).outputText;
    const localRequire = (specifier: string): unknown => {
      if (!specifier.startsWith(".")) return require(specifier);
      const target = resolve(dirname(path), specifier);
      return load([target, `${target}.ts`, `${target}.tsx`].find(existsSync)!);
    };
    new Function("require", "module", "exports", javascript)(localRequire, module, module.exports);
    return module.exports;
  }
  return (load(resolve(sourceRoot, "features/results/InstructorDashboard.tsx")) as { InstructorDashboard: ComponentType }).InstructorDashboard;
}

async function mount(query = "") {
  const dom = new JSDOM("<!doctype html><div id='root'></div>", { url: "https://example.test/app?course_key=come2201" + (query ? "&" + query : ""), pretendToBeVisual: true });
  const originals = new Map<string, PropertyDescriptor | undefined>();
  const bind = (key: string, value: unknown) => { originals.set(key, Object.getOwnPropertyDescriptor(globalThis, key)); Object.defineProperty(globalThis, key, { configurable: true, writable: true, value }); };
  Object.defineProperty(dom.window.document, "visibilityState", { value: "visible" });
  dom.window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} }) as unknown as MediaQueryList;
  const requests: URL[] = [];
  let hold = false, release: (() => void) | undefined;
  const fetcher: typeof fetch = async (input, options) => {
    assert.equal(options?.method, "GET");
    assert.equal(options?.credentials, "same-origin");
    const url = new URL(String(input), dom.window.location.href); requests.push(url);
    if (url.pathname.endsWith("/bootstrap")) return Response.json(sampleBootstrap);
    assert.equal(url.pathname, "/instructor/api/v1/courses/come2201/results");
    if (hold) { hold = false; await new Promise<void>(resolve => { release = resolve; }); }
    return Response.json({ ...sampleResults, page: Number(url.searchParams.get("page")) });
  };
  for (const [key, value] of Object.entries({ window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true, fetch: fetcher })) bind(key, value);
  const { createRoot } = await import("react-dom/client"), root = createRoot(dom.window.document.getElementById("root")!);
  await act(async () => root.render(createElement(loadDashboard())));
  const get = <T extends Element>(id: string) => {
    const element = dom.window.document.querySelector<T>(`[data-testid="${id}"]`);
    assert.ok(element, id); return element;
  };
  return { dom, requests, get,
    async select(id: string, value: string) {
      await act(async () => { const select = get<HTMLSelectElement>(id); select.value = value; select.dispatchEvent(new dom.window.Event("change", { bubbles: true })); });
    },
    async refreshWithDelay() {
      hold = true;
      await act(async () => get<HTMLButtonElement>("refresh-results").click());
      assert.ok(release);
    },
    async finishRefresh() { await act(async () => release?.()); },
    async dispose() {
      await act(async () => root.unmount()); dom.window.close();
      for (const [key, descriptor] of originals) { if (descriptor) Object.defineProperty(globalThis, key, descriptor); else Reflect.deleteProperty(globalThis, key); }
    },
  };
}

test("React: compact instructor controls retain labels, scoped requests, score semantics and explicit loading", async () => {
  const app = await mount();
  try {
    const { document } = app.dom.window;
    assert.equal(document.querySelectorAll("h1").length, 1);
    assert.equal(app.get<HTMLDetailsElement>("results-summary").open, false);
    assert.match(app.get("results-summary").querySelector("summary")!.textContent!, /접수·채점 중 1건/);
    for (const id of ["course-select", "assignment-select", "status-select", "sort-select", "student-search"]) {
      assert.ok(app.get<HTMLInputElement>(id).labels?.length, `${id} has a persistent keyboard/screen-reader label`);
    }
    const firstRow = app.get("result-row");
    assert.match(firstRow.querySelector('[data-testid="current-score"]')!.textContent!, /0 \/ 100점/);
    assert.match(firstRow.querySelector('[data-testid="previous-best"]')!.textContent!, /100 \/ 100점/);
    await app.select("assignment-select", "observer");
    await app.select("sort-select", "student");
    await app.select("status-select", "pending");
    const selected = app.requests.at(-1)!;
    assert.equal(selected.searchParams.get("assignment_id"), "observer");
    assert.equal(selected.searchParams.get("sort"), "student");
    assert.equal(selected.searchParams.get("status"), "pending");
    const input = app.get<HTMLInputElement>("student-search");
    await act(async () => {
      Object.getOwnPropertyDescriptor(app.dom.window.HTMLInputElement.prototype, "value")!.set!.call(input, "SAMPLE-001");
      input.dispatchEvent(new app.dom.window.Event("input", { bubbles: true }));
    });
    await act(async () => app.get<HTMLFormElement>("filter-form").dispatchEvent(new app.dom.window.Event("submit", { bubbles: true, cancelable: true })));
    assert.equal(app.requests.at(-1)!.searchParams.get("student"), "SAMPLE-001");
    assert.equal(app.requests.at(-1)!.searchParams.get("page"), "1");
    await app.refreshWithDelay();
    assert.equal(app.get<HTMLButtonElement>("refresh-results").disabled, true);
    assert.equal(app.get("refresh-results").textContent, "확인 중…");
    await app.finishRefresh();
    assert.equal(app.get<HTMLButtonElement>("refresh-results").disabled, false);
    assert.match(app.get("last-success").textContent!, /자동 확인 켜짐/);
    assert.ok(document.querySelector('footer details a[href*="legacy=1"]'));
  } finally { await app.dispose(); }
});

test("React: mobile instructor menu identifies the current screen and Escape restores summary focus", async () => {
  const app = await mount();
  try {
    const menu = app.get("course-navigation").querySelector<HTMLDetailsElement>("details")!;
    const summary = menu.querySelector("summary")!;
    assert.equal(menu.open, false);
    await act(async () => summary.click());
    assert.equal(menu.open, true);
    const current = menu.querySelector<HTMLAnchorElement>('a[aria-current="page"]')!;
    assert.match(current.textContent!, /제출·채점 현황현재/);
    current.focus();
    await act(async () => current.dispatchEvent(new app.dom.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true })));
    assert.equal(menu.open, false);
    assert.equal(app.dom.window.document.activeElement, summary);
  } finally { await app.dispose(); }
});

test("React: clearing result filters resets search, assignment, state, sorting and page within the selected course", async () => {
  const app = await mount("assignment_id=observer&student=SAMPLE-001&status=pending&sort=student&page=2");
  try {
    const search = app.get<HTMLInputElement>("student-search");
    assert.equal(search.value, "SAMPLE-001");
    assert.equal(app.requests.at(-1)!.searchParams.get("page"), "2");
    assert.ok(app.get("filter-scope").contains(app.get("reset-filters")));
    // Clear both applied criteria and an edited search that has not been submitted.
    await act(async () => {
      Object.getOwnPropertyDescriptor(app.dom.window.HTMLInputElement.prototype, "value")!.set!.call(search, "UNSENT SEARCH");
      search.dispatchEvent(new app.dom.window.Event("input", { bubbles: true }));
    });
    await act(async () => app.get<HTMLButtonElement>("reset-filters").click());
    const request = app.requests.at(-1)!;
    assert.equal(request.pathname, "/instructor/api/v1/courses/come2201/results");
    assert.deepEqual(Object.fromEntries(request.searchParams), { status: "all", sort: "recent", page: "1", page_size: "20" });
    assert.equal(app.get<HTMLSelectElement>("course-select").value, "come2201");
    assert.equal(app.get<HTMLSelectElement>("assignment-select").value, "");
    assert.equal(app.get<HTMLSelectElement>("status-select").value, "all");
    assert.equal(app.get<HTMLSelectElement>("sort-select").value, "recent");
    assert.equal(search.value, "");
    assert.equal(app.dom.window.document.activeElement, search);
    assert.equal(app.dom.window.location.search, "?course_key=come2201");
    assert.equal(app.dom.window.document.querySelector('[data-testid="reset-filters"]'), null);
    assert.doesNotMatch(app.get("filter-scope").textContent!, /학생 검색:/);
  } finally { await app.dispose(); }
});

test("React: reset is absent for defaults and available when any single result criterion is active", async () => {
  for (const query of ["", "assignment_id=observer", "student=SAMPLE-001", "status=pending", "sort=assignment", "page=2"]) {
    const app = await mount(query);
    try {
      assert.equal(Boolean(app.dom.window.document.querySelector('[data-testid="reset-filters"]')), Boolean(query), query || "defaults");
    } finally { await app.dispose(); }
  }
});
