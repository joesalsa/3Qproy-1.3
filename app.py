import os
import libsql
import requests
from datetime import datetime
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from apscheduler.schedulers.background import BackgroundScheduler
import uvicorn

# ==========================================
# CREDENCIALES (Variables de entorno en Render)
# ==========================================
INFER_API_KEY    = os.environ.get("INFER_API_KEY", "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TURSO_URL        = os.environ.get("TURSO_DATABASE_URL", "")
TURSO_TOKEN      = os.environ.get("TURSO_AUTH_TOKEN", "")

MULTIPLICADOR_Q3 = 1.3
MARGEN_MAXIMO    = 30

app = FastAPI(title="NBA Q3 Cierre Bot")

# ==========================================
# BASE DE DATOS (TURSO)
# ==========================================
def get_db():
    return libsql.connect(TURSO_URL, auth_token=TURSO_TOKEN)

def init_db():
    con = get_db()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS lineas_cierre_q3 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE,
                matchup TEXT,
                q3_total INTEGER,
                margen INTEGER,
                proyeccion REAL,
                linea_cierre REAL,
                cuota_over REAL,
                cuota_under REAL,
                timestamp TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS senales_cierre_q3 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE,
                matchup TEXT,
                decision TEXT,
                q3_total INTEGER,
                margen INTEGER,
                proyeccion REAL,
                linea_cierre REAL,
                cuota REAL,
                timestamp TEXT,
                status TEXT DEFAULT 'PENDING',
                final_total INTEGER
            )
        """)
        con.commit()
    finally:
        con.close()

def ya_tiene_senal(event_id):
    con = get_db()
    try:
        row = con.execute(
            "SELECT 1 FROM senales_cierre_q3 WHERE event_id = ?",
            (str(event_id),)
        ).fetchone()
        return row is not None
    finally:
        con.close()

def guardar_cierre_q3(event_id, matchup, q3_total, margen, proyeccion, linea, over_p, under_p):
    con = get_db()
    try:
        con.execute("""
            INSERT OR IGNORE INTO lineas_cierre_q3
            (event_id, matchup, q3_total, margen, proyeccion, linea_cierre, cuota_over, cuota_under, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(event_id), matchup, q3_total, margen, proyeccion,
            linea, over_p, under_p, datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ))
        con.commit()
    finally:
        con.close()

def guardar_senal_q3(event_id, matchup, decision, q3_total, margen, proyeccion, linea, cuota):
    con = get_db()
    try:
        con.execute("""
            INSERT OR IGNORE INTO senales_cierre_q3
            (event_id, matchup, decision, q3_total, margen, proyeccion, linea_cierre, cuota, timestamp, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
        """, (
            str(event_id), matchup, decision, q3_total, margen,
            proyeccion, linea, cuota, datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ))
        con.commit()
    finally:
        con.close()

# ==========================================
# TRADUCTOR DE EQUIPOS Y TELEGRAM
# ==========================================
def get_team_key(name):
    n = (name or "").lower()
    if "hawks" in n: return "atlanta"
    if "celtics" in n: return "boston"
    if "nets" in n: return "brooklyn"
    if "hornets" in n: return "charlotte"
    if "bulls" in n: return "chicago"
    if "cavaliers" in n or "cavs" in n: return "cleveland"
    if "mavericks" in n or "mavs" in n: return "dallas"
    if "nuggets" in n: return "denver"
    if "pistons" in n: return "detroit"
    if "warriors" in n or "warrior" in n or "gsw" in n: return "goldenstate"
    if "rockets" in n: return "houston"
    if "pacers" in n: return "indiana"
    if "clippers" in n or "lac" in n: return "clippers"
    if "lakers" in n or "lal" in n: return "lakers"
    if "grizzlies" in n: return "memphis"
    if "heat" in n: return "miami"
    if "bucks" in n: return "milwaukee"
    if "timberwolves" in n or "wolves" in n or "min" in n: return "minnesota"
    if "pelicans" in n or "nop" in n: return "neworleans"
    if "knicks" in n or "nyk" in n: return "newyork"
    if "thunder" in n or "okc" in n: return "oklahomacity"
    if "magic" in n or "orl" in n: return "orlando"
    if "76ers" in n or "sixers" in n or "phi" in n: return "philadelphia"
    if "suns" in n or "pho" in n: return "phoenix"
    if "blazers" in n or "trail blazers" in n or "por" in n: return "portland"
    if "kings" in n or "sac" in n: return "sacramento"
    if "spurs" in n or "sas" in n: return "sanantonio"
    if "raptors" in n or "tor" in n: return "toronto"
    if "jazz" in n or "uta" in n: return "utah"
    if "wizards" in n or "was" in n: return "washington"
    return n

def enviar_telegram(mensaje):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] Falta TELEGRAM_TOKEN o TELEGRAM_CHAT_ID.")
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "HTML"}
        r = requests.post(url, json=payload, timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[Telegram Error] {e}")
        return False

# ==========================================
# APIs: ESPN Y INFERSPORTS
# ==========================================
def get_espn_games():
    try:
        r = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
            timeout=15
        )
        out = []
        for ev in r.json().get("events", []):
            comp = ev.get("competitions", [{}])[0]
            cs = comp.get("competitors", [])
            if len(cs) < 2:
                continue
            home = next((c for c in cs if c.get("homeAway") == "home"), cs[0])
            away = next((c for c in cs if c.get("homeAway") == "away"), cs[1])
            q_h = [int(q.get("value", 0)) for q in home.get("linescores", [])]
            q_a = [int(q.get("value", 0)) for q in away.get("linescores", [])]
            st = ev.get("status", {})
            out.append({
                "id": str(ev.get("id")),
                "matchup": f"{away.get('team', {}).get('displayName')} vs {home.get('team', {}).get('displayName')}",
                "away_key": get_team_key(away.get("team", {}).get("displayName", "")),
                "home_key": get_team_key(home.get("team", {}).get("displayName", "")),
                "status": st.get("type", {}).get("description", ""),
                "state": st.get("type", {}).get("state", ""),
                "period": int(st.get("period", 0) or 0),
                "clock": st.get("displayClock", "") or "",
                "q_h": q_h,
                "q_a": q_a,
                "total": sum(q_h) + sum(q_a)
            })
        return out
    except Exception as e:
        print(f"[ESPN Error] {e}")
        return []

def get_infer_odds():
    if not INFER_API_KEY:
        return {}
    try:
        headers = {"User-Agent": "Mozilla/5.0", "Authorization": f"Bearer {INFER_API_KEY}"}
        r = requests.get("https://api.infersports.dev/v1/events/live", headers=headers, timeout=15)
        if r.status_code != 200:
            print(f"[InferSports Error] Status {r.status_code}")
            return {}

        events = [
            ev for ev in r.json().get("data", [])
            if ev.get("sport") == "basketball" and ev.get("league", {}).get("name") == "NBA"
        ]
        odds_dict = {}
        for ev in events:
            away_key = get_team_key(ev.get("away_team", {}).get("name", ""))
            home_key = get_team_key(ev.get("home_team", {}).get("name", ""))
            event_key = f"{away_key}_vs_{home_key}"

            r2 = requests.get(
                f"https://api.infersports.dev/v1/events/{ev['id']}/odds?format=decimal",
                headers=headers,
                timeout=15
            )
            if r2.status_code != 200:
                continue

            mejor_balance = 999.0
            mejor_odd = None
            for odd in r2.json().get("odds", []):
                if (
                    odd.get("market_type") == "totals"
                    and odd.get("period") == "full_time"
                    and odd.get("status") == "open"
                ):
                    try:
                        over_p = float(odd["prices"].get("over", 0))
                        under_p = float(odd["prices"].get("under", 0))
                        line_val = float(odd.get("line"))
                    except Exception:
                        continue
                    balance = abs(over_p - 2.0) + abs(under_p - 2.0)
                    if balance < mejor_balance:
                        mejor_balance = balance
                        mejor_odd = {"line": line_val, "over": over_p, "under": under_p}

            if mejor_odd:
                odds_dict[event_key] = mejor_odd
        return odds_dict
    except Exception as e:
        print(f"[InferSports Exception] {e}")
        return {}

# ==========================================
# MOTOR CIERRE Q3 -> SEÑAL OVER / UNDER
# ==========================================
def es_cierre_q3(g):
    return (
        "End of 3rd" in g["status"]
        or (g["period"] == 3 and g["clock"] in ["0.0", "0:00", "0.00", ""])
    )

def scan_cierre_q3():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔍 Buscando cierres de Q3...")
    games = get_espn_games()
    if not games:
        return

    candidatos = [g for g in games if es_cierre_q3(g) and not ya_tiene_senal(g["id"])]
    if not candidatos:
        return

    infer_odds = get_infer_odds()

    for g in candidatos:
        t3_home = sum(g["q_h"][:3]) if len(g["q_h"]) >= 3 else sum(g["q_h"])
        t3_away = sum(g["q_a"][:3]) if len(g["q_a"]) >= 3 else sum(g["q_a"])
        t3_total = t3_home + t3_away
        if t3_total == 0:
            continue

        margen = abs(t3_home - t3_away)
        if margen > MARGEN_MAXIMO:
            print(f"⏭️ {g['matchup']} omitido por margen ({margen} > {MARGEN_MAXIMO}).")
            continue

        event_key = f"{g['away_key']}_vs_{g['home_key']}"
        ln = infer_odds.get(event_key)
        if not ln:
            continue

        linea_cierre = ln["line"]
        over_p = ln["over"]
        under_p = ln["under"]
        proyeccion = t3_total * MULTIPLICADOR_Q3

        guardar_cierre_q3(
            g["id"], g["matchup"], t3_total, margen,
            proyeccion, linea_cierre, over_p, under_p
        )

        if proyeccion >= linea_cierre:
            decision = "OVER"
            cuota = over_p
            emoji_dir = "⬆️"
        else:
            decision = "UNDER"
            cuota = under_p
            emoji_dir = "⬇️"

        if not cuota or cuota <= 0:
            continue

        msg = (
            f"📡 <b>SEÑAL CIERRE Q3 — {decision}</b>\n\n"
            f"🏀 <b>{g['matchup']}</b>\n"
            f"⏱️ <b>Estado:</b> P{g['period']} ({g['clock']})\n\n"
            f"📊 <b>Puntos Q1-Q3:</b> {t3_total} pts (Dif: {margen})\n"
            f"📈 <b>Proyección (×{MULTIPLICADOR_Q3:.1f}):</b> {proyeccion:.1f} pts\n"
            f"📌 <b>Línea cierre Q3:</b> {linea_cierre:.1f}\n"
            f"🪙 <b>Cuota {decision}:</b> @{cuota:.2f}\n\n"
            f"💡 <b>Señal:</b> <b>{emoji_dir} {decision} {linea_cierre:.1f}</b>"
        )

        if enviar_telegram(msg):
            guardar_senal_q3(
                g["id"], g["matchup"], decision, t3_total,
                margen, proyeccion, linea_cierre, cuota
            )
            print(f"✅ Señal {decision} enviada: {g['matchup']} | Línea: {linea_cierre:.1f} @{cuota:.2f}")

def check_results():
    con = get_db()
    try:
        pending = con.execute(
            "SELECT id, event_id, matchup, decision, linea_cierre, cuota FROM senales_cierre_q3 WHERE status = 'PENDING'"
        ).fetchall()
    finally:
        con.close()

    if not pending:
        return

    games = get_espn_games()
    if not games:
        return

    by_id = {g["id"]: g for g in games}

    for row in pending:
        alert_id, event_id, matchup, decision, linea, cuota = row
        game = by_id.get(str(event_id))
        if not game or game.get("state") != "post":
            continue

        final_total = int(game.get("total", 0))
        linea_val = float(linea)

        if decision == "OVER":
            status = "WON" if final_total > linea_val else ("LOST" if final_total < linea_val else "PUSH")
        else:
            status = "WON" if final_total < linea_val else ("LOST" if final_total > linea_val else "PUSH")

        con = get_db()
        try:
            con.execute(
                "UPDATE senales_cierre_q3 SET status = ?, final_total = ? WHERE id = ?",
                (status, final_total, alert_id)
            )
            con.commit()
        finally:
            con.close()

        emoji = "✅" if status == "WON" else ("❌" if status == "LOST" else "⚖️")
        flecha = "⬆️" if decision == "OVER" else "⬇️"
        msg = (
            f"🏁 <b>RESULTADO SEÑAL #{alert_id}</b>\n\n"
            f"🏀 <b>{matchup}</b>\n"
            f"{flecha} <b>Jugada:</b> {decision} {linea_val:.1f} (@{float(cuota):.2f})\n"
            f"📊 <b>Total final:</b> {final_total} pts\n\n"
            f"{emoji} <b>Resultado:</b> {status}"
        )
        enviar_telegram(msg)

# ==========================================
# DASHBOARD WEB (ESTILO CLARO)
# ==========================================
@app.get("/", response_class=HTMLResponse)
def dashboard():
    try:
        con = get_db()
        try:
            rows = con.execute("""
                SELECT id, matchup, decision, q3_total, proyeccion, linea_cierre, cuota, final_total, status
                FROM senales_cierre_q3
                ORDER BY id DESC
            """).fetchall()
        finally:
            con.close()

        won = sum(1 for r in rows if r[8] == "WON")
        lost = sum(1 for r in rows if r[8] == "LOST")
        pending = sum(1 for r in rows if r[8] == "PENDING")
        settled = won + lost
        win_rate = (won / settled * 100) if settled > 0 else 0.0

        units = 0.0
        for r in rows:
            cuota_val = float(r[6] or 0.0)
            if r[8] == "WON":
                units += (cuota_val - 1.0)
            elif r[8] == "LOST":
                units -= 1.0

        wr_color = "#16a34a" if win_rate >= 50 else "#dc2626"
        units_color = "#16a34a" if units >= 0 else "#dc2626"

        filas_html = ""
        if not rows:
            filas_html = (
                "<tr><td colspan='8' style='padding: 24px; color: #64748b;'>"
                "No hay señales guardadas aún. Esperando cierres de Q3..."
                "</td></tr>"
            )
        else:
            for r in rows:
                _, matchup, decision, q3_pts, proy, linea, cuota, total_final, status = r
                badge_dir = (
                    "<span class='dir-over'>⬆️ OVER</span>"
                    if decision == "OVER"
                    else "<span class='dir-under'>⬇️ UNDER</span>"
                )
                if status == "WON":
                    badge_status = "<span class='badge won'>GANADA ✅</span>"
                elif status == "LOST":
                    badge_status = "<span class='badge lost'>PERDIDA ❌</span>"
                elif status == "PUSH":
                    badge_status = "<span class='badge push'>PUSH ⚖️</span>"
                else:
                    badge_status = "<span class='badge pending'>PENDIENTE ⏳</span>"

                filas_html += f"""
                <tr>
                    <td class='col-match'>{matchup}</td>
                    <td>{q3_pts}</td>
                    <td>{float(proy):.1f}</td>
                    <td class='col-line'>{float(linea):.1f}</td>
                    <td>{badge_dir}</td>
                    <td>@{float(cuota):.2f}</td>
                    <td>{total_final if total_final is not None else '-'}</td>
                    <td>{badge_status}</td>
                </tr>
                """

        return f"""
        <!DOCTYPE html>
        <html lang="es">
        <head>
            <meta charset="UTF-8">
            <title>Q3 Cierre Alerts</title>
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <style>
                * {{ box-sizing: border-box; }}
                body {{
                    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                    background-color: #f4f6f9;
                    color: #1e293b;
                    margin: 0;
                    padding: 28px 32px;
                }}
                .container {{ max-width: 1220px; margin: 0 auto; }}
                h1 {{
                    margin: 0 0 22px 0;
                    font-size: 28px;
                    font-weight: 800;
                    color: #0f172a;
                    display: flex;
                    align-items: center;
                    gap: 10px;
                }}
                .cards {{
                    display: grid;
                    grid-template-columns: repeat(5, 1fr);
                    gap: 16px;
                    margin-bottom: 24px;
                }}
                @media (max-width: 900px) {{
                    .cards {{ grid-template-columns: repeat(2, 1fr); }}
                }}
                @media (max-width: 520px) {{
                    .cards {{ grid-template-columns: 1fr; }}
                }}
                .card {{
                    background: #ffffff;
                    border-radius: 10px;
                    padding: 20px 16px;
                    text-align: center;
                    box-shadow: 0 2px 8px rgba(15, 23, 42, 0.05);
                }}
                .card-title {{
                    font-size: 12px;
                    font-weight: 700;
                    letter-spacing: 0.6px;
                    text-transform: uppercase;
                    color: #64748b;
                    margin-bottom: 10px;
                }}
                .card-val {{
                    font-size: 32px;
                    font-weight: 800;
                    line-height: 1.1;
                }}
                .table-wrap {{
                    background: #ffffff;
                    border-radius: 10px;
                    overflow-x: auto;
                    box-shadow: 0 2px 8px rgba(15, 23, 42, 0.05);
                }}
                table {{
                    width: 100%;
                    border-collapse: collapse;
                    min-width: 760px;
                }}
                th {{
                    padding: 15px 14px;
                    font-size: 11.5px;
                    font-weight: 700;
                    letter-spacing: 0.5px;
                    text-transform: uppercase;
                    color: #64748b;
                    background: #ffffff;
                    border-bottom: 1px solid #e2e8f0;
                    text-align: center;
                }}
                th.col-match, td.col-match {{ text-align: left; padding-left: 20px; }}
                td {{
                    padding: 15px 14px;
                    border-bottom: 1px solid #f1f5f9;
                    text-align: center;
                    font-size: 14px;
                    color: #334155;
                }}
                tr:last-child td {{ border-bottom: none; }}
                tr:hover td {{ background: #f8fafc; }}
                .col-match {{ font-weight: 600; color: #0f172a; }}
                .col-line {{ font-weight: 700; color: #2563eb; }}
                .dir-over {{
                    font-size: 12px;
                    font-weight: 700;
                    color: #15803d;
                    background: #dcfce7;
                    padding: 4px 8px;
                    border-radius: 5px;
                }}
                .dir-under {{
                    font-size: 12px;
                    font-weight: 700;
                    color: #b91c1c;
                    background: #fee2e2;
                    padding: 4px 8px;
                    border-radius: 5px;
                }}
                .badge {{
                    display: inline-block;
                    font-size: 11.5px;
                    font-weight: 800;
                    padding: 5px 10px;
                    border-radius: 5px;
                    letter-spacing: 0.3px;
                }}
                .won {{ background: #bbf7d0; color: #15803d; }}
                .lost {{ background: #fecaca; color: #b91c1c; }}
                .pending {{ background: #fef08a; color: #a16207; }}
                .push {{ background: #e2e8f0; color: #475569; }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>🎯 Q3 Cierre Alerts</h1>

                <div class="cards">
                    <div class="card" style="border-top: 4px solid {wr_color};">
                        <div class="card-title">Win Rate</div>
                        <div class="card-val" style="color: {wr_color};">{win_rate:.1f}%</div>
                    </div>
                    <div class="card" style="border-top: 4px solid {units_color};">
                        <div class="card-title">Unidades</div>
                        <div class="card-val" style="color: {units_color};">{units:+.2f} u</div>
                    </div>
                    <div class="card" style="border-top: 4px solid #16a34a;">
                        <div class="card-title">Ganadas</div>
                        <div class="card-val" style="color: #16a34a;">{won}</div>
                    </div>
                    <div class="card" style="border-top: 4px solid #dc2626;">
                        <div class="card-title">Perdidas</div>
                        <div class="card-val" style="color: #dc2626;">{lost}</div>
                    </div>
                    <div class="card" style="border-top: 4px solid #eab308;">
                        <div class="card-title">Pendientes</div>
                        <div class="card-val" style="color: #eab308;">{pending}</div>
                    </div>
                </div>

                <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th class="col-match">Partido</th>
                                <th>Pts Q3</th>
                                <th>Proyección (×1.3)</th>
                                <th>Línea Cierre</th>
                                <th>Señal</th>
                                <th>Cuota</th>
                                <th>Total Final</th>
                                <th>Estado</th>
                            </tr>
                        </thead>
                        <tbody>
                            {filas_html}
                        </tbody>
                    </table>
                </div>
            </div>
        </body>
        </html>
        """
    except Exception as e:
        return f"<h1>Error cargando dashboard</h1><p>{e}</p>"

# ==========================================
# ARRANQUE EN RENDER
# ==========================================
@app.on_event("startup")
def startup_event():
    init_db()
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(scan_cierre_q3, "interval", seconds=45)
    scheduler.add_job(check_results, "interval", minutes=5)
    scheduler.start()
    print("🚀 NBA Q3 Cierre Bot iniciado en Render.")

@app.get("/health")
@app.head("/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
