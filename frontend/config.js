(function () {
  var apiBase = window.location.origin;
  if (window.location.hostname === "easyprogrammer.github.io") {
    apiBase = "https://47.106.167.94.sslip.io";
  }
  window.TYCA_TOOL_CONFIG = { apiBase: apiBase };
})();
