from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from typing import Optional
import asyncio
import os
import math
import calendar

import pandas as pd
import numpy as np
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
    allow_origins=["https://oasa-frontend-79mk.onrender.com", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- BLINDAGEM DA API ---
def get_df_seguro():
    df = carregar_dados()
    if df.empty:
        return df
    if "Gerência" not in df.columns:
        df["Gerência"] = "OASA"
    if "Pólo" not in df.columns:
        df["Pólo"] = df["Polo"] if "Polo" in df.columns else "Não Informado"
    if "Cidade" not in df.columns:
        df["Cidade"] = "Não Informada"
    if "Sistema" not in df.columns:
        df["Sistema"] = "Não Informado"
    return df


# Filtro com Máscara Booleana
def filtrar(df, gerencia=None, polo=None, cidade=None, sistema=None, data_ini=None, data_fim=None):
    mask = pd.Series(True, index=df.index)

    if gerencia: mask &= (df["Gerência"] == gerencia)
    if polo:     mask &= (df["Pólo"] == polo)
    if cidade:   mask &= (df["Cidade"] == cidade)
    if sistema:  mask &= (df["Sistema"] == sistema)

    if data_ini or data_fim:
        data_str = df["Data"].astype(str)
        if data_ini: mask &= (data_str >= data_ini)
        if data_fim: mask &= (data_str <= data_fim)

    return df[mask]


@app.get("/status")
def status():
    return {"ok": True, "cache": get_cache_info()}


@app.get("/filtros")
def filtros():
    df = get_df_seguro()
    
    cols = ["Gerência", "Pólo", "Cidade", "Sistema"]
    # .astype(object) remove a restrição de colunas categóricas permitindo o fillna("")
    comb = df[cols].drop_duplicates().astype(object).fillna("")

    return {
        "combinacoes": comb.to_dict(orient="records"),
        "gerencias": sorted(df["Gerência"].dropna().unique().tolist()),
        "polos":     sorted(df["Pólo"].dropna().unique().tolist()),
        "cidades":   sorted(df["Cidade"].dropna().unique().tolist()),
        "sistemas":  sorted(df["Sistema"].dropna().unique().tolist()),
    }


@app.get("/producao")
def producao(
    gerencia: Optional[str] = None, polo: Optional[str] = None,
    cidade: Optional[str] = None, sistema: Optional[str] = None,
    data_ini: Optional[str] = None, data_fim: Optional[str] = None,
):
    df = get_df_seguro()
    df = filtrar(df, gerencia, polo, cidade, sistema, data_ini, data_fim)

    if df.empty:
        return {"rows": []}

    grp = df.groupby(["Gerência", "Pólo", "Cidade", "Sistema"], observed=True)
    rows = []

    for (ger, pol, cid, sis), g in grp:
        # Ordena cronologicamente
        g = g.sort_values("Data_Hora")
        leit = g[g["Tem_Leitura_Macro"]].dropna(subset=["Data_Hora"])

        if leit.empty:
            continue

        # REGRA 1: Escolher a coluna de cálculo. Prioridade para Macro Saída.
        col_macro = "MS_Num" if leit["MS_Num"].notna().any() else "ME_Num"
        
        # Filtra para ter apenas os registros onde o macro escolhido não está vazio
        leit_macro = leit.dropna(subset=[col_macro])

        if leit_macro.empty:
            continue

        # Pega exatamente a primeira e a última linha válidas desse macro
        primeira = leit_macro.iloc[0]
        ultima = leit_macro.iloc[-1]

        dt_ini = primeira["Data_Hora"]
        dt_fim = ultima["Data_Hora"]
        val_ini = primeira[col_macro]
        val_fim = ultima[col_macro]

        # REGRA 2: Cálculo real baseado nos extremos
        # Verifica se o tempo passou e o hidrômetro avançou (não foi trocado)
        if dt_fim > dt_ini and val_fim >= val_ini:
            prod = val_fim - val_ini
            horas = (dt_fim - dt_ini).total_seconds() / 3600
            dias = horas / 24.0
            media_dia = prod / dias if dias > 0 else None
        else:
            # Fallback de segurança: se for a mesma hora ou trocaram o hidrômetro no meio do mês
            prod = g["Producao"].sum()
            if pd.isna(prod): prod = 0
            horas = (leit["Data_Hora"].max() - leit["Data_Hora"].min()).total_seconds() / 3600
            dias = horas / 24.0 if horas else None
            media_dia = (prod / dias) if (dias and dias > 0) else None

        # Diferença de Horímetro (para Vazão)
        leit_hor = leit.dropna(subset=["Horimetro_Num"])
        dif_hor = None
        if not leit_hor.empty:
            hor_ini = leit_hor.iloc[0]["Horimetro_Num"]
            hor_fim = leit_hor.iloc[-1]["Horimetro_Num"]
            if hor_fim >= hor_ini:
                dif_hor = hor_fim - hor_ini

        # Projeção Mensal
        projecao_mensal = None
        if media_dia and pd.notna(ultima["Data_Hora"]):
            _, dias_no_mes = calendar.monthrange(ultima["Data_Hora"].year, ultima["Data_Hora"].month)
            projecao_mensal = media_dia * dias_no_mes

        vazao_m3h = prod / dif_hor if dif_hor and dif_hor > 0 else None
        vazao_ls  = vazao_m3h * 1000 / 3600 if vazao_m3h else None

        rows.append({
            "gerencia": ger, "polo": pol, "cidade": cid, "sistema": sis,
            "producao":  round(prod, 2),
            "media_dia": round(media_dia, 2) if media_dia else None,
            "projecao_mensal": round(projecao_mensal, 2) if projecao_mensal else None,
            "vazao_m3h": round(vazao_m3h, 2) if vazao_m3h else None,
            "vazao_ls":  round(vazao_ls, 4)  if vazao_ls  else None,
            "horas":     round(horas, 2)     if horas     else None,
        })

    rows.sort(key=lambda r: r["producao"], reverse=True)
    return {"rows": rows}


@app.get("/qualidade")
def qualidade(
    gerencia: Optional[str] = None, polo: Optional[str] = None,
    cidade: Optional[str] = None, sistema: Optional[str] = None,
    data_ini: Optional[str] = None, data_fim: Optional[str] = None,
):
    df = get_df_seguro()
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
            "adequado":   cnt("Cloro (mg/L)", "<=", 5.0),
            "inadequado": cnt("Cloro (mg/L)", ">",  5.0),
            "abaixo_min": int((an["Cloro (mg/L)"].dropna() < 0.2).sum()) if "Cloro (mg/L)" in an.columns else 0,
            "total":      int(an["Cloro (mg/L)"].dropna().count()) if "Cloro (mg/L)" in an.columns else 0,
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
    gerencia: Optional[str] = None, polo: Optional[str] = None,
    cidade: Optional[str] = None, data_ini: Optional[str] = None, data_fim: Optional[str] = None,
):
    df = get_df_seguro()
    df = filtrar(df, gerencia, polo, cidade, None, data_ini, data_fim)

    grp = df.groupby(["Gerência", "Pólo", "Cidade", "Sistema"], observed=True)
    rows = []

    for (ger, pol, cid, sis), g in grp:
        rows.append({
            "gerencia": ger, "polo": pol, "cidade": cid, "sistema": sis,
            "analises": int(g["Tem_Analise"].sum()),
            "leituras": int(g["Tem_Leitura_Macro"].sum()),
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
    sistema: Optional[str] = None, cidade: Optional[str] = None,
    data_ini: Optional[str] = None, data_fim: Optional[str] = None,
    apenas_analise: Optional[bool] = False, apenas_leitura: Optional[bool] = False,
):
    df = get_df_seguro()
    df = filtrar(df, None, None, cidade, sistema, data_ini, data_fim)

    if apenas_analise: df = df[df["Tem_Analise"]]
    if apenas_leitura: df = df[df["Tem_Leitura_Macro"]]

    cols = [
        "Data_Hora_Exibicao", "Gerência", "Pólo", "Cidade", "Sistema",
        "ME_Num", "MS_Num", "MP_Num", "Horimetro_Num",
        "Cloro (mg/L)", "Cor (uH)", "Fluoreto (mg/L)", "Turbidez (uT)",
        "Producao", "Tem_Analise", "Tem_Leitura_Macro"
    ]
    cols = [c for c in cols if c in df.columns]
    df = df[cols].copy()

    df = df.replace({np.nan: None})
    df["Data_Hora_Exibicao"] = df["Data_Hora_Exibicao"].astype(str).replace({"NaT": None, "nan": None})

    rows = df.to_dict("records")
    return {"rows": rows, "total": len(rows)}
