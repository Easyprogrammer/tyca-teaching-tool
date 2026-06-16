(function () {
  var targetOrigin = "https://47.106.167.94.sslip.io";
  if (window.location.hostname === "easyprogrammer.github.io") {
    var path = window.location.pathname.replace(/^\/tyca-teaching-tool/, "") || "/";
    window.location.replace(targetOrigin + path + window.location.search + window.location.hash);
  }
})();
