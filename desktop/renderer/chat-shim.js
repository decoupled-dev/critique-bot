// The chat view (vendor/chat.js, shared with the VS Code extension) expects
// VS Code's webview API. Here it runs in an iframe, so its messages go to the
// parent page, which forwards them to the agent manager.
(function () {
  "use strict";
  const ORIGIN = "crit://app";
  window.acquireVsCodeApi = function () {
    return {
      postMessage(message) {
        window.parent.postMessage({ source: "crit-chat", message }, ORIGIN);
      },
      getState() {
        return undefined;
      },
      setState() {},
    };
  };
  // Only the parent page may talk to the chat view.
  window.addEventListener(
    "message",
    (event) => {
      if (event.source !== window.parent) event.stopImmediatePropagation();
    },
    true,
  );
})();
