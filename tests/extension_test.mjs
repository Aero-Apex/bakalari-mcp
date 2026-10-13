// Runs extension/background.js in a sandbox with a mocked chrome.* API and checks what it sends.
import { readFileSync } from "node:fs";
import vm from "node:vm";
import assert from "node:assert/strict";

const code = readFileSync(new URL("../extension/background.js", import.meta.url), "utf8");

function sandbox({ stored, cookies, fetchImpl }) {
  const store = { ...stored };
  const listeners = { alarm: [], changed: [], message: [], installed: [], startup: [] };
  const calls = { fetch: [], alarms: [] };
  const chrome = {
    storage: { local: {
      get: async (keys) => Object.fromEntries([].concat(keys).filter((k) => k in store).map((k) => [k, store[k]])),
      set: async (obj) => Object.assign(store, obj),
    } },
    cookies: { getAll: async () => cookies, onChanged: { addListener: (f) => listeners.changed.push(f) } },
    alarms: { create: (name, info) => calls.alarms.push([name, info]), onAlarm: { addListener: (f) => listeners.alarm.push(f) } },
    action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {} },
    runtime: {
      onInstalled: { addListener: (f) => listeners.installed.push(f) },
      onStartup: { addListener: (f) => listeners.startup.push(f) },
      onMessage: { addListener: (f) => listeners.message.push(f) },
    },
  };
  const fetch = async (url, init) => { calls.fetch.push({ url, init }); return fetchImpl(url, init); };
  vm.runInNewContext(code, { chrome, fetch, URL, JSON, Promise, setTimeout, clearTimeout, console });
  return { store, listeners, calls };
}

const ok = (body) => ({ ok: true, status: 200, json: async () => body });
const configured = { server: "http://192.0.2.10:8765", token: "x".repeat(24), school: "https://school.example.cz", keepAlive: true };
const school = [
  { name: "BakaAuth", value: "AAA", domain: "school.example.cz" },
  { name: "ASP.NET_SessionId", value: "BBB", domain: "school.example.cz" },
];
const ask = (s) => new Promise((resolve) => s.listeners.message[0]({ type: "push" }, null, resolve));

// 1. a push sends every cookie of the school as one Cookie header, with the bearer token
{
  const s = sandbox({ stored: configured, cookies: school, fetchImpl: () => ok({ ok: true, cookies: 2, remaining_minutes: 14.5 }) });
  const status = await ask(s);
  const push = s.calls.fetch.find((c) => c.url.endsWith("/cookie"));
  assert.equal(push.url, "http://192.0.2.10:8765/cookie");
  assert.equal(JSON.parse(push.init.body).cookie, "BakaAuth=AAA; ASP.NET_SessionId=BBB");
  assert.equal(push.init.headers.Authorization, "Bearer " + configured.token);
  assert.equal(status.ok, true);
  assert.match(status.text, /14\.5 min left/);
  // the school's own extend call happens first (keep-alive), with the browser's cookies
  const extend = s.calls.fetch.find((c) => c.url.endsWith("/sessionextend"));
  assert.equal(extend.url, "https://school.example.cz/sessionextend");
  assert.equal(extend.init.credentials, "include");
  assert.ok(!status.text.includes("AAA") && !JSON.stringify(s.store.status).includes("AAA"), "cookie leaked into status");
}

// 2. keep-alive can be switched off
{
  const s = sandbox({ stored: { ...configured, keepAlive: false }, cookies: school, fetchImpl: () => ok({}) });
  await ask(s);
  assert.ok(!s.calls.fetch.some((c) => c.url.endsWith("/sessionextend")));
}

// 3. nothing is sent when logged out of the school or not configured
{
  const s = sandbox({ stored: configured, cookies: [{ name: "x", value: "1", domain: "school.example.cz" }], fetchImpl: () => ok({}) });
  const status = await ask(s);
  assert.equal(status.ok, false);
  assert.match(status.text, /Not logged in/);
  assert.ok(!s.calls.fetch.some((c) => c.url.endsWith("/cookie")));
  const s2 = sandbox({ stored: {}, cookies: school, fetchImpl: () => ok({}) });
  assert.match((await ask(s2)).text, /Not configured/);
}

// 4. server problems are reported, not thrown
{
  const rejected = sandbox({ stored: configured, cookies: school, fetchImpl: (u) => ({ ok: false, status: 401, json: async () => ({}) }) });
  assert.match((await ask(rejected)).text, /rejected the token/);
  const down = sandbox({ stored: configured, cookies: school, fetchImpl: (u) => { if (u.endsWith("/cookie")) throw new Error("Failed to fetch"); return ok({}); } });
  const status = await ask(down);
  assert.equal(status.ok, false);
  assert.match(status.text, /Cannot reach the server/);
}

// 5. schedules alarms; cookie changes of other sites are ignored
{
  const s = sandbox({ stored: configured, cookies: school, fetchImpl: () => ok({ ok: true, cookies: 2 }) });
  s.listeners.installed[0]();
  await new Promise((r) => setTimeout(r, 20));
  assert.deepEqual(s.calls.alarms.map((a) => a[0]).sort(), ["extend", "push"]);
  const before = s.calls.fetch.length;
  await s.listeners.changed[0]({ cookie: { domain: ".other-site.com" } });
  await new Promise((r) => setTimeout(r, 2200));
  assert.equal(s.calls.fetch.length, before, "a foreign cookie change must not trigger a push");
  await s.listeners.changed[0]({ cookie: { domain: ".school.example.cz" } });
  await new Promise((r) => setTimeout(r, 2200));
  assert.ok(s.calls.fetch.length > before, "a school cookie change should trigger a push");
}

console.log("extension tests passed");
