/**
 * overlay.js — numbers [data-cf-change] blocks and builds a floating
 * "Changes (N)" chip that lets you jump to each edited region.
 *
 * Usage:
 *   Drop this script before </body> on any page that uses overlay.css.
 *   Claude marks an edited region with:
 *     <span data-cf-change="ch-your-slug">…edited content…</span>
 *   The slug becomes the jump label in the chip (hyphens → spaces, first
 *   char uppercased). Keep slugs short and kebab-case:
 *     ch-section-rewritten, ch-table-clarified
 *
 * No dependencies. Works alongside make-pages-interactive's own feedback
 * styles without touching them.
 *
 * License: MIT (same as the make-pages-interactive repository).
 */
(function () {
  "use strict";

  function build() {
    var changes = Array.from(document.querySelectorAll("[data-cf-change]"));
    if (!changes.length) return;

    // Number each block and ensure it has a stable id for deep-linking.
    changes.forEach(function (el, i) {
      el.setAttribute("data-cf-num", String(i + 1));
      if (!el.id) el.id = el.getAttribute("data-cf-change");
    });

    // Build the floating chip.
    var chip = document.createElement("div");
    chip.id = "changes-chip";

    var hdr = document.createElement("div");
    hdr.id = "changes-chip-header";
    hdr.textContent = "Changes (" + changes.length + ")";
    hdr.addEventListener("click", function () {
      chip.classList.toggle("open");
    });

    var list = document.createElement("div");
    list.id = "changes-chip-list";

    changes.forEach(function (el, i) {
      var slug = el.getAttribute("data-cf-change") || "";
      var title = slug.replace(/^ch-/, "").replace(/-/g, " ");
      title = title.charAt(0).toUpperCase() + title.slice(1);

      var a = document.createElement("a");
      a.href = "#" + slug;
      a.innerHTML = '<span class="num">' + (i + 1) + "</span>" + title;

      a.addEventListener("click", function (e) {
        e.preventDefault();
        el.scrollIntoView({ behavior: "smooth", block: "center" });
        // Brief highlight flash.
        var orig = el.style.background;
        el.style.transition = "background .2s";
        el.style.background = "rgba(255,211,61,.35)";
        setTimeout(function () {
          el.style.background = orig;
        }, 800);
        chip.classList.remove("open");
      });

      list.appendChild(a);
    });

    chip.appendChild(hdr);
    chip.appendChild(list);
    document.body.appendChild(chip);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", build);
  } else {
    build();
  }
}());
