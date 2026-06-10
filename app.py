"""
app.py — Painel de Análise Pré-Jogo | Copa 2026
streamlit run app.py
"""

import asyncio
import queue
import sys
import threading

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import coletor
from jogadores import (
    collect_player_stats,
    fetch_team_players,
    _ALL_COPA_TEAMS,
    _NUM_COLS as _PLAYER_NUM_COLS,
    _POS_LABEL,
    _load_cache as _load_jog_cache,
)

# ── Playwright worker ─────────────────────────────────────────────────────────

class _PlaywrightWorker:
    def __init__(self):
        self._q = queue.Queue()
        self._session = None
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self):
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        while True:
            item = self._q.get()
            if item is None:
                break
            fn, rq = item
            try:
                rq.put(("ok", fn()))
            except Exception as exc:
                rq.put(("err", exc))

    def _run(self, fn):
        rq = queue.Queue()
        self._q.put((fn, rq))
        status, value = rq.get()
        if status == "err":
            raise value
        return value

    @property
    def ready(self):
        return self._session is not None

    def start_session(self):
        def _s():
            self._session = coletor.BrowserSession()
        self._run(_s)

    def stop_session(self):
        def _stop():
            if self._session:
                self._session.close()
                self._session = None
        self._run(_stop)

    def get_json(self, path):
        sess = self._session
        return self._run(lambda: sess.get_json(path))


class _WorkerSession:
    def __init__(self, w):
        self._w = w
    def get_json(self, path):
        return self._w.get_json(path)
    def close(self):
        pass


# ── Configuração ──────────────────────────────────────────────────────────────

st.set_page_config(page_title="Copa 2026 Stats", page_icon="⚽", layout="wide")

for k, v in {"worker": None, "team_data": [], "player_data": [],
             "roster_cache": {}, "caches_loaded": False}.items():
    if k not in st.session_state:
        st.session_state[k] = v

if not st.session_state.caches_loaded:
    coletor._load_cache()
    _load_jog_cache()
    st.session_state.caches_loaded = True

# ── Constantes ────────────────────────────────────────────────────────────────

_RES_COLORS = {"Vitória": "#C6EFCE", "Empate": "#FFEB9C", "Derrota": "#FFC7CE"}

_FRIENDLY_KW = ["friendly", "amistoso", "int. friendly", "international friendly", "test match"]

_STRONG = {
    "brazil", "brasil", "argentina", "france", "franca", "spain", "espanha",
    "england", "inglaterra", "portugal", "germany", "alemanha", "netherlands",
    "paises baixos", "italy", "italia", "belgium", "belgica", "uruguay", "uruguai",
}
_MEDIUM = {
    "usa", "estados unidos", "switzerland", "suica", "colombia", "croatia", "croacia",
    "mexico", "denmark", "dinamarca", "japan", "japao", "morocco", "marrocos",
    "senegal", "austria", "poland", "polonia", "sweden", "suecia", "norway", "noruega",
    "turkey", "turquia", "south korea", "coreia do sul", "ukraine", "ucrania",
}

_CHART_BASE = dict(
    height=230, margin=dict(l=0, r=0, t=30, b=30),
    plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
    font=dict(color="#ddd", size=11),
    legend=dict(orientation="h", y=-0.25, font=dict(size=10)),
    yaxis=dict(gridcolor="rgba(255,255,255,0.07)", zeroline=False),
    xaxis=dict(gridcolor="rgba(255,255,255,0.07)"),
)


# ── Funções de domínio ────────────────────────────────────────────────────────

def _norm(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", s.lower())
                   if unicodedata.category(c) != "Mn")


def _is_friendly(comp: str) -> bool:
    return any(k in comp.lower() for k in _FRIENDLY_KW)


def _opp_strength(name: str) -> str:
    n = _norm(name)
    if any(s in n for s in _STRONG): return "Forte"
    if any(s in n for s in _MEDIUM): return "Médio"
    return "Fraco"


def _add_cols(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c1, c2, out in [
        ("gols_feitos",    "gols_sofridos",      "gols_totais"),
        ("finalizacoes",   "adv_finalizacoes",    "finalizacoes_totais"),
        ("chutes_no_gol",  "adv_chutes_no_gol",   "chutes_totais"),
        ("escanteios",     "adv_escanteios",      "escanteios_totais"),
        ("cartoes_amarelos", "adv_cartoes_amarelos", "ca_totais"),
    ]:
        if c1 in df.columns and c2 in df.columns:
            df[out] = pd.to_numeric(df[c1], errors="coerce") + pd.to_numeric(df[c2], errors="coerce")
    if "adversario" in df.columns:
        df["forca_adv"] = df["adversario"].apply(_opp_strength)
    if "competicao" in df.columns:
        df["tipo_jogo"] = df["competicao"].apply(
            lambda c: "Amistoso" if _is_friendly(c) else "Oficial"
        )
    return df


def _apply_filters(df: pd.DataFrame, tipo: str, local: str, forca: str) -> pd.DataFrame:
    f = df.copy()
    if tipo  != "Todos" and "tipo_jogo" in f.columns: f = f[f["tipo_jogo"] == tipo]
    if local != "Todos" and "local"     in f.columns: f = f[f["local"]     == local]
    if forca != "Todos" and "forca_adv" in f.columns: f = f[f["forca_adv"] == forca]
    return f


def _col(df: pd.DataFrame, col: str) -> pd.Series:
    return pd.to_numeric(df.get(col, pd.Series(dtype=float)), errors="coerce").dropna()


def _mean(df: pd.DataFrame, col: str):
    s = _col(df, col)
    return float(s.mean()) if len(s) else None


def _freq(df: pd.DataFrame, col: str, thr: float):
    s = _col(df, col)
    return (s > thr).mean() * 100 if len(s) else None


def _wavg(df_or_series) -> float:
    s = (df_or_series if isinstance(df_or_series, pd.Series)
         else pd.Series(dtype=float))
    s = pd.to_numeric(s, errors="coerce").dropna().values
    n = len(s)
    if n == 0:
        return float("nan")
    w = list(range(n, 0, -1))
    return float(sum(v * wi for v, wi in zip(s, w)) / sum(w))


def _compute_lines(df: pd.DataFrame) -> list:
    lines = []

    def chk(label, col, thr, cat):
        s = _col(df, col)
        if not len(s): return
        cnt = int((s > thr).sum())
        pct = cnt / len(s) * 100
        st_ = "forte" if pct >= 80 else ("media" if pct >= 60 else "fraca")
        lines.append({"label": label, "count": cnt, "total": len(s),
                       "pct": pct, "strength": st_, "category": cat})

    chk("Time marca 0.5+ gols",   "gols_feitos",   0.5, "Gols")
    chk("Time marca 1.5+ gols",   "gols_feitos",   1.5, "Gols")
    chk("Time marca 2.5+ gols",   "gols_feitos",   2.5, "Gols")
    chk("Time sofre 0.5+ gols",   "gols_sofridos", 0.5, "Gols")
    chk("Time sofre 1.5+ gols",   "gols_sofridos", 1.5, "Gols")
    chk("Over 1.5 gols totais",   "gols_totais",   1.5, "Gols")
    chk("Over 2.5 gols totais",   "gols_totais",   2.5, "Gols")
    chk("Over 3.5 gols totais",   "gols_totais",   3.5, "Gols")

    gf = pd.to_numeric(df.get("gols_feitos",   pd.Series(dtype=float)), errors="coerce")
    gs = pd.to_numeric(df.get("gols_sofridos", pd.Series(dtype=float)), errors="coerce")
    ok = gf.notna() & gs.notna()
    if ok.sum():
        cnt = int(((gf[ok] >= 1) & (gs[ok] >= 1)).sum())
        tot = int(ok.sum())
        pct = cnt / tot * 100
        lines.append({"label": "Ambas marcam", "count": cnt, "total": tot, "pct": pct,
                       "strength": "forte" if pct >= 80 else ("media" if pct >= 60 else "fraca"),
                       "category": "Gols"})

    chk("Time over 3.5 escanteios",  "escanteios",        3.5, "Escanteios")
    chk("Time over 4.5 escanteios",  "escanteios",        4.5, "Escanteios")
    chk("Time over 5.5 escanteios",  "escanteios",        5.5, "Escanteios")
    chk("Jogo over 8.5 escanteios",  "escanteios_totais", 8.5, "Escanteios")
    chk("Jogo over 9.5 escanteios",  "escanteios_totais", 9.5, "Escanteios")
    chk("Jogo over 10.5 escanteios", "escanteios_totais", 10.5, "Escanteios")

    chk("Time over 10.5 finalizações", "finalizacoes",  10.5, "Finalizações")
    chk("Time over 12.5 finalizações", "finalizacoes",  12.5, "Finalizações")
    chk("Time over 14.5 finalizações", "finalizacoes",  14.5, "Finalizações")
    chk("Time over 3.5 chutes no gol", "chutes_no_gol",  3.5, "Finalizações")
    chk("Time over 4.5 chutes no gol", "chutes_no_gol",  4.5, "Finalizações")
    chk("Time over 5.5 chutes no gol", "chutes_no_gol",  5.5, "Finalizações")

    chk("Time over 0.5 cartão amarelo",  "cartoes_amarelos", 0.5, "Cartões")
    chk("Time over 1.5 cartões amarelos","cartoes_amarelos", 1.5, "Cartões")
    chk("Jogo over 2.5 CA totais",       "ca_totais",        2.5, "Cartões")
    chk("Jogo over 3.5 CA totais",       "ca_totais",        3.5, "Cartões")
    chk("Jogo over 4.5 CA totais",       "ca_totais",        4.5, "Cartões")

    return lines


# ── Seções de exibição ────────────────────────────────────────────────────────

def _fmt(val) -> str:
    """Formata valor numérico para exibição (sem casa decimal quando inteiro)."""
    if val is None:
        return "—"
    try:
        f = float(val)
        if pd.isna(f):
            return "—"
        return str(int(f)) if f == int(f) else f"{f:.1f}"
    except Exception:
        return str(val)


def _show_overview(df: pd.DataFrame):
    # ── 1. Cards de médias ────────────────────────────────────────────────────
    def card(cont, label, col, thrs=None):
        s = _col(df, col)
        if not len(s):
            cont.metric(label, "—"); return
        mean_v = float(s.mean())
        w      = _wavg(s)
        tip    = ("\n".join(f"{int((s>t).sum())}/{len(s)} jogos > {t}" for t in thrs)
                  if thrs else None)
        cont.metric(label, f"{mean_v:.1f}", f"pond. {w:.1f}", help=tip)

    c1, c2, c3, c4 = st.columns(4)
    card(c1, "Gols Feitos",    "gols_feitos",   [0.5, 1.5, 2.5])
    card(c2, "Gols Sofridos",  "gols_sofridos", [0.5, 1.5])
    card(c3, "Gols Totais",    "gols_totais",   [1.5, 2.5, 3.5])

    gf = pd.to_numeric(df.get("gols_feitos",   pd.Series(dtype=float)), errors="coerce")
    gs = pd.to_numeric(df.get("gols_sofridos", pd.Series(dtype=float)), errors="coerce")
    ok = gf.notna() & gs.notna()
    if ok.sum():
        am = int(((gf[ok] >= 1) & (gs[ok] >= 1)).sum())
        c4.metric("Ambas Marcam", f"{am/ok.sum()*100:.0f}%", f"{am}/{int(ok.sum())} jogos")

    c5, c6, c7, c8 = st.columns(4)
    card(c5, "Finalizações",      "finalizacoes",      [10.5, 12.5])
    card(c6, "Escanteios (time)", "escanteios",        [4.5, 5.5])
    card(c7, "Escanteios (jogo)", "escanteios_totais", [8.5, 9.5])
    card(c8, "Cart. Amarelos",    "cartoes_amarelos",  [0.5, 1.5])

    st.divider()

    # ── 2. Confrontos recentes ────────────────────────────────────────────────
    st.markdown(f"#### Últimos {len(df)} Jogos")

    _RES_STYLE = {
        "Vitória": ("#166534", "#86efac", "#0d3b0d", "#22c55e"),
        "Empate":  ("#713f12", "#fde68a", "#422006", "#eab308"),
        "Derrota": ("#7f1d1d", "#fca5a5", "#3b0a0a", "#ef4444"),
    }

    cards_html = '<div style="display:flex;flex-direction:column;gap:8px;margin-bottom:4px">'
    for _, row in df.iterrows():
        res = str(row.get("resultado", ""))
        txt_c, badge_c, bg_c, border_c = _RES_STYLE.get(
            res, ("#94a3b8", "#cbd5e1", "#1e293b", "#334155")
        )
        adv   = row.get("adversario", "?")
        placar = row.get("placar", "?")
        local  = row.get("local", "?")
        data   = row.get("data", "?")
        comp   = row.get("competicao", "?")
        forca  = row.get("forca_adv", "")

        forca_badge = ""
        if forca:
            fc = {"Forte": "#7f1d1d", "Médio": "#1e3a5f", "Fraco": "#14532d"}.get(forca, "#334155")
            forca_badge = f'<span style="background:{fc};color:#e2e8f0;border-radius:3px;padding:1px 6px;font-size:.7em;margin-left:6px">{forca}</span>'

        # Estatísticas por seção (TIME = azul, ADVERSÁRIO = laranja, TOTAIS = cinza)
        def t_stat(col, lbl):
            v = row.get(col)
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return ""
            return (f'<span style="background:rgba(33,150,243,0.12);border:1px solid rgba(33,150,243,0.2);'
                    f'border-radius:4px;padding:3px 8px;font-size:.8em">'
                    f'<span style="color:#90caf9">{lbl}</span>'
                    f' <b style="color:#e2e8f0">{_fmt(v)}</b></span>')

        def a_stat(col, lbl):
            v = row.get(col)
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return ""
            return (f'<span style="background:rgba(255,138,76,0.1);border:1px solid rgba(255,138,76,0.2);'
                    f'border-radius:4px;padding:3px 8px;font-size:.8em">'
                    f'<span style="color:#ffb74d">{lbl}</span>'
                    f' <b style="color:#e2e8f0">{_fmt(v)}</b></span>')

        def x_stat(col, lbl):
            v = row.get(col)
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return ""
            return (f'<span style="background:rgba(100,116,139,0.12);border:1px solid rgba(100,116,139,0.18);'
                    f'border-radius:4px;padding:3px 8px;font-size:.8em">'
                    f'<span style="color:#94a3b8">{lbl}</span>'
                    f' <b style="color:#e2e8f0">{_fmt(v)}</b></span>')

        team_stats = " ".join(filter(None, [
            t_stat("gols_feitos",      "Gols Feitos"),
            t_stat("finalizacoes",     "Finalizações"),
            t_stat("chutes_no_gol",    "Chutes no Gol"),
            t_stat("escanteios",       "Escanteios"),
            t_stat("cartoes_amarelos", "Cart. Amarelos"),
        ]))
        adv_stats = " ".join(filter(None, [
            a_stat("gols_sofridos",        "Gols Sofridos"),
            a_stat("adv_finalizacoes",     "Finalizações"),
            a_stat("adv_chutes_no_gol",    "Chutes no Gol"),
            a_stat("adv_escanteios",       "Escanteios"),
            a_stat("adv_cartoes_amarelos", "Cart. Amarelos"),
        ]))
        total_stats = " ".join(filter(None, [
            x_stat("gols_totais",        "Gols"),
            x_stat("finalizacoes_totais", "Finalizações"),
            x_stat("escanteios_totais",  "Escanteios"),
            x_stat("ca_totais",          "Cart. Amarelos"),
        ]))

        cards_html += f"""
<div style="background:#1e293b;border-left:4px solid {border_c};border-radius:8px;padding:12px 16px">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;flex-wrap:wrap;gap:4px">
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
      <span style="background:{bg_c};color:{badge_c};border-radius:4px;padding:2px 9px;font-size:.75em;font-weight:700">{res.upper()}</span>
      <span style="color:#64748b;font-size:.82em">{data}</span>
      <span style="color:#475569;font-size:.78em">{comp}</span>
    </div>
    <span style="color:#64748b;font-size:.82em">{local}</span>
  </div>
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;flex-wrap:wrap">
    <span style="color:#f1f5f9;font-size:1.05em;font-weight:600">vs {adv}</span>{forca_badge}
    <span style="background:rgba(255,255,255,.08);border-radius:6px;padding:2px 10px;color:{txt_c};font-size:1.1em;font-weight:700">{placar}</span>
  </div>
  <div style="display:flex;flex-direction:column;gap:5px">
    <div style="background:rgba(33,150,243,0.06);border:1px solid rgba(33,150,243,0.18);border-radius:6px;padding:6px 10px">
      <span style="color:#90caf9;font-size:.68em;font-weight:700;letter-spacing:.8px;display:block;margin-bottom:5px">TIME</span>
      <div style="display:flex;flex-wrap:wrap;gap:5px">{team_stats}</div>
    </div>
    <div style="background:rgba(255,138,76,0.06);border:1px solid rgba(255,138,76,0.18);border-radius:6px;padding:6px 10px">
      <span style="color:#ffb74d;font-size:.68em;font-weight:700;letter-spacing:.8px;display:block;margin-bottom:5px">ADVERSÁRIO</span>
      <div style="display:flex;flex-wrap:wrap;gap:5px">{adv_stats}</div>
    </div>
    <div style="background:rgba(100,116,139,0.06);border:1px solid rgba(100,116,139,0.15);border-radius:6px;padding:5px 10px">
      <span style="color:#94a3b8;font-size:.68em;font-weight:700;letter-spacing:.8px;display:block;margin-bottom:5px">TOTAIS DA PARTIDA</span>
      <div style="display:flex;flex-wrap:wrap;gap:5px">{total_stats}</div>
    </div>
  </div>
</div>"""
    cards_html += "</div>"
    st.markdown(cards_html, unsafe_allow_html=True)

    st.divider()

    # ── 3. Resumo estatístico ─────────────────────────────────────────────────
    st.markdown("#### Resumo Estatístico")
    st.markdown(
        '<p style="color:#64748b;font-size:.82em;margin:-8px 0 8px">'
        '<b style="color:#94a3b8">Média</b> = média simples &nbsp;·&nbsp; '
        '<b style="color:#94a3b8">DP</b> = desvio padrão (quanto varia) &nbsp;·&nbsp; '
        '<b style="color:#94a3b8">Média&nbsp;−&nbsp;DP</b> = piso esperado (~68% dos jogos acima) &nbsp;·&nbsp; '
        '<b style="color:#94a3b8">Mín&nbsp;−&nbsp;DP</b> = piso ultra-conservador'
        '</p>',
        unsafe_allow_html=True,
    )

    # Média = média simples | DP = desvio padrão (dispersão)
    # Média−DP = piso esperado (~68% dos jogos ficam acima) | Mín−DP = piso ultra-conservador
    _S_METRICS = [
        ("Média",      lambda s: s.mean()),
        ("DP",         lambda s: s.std()),
        ("Média − DP", lambda s: s.mean() - s.std()),
        ("Mín − DP",   lambda s: s.min() - s.std()),
    ]
    _TIME_SCOLS = [
        ("gols_feitos",      "Gols Feitos"),
        ("finalizacoes",     "Finalizações"),
        ("chutes_no_gol",    "Chutes no Gol"),
        ("escanteios",       "Escanteios"),
        ("cartoes_amarelos", "Cart. Amarelos"),
    ]
    _ADV_SCOLS = [
        ("gols_sofridos",        "Gols Sofridos"),
        ("adv_finalizacoes",     "Finalizações"),
        ("adv_chutes_no_gol",    "Chutes no Gol"),
        ("adv_escanteios",       "Escanteios"),
        ("adv_cartoes_amarelos", "Cart. Amarelos"),
    ]
    _TOT_SCOLS = [
        ("gols_totais",          "Gols"),
        ("finalizacoes_totais",  "Finalizações"),
        ("escanteios_totais",    "Escanteios"),
        ("ca_totais",            "Cart. Amarelos"),
    ]

    def _bg_gradient(col_series):
        lo, hi = col_series.min(), col_series.max()
        styles = []
        for v in col_series:
            if pd.isna(v) or hi == lo:
                styles.append("background:#1e293b;color:#e2e8f0")
                continue
            ratio = (v - lo) / (hi - lo)
            r = int(248 * (1 - ratio) + 34 * ratio)
            g = int(105 * (1 - ratio) + 197 * ratio)
            b = int(107 * (1 - ratio) + 94 * ratio)
            styles.append(f"background:rgb({r},{g},{b});color:#fff")
        return styles

    def _render_section(title, cols, border_color):
        data: dict = {}
        for col, lbl in cols:
            s = _col(df, col)
            if not len(s):
                continue
            row_data: dict = {}
            for m_name, fn in _S_METRICS:
                try:
                    v = round(float(fn(s)), 1)
                    row_data[m_name] = v if not pd.isna(v) else None
                except Exception:
                    row_data[m_name] = None
            data[lbl] = row_data
        if not data:
            return
        sdf = pd.DataFrame(data).T
        st.markdown(
            f'<div style="border-left:3px solid {border_color};padding:3px 12px;'
            f'margin:14px 0 4px;color:#e2e8f0;font-size:.9em;font-weight:600">'
            f'{title}</div>',
            unsafe_allow_html=True,
        )
        styled = sdf.style
        for gc in ["Média", "Média − DP"]:
            if gc in sdf.columns:
                styled = styled.apply(lambda c, col=gc: _bg_gradient(c), subset=[gc])
        styled = (
            styled
            .set_properties(**{
                "text-align": "center",
                "font-size": "0.88em",
                "border": "1px solid rgba(255,255,255,0.06)",
            })
            .set_table_styles([
                {"selector": "th",
                 "props": [("background", "#0f172a"), ("color", "#e2e8f0"),
                           ("font-size", "0.8em"), ("border", "1px solid rgba(255,255,255,0.08)"),
                           ("padding", "5px 10px"), ("text-align", "center")]},
                {"selector": "th.row_heading",
                 "props": [("background", "#0f172a"), ("color", "#e2e8f0"),
                           ("font-size", "0.82em"), ("text-align", "left"),
                           ("padding", "5px 14px"), ("white-space", "nowrap")]},
                {"selector": "td",
                 "props": [("padding", "5px 10px"), ("color", "#e2e8f0")]},
                {"selector": "tr:hover td",
                 "props": [("filter", "brightness(1.15)")]},
            ])
            .format("{:.1f}", na_rep="—")
        )
        st.dataframe(styled, use_container_width=True)

    _render_section("Totais do Time",          _TIME_SCOLS, "#2196F3")
    _render_section("Totais dos Adversários",  _ADV_SCOLS,  "#FF8A4C")
    _render_section("Totais da Partida",       _TOT_SCOLS,  "#64748b")

    st.divider()

    # ── 4. Gráficos ───────────────────────────────────────────────────────────
    st.markdown("#### Evolução por Jogo")

    chdf  = df.iloc[::-1].reset_index(drop=True)
    jlbls = [f"J{i+1}" for i in range(len(chdf))]

    def s(col):
        return pd.to_numeric(chdf.get(col, pd.Series(dtype=float)), errors="coerce")

    cl, cr = st.columns(2)
    with cl:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=jlbls, y=s("gols_feitos"),   mode="lines+markers",
                                  name="Feitos",  line=dict(color="#4CAF50", width=2)))
        fig.add_trace(go.Scatter(x=jlbls, y=s("gols_sofridos"), mode="lines+markers",
                                  name="Sofridos", line=dict(color="#F44336", width=2)))
        fig.add_trace(go.Scatter(x=jlbls, y=s("gols_totais"),   mode="lines",
                                  name="Total", line=dict(color="#9E9E9E", width=1, dash="dot")))
        fig.update_layout(title="Gols por Jogo", **_CHART_BASE)
        st.plotly_chart(fig, use_container_width=True)

    with cr:
        fig2 = go.Figure()
        fig2.add_trace(go.Bar(x=jlbls, y=s("finalizacoes"),  name="Finalizações",
                               marker_color="#2196F3", opacity=0.85))
        fig2.add_trace(go.Bar(x=jlbls, y=s("chutes_no_gol"), name="Chutes no Gol",
                               marker_color="#FF9800", opacity=0.85))
        fig2.update_layout(title="Finalizações por Jogo", barmode="group", **_CHART_BASE)
        st.plotly_chart(fig2, use_container_width=True)

    cl2, cr2 = st.columns(2)
    with cl2:
        fig3 = go.Figure()
        fig3.add_trace(go.Bar(x=jlbls, y=s("escanteios_totais"), name="Total (jogo)",
                               marker_color="#7B1FA2", opacity=0.45))
        fig3.add_trace(go.Bar(x=jlbls, y=s("escanteios"),        name="Time",
                               marker_color="#CE93D8", opacity=0.9))
        fig3.update_layout(title="Escanteios por Jogo", barmode="overlay", **_CHART_BASE)
        st.plotly_chart(fig3, use_container_width=True)

    with cr2:
        fig4 = go.Figure()
        fig4.add_trace(go.Scatter(x=jlbls, y=s("finalizacoes"),     mode="lines+markers",
                                   name="Time",       line=dict(color="#2196F3", width=2)))
        fig4.add_trace(go.Scatter(x=jlbls, y=s("adv_finalizacoes"), mode="lines+markers",
                                   name="Adversário", line=dict(color="#FF5722", width=2)))
        fig4.update_layout(title="Finalizações: Time vs Adversário", **_CHART_BASE)
        st.plotly_chart(fig4, use_container_width=True)


def _show_trends(df: pd.DataFrame):
    lines = _compute_lines(df)
    if not lines:
        st.info("Dados insuficientes para calcular tendências.")
        return

    _S = {
        "forte": ("background:#0d3b0d;color:#86efac;", "FORTE"),
        "media": ("background:#422006;color:#fed7aa;", "MÉDIA"),
        "fraca": ("background:#1e1b4b;color:#c4b5fd;", "FRACA"),
    }
    _BAR = {
        "forte": "#166534",
        "media": "#7c3700",
        "fraca": "#2d1f6e",
    }

    cats = list(dict.fromkeys(l["category"] for l in lines))
    for cat in cats:
        st.markdown(f"#### {cat}")
        cat_lines = sorted([l for l in lines if l["category"] == cat], key=lambda x: -x["pct"])
        rows_html = ""
        for l in cat_lines:
            css, badge_lbl = _S[l["strength"]]
            bar_c = _BAR[l["strength"]]
            rows_html += f"""
<tr style="border-bottom:1px solid rgba(255,255,255,0.05)">
  <td style="padding:7px 10px;color:#e2e8f0;font-size:.88em">{l['label']}</td>
  <td style="padding:7px 10px;color:#fff;text-align:center;font-weight:700">{l['count']}/{l['total']}</td>
  <td style="padding:7px 10px;text-align:center">
    <div style="display:flex;align-items:center;gap:6px;justify-content:center">
      <div style="background:rgba(255,255,255,.1);border-radius:4px;height:6px;width:60px;overflow:hidden">
        <div style="background:{bar_c};height:100%;width:{l['pct']:.0f}%"></div>
      </div>
      <span style="color:#fff;font-weight:700;min-width:34px">{l['pct']:.0f}%</span>
    </div>
  </td>
  <td style="padding:7px 10px;text-align:center">
    <span style="border-radius:4px;padding:2px 8px;font-size:.75em;font-weight:700;{css}">{badge_lbl}</span>
  </td>
</tr>"""
        st.markdown(f"""
<table style="width:100%;border-collapse:collapse;margin-bottom:16px">
<thead><tr style="background:rgba(255,255,255,.04)">
  <th style="padding:6px 10px;color:#94a3b8;text-align:left;font-size:.8em">Linha de Aposta</th>
  <th style="padding:6px 10px;color:#94a3b8;text-align:center;font-size:.8em">Jogos</th>
  <th style="padding:6px 10px;color:#94a3b8;text-align:center;font-size:.8em">Aderência</th>
  <th style="padding:6px 10px;color:#94a3b8;text-align:center;font-size:.8em">Sinal</th>
</tr></thead>
<tbody>{rows_html}</tbody>
</table>""", unsafe_allow_html=True)

    st.caption("⚠️ Sinal estatístico baseado em aderência histórica. Não constitui previsão ou garantia de resultado.")


def _show_raw(df: pd.DataFrame):
    rename = {
        "time": "Time", "data": "Data", "competicao": "Competição",
        "adversario": "Adversário", "local": "Local", "placar": "Placar",
        "resultado": "Resultado", "tipo_jogo": "Tipo", "forca_adv": "Força Adv.",
        "gols_feitos": "GF", "gols_sofridos": "GS", "gols_totais": "GT",
        "finalizacoes": "Fin.", "chutes_no_gol": "Chutes",
        "escanteios": "Esc.", "escanteios_totais": "Esc.T",
        "cartoes_amarelos": "CA", "cartoes_vermelhos": "CV",
        "adv_finalizacoes": "Adv.Fin.", "adv_chutes_no_gol": "Adv.Ch.",
        "adv_escanteios": "Adv.Esc.", "adv_cartoes_amarelos": "Adv.CA",
        "adv_cartoes_vermelhos": "Adv.CV",
    }
    disp = df.rename(columns=rename)
    col = "Resultado" if "Resultado" in disp.columns else "resultado"
    def _row(row):
        clr = _RES_COLORS.get(row[col] if col in row.index else "", "")
        return [f"background-color: {clr}" if clr else "" for _ in row]
    st.dataframe(disp.style.apply(_row, axis=1), use_container_width=True, hide_index=True)


def _show_comparison(df_all: pd.DataFrame):
    teams = list(df_all["time"].unique())
    if len(teams) < 2:
        st.info("Colete dados de ao menos 2 times para usar essa função.")
        return

    c1, c2 = st.columns(2)
    ta = c1.selectbox("Time A", teams, key="cmp_a")
    tb = c2.selectbox("Time B", [t for t in teams if t != ta], key="cmp_b")

    da = df_all[df_all["time"] == ta]
    db = df_all[df_all["time"] == tb]

    # Team header cards
    st.markdown(
        f'<div style="display:flex;gap:10px;margin:12px 0">'
        f'<div style="flex:1;background:#172554;border:2px solid #1d4ed8;border-radius:8px;'
        f'padding:10px 16px;text-align:center">'
        f'<div style="color:#60a5fa;font-size:1.15em;font-weight:700">{ta}</div>'
        f'<div style="color:#475569;font-size:.78em">{len(da)} jogos coletados</div></div>'
        f'<div style="flex:1;background:#2e1065;border:2px solid #7e22ce;border-radius:8px;'
        f'padding:10px 16px;text-align:center">'
        f'<div style="color:#c084fc;font-size:1.15em;font-weight:700">{tb}</div>'
        f'<div style="color:#475569;font-size:.78em">{len(db)} jogos coletados</div></div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── helpers ──────────────────────────────────────────────────────────────
    def _stats3(d, col):
        s = _col(d, col)
        if not len(s):
            return None, None, None
        m  = round(float(s.mean()), 1)
        dp = round(float(s.std()), 1) if len(s) > 1 else 0.0
        return m, dp, round(m - dp, 1)

    def _bar_row(label, col, higher=True, is_pct=False, thr=None):
        if is_pct and thr is not None:
            va = _freq(da, col, thr)
            vb = _freq(db, col, thr)
            fa = f"{va:.0f}%" if va is not None else "—"
            fb = f"{vb:.0f}%" if vb is not None else "—"
            sub_a = sub_b = ""
            sa, sb = va or 0, vb or 0
        else:
            va, dpa, floora = _stats3(da, col)
            vb, dpb, floorb = _stats3(db, col)
            fa = f"{va:.1f}" if va is not None else "—"
            fb = f"{vb:.1f}" if vb is not None else "—"
            sub_a = (f'<div style="color:#475569;font-size:.7em">'
                     f'DP {dpa} &nbsp;·&nbsp; Piso {floora}</div>') if va is not None else ""
            sub_b = (f'<div style="color:#475569;font-size:.7em">'
                     f'DP {dpb} &nbsp;·&nbsp; Piso {floorb}</div>') if vb is not None else ""
            sa, sb = va or 0, vb or 0

        mx = max(abs(sa), abs(sb), 0.01)
        bar_a = min(abs(sa) / mx * 45, 45)
        bar_b = min(abs(sb) / mx * 45, 45)

        a_wins = va is not None and vb is not None and (
            (higher and va > vb + 0.05) or (not higher and va < vb - 0.05)
        )
        b_wins = va is not None and vb is not None and (
            (higher and vb > va + 0.05) or (not higher and vb < va - 0.05)
        )
        ac = "#60a5fa" if a_wins else ("#94a3b8" if not b_wins else "#475569")
        bc = "#c084fc" if b_wins else ("#94a3b8" if not a_wins else "#475569")

        return (
            f'<div style="display:grid;grid-template-columns:1fr 180px 1fr;align-items:center;'
            f'gap:8px;padding:8px 12px;border-radius:6px;background:#0f172a;margin:3px 0">'
            f'<div style="text-align:right">'
            f'<span style="color:{ac};font-size:1.05em;font-weight:700">{fa}</span>{sub_a}'
            f'</div>'
            f'<div style="text-align:center">'
            f'<div style="position:relative;height:8px;background:#1e293b;border-radius:4px;margin-bottom:5px">'
            f'<div style="position:absolute;top:0;right:50%;height:100%;width:{bar_a:.0f}%;'
            f'background:#1d4ed8;border-radius:4px 0 0 4px"></div>'
            f'<div style="position:absolute;top:0;left:50%;height:100%;width:{bar_b:.0f}%;'
            f'background:#7e22ce;border-radius:0 4px 4px 0"></div>'
            f'</div>'
            f'<span style="color:#64748b;font-size:.72em">{label}</span>'
            f'</div>'
            f'<div style="text-align:left">'
            f'<span style="color:{bc};font-size:1.05em;font-weight:700">{fb}</span>{sub_b}'
            f'</div>'
            f'</div>'
        )

    def _section(title, border_color, items):
        html = (
            f'<div style="border-left:3px solid {border_color};padding:3px 12px;'
            f'margin:18px 0 6px;color:#e2e8f0;font-size:.9em;font-weight:600">{title}</div>'
        )
        html += "".join(items)
        st.markdown(html, unsafe_allow_html=True)

    # ── Legenda ───────────────────────────────────────────────────────────────
    st.markdown(
        f'<div style="display:flex;justify-content:center;gap:24px;margin:4px 0 2px;'
        f'font-size:.75em;color:#64748b">'
        f'<span>← <span style="color:#60a5fa">■</span> {ta}</span>'
        f'<span style="color:#475569">Média &nbsp;|&nbsp; DP &nbsp;|&nbsp; Piso (Média−DP)</span>'
        f'<span><span style="color:#c084fc">■</span> {tb} →</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    # ── Projeção da partida (modelo blended) ─────────────────────────────────
    # Cada métrica cruza ataque do time A com defesa do time B e vice-versa,
    # para uma estimativa mais realista do que simples médias isoladas.
    def _blend(col_att, d_att, col_def, d_def):
        m_att, dp_att, fl_att = _stats3(d_att, col_att)
        m_def, dp_def, fl_def = _stats3(d_def, col_def)
        if m_att is None or m_def is None:
            return None, None, None
        proj  = round((m_att + m_def) / 2, 2)
        floor = round((fl_att + fl_def) / 2, 2) if fl_att is not None and fl_def is not None else None
        cl_att = round(m_att + (dp_att or 0), 2)
        cl_def = round(m_def + (dp_def or 0), 2)
        ceil  = round((cl_att + cl_def) / 2, 2)
        return proj, floor, ceil

    ga_p, ga_fl, ga_cl = _blend("gols_feitos",        da, "gols_sofridos",         db)
    gb_p, gb_fl, gb_cl = _blend("gols_feitos",        db, "gols_sofridos",         da)
    fa_p, fa_fl, fa_cl = _blend("finalizacoes",       da, "adv_finalizacoes",      db)
    fb_p, fb_fl, fb_cl = _blend("finalizacoes",       db, "adv_finalizacoes",      da)
    cha_p, cha_fl, cha_cl = _blend("chutes_no_gol",   da, "adv_chutes_no_gol",     db)
    chb_p, chb_fl, chb_cl = _blend("chutes_no_gol",   db, "adv_chutes_no_gol",     da)
    ea_p, ea_fl, ea_cl = _blend("escanteios",         da, "adv_escanteios",        db)
    eb_p, eb_fl, eb_cl = _blend("escanteios",         db, "adv_escanteios",        da)
    ca_p, ca_fl, ca_cl = _blend("cartoes_amarelos",   da, "adv_cartoes_amarelos",  db)
    cb_p, cb_fl, cb_cl = _blend("cartoes_amarelos",   db, "adv_cartoes_amarelos",  da)

    def _psum(a, b):
        return round(a + b, 1) if a is not None and b is not None else None

    def _proj_badge(val, floor, thresholds):
        if val is None:
            return ""
        for thr, lbl, bg, fg in sorted(thresholds, key=lambda x: -x[0]):
            if val >= thr:
                strong = floor is not None and floor >= thr
                star   = "★ " if strong else ""
                border = ";border:1px solid #16a34a" if strong else ""
                return (f'<div style="margin-top:5px">'
                        f'<span style="background:{bg};color:{fg};border-radius:3px;'
                        f'padding:2px 7px;font-size:.65em{border}">{star}{lbl}</span>'
                        f'</div>')
        return ""

    def _proj_card(title, total, floor, ceil, sub_a, sub_b, badge=""):
        if total is None:
            return ""
        range_line = ""
        if floor is not None and ceil is not None:
            range_line = (
                f'<div style="display:flex;justify-content:center;gap:6px;'
                f'margin-top:3px;font-size:.72em">'
                f'<span style="color:#ef4444" title="piso (Média−DP)">▼ {floor}</span>'
                f'<span style="color:#334155">·</span>'
                f'<span style="color:#22c55e" title="teto (Média+DP)">▲ {ceil}</span>'
                f'</div>'
            )
        team_row = (
            f'<div style="display:flex;justify-content:space-around;'
            f'margin-top:7px;border-top:1px solid #1e293b;padding-top:5px">'
            f'<span style="font-size:.7em"><span style="color:#60a5fa">'
            f'{ta[:8]}</span> <b style="color:#93c5fd">{sub_a}</b></span>'
            f'<span style="font-size:.7em"><span style="color:#c084fc">'
            f'{tb[:8]}</span> <b style="color:#d8b4fe">{sub_b}</b></span>'
            f'</div>'
        ) if sub_a and sub_b else ""
        return (
            f'<div style="background:#0f172a;border-radius:8px;padding:12px 14px;'
            f'text-align:center;flex:1;min-width:130px">'
            f'<div style="color:#64748b;font-size:.72em;letter-spacing:.3px;'
            f'margin-bottom:4px">{title}</div>'
            f'<div style="color:#e2e8f0;font-size:1.6em;font-weight:700">{total}</div>'
            f'{range_line}{badge}{team_row}</div>'
        )

    gt   = _psum(ga_p,  gb_p);  gt_fl  = _psum(ga_fl,  gb_fl);  gt_cl  = _psum(ga_cl,  gb_cl)
    ft   = _psum(fa_p,  fb_p);  ft_fl  = _psum(fa_fl,  fb_fl);  ft_cl  = _psum(fa_cl,  fb_cl)
    cht  = _psum(cha_p, chb_p); cht_fl = _psum(cha_fl, chb_fl); cht_cl = _psum(cha_cl, chb_cl)
    et   = _psum(ea_p,  eb_p);  et_fl  = _psum(ea_fl,  eb_fl);  et_cl  = _psum(ea_cl,  eb_cl)
    ct   = _psum(ca_p,  cb_p);  ct_fl  = _psum(ca_fl,  cb_fl);  ct_cl  = _psum(ca_cl,  cb_cl)

    if any(v is not None for v in [gt, ft, cht, et, ct]):
        proj_html = (
            '<div style="border-left:3px solid #f59e0b;padding:3px 12px;'
            'margin:18px 0 8px;color:#e2e8f0;font-size:.9em;font-weight:600">'
            'Projeção da Partida'
            '<span style="color:#64748b;font-size:.78em;font-weight:400;margin-left:8px">'
            '— modelo blended (ataque × defesa cruzados)</span></div>'
            '<p style="color:#64748b;font-size:.75em;margin:-4px 0 8px">'
            '▼ piso (Média−DP) &nbsp;·&nbsp; ▲ teto (Média+DP) &nbsp;·&nbsp; '
            '★ = piso também passa o threshold → sinal forte</p>'
            '<div style="display:flex;gap:8px;flex-wrap:wrap">'
        )
        proj_html += _proj_card(
            "Gols no Jogo", gt, gt_fl, gt_cl,
            f"{ga_p:.1f}" if ga_p else "—", f"{gb_p:.1f}" if gb_p else "—",
            badge=_proj_badge(gt, gt_fl, [
                (3.5, "Over 3.5", "#14532d", "#86efac"),
                (2.5, "Over 2.5", "#166534", "#86efac"),
                (1.5, "Over 1.5", "#1e3a5f", "#93c5fd"),
            ]),
        )
        proj_html += _proj_card(
            "Finalizações", ft, ft_fl, ft_cl,
            f"{fa_p:.1f}" if fa_p else "—", f"{fb_p:.1f}" if fb_p else "—",
            badge=_proj_badge(ft, ft_fl, [
                (30.0, "Over 30", "#1e3a5f", "#93c5fd"),
                (20.0, "Over 20", "#172554", "#93c5fd"),
            ]),
        )
        proj_html += _proj_card(
            "Chutes no Gol", cht, cht_fl, cht_cl,
            f"{cha_p:.1f}" if cha_p else "—", f"{chb_p:.1f}" if chb_p else "—",
        )
        proj_html += _proj_card(
            "Escanteios", et, et_fl, et_cl,
            f"{ea_p:.1f}" if ea_p else "—", f"{eb_p:.1f}" if eb_p else "—",
            badge=_proj_badge(et, et_fl, [
                (10.5, "Over 10.5", "#14532d", "#86efac"),
                (9.5,  "Over 9.5",  "#166534", "#86efac"),
                (8.5,  "Over 8.5",  "#1e3a5f", "#93c5fd"),
            ]),
        )
        proj_html += _proj_card(
            "Cart. Amarelos", ct, ct_fl, ct_cl,
            f"{ca_p:.1f}" if ca_p else "—", f"{cb_p:.1f}" if cb_p else "—",
        )
        proj_html += '</div>'
        st.markdown(proj_html, unsafe_allow_html=True)

    # ── Seções ────────────────────────────────────────────────────────────────
    _section("Ataque", "#2196F3", [
        _bar_row("Gols Feitos",   "gols_feitos"),
        _bar_row("Finalizações",  "finalizacoes"),
        _bar_row("Chutes no Gol", "chutes_no_gol"),
        _bar_row("Escanteios",    "escanteios"),
    ])
    _section("Defesa", "#FF8A4C", [
        _bar_row("Gols Sofridos",     "gols_sofridos",     higher=False),
        _bar_row("Fin. Adversário",   "adv_finalizacoes",  higher=False),
        _bar_row("Ch. Adversário",    "adv_chutes_no_gol", higher=False),
        _bar_row("Esc. Adversário",   "adv_escanteios",    higher=False),
    ])
    _section("Totais da Partida", "#64748b", [
        _bar_row("Gols Totais",       "gols_totais"),
        _bar_row("Escanteios Totais", "escanteios_totais"),
        _bar_row("Cart. Amarelos",    "cartoes_amarelos",  higher=False),
    ])
    _section("Mercados (frequência %)", "#22c55e", [
        _bar_row("Time marca 1.5+",  "gols_feitos",        is_pct=True, thr=1.5),
        _bar_row("Time marca 2.5+",  "gols_feitos",        is_pct=True, thr=2.5),
        _bar_row("Over 2.5 gols",    "gols_totais",        is_pct=True, thr=2.5),
        _bar_row("Over 3.5 gols",    "gols_totais",        is_pct=True, thr=3.5),
        _bar_row("Over 8.5 esc.",    "escanteios_totais",  is_pct=True, thr=8.5),
        _bar_row("Over 9.5 esc.",    "escanteios_totais",  is_pct=True, thr=9.5),
    ])

    # ── Resultados (V/E/D) ────────────────────────────────────────────────────
    wdl_html = '<div style="display:flex;gap:8px;margin:16px 0">'
    for res, color in [("Vitória", "#16a34a"), ("Empate", "#ca8a04"), ("Derrota", "#dc2626")]:
        na = int((da["resultado"] == res).sum()) if "resultado" in da.columns else 0
        nb = int((db["resultado"] == res).sum()) if "resultado" in db.columns else 0
        wdl_html += (
            f'<div style="flex:1;background:#0f172a;border-radius:8px;padding:8px;text-align:center">'
            f'<div style="color:{color};font-size:.72em;font-weight:600;margin-bottom:4px">{res}</div>'
            f'<div style="display:flex;justify-content:space-around;align-items:center">'
            f'<span style="color:#60a5fa;font-size:1.1em;font-weight:700">{na}</span>'
            f'<span style="color:#334155;font-size:.8em">vs</span>'
            f'<span style="color:#c084fc;font-size:1.1em;font-weight:700">{nb}</span>'
            f'</div></div>'
        )
    wdl_html += '</div>'
    st.markdown(wdl_html, unsafe_allow_html=True)

    st.divider()
    st.markdown("### Leitura Automática")

    gfa, gfb = _mean(da, "gols_feitos"),    _mean(db, "gols_feitos")
    gsa, gsb = _mean(da, "gols_sofridos"),  _mean(db, "gols_sofridos")
    esca, escb = _mean(da, "escanteios"),   _mean(db, "escanteios")
    p25a, p25b = _freq(da, "gols_totais", 2.5), _freq(db, "gols_totais", 2.5)
    p35a, p35b = _freq(da, "gols_totais", 3.5), _freq(db, "gols_totais", 3.5)
    t_a = [l for l in _compute_lines(da) if l["strength"] == "forte"]
    t_b = [l for l in _compute_lines(db) if l["strength"] == "forte"]

    parts = []
    if gfa and gfb:
        if gfa > gfb + 0.5:
            parts.append(f"**{ta}** apresenta volume ofensivo superior — {gfa:.1f} gols/jogo vs {gfb:.1f} de **{tb}**.")
        elif gfb > gfa + 0.5:
            parts.append(f"**{tb}** apresenta volume ofensivo superior — {gfb:.1f} gols/jogo vs {gfa:.1f} de **{ta}**.")
        else:
            parts.append(f"Volume ofensivo similar: {gfa:.1f} ({ta}) vs {gfb:.1f} ({tb}) gols/jogo.")

    if gsa and gsb:
        if gsa < gsb - 0.3:
            parts.append(f"Defensivamente **{ta}** aparece mais sólido ({gsa:.1f} gols sofridos/jogo vs {gsb:.1f}).")
        elif gsb < gsa - 0.3:
            parts.append(f"Defensivamente **{tb}** aparece mais sólido ({gsb:.1f} gols sofridos/jogo vs {gsa:.1f}).")

    if esca and escb and abs(esca - escb) > 1:
        ldr = ta if esca > escb else tb
        parts.append(f"**{ldr}** tende a dominar nos escanteios ({max(esca,escb):.1f} vs {min(esca,escb):.1f}/jogo).")

    if p25a and p25b:
        avg = (p25a + p25b) / 2
        note = "sinal estatístico relevante" if avg >= 70 else ("tendência para jogos fechados" if avg <= 40 else "aderência moderada")
        parts.append(f"Over 2.5 gols — aderência combinada de {avg:.0f}% ({note}).")

    if p35a and p35b:
        parts.append(f"Over 3.5 gols — aderência combinada de {(p35a+p35b)/2:.0f}%.")

    if t_a:
        parts.append(f"Tendências fortes de **{ta}**: {', '.join(l['label'] for l in t_a[:3])}.")
    if t_b:
        parts.append(f"Tendências fortes de **{tb}**: {', '.join(l['label'] for l in t_b[:3])}.")

    if parts:
        for p in parts:
            st.markdown(f"- {p}")
        st.caption("*Leitura estatística baseada nos últimos jogos coletados. Não constitui garantia de resultado.*")
    else:
        st.info("Dados insuficientes para análise automática.")


# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("Copa 2026 Stats")

    worker = st.session_state.worker
    if worker is None or not worker.ready:
        st.warning("Sessão inativa")
        if st.button("Iniciar Sessão", type="primary", use_container_width=True):
            if worker is None:
                worker = _PlaywrightWorker()
                st.session_state.worker = worker
            with st.spinner("Abrindo Chromium..."):
                worker.start_session()
            st.rerun()
    else:
        st.success("Sessão ativa")
        if st.button("Encerrar sessão", use_container_width=True):
            worker.stop_session()
            st.session_state.worker = None
            st.session_state.roster_cache = {}
            st.rerun()

    st.divider()
    mode  = st.radio("Modo", ["Times", "Jogadores"])
    ready = worker is not None and worker.ready
    st.divider()

    if mode == "Times":
        sorted_teams = sorted(coletor.ALL_TEAMS.items(), key=lambda x: x[1])
        name_to_id   = {n: tid for tid, n in sorted_teams}
        sel_names    = st.multiselect("Times", list(name_to_id), placeholder="Buscar time...")
        n_games      = st.slider("Jogos por time", 1, 15, 5)

        with st.expander("Filtros de análise"):
            f_tipo  = st.radio("Tipo de jogo", ["Todos", "Oficial", "Amistoso"], key="f_tipo")
            f_local = st.radio("Local", ["Todos", "Casa", "Fora"], key="f_local")
            f_forca = st.radio("Força do adversário",
                               ["Todos", "Forte", "Médio", "Fraco"], key="f_forca")

        do_collect_teams   = st.button("Coletar", type="primary", use_container_width=True,
                                        disabled=not (sel_names and ready))
        do_collect_players = False

    else:
        sorted_copa     = sorted(_ALL_COPA_TEAMS.items(), key=lambda x: x[1])
        copa_name_to_id = {n: tid for tid, n in sorted_copa}
        chosen_team     = st.selectbox("Time", [""] + list(copa_name_to_id),
                                        format_func=lambda x: "Selecione..." if x == "" else x)

        player_options: dict = {}
        if chosen_team and ready:
            tid = copa_name_to_id[chosen_team]
            if tid not in st.session_state.roster_cache:
                with st.spinner("Buscando elenco..."):
                    st.session_state.roster_cache[tid] = fetch_team_players(
                        _WorkerSession(worker), tid
                    )
            for p in sorted(st.session_state.roster_cache.get(tid, []),
                            key=lambda x: x["position"] + x["name"]):
                lbl = f"{p['name']} [{_POS_LABEL.get(p['position'], p['position'])}]"
                player_options[lbl] = p
        elif chosen_team and not ready:
            st.info("Inicie a sessão para ver o elenco.")

        sel_labels    = st.multiselect("Jogadores", list(player_options), placeholder="Selecionar...")
        n_games       = st.slider("Jogos por jogador", 1, 15, 5)
        selecao_only  = st.checkbox("Apenas jogos pela seleção", value=True,
                                     help="Coleta só jogos onde a seleção participou, ignorando jogos pelo clube")

        do_collect_players = st.button("Coletar", type="primary", use_container_width=True,
                                        disabled=not (sel_labels and ready))
        do_collect_teams   = False

# ── Coleta ────────────────────────────────────────────────────────────────────

if do_collect_teams:
    sess  = _WorkerSession(st.session_state.worker)
    teams = {name_to_id[n]: n for n in sel_names}
    total = len(teams)
    bar   = st.progress(0, text="Iniciando...")
    rows  = []
    for i, (tid, tname) in enumerate(teams.items()):
        bar.progress(i / total, text=f"Coletando {tname} ({i+1}/{total})...")
        rows.extend(coletor.collect_team_stats(sess, tid, tname, n_games=n_games))
        bar.progress((i + 1) / total)
    st.session_state.team_data = rows
    bar.empty()
    st.rerun()

if do_collect_players:
    sess  = _WorkerSession(st.session_state.worker)
    tid   = copa_name_to_id[chosen_team]
    plist = [player_options[lbl] for lbl in sel_labels if lbl in player_options]
    total = len(plist)
    bar   = st.progress(0, text="Iniciando...")
    rows  = []
    for i, p in enumerate(plist):
        bar.progress(i / total, text=f"Coletando {p['name']} ({i+1}/{total})...")
        rows.extend(collect_player_stats(sess, p["id"], p["name"], tid, chosen_team, n_games,
                                          selecao_only=selecao_only))
        bar.progress((i + 1) / total)
    st.session_state.player_data = rows
    bar.empty()
    st.rerun()

# ── Exibição: Times ───────────────────────────────────────────────────────────

if mode == "Times":
    if st.session_state.team_data:
        df_all = _add_cols(pd.DataFrame(st.session_state.team_data))

        f_tipo  = st.session_state.get("f_tipo",  "Todos")
        f_local = st.session_state.get("f_local", "Todos")
        f_forca = st.session_state.get("f_forca", "Todos")

        teams     = list(df_all["time"].unique())
        tab_names = teams + (["Comparar Times"] if len(teams) >= 2 else [])
        outer     = st.tabs(tab_names)

        for i, tab in enumerate(outer):
            with tab:
                if i < len(teams):
                    tname = teams[i]
                    tdf   = df_all[df_all["time"] == tname]
                    fdf   = _apply_filters(tdf, f_tipo, f_local, f_forca)

                    n_shown = len(fdf)
                    if n_shown < 8:
                        st.warning(
                            f"Atenção: análise baseada em {n_shown} jogo(s) após os filtros. "
                            "Use como tendência inicial, não como previsão definitiva."
                        )
                    if n_shown == 0:
                        st.info("Nenhum jogo corresponde aos filtros.")
                        continue

                    sub = st.tabs(["Visão Geral", "Tendências de Mercado"])
                    with sub[0]: _show_overview(fdf)
                    with sub[1]: _show_trends(fdf)
                else:
                    _show_comparison(df_all)
    else:
        st.title("Copa 2026 — Análise Pré-Jogo")
        st.info("Selecione os times na barra lateral e clique em **Coletar**.")

# ── Exibição: Jogadores ───────────────────────────────────────────────────────

elif mode == "Jogadores":
    if st.session_state.player_data:
        df = pd.DataFrame(st.session_state.player_data)

        # ── Filtros ───────────────────────────────────────────────────────────
        _SELECAO_KW = [
            "international friendly", "friendly games", "world cup", "copa do mundo",
            "qualifier", "qualification", "eliminat", "nations league",
            "copa america", "copa américa", "gold cup", "concacaf", "conmebol",
            "africa cup", "afcon", "caf ", "asian cup", "afc ", "uefa euro",
            "uefa nations", "fifa", "olimp",
        ]
        def _is_selecao(c):
            cl = str(c).lower()
            return any(k in cl for k in _SELECAO_KW)

        max_g = int(df.groupby("jogador").size().max()) if len(df) else 5
        fc1, fc2, fc3 = st.columns([1, 1, 1])
        fonte_p   = fc1.radio("Fonte", ["Clube + Seleção", "Apenas Seleção"],
                               horizontal=True, key="pf_fonte")
        n_pfilter = fc2.slider("Últimos N jogos", 1, max_g, min(max_g, 5), key="pf_n")
        tipo_p    = fc3.radio("Tipo", ["Todos", "Oficial", "Amistoso"],
                               horizontal=True, key="pf_tipo")

        # 1. Filtrar por fonte ANTES de pegar N jogos (ordem importa)
        if fonte_p == "Apenas Seleção" and "competicao" in df.columns:
            df = df[df["competicao"].apply(_is_selecao)]

        # 2. Ordenar por data e pegar os N mais recentes por jogador
        if "data" in df.columns:
            df["_ds"] = pd.to_datetime(df["data"], errors="coerce")
            df = df.sort_values(["jogador", "_ds"], ascending=[True, False]).drop(columns="_ds")
        df = df.groupby("jogador", sort=False).head(n_pfilter).reset_index(drop=True)

        # 3. Filtrar por tipo de jogo
        if tipo_p != "Todos" and "competicao" in df.columns:
            mask = df["competicao"].apply(
                lambda c: any(k in str(c).lower() for k in ["friendly", "amistoso"])
            )
            df = df[mask] if tipo_p == "Amistoso" else df[~mask]

        if df.empty:
            st.warning("Nenhum jogo encontrado com os filtros aplicados.")

        # Ordem e cores de cada estatística do jogador
        _P_STATS = [
            ("gols",             "Gols",           "#86efac"),
            ("assistencias",     "Assistências",   "#93c5fd"),
            ("finalizacoes",     "Finalizações",   "#94a3b8"),
            ("chutes_no_gol",    "Chutes no Gol",  "#7dd3fc"),
            ("faltas_cometidas", "Faltas Comet.",  "#fca5a5"),
            ("faltas_sofridas",  "Faltas Sof.",    "#fed7aa"),
            ("cartao_amarelo",   "Cart. Amarelo",  "#fde68a"),
            ("cartao_vermelho",  "Cart. Vermelho", "#f87171"),
            ("minutos",          "Minutos",        "#64748b"),
        ]

        _RS = {
            "Vitória": ("#166534", "#86efac", "#0d3b0d", "#22c55e"),
            "Empate":  ("#713f12", "#fde68a", "#422006", "#eab308"),
            "Derrota": ("#7f1d1d", "#fca5a5", "#3b0a0a", "#ef4444"),
        }

        players = list(df["jogador"].unique())
        for tab, pname in zip(st.tabs(players), players):
            with tab:
                pdf = df[df["jogador"] == pname].copy()
                if pdf.empty:
                    st.info("Sem jogos com estes filtros.")
                    continue

                team_name = str(pdf["time"].iloc[0]) if "time" in pdf.columns else ""
                n_j       = len(pdf)

                # ── Header ──────────────────────────────────────────────────────
                st.markdown(
                    f'<div style="background:#0f172a;border-radius:10px;padding:16px 20px;'
                    f'margin-bottom:14px;display:flex;justify-content:space-between;'
                    f'align-items:center;border:1px solid #1e293b">'
                    f'<div>'
                    f'<div style="color:#e2e8f0;font-size:1.3em;font-weight:700">{pname}</div>'
                    f'<div style="color:#64748b;font-size:.85em;margin-top:2px">{team_name}</div>'
                    f'</div>'
                    f'<div style="background:#1e293b;border-radius:6px;padding:6px 14px;'
                    f'color:#94a3b8;font-size:.82em">{n_j} jogos analisados</div>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

                # ── Métricas resumidas (2 linhas de 4 ou 5) ─────────────────────
                avail = [(c, lbl) for c, lbl, _ in _P_STATS if c in pdf.columns]
                for chunk_start in range(0, len(avail), 4):
                    chunk    = avail[chunk_start:chunk_start + 4]
                    cols_row = st.columns(len(chunk))
                    for ci, (c, lbl) in enumerate(chunk):
                        s = _col(pdf, c)
                        if not len(s):
                            continue
                        m  = s.mean()
                        dp = s.std() if len(s) > 1 else 0.0
                        cols_row[ci].metric(
                            lbl, f"{m:.1f}",
                            f"DP ±{dp:.1f}  piso {m - dp:.1f}" if len(s) > 1 else "",
                        )

                st.divider()
                st.markdown(f"#### Últimos {n_j} Jogos")

                # ── Cards por jogo ───────────────────────────────────────────────
                html = '<div style="display:flex;flex-direction:column;gap:8px">'
                for _, row in pdf.iterrows():
                    res   = str(row.get("resultado", ""))
                    txt_c, badge_c, bg_c, border_c = _RS.get(
                        res, ("#94a3b8", "#cbd5e1", "#1e293b", "#334155")
                    )
                    adv    = row.get("adversario", "?")
                    placar = row.get("placar", "?")
                    local  = row.get("local", "?")
                    data   = row.get("data", "?")
                    comp   = row.get("competicao", "?")

                    def ps(col, lbl, clr):
                        v = row.get(col)
                        if v is None or (isinstance(v, float) and pd.isna(v)):
                            return ""
                        if col not in ("minutos", "rating") and float(v) == 0:
                            return ""
                        return (
                            f'<span style="background:rgba(255,255,255,.06);'
                            f'border:1px solid rgba(255,255,255,.08);border-radius:4px;'
                            f'padding:3px 8px;font-size:.8em">'
                            f'<span style="color:{clr};font-size:.82em">{lbl}</span>'
                            f' <b style="color:#e2e8f0">{_fmt(v)}</b></span>'
                        )

                    # Rating com cor
                    rating_v = row.get("rating")
                    rating_html = ""
                    if rating_v is not None and not (isinstance(rating_v, float) and pd.isna(rating_v)):
                        r  = float(rating_v)
                        rc = "#16a34a" if r >= 7.5 else ("#ca8a04" if r >= 6.5 else "#dc2626")
                        rating_html = (
                            f'<span style="background:{rc};color:#fff;border-radius:5px;'
                            f'padding:2px 9px;font-weight:700;font-size:.9em">{r:.1f}</span>'
                        )

                    stats_row = " ".join(filter(None, [
                        ps(c, lbl, clr) for c, lbl, clr in _P_STATS if c != "rating"
                    ]))

                    html += (
                        f'<div style="background:#1e293b;border-left:4px solid {border_c};'
                        f'border-radius:8px;padding:12px 16px">'
                        f'<div style="display:flex;justify-content:space-between;'
                        f'align-items:center;margin-bottom:6px;flex-wrap:wrap;gap:4px">'
                        f'<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
                        f'<span style="background:{bg_c};color:{badge_c};border-radius:4px;'
                        f'padding:2px 9px;font-size:.75em;font-weight:700">{res.upper()}</span>'
                        f'<span style="color:#64748b;font-size:.82em">{data}</span>'
                        f'<span style="color:#475569;font-size:.78em">{comp}</span>'
                        f'</div>'
                        f'<span style="color:#64748b;font-size:.82em">{local}</span>'
                        f'</div>'
                        f'<div style="display:flex;align-items:center;gap:10px;'
                        f'margin-bottom:10px;flex-wrap:wrap">'
                        f'<span style="color:#f1f5f9;font-size:1.05em;font-weight:600">vs {adv}</span>'
                        f'<span style="background:rgba(255,255,255,.08);border-radius:6px;'
                        f'padding:2px 10px;color:{txt_c};font-size:1.1em;font-weight:700">{placar}</span>'
                        f'{rating_html}</div>'
                        f'<div style="display:flex;flex-wrap:wrap;gap:5px">{stats_row}</div>'
                        f'</div>'
                    )
                html += '</div>'
                st.markdown(html, unsafe_allow_html=True)
    else:
        st.title("Copa 2026 — Estatísticas de Jogadores")
        st.info("Selecione os jogadores na barra lateral e clique em **Coletar**.")
