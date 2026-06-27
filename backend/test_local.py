"""
Teste local do ETL + API com dados SINTÉTICOS — não toca o Google Sheets real.
Objetivo: achar erros de execução antes de subir pro Render.
Roda com: python test_local.py
"""
import sys, traceback
import pandas as pd
import numpy as np

import etl

# ── 1. Monta planilha de leituras sintética ────────────────────────────────
linhas = []
sistemas = [
    ("Avaré", "Avaré", "Sistema Central"),
    ("Avaré", "Avaré", "Sistema Central"),
    ("Itapetininga", "Itapetininga", "ETA Norte"),
    ("Itapeva", "Itapeva", "Poço 01"),
]
base_me, base_ms, base_mp, base_hor = 1000, 800, 200, 5000
for dia in range(5):
    for pol, cid, sis in sistemas:
        linhas.append({
            "ID": f"{dia}-{sis}",
            "Data de Registo (Servidor)": f"2026-01-{dia+1:02d} 08:00:00",
            "Data/Hora (Leitura)": f"{dia+1:02d}/01/2026 07:00:00",
            "Operador": "Teste",
            "Gerência": "OASA",
            "Pólo": pol,
            "Cidade": cid,
            "Sistema": sis,
            "Macro Entrada": str(base_me + dia * 50),
            "Macro Saída ": str(base_ms + dia * 40),   # nota o espaço, igual ao real
            "Macro Processo": str(base_mp + dia * 10),
            "Horímetro": str(base_hor + dia * 24),
            "Energia (kWh)": str(100 + dia),
            "Turbidez (uT)": "0,8" if dia % 2 == 0 else "",
            "Cor (uH)": "10" if dia % 2 == 0 else "",
            "Cloro (mg/L)": "1,5" if dia % 2 == 0 else "",
            "Fluoreto (mg/L)": "0,7" if dia % 2 == 0 else "",
            "pH": "7,1",
            "Observações": "",
        })

# linha com 00:00 (testa normalização de meia-noite) e linha com lixo (testa robustez)
linhas.append({**linhas[0], "ID": "midnight", "Data/Hora (Leitura)": "06/01/2026 00:00:00"})
linhas.append({**linhas[0], "ID": "lixo", "Macro Entrada": "abc", "Horímetro": ""})

df_leituras = pd.DataFrame(linhas)

# ── 2. Monta planilha de particularidades sintética ────────────────────────
df_part = pd.DataFrame([
    {
        "Cidade_Ref_Normalizada": "ITAPETININGA",
        "Sistema_Ref_Normalizado": "ETA NORTE",
        "Tipo_Regra_Calculo": "DESCONTO_PERCENTUAL",
        "Percentual_Desconto": "10",
        "Usar_Macro_Processo_Como_Saida2": "FALSE",
    },
    {
        "Cidade_Ref_Normalizada": "ITAPEVA",
        "Sistema_Ref_Normalizado": "POCO 01",
        "Tipo_Regra_Calculo": "SUBTRAIR_MACRO_PROCESSO",
        "Percentual_Desconto": "",
        "Usar_Macro_Processo_Como_Saida2": "TRUE",
    },
])

# ── 3. Monkeypatch: troca a leitura real do Sheets pelos dados sintéticos ──
def _fake_ler_sheet(service, sheet_id, aba):
    if sheet_id == etl.SHEET_ID_PARTICULARIDADES:
        return df_part.copy()
    return df_leituras.copy()

etl._ler_sheet = _fake_ler_sheet
etl._get_service = lambda: None  # nunca deveria ser realmente chamado p/ autenticar

erros = []

def checar(nome, fn):
    try:
        r = fn()
        print(f"OK   {nome}")
        return r
    except Exception as e:
        print(f"FALHOU {nome}: {e}")
        traceback.print_exc()
        erros.append(nome)
        return None

print("=== ETL ===")
df = checar("_executar_etl", etl._executar_etl)

if df is not None:
    print(f"\nLinhas resultantes: {len(df)}")
    print(f"Colunas: {list(df.columns)}\n")
    cols_chave = ["Pólo", "Cidade", "Sistema", "Data_Hora", "Producao",
                  "Producao_Media_Dia", "Tipo_Regra_Calculo", "Tem_Analise", "Tem_Leitura_Macro"]
    print(df[cols_chave].to_string())

    print("\n=== Verificações de regras de negócio ===")
    # Itapetininga deveria ter Tipo_Regra_Calculo == DESCONTO_PERCENTUAL
    g = df[df["Cidade"] == "Itapetininga"]
    if (g["Tipo_Regra_Calculo"] == "DESCONTO_PERCENTUAL").all():
        print("OK   Merge de particularidades aplicou DESCONTO_PERCENTUAL em Itapetininga")
    else:
        print("FALHOU merge de particularidades não aplicou regra esperada em Itapetininga")
        erros.append("merge_particularidades")

    # checar se "midnight" foi normalizado pra 23:59 na exibição
    if "Data_Hora_Exibicao" in df.columns:
        print("OK   Data_Hora_Exibicao presente")

print("\n=== Funções da API (main.py) ===")
import importlib
# Evita que main.py tente criar app de verdade sem problemas; só testamos a lógica
import main as api

api.carregar_dados = lambda: df  # injeta o df sintético já calculado

checar("filtros()", api.filtros)
checar("producao()", lambda: api.producao())
checar("qualidade()", lambda: api.qualidade())
checar("acompanhamento()", lambda: api.acompanhamento())
checar("leituras()", lambda: api.leituras())

print("\n" + "=" * 60)
if erros:
    print(f"RESULTADO: {len(erros)} falha(s): {erros}")
    sys.exit(1)
else:
    print("RESULTADO: tudo OK")
