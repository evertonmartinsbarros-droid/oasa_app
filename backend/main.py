from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from typing import Optional
import asyncio
import os
from etl import carregar_dados, get_cache_info

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(carregar_em_background())
    yield

async def carregar_em_background():
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, carregar_dados)

app = FastAPI(title="OASA Dashboard API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

def filtrar(df, gerencia=None, polo=None, cidade=None, sistema=None,
            data_ini=None, data_fim=None):
    if gerencia:
        df = df[df["Gerência"] == gerencia]
    if polo:
        df = df[df["Pólo"] == polo]
    if cidade:
        df = df[df["Cidade"] == cidade]
    if sistema:
        df = df[df["Sistema"] == sistema]
    if data_ini:
        df = df[df["Data"] >= data_ini]
    if data_fim:
        df = df[df["Data"] <= data_fim]
    return df

@app.get("/status")
def status():
    info = get_cache_info()
    return {"ok": True, "cache": info}

@app.get("/filtros")
def filtros():
    df = carregar_dados()
    return {
        "gerencias": sorted(df["Gerência"].dropna().unique().tolist()),
        "polos":     sorted(df["Pólo"].dropna().unique().tolist()),
        "cidades":   sorted(df["Cidade"].dropna().unique().tolist()),
        "sistemas":  sorted(df["Sistema"].dropna().unique().tolist()),
    }

@app.get("/producao")
def producao(
    gerencia: Optional[str] = None,
    polo: Optional[str] = None,
    cidade: Optional[str] = None,
    sistema: Optional[str] = None,
    data_ini: Optional[str] = None,
    data_fim: Optional[str] = None,
):
    import pandas as pd
    df = carregar_dados()
    df = filtrar(df, gerencia, polo, cidade, sistema, data_ini, data_fim)

    if df.empty:
        return {"rows": []}

    grp = df.groupby(["Gerência", "Pólo", "Cidade", "Sistema"])
    rows = []
    for (ger, pol, cid, sis), g in grp:
        g = g.sort_values("Data_Hora")
        leit = g[g["Tem_Leitura_Macro"]]
        if leit.empty:
            continue

        dt_ini = leit["Data_Hora"].min()
        dt_fim = leit["Data_Hora"].max()
        horas = (dt_fim - dt_ini).total_seconds() / 3600 if dt_ini != dt_fim else None

        hor_ini = leit.loc[leit["Data_Hora"] == dt_ini, "Horimetro_Num"].max()
        hor_fim = leit.loc[leit["Data_Hora"] == dt_fim, "Horimetro_Num"].max()
        dif_hor = (hor_fim - hor_ini) if pd.notna(hor_ini) and pd.notna(hor_fim) and hor_fim >= hor_ini else None

        prod = g["Producao"].sum() if pd.notna(g["Producao"].sum()) else 0
        dias = horas / 24 if horas else None
        media_dia = prod / dias if dias and dias > 0 else None
        vazao_m3h = prod / dif_hor if dif_hor and dif_hor > 0 else None
        vazao_ls  = vazao_m3h * 1000 / 3600 if vazao_m3h else None

        rows.append({
            "gerencia":   ger,
            "polo":       pol,
            "cidade":     cid,
            "sistema":    sis,
            "producao":   round(prod, 2),
            "media_dia":  round(media_dia, 2) if media_dia else None,
            "vazao_m3h":  round(vazao_m3h, 2) if vazao_m3h else None,
            "vazao_ls":   round(vazao_ls, 4) if vazao_ls else None,
            "horas":      round(horas, 2) if horas else None,
        })

    rows.sort(key=lambda r: r["producao"], reverse=True)
    return {"rows": rows}

@app.get("/qualidade")
def qualidade(
    gerencia: Optional[str] = None,
    polo: Optional[str] = None,
    cidade: Optional[str] = None,
    sistema: Optional[str] = None,
    data_ini: Optional[str] = None,
    data_fim: Optional[str] = None,
):
    df = carregar_dados()
    df = filtrar(df, gerencia, polo, cidade, sistema, data_ini, data_fim)
    an = df[df["Tem_Analise"]]

    def cnt(col, op, val):
        if col not in an.columns:
            return 0
        s = an[col].dropna()
        if op == "<=": return int((s <= val).sum())
        if op == ">":  return int((s > val).sum())
        return 0

    return {
        "cloro": {
            "adequado":    cnt("Cloro (mg/L)", "<=", 5.0),
            "inadequado":  cnt("Cloro (mg/L)", ">",  5.0),
            "abaixo_min":  int((an["Cloro (mg/L)"].dropna() < 0.2).sum()) if "Cloro (mg/L)" in an.columns else 0,
            "total":       int(an["Cloro (mg/L)"].dropna().count()) if "Cloro (mg/L)" in an.columns else 0,
        },
        "turbidez": {
            "adequado":   cnt("Turbidez (uT)", "<=", 1.0),
            "inadequado": cnt("Turbidez (uT)", ">",  1.0),
            "total":      int(an["Turbidez (uT)"].dropna().count()) if "Turbidez (uT)" in an.columns else 0,
        },
        "cor": {
            "adequado":   cnt("Cor (uH)", "<=", 15),
            "inadequado": cnt("Cor (uH)", ">",  15),
            "total":      int(an["Cor (uH)"].dropna().count()) if "Cor (uH)" in an.columns else 0,
        },
        "fluoreto": {
            "adequado":   cnt("Fluoreto (mg/L)", "<=", 1.5),
            "inadequado": cnt("Fluoreto (mg/L)", ">",  1.5),
            "total":      int(an["Fluoreto (mg/L)"].dropna().count()) if "Fluoreto (mg/L)" in an.columns else 0,
        },
    }

@app.get("/acompanhamento")
def acompanhamento(
    gerencia: Optional[str] = None,
    polo: Optional[str] = None,
    cidade: Optional[str] = None,
    data_ini: Optional[str] = None,
    data_fim: Optional[str] = None,
):
    df = carregar_dados()
    df = filtrar(df, gerencia, polo, cidade, None, data_ini, data_fim)

    grp = df.groupby(["Gerência", "Pólo", "Cidade", "Sistema"])
    rows = []
    for (ger, pol, cid, sis), g in grp:
        rows.append({
            "gerencia":    ger,
            "polo":        pol,
            "cidade":      cid,
            "sistema":     sis,
            "analises":    int(g["Tem_Analise"].sum()),
            "leituras":    int(g["Tem_Leitura_Macro"].sum()),
        })

    rows.sort(key=lambda r: r["analises"])
    rank = 1
    prev = None
    for i, r in enumerate(rows):
        if r["analises"] != prev:
            rank = i + 1
            prev = r["analises"]
        r["rank"] = rank

    return {"rows": rows}

@app.get("/leituras")
def leituras(
    sistema: Optional[str] = None,
    cidade: Optional[str] = None,
    data_ini: Optional[str] = None,
    data_fim: Optional[str] = None,
    apenas_analise: Optional[bool] = False,
    apenas_leitura: Optional[bool] = False,
):
    import math
    df = carregar_dados()
    df = filtrar(df, None, None, cidade, sistema, data_ini, data_fim)

    if apenas_analise:
        df = df[df["Tem_Analise"]]
    if apenas_leitura:
        df = df[df["Tem_Leitura_Macro"]]

    cols = [
        "Data_Hora_Exibicao", "Gerência", "Pólo", "Cidade", "Sistema",
        "ME_Num", "MS_Num", "MP_Num", "Horimetro_Num",
        "Cloro (mg/L)", "Cor (uH)", "Fluoreto (mg/L)", "Turbidez (uT)",
        "Producao", "Tem_Analise", "Tem_Leitura_Macro"
    ]
    cols = [c for c in cols if c in df.columns]
    df = df[cols].copy()
    df["Data_Hora_Exibicao"] = df["Data_Hora_Exibicao"].astype(str)

    def clean(v):
        if v is None: return None
        try:
            if math.isnan(float(v)): return None
        except: pass
        return v

    rows = [{k: clean(v) for k, v in r.items()} for r in df.to_dict("records")]
    return {"rows": rows, "total": len(rows)}
