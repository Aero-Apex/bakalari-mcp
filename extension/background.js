"use strict";

// Sends the Bakaláři session cookie of this browser to your own MCP server and keeps the
// session alive with the school's own "extend session" call (what its "still there?" popup does).

const DEFAULTS = { school: "https://school.example.cz", server: "", token: "", keepAlive: true };
const PUSH_EVERY_MIN = 3;
const EXTEND_EVERY_MIN = 5;

const trimSlash = (url) => url.replace(/\/+$/, "");

async function getConfig() {
  return { ...DEFAULTS, ...(await chrome.storage.local.get(Object.keys(DEFAULTS))) };
}

async function setStatus(text, ok) {
  await chrome.storage.local.set({ status: { text, ok, at: Date.now() } });
  await chrome.action.setBadgeText({ text: ok ? "" : "!" });
  await chrome.action.setBadgeBackgroundColor({ color: "#c0392b" });
  return { text, ok };
}

async function extendSession(config) {
  try {
    await fetch(trimSlash(config.school) + "/sessionextend", { credentials: "include", cache: "no-store" });
  } catch (_) {
    // offline or the school is down: the next push reports it
  }
}

let queue = Promise.resolve();

// One push at a time, so quick successive cookie changes never overlap.
function pushCookie() {
  queue = queue.then(doPush, doPush);
  return queue;
}

async function doPush() {
  const config = await getConfig();
  if (!config.server || !config.token) {
    return setStatus("Not configured - open the extension options.", false);
  }
  const cookies = await chrome.cookies.getAll({ url: trimSlash(config.school) + "/" });
  if (!cookies.some((c) => c.name === "BakaAuth")) {
    return setStatus("Not logged in to the school website in this browser.", false);
  }
  const header = cookies.map((c) => `${c.name}=${c.value}`).join("; ");
  try {
    const response = await fetch(trimSlash(config.server) + "/cookie", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + config.token },
      body: JSON.stringify({ cookie: header }),
    });
    if (response.status === 401) return setStatus("The server rejected the token - check the options.", false);
    if (!response.ok) return setStatus(`The server answered HTTP ${response.status}.`, false);
    const body = await response.json().catch(() => ({}));
    const left = body.remaining_minutes != null ? `, school session has ${body.remaining_minutes} min left` : "";
    return setStatus(`Sent ${body.cookies ?? cookies.length} cookies to the server${left}.`, true);
  } catch (error) {
    return setStatus(`Cannot reach the server ${config.server} (${error.message}).`, false);
  }
}

async function tick(extend) {
  const config = await getConfig();
  if (extend && config.keepAlive) await extendSession(config);
  return pushCookie();
}

function schedule() {
  chrome.alarms.create("push", { periodInMinutes: PUSH_EVERY_MIN });
  chrome.alarms.create("extend", { periodInMinutes: EXTEND_EVERY_MIN });
}

chrome.runtime.onInstalled.addListener(() => {
  schedule();
  tick(false);
});
chrome.runtime.onStartup.addListener(() => {
  schedule();
  tick(false);
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "push") tick(false);
  if (alarm.name === "extend") tick(true);
});

// React quickly when the school renews or replaces the login cookie.
let debounce = null;
chrome.cookies.onChanged.addListener(async ({ cookie }) => {
  const config = await getConfig();
  const host = new URL(config.school).hostname;
  if (cookie.domain.replace(/^\./, "") !== host) return;
  clearTimeout(debounce);
  debounce = setTimeout(pushCookie, 2000);
});

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message && message.type === "push") {
    tick(true).then(sendResponse);
    return true; // async response
  }
  return false;
});
