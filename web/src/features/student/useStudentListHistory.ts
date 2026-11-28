import {useEffect, useState} from "react";

type Entry = {scope: string; page: string; option: string; queryId?: string; sealedQuery?: string};
const tabSearches = new Map<string, string>();
export const clearStudentSearches = () => tabSearches.clear();
export const maxSearchLength = 200;

function read(scope: string | undefined, page: string): Entry | undefined {
  const entry = typeof window !== "undefined" && window.history.state?.autogradeStudentList;
  return scope && entry?.scope === scope && entry.page === page ? entry : undefined;
}

function write(entry: Entry) {
  try {
    window.history.replaceState({...window.history.state, autogradeStudentList: entry}, "");
  } catch {
    // Browsers may limit history writes. The live controls still work.
  }
}

const encode = (bytes: Uint8Array) => btoa(String.fromCharCode(...bytes));
const decode = (value: string) => Uint8Array.from(atob(value), c => c.charCodeAt(0));
const cacheKey = (entry: Entry) => JSON.stringify([entry.scope, entry.page, entry.queryId]);
const algorithm = (entry: Entry) => ({
  name: "AES-GCM", iv: decode(entry.queryId!), additionalData: new TextEncoder().encode(cacheKey(entry)),
});

async function searchKey(csrf: string) {
  const material = new TextEncoder().encode("autograde-material-search\0" + csrf);
  const digest = await crypto.subtle.digest("SHA-256", material);
  return crypto.subtle.importKey("raw", digest, "AES-GCM", false, ["encrypt", "decrypt"]);
}

// The parent remounts these controls on navigation or session change. Only
// the server's non-credential session tag and public enum are saved in clear.
export function useStudentListHistory<T extends string>(scope: string | undefined, page: string, choices: readonly T[], {csrf = "", initialOption}: {csrf?: string; initialOption?: T} = {}) {
  const entry = read(scope, page);
  const [option, setOption] = useState<T>(() => initialOption ?? (choices.includes(entry?.option as T) ? entry!.option as T : choices[0]));
  const [query, setQuery] = useState(() => entry?.queryId ? tabSearches.get(cacheKey(entry)) || "" : "");

  useEffect(() => {
    const saved = read(scope, page);
    if (!saved?.queryId || tabSearches.has(cacheKey(saved)) || !csrf || !crypto.subtle ||
        typeof saved.sealedQuery !== "string" || saved.sealedQuery.length > 2000) return;
    let current = true;
    async function restore(saved: Entry) {
      try {
        const plain = await crypto.subtle.decrypt(algorithm(saved), await searchKey(csrf), decode(saved.sealedQuery!));
        if (current && read(scope, page)?.queryId === saved.queryId) {
          setQuery(new TextDecoder().decode(plain).slice(0, maxSearchLength));
        }
      } catch { /* An expired or unreadable search opens with the default query. */ }
    }
    void restore(saved);
    return () => { current = false; };
  }, [scope, page, csrf]);

  function choose(value: string) {
    const allowed = choices.find(choice => choice === value);
    if (!allowed) return;
    setOption(allowed);
    if (scope) write({...read(scope, page), scope, page, option: allowed});
  }

  async function search(text: string) {
    const value = text.slice(0, maxSearchLength);
    setQuery(value);
    if (!scope || !csrf) return;
    const previous = read(scope, page);
    if (previous?.queryId) tabSearches.delete(cacheKey(previous));
    const next: Entry = {scope, page, option};
    if (value) {
      next.queryId = encode(crypto.getRandomValues(new Uint8Array(12)));
      if (tabSearches.size >= 64) tabSearches.delete(tabSearches.keys().next().value!);
      tabSearches.set(cacheKey(next), value);
    }
    write(next);
    if (!next.queryId || !crypto.subtle) return;
    try {
      const sealed = await crypto.subtle.encrypt(algorithm(next), await searchKey(csrf), new TextEncoder().encode(value));
      const live = read(scope, page);
      if (live?.queryId === next.queryId) write({...live, sealedQuery: encode(new Uint8Array(sealed))});
    } catch { /* Plain HTTP and restricted browsers retain searches only in this document. */ }
  }
  return {option, choose, query, search};
}
