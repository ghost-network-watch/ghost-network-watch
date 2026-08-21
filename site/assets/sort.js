// Click a header to sort tables marked class="sortable". No dependencies.
document.querySelectorAll("table.sortable").forEach(function (table) {
  var headers = table.querySelectorAll("thead th");
  headers.forEach(function (th, col) {
    if (!th.textContent.trim()) return;
    th.style.cursor = "pointer";
    th.setAttribute("role", "button");
    th.setAttribute("title", (th.getAttribute("title") ? th.getAttribute("title") + ". " : "") + "Click to sort");
    th.addEventListener("click", function () {
      var tbody = table.querySelector("tbody");
      var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr"));
      var dir = th.dataset.dir === "asc" ? -1 : 1;
      headers.forEach(function (h) { delete h.dataset.dir; });
      th.dataset.dir = dir === 1 ? "asc" : "desc";
      function key(row) {
        var cell = row.children[col];
        if (!cell) return "";
        var text = cell.textContent.trim().replace(/[%,]/g, "");
        var grade = cell.querySelector(".grade");
        if (grade) {
          var order = { A: 1, B: 2, C: 3, D: 4, F: 5, X: 6 };
          if (order[grade.textContent.trim()]) return order[grade.textContent.trim()];
        }
        var num = parseFloat(text);
        return isNaN(num) ? text.toLowerCase() : num;
      }
      rows.sort(function (a, b) {
        var ka = key(a), kb = key(b);
        if (typeof ka === "number" && typeof kb === "number") return dir * (ka - kb);
        return dir * String(ka).localeCompare(String(kb));
      });
      rows.forEach(function (r) { tbody.appendChild(r); });
    });
  });
});
