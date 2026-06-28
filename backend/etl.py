ABA_PARTICULARIDADES      = os.getenv("ABA_PARTICULARIDADES", "04_Chaves_Merge")
CACHE_TTL_SEGUNDOS        = int(os.getenv("CACHE_TTL", "300"))

COLUNAS_LEITURAS = [
    "Data/Hora (Leitura)", "Gerência", "Pólo", "Cidade", "Sistema",
    "Macro Entrada", "Macro Saída ", "Macro Processo", "Horímetro",
    "Turbidez (uT)", "Cor (uH)", "Cloro (mg/L)", "Fluoreto (mg/L)",
]
# Mapeamento de nomes alternativos que o Sheets pode retornar
# chave = nome canônico interno, valores = variações aceitas
ALIAS_COLUNAS = {
    "Pólo":            ["Pólo", "Polo", "polo", "POLO", "Pólo "],
    "Cidade":          ["Cidade", "cidade", "municipio", "Município", "Municipio", "CIDADE"],
    "Sistema":         ["Sistema", "sistema", "SISTEMA"],
    "Gerência":        ["Gerência", "Gerencia", "gerencia", "GERÊNCIA", "GERENCIA"],
    "Data/Hora (Leitura)": ["Data/Hora (Leitura)", "Data/Hora(Leitura)", "Data Hora Leitura"],
    "Macro Entrada":   ["Macro Entrada", "MacroEntrada", "macro_entrada"],
    "Macro Saída ":    ["Macro Saída ", "Macro Saída", "MacroSaida", "macro_saida"],
    "Macro Processo":  ["Macro Processo", "MacroProcesso", "macro_processo"],
    "Horímetro":       ["Horímetro", "Horimetro", "horimetro", "HORÍMETRO"],
    "Turbidez (uT)":   ["Turbidez (uT)", "Turbidez(uT)", "turbidez"],
    "Cor (uH)":        ["Cor (uH)", "Cor(uH)", "cor"],
    "Cloro (mg/L)":    ["Cloro (mg/L)", "Cloro(mg/L)", "cloro"],
    "Fluoreto (mg/L)": ["Fluoreto (mg/L)", "Fluoreto(mg/L)", "fluoreto"],
}

_cache = {"df": None, "ts": 0}


# ── Credenciais ───────────────────────────────────────────────────────────────
def _get_service():
cred_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
@@ -47,18 +60,21 @@ def _get_service():
creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
return build("sheets", "v4", credentials=creds)


# ── Funções auxiliares ────────────────────────────────────────────────────────
def _limpar_texto(v) -> Optional[str]:
if pd.isna(v) or str(v).strip() == "": return None
t = " ".join(str(v).replace("\xa0", " ").replace("_", " ").split())
return t if t else None


def _normalizar_chave(v) -> Optional[str]:
t = _limpar_texto(v)
if not t: return None
t = unicodedata.normalize("NFKD", t)
return "".join(c for c in t if not unicodedata.combining(c)).upper().strip()


def _para_numero(v, limit=None) -> Optional[float]:
if pd.isna(v) or str(v).strip() == "": return None
try:
@@ -68,6 +84,21 @@ def _para_numero(v, limit=None) -> Optional[float]:
except:
return None


def _normalizar_colunas(df: pd.DataFrame) -> pd.DataFrame:
    """Renomeia colunas do DataFrame para os nomes canônicos usando ALIAS_COLUNAS."""
    rename_map = {}
    for nome_canonico, aliases in ALIAS_COLUNAS.items():
        for alias in aliases:
            if alias in df.columns and alias != nome_canonico:
                rename_map[alias] = nome_canonico
                break
    if rename_map:
        print(f"[ETL] Renomeando colunas: {rename_map}")
        df = df.rename(columns=rename_map)
    return df


# ── Leitura Google Sheets ─────────────────────────────────────────────────────
def _ler_sheet(service, sheet_id: str, aba: str, colunas_filtro=None) -> pd.DataFrame:
result = service.spreadsheets().values().get(spreadsheetId=sheet_id, range=aba).execute()
@@ -81,33 +112,30 @@ def _ler_sheet(service, sheet_id: str, aba: str, colunas_filtro=None) -> pd.Data
del result
gc.collect()

    # Normaliza nomes de colunas antes de filtrar
    df = _normalizar_colunas(df)

if colunas_filtro:
cols_existentes = [c for c in colunas_filtro if c in df.columns]
df = df[cols_existentes]

return df


# ── Leitura de Particularidades / Regras de Cálculo ───────────────────────────
def _carregar_particularidades(service) -> pd.DataFrame:
    """
    A aba 04_Chaves_Merge usa os nomes:
      Cidade_Ref_Normalizada  →  equivalente ao Cidade_Norm do ETL
      Sistema_Ref_Normalizado →  equivalente ao Sistema_Norm do ETL
    Renomeamos para fazer o merge funcionar corretamente.
    """
try:
df_part = _ler_sheet(service, SHEET_ID_PARTICULARIDADES, ABA_PARTICULARIDADES)

if df_part.empty:
return pd.DataFrame(columns=["Cidade_Norm", "Sistema_Norm", "Tipo_Regra_Calculo", "Percentual_Desconto"])

        # Renomeia as colunas da planilha para o padrão interno do ETL
        # A aba 04_Chaves_Merge usa Cidade_Ref_Normalizada / Sistema_Ref_Normalizado
df_part = df_part.rename(columns={
"Cidade_Ref_Normalizada":  "Cidade_Norm",
"Sistema_Ref_Normalizado": "Sistema_Norm",
})

        # Garante que as colunas de chave existam após o rename
if "Cidade_Norm" not in df_part.columns or "Sistema_Norm" not in df_part.columns:
return pd.DataFrame(columns=["Cidade_Norm", "Sistema_Norm", "Tipo_Regra_Calculo", "Percentual_Desconto"])

@@ -125,7 +153,6 @@ def _carregar_particularidades(service) -> pd.DataFrame:
if "Tipo_Regra_Calculo" not in df_part.columns:
df_part["Tipo_Regra_Calculo"] = "SEM_REGRA"

        # Remove duplicatas de chave (mantém a primeira ocorrência)
df_part = df_part.drop_duplicates(subset=["Cidade_Norm", "Sistema_Norm"])

return df_part[["Cidade_Norm", "Sistema_Norm", "Tipo_Regra_Calculo", "Percentual_Desconto"]]
@@ -134,6 +161,7 @@ def _carregar_particularidades(service) -> pd.DataFrame:
print(f"[ETL] Erro ao carregar particularidades: {e}")
return pd.DataFrame(columns=["Cidade_Norm", "Sistema_Norm", "Tipo_Regra_Calculo", "Percentual_Desconto"])


# ── Cálculo de produção por grupo ─────────────────────────────────────────────
def _calcular_grupo(g: pd.DataFrame) -> pd.DataFrame:
g = g.sort_values("Data_Hora").reset_index(drop=True)
@@ -201,15 +229,24 @@ def _calcular_grupo(g: pd.DataFrame) -> pd.DataFrame:
)
return g


# ── ETL principal ─────────────────────────────────────────────────────────────
def _executar_etl() -> pd.DataFrame:
service = _get_service()
frames = []

    COLUNAS_LEITURAS = [
        "Data/Hora (Leitura)", "Gerência", "Pólo", "Cidade", "Sistema",
        "Macro Entrada", "Macro Saída ", "Macro Processo", "Horímetro",
        "Turbidez (uT)", "Cor (uH)", "Cloro (mg/L)", "Fluoreto (mg/L)",
    ]

for sid in SHEET_IDS_LEITURAS:
try:
f = _ler_sheet(service, sid, ABA_LEITURAS, colunas_filtro=COLUNAS_LEITURAS)
            if not f.empty: frames.append(f)
            if not f.empty:
                print(f"[ETL] Planilha {sid}: {len(f)} linhas, colunas: {list(f.columns)}")
                frames.append(f)
except Exception as e:
print(f"[ETL] Erro ao ler planilha {sid}: {e}")

@@ -223,6 +260,8 @@ def _executar_etl() -> pd.DataFrame:
for col in col_str:
if col in df.columns:
df[col] = df[col].apply(_limpar_texto).astype("category")
        else:
            print(f"[ETL] AVISO: coluna '{col}' não encontrada após concat. Colunas presentes: {list(df.columns)}")

if "Gerência" not in df.columns:
df["Gerência"] = pd.Series(["OASA"] * len(df), dtype="category")
@@ -251,7 +290,7 @@ def to_float32(col_name, limit=None):
gc.collect()

df = df.sort_values("Data_Hora")
    # groupby mantendo Pólo: inclui no agrupamento para não perder a coluna
    # Inclui Pólo no groupby para não perder a coluna
df = df.groupby(["Pólo", "Cidade", "Sistema", "Data"], as_index=False).last()

df["Cidade_Norm"]  = df["Cidade"].apply(_normalizar_chave).astype("category")
@@ -281,9 +320,11 @@ def to_float32(col_name, limit=None):
df.loc[mask_midnight, "Data_Hora"] + pd.Timedelta(hours=23, minutes=59)
)

    print(f"[ETL] Concluído. Shape final: {df.shape}. Colunas: {list(df.columns)}")
gc.collect()
return df


# ── Cache público ─────────────────────────────────────────────────────────────
def carregar_dados() -> pd.DataFrame:
agora = time.time()
@@ -292,6 +333,7 @@ def carregar_dados() -> pd.DataFrame:
_cache["ts"] = agora
return _cache["df"]


def get_cache_info():
if _cache["df"] is None:
return {"status": "vazio"}
