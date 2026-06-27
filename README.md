# OASA Dashboard — Guia de Deploy

## Estrutura
```
oasa_app/
├── backend/
│   ├── main.py          # API FastAPI
│   ├── etl.py           # Lógica ETL + cálculos
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   └── index.html       # Dashboard completo (arquivo único)
└── render.yaml          # Deploy automático no Render
```

---

## Passo 1 — Configurar as planilhas Google Sheets

1. Converta seus arquivos Excel para Google Sheets
2. A planilha de leituras deve ter uma aba chamada **`Registos`** com as colunas:
   - ID, Data de Registo (Servidor), **Data/Hora (Leitura)**, Operador, Gerência,
     Pólo, Cidade, Sistema, Macro Entrada, Macro Saída, Macro Processo,
     Horímetro, Energia (kWh), Turbidez (uT), Cor (uH), Cloro (mg/L),
     Fluoreto (mg/L), pH, Observações
3. A planilha de particularidades deve ter a aba **`04_Chaves_Merge`**
4. Compartilhe ambas com o e-mail da sua Service Account (permissão **Leitor**)

---

## Passo 2 — Pegar os IDs das planilhas

Na URL de cada planilha:
```
https://docs.google.com/spreadsheets/d/ESTE_É_O_ID/edit
```

---

## Passo 3 — Deploy no Render

### 3.1 Subir o código
- Crie um repositório no GitHub e suba esta pasta `oasa_app`

### 3.2 Criar os serviços no Render
- Acesse https://render.com → New → Blueprint
- Conecte o repositório — o `render.yaml` configura tudo automaticamente

### 3.3 Configurar as variáveis de ambiente (no Render Dashboard)

Para o serviço **oasa-backend**, adicione:

| Variável | Valor |
|---|---|
| `SHEET_ID_LEITURAS` | ID da planilha de leituras |
| `SHEET_ID_PARTICULARIDADES` | ID da planilha de particularidades |
| `GOOGLE_CREDENTIALS_JSON` | Conteúdo completo do arquivo credentials.json (em uma linha) |

> **Como colocar o JSON em uma linha:**
> ```bash
> cat credentials.json | tr -d '\n'
> ```
> Cole o resultado na variável `GOOGLE_CREDENTIALS_JSON`

### 3.4 Conectar o frontend ao backend
O `render.yaml` já gera automaticamente um `config.js` no build do frontend, apontando
para o backend — **não é preciso editar `index.html`**. Só falta você informar a URL:

1. Depois que o `oasa-backend` for criado, copie a URL pública dele (Render Dashboard →
   oasa-backend → algo como `https://oasa-backend-xxxx.onrender.com`).
2. Vá em **oasa-frontend → Environment** e defina `API_BASE_URL` com essa URL.
3. Clique em **Manual Deploy → Deploy latest commit** no oasa-frontend pra regerar o `config.js`.

> Por que esse passo manual? O nome do serviço (`oasa-backend`) não garante a URL final —
> se o subdomínio já estiver em uso por outra conta Render, a plataforma usa um sufixo
> diferente. Por isso a URL real só existe depois do backend ser criado.

---

## Passo 4 — Testar localmente (opcional, mas recomendado)

```bash
cd backend
pip install -r requirements.txt

# Crie um arquivo .env com base no .env.example
cp .env.example .env
# Edite o .env com seus IDs e credencial
```

**Teste 1 — sem credenciais reais, só lógica:**
```bash
python test_local.py
```
Roda o ETL e todos os endpoints com dados sintéticos (não toca o Google Sheets).
Serve para pegar erro de código antes de gastar tempo configurando credencial.

**Teste 2 — com a planilha real:**
```bash
uvicorn main:app --reload
```
Abra `frontend/index.html` direto no navegador (ele detecta `localhost` e aponta
pra `http://localhost:8000` automaticamente).

---

## Endpoints da API

| Endpoint | Descrição |
|---|---|
| `GET /status` | Status do cache |
| `GET /filtros` | Listas de filtros disponíveis |
| `GET /producao` | Produção por sistema |
| `GET /qualidade` | Indicadores de qualidade |
| `GET /acompanhamento` | Análises vs leituras + rank |
| `GET /leituras` | Tabela linha a linha |

Todos aceitam os parâmetros: `gerencia`, `polo`, `cidade`, `sistema`, `data_ini`, `data_fim`

## Limitações conhecidas

- **Sem autenticação.** A API é pública — qualquer pessoa com a URL acessa `/leituras`,
  `/producao` etc. Para uso interno isso pode ser aceitável, mas são dados operacionais
  da OASA. Se isso for um problema, dá pra adicionar autenticação simples (API key num
  header, por exemplo) sem muito esforço — só não fiz isso "no escuro" porque é uma
  decisão de produto, não um bug.
- **Cache em memória.** Cada reinício do serviço no Render (deploy, restart automático)
  zera o cache; a primeira requisição depois disso demora mais (lê o Sheets de novo).

---



Os dados são carregados do Google Sheets e mantidos em memória por **5 minutos** (configurável via `CACHE_TTL`).
A primeira requisição após o TTL dispara uma nova leitura da planilha.
