"use strict";

const DEFAULTS = { school: "https://school.example.cz", server: "", token: "", keepAlive: true };
const $ = (id) => document.getElementById(id);

function show(text, ok) {
  $("msg").textContent = text;
  $("msg").className = ok ? "ok" : "bad";
}

async function load() {
  const config = { ...DEFAULTS, ...(await chrome.storage.local.get(Object.keys(DEFAULTS))) };
  $("server").value = config.server;
  $("token").value = config.token;
  $("school").value = config.school;
  $("keepAlive").checked = config.keepAlive;
}

$("save").addEventListener("click", async () => {
  let server;
  let school;
  try {
    server = new URL($("server").value.trim());
    school = new URL($("school").value.trim());
  } catch (_) {
    return show("Server address and school website must be full addresses like http://192.0.2.10:8765", false);
  }
  const token = $("token").value.trim();
  if (token.length < 16) return show("The token looks too short.", false);

  // Must be asked straight from the click. Ports are not part of match patterns.
  const origins = [`${server.protocol}//${server.hostname}/*`, `${school.origin}/*`];
  const granted = await chrome.permissions.request({ origins });
  if (!granted) return show("Permission to reach the server was refused, so nothing can be sent.", false);

  await chrome.storage.local.set({
    server: server.origin,
    token,
    school: school.origin,
    keepAlive: $("keepAlive").checked,
  });
  show("Saved. Sending the cookie now...", true);
  const status = await chrome.runtime.sendMessage({ type: "push" });
  show(`Saved. ${status ? status.text : "No answer from the background worker."}`, !!(status && status.ok));
});

load();
