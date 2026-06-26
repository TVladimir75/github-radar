#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sqlite3, json
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.db")
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
TOP_N = 12


def load_data():
    if not os.path.exists(DB_PATH):
        return None
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT repo, seen_at, stars, language, url FROM snapshots ORDER BY seen_at").fetchall()
    series, meta, all_dates = {}, {}, set()
    for repo, d, stars, lang, url in rows:
        series.setdefault(repo, {})[d] = stars
        meta[repo] = {"language": lang or "-", "url": url}
        all_dates.add(d)
    dates = sorted(all_dates)
    growth = []
    for repo, by_date in series.items():
        vals = [by_date[d] for d in sorted(by_date)]
        delta = vals[-1] - vals[0] if len(vals) > 1 else 0
        growth.append((delta, vals[-1], repo))
    growth.sort(reverse=True)
    top_repos = [repo for _, _, repo in growth[:TOP_N]]
    obs = con.execute("SELECT seen_at, repo, note FROM observations ORDER BY seen_at DESC").fetchall()
    con.close()
    return {"dates": dates, "series": series, "meta": meta, "top_repos": top_repos, "growth": growth, "obs": obs}


def build_html(data):
    if not data or not data["dates"]:
        return "<html><body style='font-family:sans-serif;padding:40px'><h2>Данных пока нет</h2><p>Запусти радар хотя бы раз.</p></body></html>"
    dates = data["dates"]
    datasets = []
    for repo in data["top_repos"]:
        by_date = data["series"][repo]
        datasets.append({"label": repo, "data": [by_date.get(d) for d in dates]})
    rows_html = ""
    for delta, last, repo in data["growth"][:25]:
        m = data["meta"].get(repo, {})
        sign = f"+{delta}" if delta >= 0 else str(delta)
        rows_html += (f"<tr><td><a href='{m.get('url','#')}' target='_blank'>{repo}</a></td>"
            f"<td>{m.get('language','-')}</td><td style='text-align:right'>{last}</td>"
            f"<td style='text-align:right;color:#2da44e'>{sign}</td></tr>")
    obs_html = ""
    if data["obs"]:
        cur = None
        for d, repo, note in data["obs"]:
            if d != cur:
                obs_html += f"<div class='obs-date'>{d}</div>"; cur = d
            obs_html += f"<div class='obs-item'><span class='obs-repo'>{repo}</span> <span class='obs-note'>{note}</span></div>"
    else:
        obs_html = "<p style='color:#888'>Наблюдения появятся после нескольких запусков.</p>"
    generated = datetime.now().strftime("%d.%m.%Y %H:%M")
    return f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>GitHub Radar</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
body{{font-family:-apple-system,sans-serif;background:#0d1117;color:#e6edf3;margin:0;padding:24px}}
h1{{font-size:22px;margin:0 0 4px}} .sub{{color:#8b949e;font-size:13px;margin-bottom:24px}}
.card{{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:20px;margin-bottom:20px}}
.card h2{{font-size:16px;margin:0 0 16px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{padding:8px 10px;border-bottom:1px solid #21262d}} th{{text-align:left;color:#8b949e}}
a{{color:#58a6ff;text-decoration:none}} a:hover{{text-decoration:underline}}
.obs-date{{color:#8b949e;font-size:12px;margin:14px 0 6px;font-weight:600}}
.obs-item{{padding:6px 0;border-bottom:1px solid #21262d;font-size:13px}}
.obs-repo{{color:#58a6ff;font-weight:600}} .obs-note{{color:#c9d1d9}}
canvas{{max-height:380px}}
</style></head><body>
<h1>📡 GitHub Radar</h1>
<div class="sub">Обновлено: {generated} · дней истории: {len(dates)}</div>
<div class="card"><h2>📈 Рост звёзд (топ по приросту)</h2><canvas id="growthChart"></canvas></div>
<div class="card"><h2>🎯 Наблюдения Claude по дням</h2>{obs_html}</div>
<div class="card"><h2>🔥 Топ по приросту</h2><table>
<tr><th>Проект</th><th>Язык</th><th style="text-align:right">Звёзд</th><th style="text-align:right">Прирост</th></tr>
{rows_html}</table></div>
<script>
const labels={json.dumps(dates,ensure_ascii=False)};
const datasets={json.dumps(datasets,ensure_ascii=False)};
const palette=['#58a6ff','#2da44e','#f78166','#d2a8ff','#ffa657','#79c0ff','#56d364','#ff7b72','#bc8cff','#e3b341','#a5d6ff','#7ee787'];
new Chart(document.getElementById('growthChart'),{{type:'line',data:{{labels:labels,
datasets:datasets.map((ds,i)=>({{label:ds.label,data:ds.data,borderColor:palette[i%palette.length],
backgroundColor:palette[i%palette.length],tension:0.3,spanGaps:true,pointRadius:3,borderWidth:2}}))}},
options:{{responsive:true,plugins:{{legend:{{labels:{{color:'#8b949e',boxWidth:12,font:{{size:11}}}}}}}},
scales:{{x:{{ticks:{{color:'#8b949e'}},grid:{{color:'#21262d'}}}},y:{{ticks:{{color:'#8b949e'}},grid:{{color:'#21262d'}}}}}}}}}});
</script></body></html>"""


def main():
    data = load_data()
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(build_html(data))
    print(f"[ok] Дашборд создан: {OUT_PATH}")
    if data and data["dates"]:
        print(f"     Дней истории: {len(data['dates'])} | проектов: {len(data['series'])}")


if __name__ == "__main__":
    main()
