#!/usr/bin/env python3
"""Serve the locally collected listings and their price history."""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sqlite3
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "reality.sqlite3"

PAGE = """<!doctype html>
<html lang="cs">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Byty — Frýdek-Místek a okolí</title>
  <style>
    :root { color-scheme: dark; font-family: system-ui, sans-serif; color: #e6edf3; background: #10161d; }
    body { margin: 0; }
    header { background: #172b3b; color: #f4f8fb; padding: 1.2rem max(1rem, calc((100% - 1200px) / 2)); border-bottom: 1px solid #2a3c4b; }
    header h1 { margin: 0 0 .25rem; font-size: 1.5rem; }
    main { max-width: 1200px; margin: 1.5rem auto; padding: 0 1rem; }
    .filters, .table-panel { background: #19232d; border: 1px solid #2b3946; border-radius: .65rem; padding: 1rem; margin-bottom: 1rem; box-shadow: 0 2px 10px #0003; }
    .filters { display: flex; flex-wrap: wrap; align-items: end; gap: .8rem 1rem; }
    label { display: grid; gap: .25rem; font-size: .9rem; }
    input, select { color: #e6edf3; background: #111a23; font: inherit; padding: .4rem; border: 1px solid #435260; border-radius: .35rem; }
    input::placeholder { color: #82909c; }
    input:focus, select:focus, button:focus-visible, a:focus-visible { outline: 2px solid #79c0ff; outline-offset: 2px; }
    .favorite-filter { display: flex; align-items: center; gap: .45rem; min-height: 2rem; }
    .favorite-filter input { width: 1rem; height: 1rem; accent-color: #f2cc60; }
    #status { margin-left: auto; color: #a6b3bf; font-size: .9rem; }
    .history-row[hidden] { display: none; }
    .history-chart { padding: .5rem 1rem 1rem; }
    .history-chart canvas { width: 100%; height: 220px; display: block; }
    .listing-links { display: flex; flex-wrap: wrap; align-items: center; gap: .25rem .75rem; }
    .table-wrap { overflow-x: auto; }
    table { border-collapse: collapse; width: 100%; font-size: .92rem; }
    th, td { padding: .65rem .5rem; text-align: left; vertical-align: top; border-bottom: 1px solid #2b3946; }
    th { white-space: nowrap; color: #a6b3bf; }
    tr.inactive { color: #8996a3; }
    a { color: #79c0ff; }
    a:hover { color: #b6e3ff; }
    .favorite-toggle { display: inline-grid; place-items: center; width: 2rem; height: 2rem; padding: 0; border: 1px solid #435260; border-radius: .4rem; color: #9aa8b5; background: #111a23; cursor: pointer; font-size: 1.25rem; line-height: 1; }
    .favorite-toggle[aria-pressed="true"] { color: #f2cc60; border-color: #806b2f; }
    .favorite-toggle:hover { background: #263441; }
    .photos { display: flex; gap: .25rem; }
    .photos img { width: 62px; height: 48px; object-fit: cover; border-radius: .25rem; }
    .desc { max-width: 25rem; white-space: pre-line; }
    @media (max-width: 640px) { .desc { max-width: 14rem; } #status { margin-left: 0; } }
  </style>
</head>
<body>
  <header><h1>Prodej bytů — Frýdek-Místek a okolí</h1><div>Lokální přehled inzerátů a vývoje nabídkových cen</div></header>
  <main>
    <section class="filters">
      <label>Dispozice<select id="layout"><option value="">Všechny dispozice</option></select></label>
      <label>Min. cena (Kč)<input id="min-price" type="number" min="0" placeholder="bez limitu"></label>
      <label>Max. cena (Kč)<input id="max-price" type="number" min="0" placeholder="bez limitu"></label>
      <label><span>Stav</span><select id="status-filter"><option value="">Aktivní i neaktivní</option><option value="active">Aktivní</option><option value="inactive">Neaktivní</option></select></label>
      <label>Řazení<select id="sort-order"><option value="default">Výchozí</option><option value="price-asc">Cena: od nejnižší</option><option value="price-desc">Cena: od nejvyšší</option></select></label>
      <label class="favorite-filter"><input id="favorites-only" type="checkbox"> Pouze oblíbené</label>
      <span id="status">Načítám…</span>
    </section>
    <section class="table-panel"><div class="table-wrap">
      <table><thead><tr><th>Fotografie</th><th>Inzerát</th><th>Lokalita</th><th>Cena</th><th>Historie</th><th>Stav</th></tr></thead><tbody id="listings"></tbody></table>
    </div></section>
  </main>
  <script>
    const money = n => n == null ? "Cena neuvedena" : new Intl.NumberFormat("cs-CZ").format(n) + " Kč";
    const FAVORITES_KEY = "realscrapper-favorites-v1";
    let allListings = [];
    const openHistories = new Set();
    let favoritesWarning = "";
    let favorites = loadFavorites();
    const $ = id => document.getElementById(id);
    function loadFavorites() {
      try {
        const saved = JSON.parse(localStorage.getItem(FAVORITES_KEY) || "[]");
        if (!Array.isArray(saved) || saved.some(id => typeof id !== "string")) {
          throw new Error("uložený seznam oblíbených nemá očekávaný formát");
        }
        return new Set(saved);
      } catch (error) {
        favoritesWarning = " · Oblíbené nelze načíst: " + error.message;
        return new Set();
      }
    }
    async function refresh() {
      const response = await fetch("/api/listings", {cache: "no-store"});
      if (!response.ok) throw new Error("Server vrátil HTTP " + response.status);
      allListings = await response.json();
      const layout = $("layout");
      const selectedLayout = layout.value;
      const dispositions = [...new Set(allListings.map(x => x.disposition).filter(Boolean))]
        .sort((a, b) => a.localeCompare(b, "cs"));
      layout.innerHTML = '<option value="">Všechny dispozice</option>' +
        dispositions.map(value => `<option value="${esc(value)}">${esc(value)}</option>`).join("");
      if (dispositions.includes(selectedLayout)) layout.value = selectedLayout;
      $("status").textContent = "Aktualizováno " + new Date().toLocaleTimeString("cs-CZ") + " · " + allListings.length + " záznamů" + favoritesWarning;
      render();
    }
    function filtered() {
      const min = Number($("min-price").value) || 0;
      const max = Number($("max-price").value) || Infinity;
      const rows = allListings.filter(x =>
        (!$("layout").value || x.disposition === $("layout").value) &&
        (!$("status-filter").value || x.status === $("status-filter").value) &&
        (!$("favorites-only").checked || favorites.has(String(x.property_id))) &&
        ((!min && max === Infinity) || (x.price_czk != null && x.price_czk >= min && x.price_czk <= max))
      );
      const sortOrder = $("sort-order").value;
      if (sortOrder === "price-asc" || sortOrder === "price-desc") {
        const direction = sortOrder === "price-asc" ? 1 : -1;
        rows.sort((a, b) => {
          if (a.price_czk == null) return b.price_czk == null ? 0 : 1;
          if (b.price_czk == null) return -1;
          return direction * (a.price_czk - b.price_czk);
        });
      }
      return rows;
    }
    function render() {
      const rows = filtered();
      const visibleIds = new Set(rows.map(x => String(x.property_id)));
      for (const id of openHistories) if (!visibleIds.has(id)) openHistories.delete(id);
      $("listings").innerHTML = rows.map(x => {
        const id = String(x.property_id);
        const historyId = "history-" + id;
        const expanded = openHistories.has(id);
        const isFavorite = favorites.has(id);
        const images = x.display_image_urls || x.image_urls;
        return `<tr class="${x.status}">
          <td><div class="photos">${images.slice(0, 2).map(src => `<img loading="lazy" src="${esc(src)}" alt="">`).join("")}</div></td>
          <td><div class="listing-links"><button class="favorite-toggle" type="button" data-id="${id}" aria-label="${isFavorite ? "Odebrat z oblíbených" : "Přidat do oblíbených"}" aria-pressed="${isFavorite}" title="${isFavorite ? "Odebrat z oblíbených" : "Přidat do oblíbených"}">${isFavorite ? "★" : "☆"}</button><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title || x.disposition)}</a><a class="history-link" href="#${historyId}" data-id="${id}" aria-expanded="${expanded}">${expanded ? "Skrýt historii" : "Historie cen"}</a></div><div>${esc(x.disposition)} · ${x.area_m2 == null ? "plocha neuvedena" : x.area_m2 + " m²"}</div><div>${(x.attributes || []).slice(0, 5).map(a => `${esc(a.name)}: ${esc(a.value)}`).join(" · ")}</div><div class="desc">${esc((x.description || "").slice(0, 280))}</div></td>
          <td>${esc(x.location)}</td><td>${money(x.price_czk)}</td>
          <td>${x.price_history.map((p, i) => `${i ? "<br>" : ""}${money(p.price_czk)}<br><small>${new Date(p.observed_at).toLocaleDateString("cs-CZ")}</small>`).join("")}</td>
          <td>${x.status === "active" ? "Aktivní" : "Neaktivní"}</td></tr>
          <tr id="${historyId}" class="history-row" ${expanded ? "" : "hidden"}><td colspan="6"><div class="history-chart"><strong>Vývoj ceny: ${esc(x.title || x.disposition)}</strong><canvas id="chart-${id}" aria-label="Graf historie ceny" role="img"></canvas></div></td></tr>`;
      }).join("");
      for (const id of openHistories) {
        const listing = rows.find(x => String(x.property_id) === id);
        const canvas = $("chart-" + id);
        if (listing && canvas) drawChart(canvas, listing);
      }
    }
    function esc(value) {
      return String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
    }
    function drawChart(canvas, listing) {
      const scale = window.devicePixelRatio || 1;
      const width = canvas.clientWidth, height = 240;
      canvas.width = width * scale; canvas.height = height * scale;
      const ctx = canvas.getContext("2d"); ctx.scale(scale, scale); ctx.clearRect(0, 0, width, height);
      const points = listing ? listing.price_history : [];
      if (points.length < 2) {
        ctx.fillStyle = "#526574"; ctx.font = "14px system-ui";
        ctx.fillText(points.length ? "Zatím je zaznamenána jen jedna cena." : "Pro tento inzerát zatím nejsou cenová data.", 12, 32);
        return;
      }
      const pad = {left: 78, right: 15, top: 16, bottom: 32};
      const values = points.map(p => p.price_czk), min = Math.min(...values), max = Math.max(...values);
      const span = max - min || max * .05 || 1;
      ctx.font = "12px system-ui"; ctx.strokeStyle = "#dbe2e7"; ctx.fillStyle = "#526574";
      for (let i = 0; i <= 4; i++) {
        const y = pad.top + (height - pad.top - pad.bottom) * i / 4;
        const value = max - span * i / 4;
        ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
        ctx.fillText(new Intl.NumberFormat("cs-CZ", {notation:"compact"}).format(value) + " Kč", 4, y + 4);
      }
      ctx.strokeStyle = "#1478ad"; ctx.lineWidth = 2; ctx.beginPath();
      points.forEach((point, i) => {
        const x = pad.left + (width - pad.left - pad.right) * i / (points.length - 1);
        const y = pad.top + (height - pad.top - pad.bottom) * (max - point.price_czk) / span;
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke(); ctx.fillStyle = "#1478ad";
      points.forEach((point, i) => {
        const x = pad.left + (width - pad.left - pad.right) * i / (points.length - 1);
        const y = pad.top + (height - pad.top - pad.bottom) * (max - point.price_czk) / span;
        ctx.beginPath(); ctx.arc(x, y, 4, 0, Math.PI * 2); ctx.fill();
      });
      ctx.fillStyle = "#526574";
      ctx.fillText(new Date(points[0].observed_at).toLocaleDateString("cs-CZ"), pad.left, height - 8);
      const last = new Date(points[points.length - 1].observed_at).toLocaleDateString("cs-CZ");
      ctx.fillText(last, width - pad.right - ctx.measureText(last).width, height - 8);
    }
    ["layout", "min-price", "max-price", "status-filter", "sort-order", "favorites-only"].forEach(id => $(id).addEventListener("input", render));
    $("listings").addEventListener("click", event => {
      const favoriteButton = event.target.closest("button.favorite-toggle");
      if (favoriteButton) {
        const id = favoriteButton.dataset.id;
        const wasFavorite = favorites.has(id);
        if (wasFavorite) favorites.delete(id); else favorites.add(id);
        try {
          localStorage.setItem(FAVORITES_KEY, JSON.stringify([...favorites]));
          favoritesWarning = "";
        } catch (error) {
          if (wasFavorite) favorites.add(id); else favorites.delete(id);
          favoritesWarning = " · Oblíbené se nepodařilo uložit: " + error.message;
          $("status").textContent = favoritesWarning.trim();
        }
        render();
        return;
      }
      const link = event.target.closest("a.history-link");
      if (!link) return;
      event.preventDefault();
      const id = link.dataset.id;
      if (openHistories.has(id)) openHistories.delete(id); else openHistories.add(id);
      render();
      const panel = $("history-" + id);
      if (panel && openHistories.has(id)) panel.scrollIntoView({behavior: "smooth", block: "nearest"});
    });
    window.addEventListener("resize", render);
    refresh().catch(error => $("status").textContent = "Chyba načítání: " + error.message);
    setInterval(() => refresh().catch(error => $("status").textContent = "Chyba načítání: " + error.message), 30000);
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    database = DEFAULT_DB

    def do_GET(self) -> None:
        route = urlsplit(self.path)
        if route.path == "/api/listings":
            self.send_listings()
        elif route.path == "/":
            self.send_body(PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif route.path.startswith("/images/"):
            self.send_image(unquote(route.path.removeprefix("/images/")))
        else:
            self.send_body(b"Not found", "text/plain; charset=utf-8", 404)

    def send_image(self, filename: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{64}\.(?:webp|jpe?g|png|gif)", filename):
            self.send_body(b"Not found", "text/plain; charset=utf-8", 404)
            return
        image_path = self.database.parent / "images" / filename
        try:
            image = image_path.read_bytes()
        except FileNotFoundError:
            self.send_body(b"Not found", "text/plain; charset=utf-8", 404)
            return
        except OSError as exc:
            self.send_body(
                f"Chyba při čtení obrázku: {exc}".encode("utf-8"),
                "text/plain; charset=utf-8",
                500,
            )
            return
        self.send_body(
            image,
            mimetypes.guess_type(filename)[0] or "application/octet-stream",
        )

    def send_listings(self) -> None:
        if not self.database.exists():
            self.send_body(
                json.dumps({"error": "Databáze zatím neexistuje; nejdřív spusťte scraper."}).encode(),
                "application/json; charset=utf-8",
                503,
            )
            return
        try:
            with closing(sqlite3.connect(self.database)) as db:
                db.row_factory = sqlite3.Row
                properties = db.execute(
                    "SELECT property_id, status, data_json FROM properties ORDER BY last_seen DESC"
                ).fetchall()
                response = []
                for row in properties:
                    listing = json.loads(row["data_json"])
                    listing["property_id"] = row["property_id"]
                    listing["status"] = row["status"]
                    listing["price_history"] = [
                        dict(item)
                        for item in db.execute(
                            """SELECT observed_at, price_czk FROM price_history
                               WHERE property_id = ? ORDER BY observed_at, history_id""",
                            (row["property_id"],),
                        )
                    ]
                    response.append(listing)
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            self.send_body(
                json.dumps({"error": f"Chyba při čtení databáze: {exc}"}).encode(),
                "application/json; charset=utf-8",
                500,
            )
            return
        self.send_body(
            json.dumps(response, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def send_body(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} - {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0", help="rozhraní (výchozí: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", type=Path, help="SQLite databáze (přepíše config.json)")
    args = parser.parse_args()
    database = args.db
    if database is None:
        config_path = ROOT / "config.json"
        if config_path.exists():
            try:
                configured_path = Path(
                    json.loads(config_path.read_text(encoding="utf-8")).get(
                        "database", str(DEFAULT_DB)
                    )
                )
            except (OSError, json.JSONDecodeError, AttributeError) as exc:
                parser.error(f"Nelze načíst databázi z {config_path}: {exc}")
            database = (
                configured_path
                if configured_path.is_absolute()
                else config_path.parent / configured_path
            )
        else:
            database = DEFAULT_DB
    Handler.database = database
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Web server běží na http://{args.host}:{args.port}/ (Ctrl+C ukončí)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nUkončuji server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
