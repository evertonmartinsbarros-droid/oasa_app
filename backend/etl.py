"""
ETL OASA — lê Google Sheets, replica Power Query, calcula produção/qualidade.
Cache em memória com TTL de 5 minutos para não bater a API a cada request.
"""
import os, re, unicodedata, time, json
from typing import Optional
import pandas as pd
import numpy as np
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# Carrega .env em desenvolvimento local. No Render, as variáveis já vêm do
# ambiente (render.yaml) e load_dotenv() simplesmente não encontra arquivo
# e não faz nada — seguro em produção.
load_dotenv()

# ── Configuração ──────────────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

# IDs das planilhas
# Múltiplas planilhas de leituras separadas por vírgula
_ids_leituras_env = os.getenv(
    "SHEET_IDS_LEITURAS",
    "1iPcUbhIqgEpkhjLo5_HcbmNWlsqlEjY24NvRj3F-3u4,1zoca61vK-f4t0s4Utj7vDtuUNdDHp_B826xxMsOdZCs"
)
SHEET_IDS_LEITURAS        = [s.strip() for s in _ids_leituras_env.split(",") if s.strip()]
SHEET_ID_PARTICULARIDADES = os.getenv("SHEET_ID_PARTICULARIDADES", "1rYXoJNGvgZQhWOZNpbPZEblxE8Srkn7IC0Jz5gBPV0Y")
ABA_LEITURAS              = os.getenv("ABA_LEITURAS", "Registos")
ABA_PARTICULARIDADES      = os.getenv("ABA_PARTICULARIDADES", "04_Chaves_Merge")
CACHE_TTL_SEGUNDOS        = int(os.getenv("CACHE_TTL", "300"))  # 5 min

_cache = {"df": None, "ts": 0}

# ── Credenciais ───────────────────────────────────────────────────────────────
def _get_service():
    cred_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if cred_json:
        info = json.loads(cred_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    return build("sheets", "v4", credentials=creds)

# ── Funções auxiliares ────────────────────────────────────────────────────────
def _limpar_texto(v) -> Optional[str]:
    if v is None or str(v).strip() == "":
        return None
    t = str(v).replace("\xa0", " ").replace("_", " ").strip()
    t = " ".join(t.split())
    return t if t else None

def _normalizar_chave(v) -> Optional[str]:
    t = _limpar_texto(v)
    if t is None:
        return None
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    return t.upper().strip()

def _para_numero(v, limite=None) -> Optional[float]:
    if v is None or (isinstance(v, float) and np.isnan(v)) or str(v).strip() == "":
        return None
    if isinstance(v, (int, float)):
        r = float(v)
    else:
        txt = str(v).strip()
        try:
            r = float(txt.replace(",", "."))
        except Exception:
            return None
    if limite is not None and abs(r) > limite:
        return None
    return r

def _para_datetime(v) -> Optional[pd.Timestamp]:
    if v is None or str(v).strip() == "":
        return None
    if isinstance(v, pd.Timestamp):
        return v
    try:
        ts = pd.to_datetime(str(v), dayfirst=True, errors="coerce")
        if pd.isna(ts):
            return None
        if ts.year < 2020 or ts.year > 2030:
            return None
        return ts
    except Exception:
        return None

# ── Leitura Google Sheets ─────────────────────────────────────────────────────
def _ler_sheet(service, sheet_id: str, aba: str) -> pd.DataFrame:
    result = service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=aba
    ).execute()
    values = result.get("values", [])
    if not values:
        return pd.DataFrame()
    header = values[0]
    rows = []
    for row in values[1:]:
        row_padded = row + [None] * (len(header) - len(row))
        rows.append(row_padded[:len(header)])
    return pd.DataFrame(rows, columns=header)

# ── Particularidades ──────────────────────────────────────────────────────────
def _carregar_particularidades(service) -> pd.DataFrame:
    df = _ler_sheet(service, SHEET_ID_PARTICULARIDADES, ABA_PARTICULARIDADES)
    if df.empty:
        return pd.DataFrame(columns=[
            "Cidade_Norm", "Sistema_Norm",
            "Tipo_Regra_Calculo", "Percentual_Desconto", "Usar_Macro_Processo"
        ])

    rename = {
        "Cidade_Ref_Normalizada":  "Cidade_Norm",
        "Sistema_Ref_Normalizado": "Sistema_Norm",
    }
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    if "Percentual_Desconto" in df.columns:
        df["Percentual_Desconto"] = df["Percentual_Desconto"].apply(_para_numero)
    else:
        df["Percentual_Desconto"] = None

    if "Usar_Macro_Processo_Como_Saida2" in df.columns:
        df["Usar_Macro_Processo"] = df["Usar_Macro_Processo_Como_Saida2"].apply(
            lambda v: str(v).strip().upper() in ("TRUE", "1", "VERDADEIRO")
        )
    else:
        df["Usar_Macro_Processo"] = False

    cols = ["Cidade_Norm", "Sistema_Norm", "Tipo_Regra_Calculo",
            "Percentual_Desconto", "Usar_Macro_Processo"]
    for c in cols:
        if c not in df.columns:
            df[c] = None

    df = df[cols].dropna(subset=["Cidade_Norm", "Sistema_Norm"]).drop_duplicates(
        subset=["Cidade_Norm", "Sistema_Norm"]
    )
    return df

# ── Cálculo de produção por grupo (sistema) ───────────────────────────────────
def _calcular_grupo(g: pd.DataFrame) -> pd.DataFrame:
    g = g.sort_values("Data_Hora").reset_index(drop=True)

    dif_me, dif_ms, dif_mp, dif_horas, dias_int = [], [], [], [], []
    prod_base, producao, prod_media_dia = [], [], []

    me_ant = ms_ant = mp_ant = hor_ant = dt_ant = None
    tipo_regra_ant = "SEM_REGRA"
    pct_ant = None

    for i, row in g.iterrows():
        me  = _para_numero(row.get("ME_Num"))
        ms  = _para_numero(row.get("MS_Num"))
        mp  = _para_numero(row.get("MP_Num"))
        hor = _para_numero(row.get("Horimetro_Num"), limite=10_000_000)
        dt  = row["Data_Hora"]
        tipo_regra = row.get("Tipo_Regra_Calculo", "SEM_REGRA") or "SEM_REGRA"
        pct        = _para_numero(row.get("Percentual_Desconto"))

        if me_ant is not None and me is not None and me >= me_ant:
            dme = me - me_ant
        else:
            dme = None

        if ms_ant is not None and ms is not None and ms >= ms_ant:
            dms = ms - ms_ant
        else:
            dms = None

        if mp_ant is not None and mp is not None and mp >= mp_ant:
            dmp = mp - mp_ant
        else:
            dmp = None

        if hor_ant is not None and hor is not None and hor >= hor_ant:
            dhor = hor - hor_ant
        else:
            dhor = None

        if dt_ant is not None and dt is not None and pd.notna(dt_ant) and pd.notna(dt):
            delta_h = (dt - dt_ant).total_seconds() / 3600
            dias = delta_h / 24 if delta_h > 0 else None
        else:
            dias = None

        # base de produção: prioridade Saída > Entrada > Processo
        pb = dms if dms is not None else (dme if dme is not None else dmp)

        # aplicar regra de particularidade
        if pb is not None:
            pct_seguro = pct_ant
            if pct_seguro is not None and pct_seguro > 1:
                pct_seguro = pct_seguro / 100
            if tipo_regra_ant == "DESCONTO_PERCENTUAL" and pct_seguro is not None:
                pf = pb * (1 - pct_seguro)
            elif tipo_regra_ant in ("SUBTRAIR_MACRO_PROCESSO", "SUBTRAIR_SAIDA2"):
                pf = pb - (dmp or 0)
            else:
                pf = pb
            pf = max(0, pf)
        else:
            pf = None

        pm = pf / dias if (pf is not None and dias and dias > 0) else None

        dif_me.append(dme)
        dif_ms.append(dms)
        dif_mp.append(dmp)
        dif_horas.append(dhor)
        dias_int.append(dias)
        prod_base.append(pb)
        producao.append(pf)
        prod_media_dia.append(pm)

        # atualizar estado anterior
        if me is not None: me_ant = me
        if ms is not None: ms_ant = ms
        if mp is not None: mp_ant = mp
        if hor is not None: hor_ant = hor
        dt_ant = dt
        tipo_regra_ant = tipo_regra
        pct_ant = pct

    g["Dif_Macro_Entrada"] = dif_me
    g["Dif_Macro_Saida"]   = dif_ms
    g["Dif_Macro_Processo"] = dif_mp
    g["Dif_Horas"]         = dif_horas
    g["Dias_Intervalo"]    = dias_int
    g["Producao_Base"]     = prod_base
    g["Producao"]          = producao
    g["Producao_Media_Dia"] = prod_media_dia
    return g

# ── ETL principal ─────────────────────────────────────────────────────────────
def _executar_etl() -> pd.DataFrame:
    service = _get_service()

    # 1. Leituras — combina todas as planilhas
    frames = []
    for sid in SHEET_IDS_LEITURAS:
        try:
            f = _ler_sheet(service, sid, ABA_LEITURAS)
            if not f.empty:
                frames.append(f)
        except Exception as e:
            print(f"[ETL] Erro ao ler planilha {sid}: {e}")
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)

    # 2. Limpeza de texto
    for col in ["Pólo", "Cidade", "Sistema", "Gerência"]:
        if col in df.columns:
            df[col] = df[col].apply(_limpar_texto)

    # 3. Data_Hora
    df["Data_Hora"] = df.get("Data/Hora (Leitura)", pd.Series()).apply(_para_datetime)
    df = df[df["Data_Hora"].notna()].copy()
    df["Data"] = df["Data_Hora"].apply(lambda x: x.date() if pd.notna(x) else None)

    # 4. Colunas numéricas
    df["ME_Num"]         = df.get("Macro Entrada",   pd.Series()).apply(_para_numero)
    df["MS_Num"]         = df.get("Macro Saída ",    pd.Series(dtype=str)).apply(_para_numero)
    if df["MS_Num"].isna().all():
        df["MS_Num"]     = df.get("Macro Saída",     pd.Series()).apply(_para_numero)
    df["MP_Num"]         = df.get("Macro Processo",  pd.Series()).apply(_para_numero)
    df["Horimetro_Num"]  = df.get("Horímetro",       pd.Series()).apply(lambda v: _para_numero(v, 10_000_000))

    # 5. Uma leitura por dia por sistema (a de maior Data_Hora)
    df = df.sort_values("Data_Hora")
    df = df.groupby(["Cidade", "Sistema", "Data"], group_keys=False).apply(
        lambda g: g.loc[[g["Data_Hora"].idxmax()]]
    ).reset_index(drop=True)

    # 6. Chave de normalização para merge com particularidades
    df["Cidade_Norm"]  = df["Cidade"].apply(_normalizar_chave)
    df["Sistema_Norm"] = df["Sistema"].apply(_normalizar_chave)

    # 7. Particularidades
    part = _carregar_particularidades(service)
    df = df.merge(part, on=["Cidade_Norm", "Sistema_Norm"], how="left")
    df["Tipo_Regra_Calculo"] = df.get("Tipo_Regra_Calculo", pd.Series()).fillna("SEM_REGRA")

    # 8. Cálculo de produção por grupo
    chave = ["Pólo", "Cidade", "Sistema"]
    df = df.groupby(chave, group_keys=False).apply(_calcular_grupo).reset_index(drop=True)

    # 9. Colunas de flag linha a linha
    qual_cols = ["Cloro (mg/L)", "Cor (uH)", "Fluoreto (mg/L)", "Turbidez (uT)"]
    for col in qual_cols:
        if col not in df.columns:
            df[col] = None
        # converte para número (a planilha vem com vírgula decimal, ex: "1,5").
        # sem isso, /qualidade quebra ao comparar string com float (<=, >).
        df[col] = df[col].apply(_para_numero)

    def _tem_analise(row):
        for c in qual_cols:
            v = _para_numero(row.get(c))
            if v is not None:
                return True
        return False

    def _tem_leitura(row):
        return any(_para_numero(row.get(c)) is not None for c in ["ME_Num", "MS_Num", "MP_Num"])

    df["Tem_Analise"]       = df.apply(_tem_analise, axis=1)
    df["Tem_Leitura_Macro"] = df.apply(_tem_leitura, axis=1)

    # 10. Data_Hora_Exibicao: 00:00 → 23:59
    def _exibicao(dt):
        if pd.isna(dt):
            return dt
        if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
            return dt.replace(hour=23, minute=59, second=0)
        return dt
    df["Data_Hora_Exibicao"] = df["Data_Hora"].apply(_exibicao)

    # 11. Garantir Gerência
    if "Gerência" not in df.columns:
        df["Gerência"] = "OASA"

    return df

# ── Cache público ─────────────────────────────────────────────────────────────
def carregar_dados() -> pd.DataFrame:
    agora = time.time()
    if _cache["df"] is None or (agora - _cache["ts"]) > CACHE_TTL_SEGUNDOS:
        _cache["df"] = _executar_etl()
        _cache["ts"] = agora
    return _cache["df"]

def get_cache_info():
    if _cache["df"] is None:
        return {"status": "vazio"}
    idade = int(time.time() - _cache["ts"])
    linhas = len(_cache["df"])
    return {"status": "ok", "linhas": linhas, "idade_segundos": idade, "ttl": CACHE_TTL_SEGUNDOS}
