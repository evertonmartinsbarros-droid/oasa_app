from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from typing import Optional
import asyncio
import math
import calendar
from datetime import datetime, date

import pandas as pd
import numpy as np

from etl import carregar_dados, get_cache_info


# =========================================================
# FUNÇÕES DE SEGURANÇA PARA JSON
# =========================================================

def valor_json_seguro(valor):
    """
    Converte valores Pandas/Numpy para tipos nativos do Python,
    evitando erro de serialização no FastAPI.
    """

    if valor is None:
        return None

    try:
        if pd.isna(valor):
            return None
    except Exception:
        pass

    if isinstance(valor, (np.integer,)):
        return int(valor)

    if isinstance(valor, (np.floating,)):
        valor = float(valor)
        if math.isnan(valor) or math.isinf(valor):
            return None
        return valor

    if isinstance(valor, (np.bool_,)):
        return bool(valor)

    if isinstance(valor, (pd.Timestamp, datetime, date)):
        return valor.isoformat()

    if isinstance(valor, float):
        if math.isnan(valor) or math.isinf(valor):
            return None
        return valor

    return valor


def dict_json_seguro(d):
    """
    Aplica conversão segura em um dicionário.
    """
    return {k: valor_json_seguro(v) for k, v in d.items()}


def lista_json_segura(lista):
    """
    Aplica conversão segura em uma lista de dicionários.
    """
    return [dict_json_seguro(item) for item in lista]


def arredondar_seguro(valor, casas=2):
    """
    Arredonda somente se o valor for válido.
    Retorna float nativo do Python.
    """
    if valor is None:
        return None

    try:
        if pd.isna(valor):
            return None
    except Exception:
        pass

    try:
        valor = float(valor)
        if math.isnan(valor) or math.isinf(valor):
            return None
        return round(valor, casas)
    except Exception:
        return None


# =========================================================
# LIFESPAN
# =========================================================

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
    allow_origins=[
        "https://oasa-frontend-79mk.onrender.com",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# BLINDAGEM DA API
# =========================================================

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


def filtrar(
    df,
    gerencia=None,
    polo=None,
    cidade=None,
    sistema=None,
    data_ini=None,
    data_fim=None,
):
    mask = pd.Series(True, index=df.index)

    if gerencia:
        mask &= df["Gerência"].astype(str).eq(str(gerencia))

    if polo:
        mask &= df["Pólo"].astype(str).eq(str(polo))

    if cidade:
        mask &= df["Cidade"].astype(str).eq(str(cidade))

    if sistema:
        mask &= df["Sistema"].astype(str).eq(str(sistema))

    if data_ini or data_fim:
        if "Data" in df.columns:
            data_str = df["Data"].astype(str)

            if data_ini:
                mask &= data_str >= data_ini

            if data_fim:
                mask &= data_str <= data_fim

    return df[mask]


# =========================================================
# ROTAS BÁSICAS
# =========================================================

@app.get("/")
def home():
    return {
        "ok": True,
        "mensagem": "OASA Dashboard API online",
        "docs": "/docs",
        "status": "/status",
    }


@app.get("/status")
def status():
    return {
        "ok": True,
        "cache": get_cache_info(),
    }


# =========================================================
# FILTROS
# =========================================================

@app.get("/filtros")
def filtros():
    df = get_df_seguro()

    if df.empty:
        return {
            "combinacoes": [],
            "gerencias": [],
            "polos": [],
            "cidades": [],
            "sistemas": [],
        }

    cols = ["Gerência", "Pólo", "Cidade", "Sistema"]

    for col in cols:
        if col not in df.columns:
            df[col] = ""

    comb = df[cols].drop_duplicates().astype(object).fillna("")
    combinacoes = comb.to_dict(orient="records")

    return {
        "combinacoes": lista_json_segura(combinacoes),
        "gerencias": sorted([str(x) for x in df["Gerência"].dropna().unique().tolist()]),
        "polos": sorted([str(x) for x in df["Pólo"].dropna().unique().tolist()]),
        "cidades": sorted([str(x) for x in df["Cidade"].dropna().unique().tolist()]),
        "sistemas": sorted([str(x) for x in df["Sistema"].dropna().unique().tolist()]),
    }


# =========================================================
# PRODUÇÃO
# =========================================================

@app.get("/producao")
def producao(
    gerencia: Optional[str] = None,
    polo: Optional[str] = None,
    cidade: Optional[str] = None,
    sistema: Optional[str] = None,
    data_ini: Optional[str] = None,
    data_fim: Optional[str] = None,
):
    df = get_df_seguro()
    df = filtrar(df, gerencia, polo, cidade, sistema, data_ini, data_fim)

    if df.empty:
        return {"rows": []}

    colunas_obrigatorias = [
        "Gerência",
        "Pólo",
        "Cidade",
        "Sistema",
        "Data_Hora",
        "Tem_Leitura_Macro",
    ]

    for col in colunas_obrigatorias:
        if col not in df.columns:
            return {"rows": []}

    grp = df.groupby(
        ["Gerência", "Pólo", "Cidade", "Sistema"],
        observed=True,
        dropna=False,
    )

    rows = []

    for (ger, pol, cid, sis), g in grp:
        g = g.copy()
        g = g.dropna(subset=["Data_Hora"])

        if g.empty:
            continue

        g = g.sort_values("Data_Hora")

        # =========================================================
        # REGRA PRINCIPAL:
        # PRODUÇÃO USA SOMENTE LINHAS COM LEITURA DE MACRO
        # =========================================================
        # Linha apenas com análise não entra no cálculo de produção.
        # Linha apenas com leitura entra.
        # Linha com análise + leitura entra.

        leit = g[g["Tem_Leitura_Macro"] == True].copy()
        leit = leit.dropna(subset=["Data_Hora"])

        if leit.empty:
            continue

        leit = leit.sort_values("Data_Hora")

        # =========================================================
        # ESCOLHER MACRO PARA CÁLCULO DOS EXTREMOS
        # =========================================================
        # Prioridade:
        # 1. Macro Saída - MS_Num
        # 2. Macro Entrada - ME_Num

        col_macro = None

        if "MS_Num" in leit.columns and leit["MS_Num"].notna().any():
            col_macro = "MS_Num"
        elif "ME_Num" in leit.columns and leit["ME_Num"].notna().any():
            col_macro = "ME_Num"

        if not col_macro:
            continue

        leit_macro = leit.dropna(subset=[col_macro]).copy()

        if leit_macro.empty:
            continue

        leit_macro = leit_macro.sort_values("Data_Hora")

        primeira = leit_macro.iloc[0]
        ultima = leit_macro.iloc[-1]

        dt_ini = primeira["Data_Hora"]
        dt_fim = ultima["Data_Hora"]

        val_ini = valor_json_seguro(primeira[col_macro])
        val_fim = valor_json_seguro(ultima[col_macro])

        try:
            val_ini = float(val_ini)
            val_fim = float(val_fim)
        except Exception:
            continue

        # =========================================================
        # PRODUÇÃO PRINCIPAL PELOS EXTREMOS
        # =========================================================
        # Esta é a regra solicitada:
        # produção média/produção do período = macro final - macro inicial.

        prod_extremos = None

        if dt_fim > dt_ini and val_fim >= val_ini:
            prod_extremos = val_fim - val_ini

        # =========================================================
        # PRODUÇÃO DAS LINHAS APENAS PARA CONFERÊNCIA/FALLBACK
        # =========================================================
        # A coluna Producao pode conter particularidades do ETL.
        # Aqui ela NÃO é a regra principal.
        # Só entra se a diferença dos extremos não for possível.

        prod_linhas = None

        if "Producao" in leit.columns:
            serie_prod = pd.to_numeric(leit["Producao"], errors="coerce").dropna()

            if not serie_prod.empty:
                prod_linhas = float(serie_prod.sum())

        # Produção final do dashboard:
        # 1. Diferença dos extremos.
        # 2. Fallback: soma da coluna Producao das linhas com leitura.
        # 3. Fallback final: zero.

        prod = prod_extremos

        if prod is None:
            prod = prod_linhas

        if prod is None:
            prod = 0

        # =========================================================
        # TEMPO ENTRE EXTREMOS PARA MÉDIA DIÁRIA
        # =========================================================
        # Usa a data/hora da primeira e última leitura válida.

        horas_periodo = None
        dias = None
        media_dia = None

        try:
            if dt_fim > dt_ini:
                horas_periodo = (dt_fim - dt_ini).total_seconds() / 3600
                dias = horas_periodo / 24.0

                if dias > 0:
                    media_dia = prod / dias
        except Exception:
            horas_periodo = None
            dias = None
            media_dia = None

        # =========================================================
        # DIFERENÇA DE HORÍMETRO
        # =========================================================
        # Também usa somente linhas com leitura de macro.

        dif_hor = None

        if "Horimetro_Num" in leit.columns:
            leit_hor = leit.dropna(subset=["Horimetro_Num"]).copy()

            if not leit_hor.empty:
                leit_hor = leit_hor.sort_values("Data_Hora")

                try:
                    hor_ini = float(valor_json_seguro(leit_hor.iloc[0]["Horimetro_Num"]))
                    hor_fim = float(valor_json_seguro(leit_hor.iloc[-1]["Horimetro_Num"]))

                    if hor_fim >= hor_ini:
                        dif_hor = hor_fim - hor_ini
                except Exception:
                    dif_hor = None

        # =========================================================
        # PROJEÇÃO MENSAL
        # =========================================================
        # Baseada na média diária da diferença dos extremos.

        projecao_mensal = None

        try:
            if media_dia is not None and pd.notna(ultima["Data_Hora"]):
                _, dias_no_mes = calendar.monthrange(
                    ultima["Data_Hora"].year,
                    ultima["Data_Hora"].month,
                )

                projecao_mensal = media_dia * dias_no_mes
        except Exception:
            projecao_mensal = None

        # =========================================================
        # VAZÃO PELO HORÍMETRO
        # =========================================================
        # Usa produção dos extremos / diferença do horímetro.

        vazao_m3h = None
        vazao_ls = None

        if dif_hor is not None and dif_hor > 0:
            try:
                vazao_m3h = prod / dif_hor
                vazao_ls = vazao_m3h * 1000 / 3600
            except Exception:
                vazao_m3h = None
                vazao_ls = None

        linha = {
            "gerencia": str(valor_json_seguro(ger)),
            "polo": str(valor_json_seguro(pol)),
            "cidade": str(valor_json_seguro(cid)),
            "sistema": str(valor_json_seguro(sis)),

            # Produção principal: diferença dos extremos.
            "producao": arredondar_seguro(prod, 2),

            # Conferências.
            "producao_extremos": arredondar_seguro(prod_extremos, 2),
            "producao_linhas": arredondar_seguro(prod_linhas, 2),

            # Média/projeção baseadas nos extremos.
            "media_dia": arredondar_seguro(media_dia, 2),
            "projecao_mensal": arredondar_seguro(projecao_mensal, 2),

            # Vazão baseada na produção dos extremos e horímetro.
            "vazao_m3h": arredondar_seguro(vazao_m3h, 2),
            "vazao_ls": arredondar_seguro(vazao_ls, 4),

            # Tempo corrido entre primeira e última leitura.
            "horas": arredondar_seguro(horas_periodo, 2),

            # Diferença do horímetro para o frontend calcular H/D.
            "horimetro_horas": arredondar_seguro(dif_hor, 2),
        }

        rows.append(dict_json_seguro(linha))

    rows.sort(
        key=lambda r: r["producao"] if r["producao"] is not None else 0,
        reverse=True,
    )

    return {"rows": rows}


# =========================================================
# QUALIDADE
# =========================================================

@app.get("/qualidade")
def qualidade(
    gerencia: Optional[str] = None,
    polo: Optional[str] = None,
    cidade: Optional[str] = None,
    sistema: Optional[str] = None,
    data_ini: Optional[str] = None,
    data_fim: Optional[str] = None,
):
    df = get_df_seguro()
    df = filtrar(df, gerencia, polo, cidade, sistema, data_ini, data_fim)

    if df.empty or "Tem_Analise" not in df.columns:
        return {
            "cloro": {"adequado": 0, "inadequado": 0, "abaixo_min": 0, "total": 0},
            "turbidez": {"adequado": 0, "inadequado": 0, "total": 0},
            "cor": {"adequado": 0, "inadequado": 0, "total": 0},
            "fluoreto": {"adequado": 0, "inadequado": 0, "total": 0},
        }

    an = df[df["Tem_Analise"] == True]

    def cnt(col, op, val):
        if col not in an.columns:
            return 0

        s = pd.to_numeric(an[col], errors="coerce").dropna()

        if op == "<=":
            return int((s <= val).sum())

        if op == ">":
            return int((s > val).sum())

        return 0

    def total_col(col):
        if col not in an.columns:
            return 0

        return int(pd.to_numeric(an[col], errors="coerce").dropna().count())

    cloro_col = "Cloro (mg/L)"
    turbidez_col = "Turbidez (uT)"
    cor_col = "Cor (uH)"
    fluoreto_col = "Fluoreto (mg/L)"

    abaixo_min_cloro = 0

    if cloro_col in an.columns:
        s_cloro = pd.to_numeric(an[cloro_col], errors="coerce").dropna()
        abaixo_min_cloro = int((s_cloro < 0.2).sum())

    return {
        "cloro": {
            "adequado": cnt(cloro_col, "<=", 5.0),
            "inadequado": cnt(cloro_col, ">", 5.0),
            "abaixo_min": abaixo_min_cloro,
            "total": total_col(cloro_col),
        },
        "turbidez": {
            "adequado": cnt(turbidez_col, "<=", 1.0),
            "inadequado": cnt(turbidez_col, ">", 1.0),
            "total": total_col(turbidez_col),
        },
        "cor": {
            "adequado": cnt(cor_col, "<=", 15),
            "inadequado": cnt(cor_col, ">", 15),
            "total": total_col(cor_col),
        },
        "fluoreto": {
            "adequado": cnt(fluoreto_col, "<=", 1.5),
            "inadequado": cnt(fluoreto_col, ">", 1.5),
            "total": total_col(fluoreto_col),
        },
    }


# =========================================================
# ACOMPANHAMENTO
# =========================================================

@app.get("/acompanhamento")
def acompanhamento(
    gerencia: Optional[str] = None,
    polo: Optional[str] = None,
    cidade: Optional[str] = None,
    sistema: Optional[str] = None,
    data_ini: Optional[str] = None,
    data_fim: Optional[str] = None,
):
    df = get_df_seguro()
    df = filtrar(df, gerencia, polo, cidade, sistema, data_ini, data_fim)

    if df.empty:
        return {"rows": []}

    for col in ["Tem_Analise", "Tem_Leitura_Macro"]:
        if col not in df.columns:
            df[col] = False

    grp = df.groupby(
        ["Gerência", "Pólo", "Cidade", "Sistema"],
        observed=True,
        dropna=False,
    )

    rows = []

    for (ger, pol, cid, sis), g in grp:
        linha = {
            "gerencia": str(valor_json_seguro(ger)),
            "polo": str(valor_json_seguro(pol)),
            "cidade": str(valor_json_seguro(cid)),
            "sistema": str(valor_json_seguro(sis)),
            "analises": int(g["Tem_Analise"].sum()),
            "leituras": int(g["Tem_Leitura_Macro"].sum()),
        }

        rows.append(dict_json_seguro(linha))

    rows.sort(key=lambda r: r["analises"])

    rank = 1
    prev = None

    for i, r in enumerate(rows):
        if r["analises"] != prev:
            rank = i + 1
            prev = r["analises"]

        r["rank"] = rank

    return {"rows": rows}


# =========================================================
# LEITURAS
# =========================================================

@app.get("/leituras")
def leituras(
    sistema: Optional[str] = None,
    cidade: Optional[str] = None,
    data_ini: Optional[str] = None,
    data_fim: Optional[str] = None,
    apenas_analise: Optional[bool] = False,
    apenas_leitura: Optional[bool] = False,
):
    df = get_df_seguro()
    df = filtrar(df, None, None, cidade, sistema, data_ini, data_fim)

    if df.empty:
        return {"rows": [], "total": 0}

    if apenas_analise and "Tem_Analise" in df.columns:
        df = df[df["Tem_Analise"] == True]

    if apenas_leitura and "Tem_Leitura_Macro" in df.columns:
        df = df[df["Tem_Leitura_Macro"] == True]

    cols = [
        "Data_Hora_Exibicao",
        "Gerência",
        "Pólo",
        "Cidade",
        "Sistema",
        "ME_Num",
        "MS_Num",
        "MP_Num",
        "Horimetro_Num",
        "Cloro (mg/L)",
        "Cor (uH)",
        "Fluoreto (mg/L)",
        "Turbidez (uT)",
        "Producao",
        "Tem_Analise",
        "Tem_Leitura_Macro",
    ]

    cols = [c for c in cols if c in df.columns]

    df = df[cols].copy()
    df = df.replace({np.nan: None})

    if "Data_Hora_Exibicao" in df.columns:
        df["Data_Hora_Exibicao"] = (
            df["Data_Hora_Exibicao"]
            .astype(str)
            .replace({"NaT": None, "nan": None, "None": None})
        )

    rows = df.to_dict("records")
    rows = lista_json_segura(rows)

    return {
        "rows": rows,
        "total": len(rows),
    }
