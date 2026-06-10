"""
Busca os IDs de todos os times da Copa do Mundo 2026 no SofaScore
e imprime o dicionário TEAMS pronto para colar no coletor.py.

Uso:  python ids.py
"""

import time
from playwright.sync_api import sync_playwright

TOURNAMENT_ID = 16       # FIFA World Cup (fixo no SofaScore)
SEASON_ID     = 58210    # Edição 2026

# Nomes em português para exibição na planilha
_PT_NAMES = {
    "Algeria":            "Argélia",
    "Argentina":          "Argentina",
    "Australia":          "Austrália",
    "Austria":            "Áustria",
    "Belgium":            "Bélgica",
    "Bosnia & Herzegovina": "Bósnia e Herzegovina",
    "Brazil":             "Brasil",
    "Cabo Verde":         "Cabo Verde",
    "Canada":             "Canadá",
    "Colombia":           "Colômbia",
    "Croatia":            "Croácia",
    "Curaçao":            "Curaçao",
    "Czechia":            "República Tcheca",
    "Côte d'Ivoire":      "Costa do Marfim",
    "DR Congo":           "Congo RD",
    "Ecuador":            "Equador",
    "Egypt":              "Egito",
    "England":            "Inglaterra",
    "France":             "França",
    "Germany":            "Alemanha",
    "Ghana":              "Gana",
    "Haiti":              "Haiti",
    "Iran":               "Irã",
    "Iraq":               "Iraque",
    "Japan":              "Japão",
    "Jordan":             "Jordânia",
    "Mexico":             "México",
    "Morocco":            "Marrocos",
    "Netherlands":        "Países Baixos",
    "New Zealand":        "Nova Zelândia",
    "Norway":             "Noruega",
    "Panama":             "Panamá",
    "Paraguay":           "Paraguai",
    "Portugal":           "Portugal",
    "Qatar":              "Catar",
    "Saudi Arabia":       "Arábia Saudita",
    "Scotland":           "Escócia",
    "Senegal":            "Senegal",
    "South Africa":       "África do Sul",
    "South Korea":        "Coreia do Sul",
    "Spain":              "Espanha",
    "Sweden":             "Suécia",
    "Switzerland":        "Suíça",
    "Tunisia":            "Tunísia",
    "Türkiye":            "Turquia",
    "USA":                "Estados Unidos",
    "Uruguay":            "Uruguai",
    "Uzbekistan":         "Uzbequistão",
}


def fetch_teams() -> list[dict]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            )
        )
        page = ctx.new_page()

        print("Iniciando sessão...")
        page.goto("https://www.sofascore.com/", wait_until="networkidle", timeout=30_000)
        time.sleep(1)

        url = (
            f"https://api.sofascore.com/api/v1"
            f"/unique-tournament/{TOURNAMENT_ID}/season/{SEASON_ID}/teams"
        )
        data = page.evaluate(
            """async (url) => {
                const r = await fetch(url, {
                    headers: {
                        'Accept': 'application/json, text/plain, */*',
                        'Referer': 'https://www.sofascore.com/'
                    }
                });
                if (!r.ok) return { __err: r.status };
                return await r.json();
            }""",
            url,
        )
        browser.close()

    if "__err" in data:
        raise RuntimeError(f"API retornou HTTP {data['__err']}")

    return data.get("teams", [])


if __name__ == "__main__":
    teams = fetch_teams()
    print(f"{len(teams)} times encontrados.\n")

    print("TEAMS = {")
    for t in sorted(teams, key=lambda x: x["name"]):
        en_name = t["name"]
        pt_name = _PT_NAMES.get(en_name, en_name)
        print(f'    {t["id"]}: "{pt_name}",')
    print("}")
