"use strict";

const $ = (id) => document.getElementById(id);

function render(status) {
  if (!status) {
    $("status").textContent = "Nothing sent yet.";
    $("status").className = "";
    $("when").textContent = "";
    return;
  }
  $("status").textContent = status.text;
  $("status").className = status.ok ? "ok" : "bad";
  $("when").textContent = "Last check: " + new Date(status.at).toLocaleTimeString();
}

chrome.storage.local.get("status").then(({ status }) => render(status));

$("push").addEventListener("click", async () => {
  $("status").textContent = "Sending...";
  const result = await chrome.runtime.sendMessage({ type: "push" });
  const { status } = await chrome.storage.local.get("status");
  render(status || result);
});

$("options").addEventListener("click", () => chrome.runtime.openOptionsPage());
