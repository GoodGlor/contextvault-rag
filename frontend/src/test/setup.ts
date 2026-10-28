import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach } from "vitest";
import { cleanup } from "@testing-library/react";
import i18n from "../i18n";

// Node 25 exposes a non-functional experimental `localStorage` global that shadows
// jsdom's, so we install a small in-memory Storage the app can actually read/write.
class MemoryStorage implements Storage {
  private store = new Map<string, string>();
  get length(): number {
    return this.store.size;
  }
  clear(): void {
    this.store.clear();
  }
  getItem(key: string): string | null {
    return this.store.has(key) ? (this.store.get(key) as string) : null;
  }
  key(index: number): string | null {
    return Array.from(this.store.keys())[index] ?? null;
  }
  removeItem(key: string): void {
    this.store.delete(key);
  }
  setItem(key: string, value: string): void {
    this.store.set(key, String(value));
  }
}

const storage = new MemoryStorage();
for (const target of [globalThis, window]) {
  Object.defineProperty(target, "localStorage", {
    value: storage,
    configurable: true,
    writable: true,
  });
}

// jsdom implements no Blob URL support at all — `URL.createObjectURL` and
// `URL.revokeObjectURL` are simply absent, not stubbed — so a bare property
// lookup finds nothing and `vi.spyOn(URL, "createObjectURL")` throws "does not
// exist" before a test even runs. Installing real (no-op) functions here gives
// `vi.spyOn` something to find and override per test, without the alternative
// of replacing the whole `URL` global: a `{ ...URL, createObjectURL }` spread
// copies none of its static methods (they are non-enumerable) and drops the
// constructor, breaking any code path that still does `new URL(...)`.
if (typeof URL.createObjectURL !== "function") {
  URL.createObjectURL = () => "blob:unset";
}
if (typeof URL.revokeObjectURL !== "function") {
  URL.revokeObjectURL = () => undefined;
}

// jsdom performs no layout, so it implements no scrolling either: `scrollIntoView`
// is absent from every element rather than a no-op, and calling it throws. The
// design canvas scrolls the selected block into view, which is a real behaviour a
// browser performs and jsdom can neither do nor observe — so it gets a no-op here
// and is checked in e2e instead.
if (typeof Element.prototype.scrollIntoView !== "function") {
  Element.prototype.scrollIntoView = () => undefined;
}

// The app defaults to Ukrainian; the existing suite asserts on English strings, so
// pin the test locale to English before each test (kept in sync via i18n keys).
beforeEach(() => {
  void i18n.changeLanguage("en");
});

// Unmount React trees and reset storage between tests so no state leaks across them.
afterEach(() => {
  cleanup();
  localStorage.clear();
});
