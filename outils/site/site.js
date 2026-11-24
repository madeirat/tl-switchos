// TL-SwitchOS - site vitrine : bouton "Copier" du SHA-256.
document.addEventListener("click", function (ev) {
  var b = ev.target.closest(".copier");
  if (!b) return;
  var txt = b.getAttribute("data-copie") || "";
  var fini = function () {
    b.textContent = "Copié";
    b.classList.add("ok");
    setTimeout(function () { b.textContent = "Copier"; b.classList.remove("ok"); }, 1600);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(txt).then(fini, function () {});
  } else {
    // page servie en http : copie par selection
    var t = document.createElement("textarea");
    t.value = txt; t.setAttribute("readonly", ""); t.style.position = "fixed"; t.style.opacity = "0";
    document.body.appendChild(t); t.select();
    try { document.execCommand("copy"); fini(); } catch (e) {}
    document.body.removeChild(t);
  }
});

// page Commandes : filtre en direct
(function () {
  var f = document.getElementById("filtre");
  if (!f) return;
  f.addEventListener("input", function () {
    var q = f.value.trim().toLowerCase();
    document.querySelectorAll(".commandes").forEach(function (tab) {
      var vus = 0;
      tab.querySelectorAll("tr").forEach(function (tr, i) {
        if (i === 0) return;
        var ok = !q || tr.textContent.toLowerCase().indexOf(q) >= 0;
        tr.style.display = ok ? "" : "none";
        if (ok) vus++;
      });
      tab.closest(".doc-groupe").style.display = vus ? "" : "none";
    });
  });
})();
