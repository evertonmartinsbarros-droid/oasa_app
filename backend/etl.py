"""
ETL OASA — lê Google Sheets, replica Power Query, calcula produção/qualidade.
Otimizado para Baixo Consumo de Memória (Serverless/Free Tier < 512MB)
Cache em memória com TTL de 5 minutos.
"""

import os
import unicodedata
import time
import json
import gc
import threading
from typing import Optional

import pandas as pd
import numpy as np
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


load_dotenv()


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


_ids_leituras_env = os.getenv(
    "SHEET_IDS_LEITURAS",
    "1iPcUbhIqgEpkhjLo5_HcbmNWlsqlEjY24NvRj3F-3u4,1zoca61vK-f4t0s4Utj7vDtuUNdDHp_B826xxMsOdZCs"
)

SHEET_IDS_LEITURAS = [
    s.strip()
    for s in _ids_leituras_env.split(",")
    if s.strip()
]

SHEET_ID_PARTICULARIDADES = os.getenv(
    "SHEET_ID_PARTICULARIDADES",
    "1rYXoJNGvgZQhWOZNpbPZEblxE8Srkn7IC0Jz5gBPV0Y"
)

ABA_LEITURAS = os.getenv("ABA_LEITURAS", "Registos")
ABA_PARTICULARIDADES = os.getenv("ABA_PARTICULARIDADES", "04_Chaves_Merge")
CACHE_TTL_SEGUNDOS = int(os.getenv("CACHE_TTL", "300"))


COLUNAS_LEITURAS = [
    "Data/Hora (Leitura)",
    "Gerência",
    "Pólo",
    "Cidade",
    "Sistema",
    "Macro Entrada",
    "Macro Saída ",
    "Macro Processo",
    "Horímetro",
    "Turbidez (uT)",
    "Cor (uH)",
    "Cloro (mg/L)",
    "Fluoreto (mg/L)",
]


_cache = {
    "df": None,
    "ts": 0,
}

_cache_lock = threading.Lock()


# =========================================================
# GOOGLE SERVICE
# =========================================================

def _get_service():
    cred_json = os.getenv("GOOGLE_CREDENTIALS_JSON")

    if cred_json:
        info = json.loads(cred_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)

    return build("sheets", "v4", credentials=creds)


# =========================================================
# LIMPEZA / NORMALIZAÇÃO
# =========================================================

def _limpar_texto(v) -> Optional[str]:
    if pd.isna(v) or str(v).strip() == "":
        return None

    t = " ".join(
        str(v)
        .replace("\xa0", " ")
        .replace("_", " ")
        .split()
    )

    return t if t else None


def _normalizar_chave(v) -> Optional[str]:
    t = _limpar_texto(v)

    if not t:
        return None

    t = unicodedata.normalize("NFKD", t)

    return "".join(
        c for c in t
        if not unicodedata.combining(c)
    ).upper().strip()


# =========================================================
# LEITURA GOOGLE SHEETS
# =========================================================

def _ler_sheet(
    service,
    sheet_id: str,
    aba: str,
    colunas_filtro=None,
) -> pd.DataFrame:
    result = (
        service
        .spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range=aba)
        .execute()
    )

    values = result.get("values", [])

    if not values:
        return pd.DataFrame()

    header = values[0]
    df = pd.DataFrame(values[1:], columns=header)

    del values
    del result
    gc.collect()

    if colunas_filtro:
        cols_existentes = [
            c for c in colunas_filtro
            if c in df.columns
        ]

        df = df[cols_existentes]

    return df


# =========================================================
# PARTICULARIDADES
# =========================================================

def _carregar_particularidades(service) -> pd.DataFrame:
    try:
        df_part = _ler_sheet(
            service,
            SHEET_ID_PARTICULARIDADES,
            ABA_PARTICULARIDADES,
        )

        if df_part.empty:
            return pd.DataFrame(
                columns=[
                    "Cidade_Norm",
                    "Sistema_Norm",
                    "Tipo_Regra_Calculo",
                    "Percentual_Desconto",
                ]
            )

        df_part = df_part.rename(
            columns={
                "Cidade_Ref_Normalizada": "Cidade_Norm",
                "Sistema_Ref_Normalizado": "Sistema_Norm",
            }
        )

        if (
            "Cidade_Norm" not in df_part.columns
            or "Sistema_Norm" not in df_part.columns
        ):
            return pd.DataFrame(
                columns=[
                    "Cidade_Norm",
                    "Sistema_Norm",
                    "Tipo_Regra_Calculo",
                    "Percentual_Desconto",
                ]
            )

        df_part["Cidade_Norm"] = df_part["Cidade_Norm"].apply(_normalizar_chave)
        df_part["Sistema_Norm"] = df_part["Sistema_Norm"].apply(_normalizar_chave)

        if "Percentual_Desconto" in df_part.columns:
            df_part["Percentual_Desconto"] = pd.to_numeric(
                df_part["Percentual_Desconto"]
                .astype(str)
                .str.replace("%", "", regex=False)
                .str.replace(",", ".", regex=False),
                errors="coerce",
            )
        else:
            df_part["Percentual_Desconto"] = None

        if "Tipo_Regra_Calculo" not in df_part.columns:
            df_part["Tipo_Regra_Calculo"] = "SEM_REGRA"

        df_part = df_part.drop_duplicates(
            subset=[
                "Cidade_Norm",
                "Sistema_Norm",
            ]
        )

        return df_part[
            [
                "Cidade_Norm",
                "Sistema_Norm",
                "Tipo_Regra_Calculo",
                "Percentual_Desconto",
            ]
        ]

    except Exception as e:
        print(f"[ETL] Erro ao carregar particularidades: {e}")

        return pd.DataFrame(
            columns=[
                "Cidade_Norm",
                "Sistema_Norm",
                "Tipo_Regra_Calculo",
                "Percentual_Desconto",
            ]
        )


# =========================================================
# CÁLCULO POR GRUPO
# =========================================================

def _calcular_grupo(g: pd.DataFrame) -> pd.DataFrame:
    g = g.sort_values("Data_Hora").reset_index(drop=True)

    dif_me = []
    dif_ms = []
    dif_mp = []
    dif_horas = []
    dias_int = []
    prod_base = []
    producao = []
    prod_media_dia = []

    me_ant = None
    ms_ant = None
    mp_ant = None
    hor_ant = None
    dt_ant = None

    tipo_regra_ant = "SEM_REGRA"
    pct_ant = None

    for row in g.itertuples():
        me = row.ME_Num if pd.notna(row.ME_Num) else None
        ms = row.MS_Num if pd.notna(row.MS_Num) else None
        mp = row.MP_Num if pd.notna(row.MP_Num) else None
        hor = row.Horimetro_Num if pd.notna(row.Horimetro_Num) else None
        dt = row.Data_Hora if pd.notna(row.Data_Hora) else None

        tipo_regra = (
            row.Tipo_Regra_Calculo
            if pd.notna(row.Tipo_Regra_Calculo)
            else "SEM_REGRA"
        )

        pct = (
            row.Percentual_Desconto
            if pd.notna(row.Percentual_Desconto)
            else None
        )

        dme = (
            me - me_ant
            if (
                me_ant is not None
                and me is not None
                and me >= me_ant
            )
            else None
        )

        dms = (
            ms - ms_ant
            if (
                ms_ant is not None
                and ms is not None
                and ms >= ms_ant
            )
            else None
        )

        dmp = (
            mp - mp_ant
            if (
                mp_ant is not None
                and mp is not None
                and mp >= mp_ant
            )
            else None
        )

        dhor = (
            hor - hor_ant
            if (
                hor_ant is not None
                and hor is not None
                and hor >= hor_ant
            )
            else None
        )

        if dt_ant is not None and dt is not None:
            delta_h = (dt - dt_ant).total_seconds() / 3600
            dias = delta_h / 24 if delta_h > 0 else None
        else:
            dias = None

        # Prioridade:
        # Macro Saída > Macro Entrada > Macro Processo
        pb = dms if dms is not None else (
            dme if dme is not None else dmp
        )

        if pb is not None:
            pct_seguro = pct_ant

            if pct_seguro is not None and pct_seguro > 1:
                pct_seguro = pct_seguro / 100

            if (
                tipo_regra_ant == "DESCONTO_PERCENTUAL"
                and pct_seguro is not None
            ):
                pf = pb * (1 - pct_seguro)

            elif tipo_regra_ant in (
                "SUBTRAIR_MACRO_PROCESSO",
                "SUBTRAIR_SAIDA2",
            ):
                pf = pb - (dmp or 0)

            else:
                pf = pb

            pf = max(0, pf)

        else:
            pf = None

        pm = (
            pf / dias
            if (
                pf is not None
                and dias
                and dias > 0
            )
            else None
        )

        dif_me.append(dme)
        dif_ms.append(dms)
        dif_mp.append(dmp)
        dif_horas.append(dhor)nd(dias)
        prod_base.append(pb)
        producao.append(pf)
        prod_media_dia.append(pm)

        if me is not None:
            me_ant = me

        if ms is not None:
            ms_ant = ms

        if mp is not None:
            mp_ant = mp

        if hor is not None:
            hor_ant = hor

        dt_ant = dt
        tipo_regra_ant = tipo_regra
        pct_ant = pct

    g = g.assign(
        Dif_Macro_Entrada=dif_me,
        Dif_Macro_Saida=dif_ms,
        Dif_Macro_Processo=dif_mp,
        Dif_Horas=dif_horas,
        Dias_Intervalo=dias_int,
        Producao_Base=prod_base,
        Producao=producao,
        Producao_Media_Dia=prod_media_dia,
    )

    return g


# =========================================================
# EXECUTAR ETL
# =========================================================

def _executar_etl() -> pd.DataFrame:
    service = _get_service()
    frames = []

    for sid in SHEET_IDS_LEITURAS:
        try:
            f = _ler_sheet(
                service,
                sid,
                ABA_LEITURAS,
                colunas_filtro=COLUNAS_LEITURAS,
            )

            if not f.empty:
                f = f.dropna(axis=1, how="all")

                print(f"[ETL] Planilha {sid}: {len(f)} linhas lidas.")

                frames.append(f)

        except Exception as e:
            print(f"[ETL] Erro ao ler planilha {sid}: {e}")

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)

    del frames
    gc.collect()

    col_str = [
        "Pólo",
        "Cidade",
        "Sistema",
        "Gerência",
    ]

    for col in col_str:
        if col in df.columns:
            df[col] = df[col].apply(_limpar_texto)

    if "Gerência" not in df.columns:
        df["Gerência"] = "OASA"

    df["Data_Hora"] = pd.to_datetime(
        df.get(
            "Data/Hora (Leitura)",
            pd.Series(dtype="object"),
        ),
        dayfirst=True,
        errors="coerce",
        format="mixed",
    )

    df = df.dropna(subset=["Data_Hora"]).copy()
    df["Data"] = df["Data_Hora"].dt.date

    df.drop(
        columns=["Data/Hora (Leitura)"],
        errors="ignore",
        inplace=True,
    )

    # =========================================================
    # CONVERSÃO NUMÉRICA
    # =========================================================

    def to_float32(col_name, limit=None, zero_as_na=False):
        """
        Converte coluna para float32.

        zero_as_na=True:
        - trata valor 0 como vazio/sem leitura;
        - deve ser usado para macros e horímetro;
        - não deve ser usado em análises de qualidade.
        """

        if col_name in df.columns:
            s = df[col_name].astype(str).str.strip()

            s = s.replace(
                {
                    "": np.nan,
                    "nan": np.nan,
                    "NaN": np.nan,
                    "None": np.nan,
                    "-": np.nan,
                }
            )

            s = s.apply(
                lambda x: x.replace(".", "").replace(",", ".")
                if isinstance(x, str) and "," in x
                else x
            )

            s = pd.to_numeric(s, errors="coerce")

            if limit:
                s = s.where(s.abs() <= limit)

            if zero_as_na:
                s = s.mask(s == 0)

            return s.astype("float32")

        return pd.Series(
            np.nan,
            index=df.index,
            dtype="float32",
        )

    # =========================================================
    # MACROS / HORÍMETRO
    # =========================================================
    # Valor 0 deve ser tratado como sem leitura.

    df["ME_Num"] = to_float32(
        "Macro Entrada",
        zero_as_na=True,
    )

    df["MS_Num"] = (
        to_float32(
            "Macro Saída ",
            zero_as_na=True,
        )
        if "Macro Saída " in df.columns
        else to_float32(
            "Macro Saída",
            zero_as_na=True,
        )
    )

    df["MP_Num"] = to_float32(
        "Macro Processo",
        zero_as_na=True,
    )

    df["Horimetro_Num"] = to_float32(
        "Horímetro",
        limit=10_000_000,
        zero_as_na=True,
    )

    df.drop(
        columns=[
            "Macro Entrada",
            "Macro Saída ",
            "Macro Saída",
            "Macro Processo",
            "Horímetro",
        ],
        errors="ignore",
        inplace=True,
    )

    gc.collect()

    df = df.sort_values("Data_Hora")

    df = df.groupby(
        [
            "Cidade",
            "Sistema",
            "Data",
        ],
        as_index=False,
    ).last()

    for col in col_str:
        if col in df.columns:
            df[col] = df[col].astype("category")

    df["Cidade_Norm"] = df["Cidade"].apply(_normalizar_chave)
    df["Sistema_Norm"] = df["Sistema"].apply(_normalizar_chave)

    part = _carregar_particularidades(service)

    df = df.merge(
        part,
        on=[
            "Cidade_Norm",
            "Sistema_Norm",
        ],
        how="left",
    )

    df["Tipo_Regra_Calculo"] = (
        df["Tipo_Regra_Calculo"]
        .fillna("SEM_REGRA")
        .astype("category")
    )

    chave = [
        "Pólo",
        "Cidade",
        "Sistema",
    ]

    df = (
        df.groupby(
            chave,
            group_keys=False,
            observed=True,
        )
        .apply(_calcular_grupo)
        .reset_index(drop=True)
    )

    # =========================================================
    # ANÁLISES DE QUALIDADE
    # =========================================================
    # Não usa zero_as_na nas análises.

    qual_cols = [
        "Cloro (mg/L)",
        "Cor (uH)",
        "Fluoreto (mg/L)",
        "Turbidez (uT)",
    ]

    for col in qual_cols:
        df[col] = to_float32(col)

    df["Tem_Analise"] = df[qual_cols].notna().any(axis=1)

    df["Tem_Leitura_Macro"] = (
        df[
            [
                "ME_Num",
                "MS_Num",
                "MP_Num",
            ]
        ]
        .notna()
        .any(axis=1)
    )

    mask_midnight = (
        (df["Data_Hora"].dt.hour == 0)
        & (df["Data_Hora"].dt.minute == 0)
        & (df["Data_Hora"].dt.second == 0)
    )

    df["Data_Hora_Exibicao"] = df["Data_Hora"]

    df.loc[
        mask_midnight,
        "Data_Hora_Exibicao",
    ] = (
        df.loc[
            mask_midnight,
            "Data_Hora",
        ]
        + pd.Timedelta(
            hours=23,
            minutes=59,
        )
    )

    print(f"[ETL] Concluído. Shape: {df.shape}.")

    gc.collect()

    return df


# =========================================================
# CACHE
# =========================================================

def carregar_dados() -> pd.DataFrame:
    agora = time.time()

    if (
        _cache["df"] is not None
        and (agora - _cache["ts"]) <= CACHE_TTL_SEGUNDOS
    ):
        return _cache["df"]

    with _cache_lock:
        agora = time.time()

        if (
            _cache["df"] is not None
            and (agora - _cache["ts"]) <= CACHE_TTL_SEGUNDOS
        ):
            return _cache["df"]

        _cache["df"] = _executar_etl()
        _cache["ts"] = agora

    return _cache["df"]


def get_cache_info():
    if _cache["df"] is None:
        return {
            "status": "vazio",
        }

    idade = int(time.time() - _cache["ts"])
    linhas = len(_cache["df"])

    return {
        "status": "ok",
        "linhas": linhas,
        "idade_segundos": idade,
        "ttl": CACHE_TTL_SEGUNDOS,
    }
