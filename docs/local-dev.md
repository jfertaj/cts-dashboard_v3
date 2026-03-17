# CTS Dashboard — Guía de desarrollo local

> **Stack**: FastAPI (Python) + React/Vite (TypeScript) + PostgreSQL (AWS RDS) + Salesforce REST API

---

## Índice

1. [Prerequisitos](#prerequisitos)
2. [Primera vez](#primera-vez)
3. [Uso diario](#uso-diario)
4. [Login con Salesforce](#login-con-salesforce)
5. [Moby AI](#moby-ai)
6. [Probar las features nuevas](#probar-las-features-nuevas)
7. [Logs y depuración](#logs-y-depuración)
8. [Comandos de referencia rápida](#referencia-rápida)
9. [Troubleshooting](#troubleshooting)

---

## Prerequisitos

| Herramienta | Versión mínima | Comprobar |
|-------------|---------------|-----------|
| Python | 3.10+ | `python3 --version` |
| pip / venv | cualquiera | `pip --version` |
| Node.js | 18+ | `node --version` |
| npm | 9+ | `npm --version` |
| AWS CLI v2 | 2.x | `aws --version` |
| AWS SSO profile `juan` | configurado | `aws configure list-profiles` |

### Instalar dependencias de Python (una sola vez)

```bash
cd backend
pip install -r requirements.txt
```

### Instalar dependencias de Node (una sola vez)

```bash
cd frontend
npm install
```

---

## Primera vez

### 1 · Login SSO en AWS

```bash
aws sso login --profile juan
```

Se abre el navegador → autoriza → vuelves a la terminal.
Las credenciales duran ~8 horas.

### 2 · Generar `backend/.env`

El archivo `.env` contiene todos los secretos (DB, Salesforce OAuth, Google Maps API, Anthropic API, etc.) y se obtiene desde AWS Secrets Manager:

```bash
python3 scripts/gen_local_env.py
```

Esto escribe `backend/.env` (está en `.gitignore` — nunca se commitea).

### 3 · Arrancar todo con una línea

```bash
bash scripts/dev.sh --login   # SSO login + gen .env + arrancar
```

O si ya estás logado:

```bash
bash scripts/dev.sh --setup   # solo gen .env + arrancar
```

### 4 · Login en la aplicación

1. Abre **http://localhost:5173**
2. Haz clic en **Login** (esquina superior derecha)
3. Se abre el flujo OAuth de Salesforce → autorizas → vuelves a la app

La sesión se guarda en la base de datos (`sf_sessions`) y sobrevive reinicios del backend.

---

## Uso diario

Si el `.env` sigue siendo válido (generalmente lo es hasta que roten los secretos):

```bash
bash scripts/dev.sh
```

Si quieres refrescar los secretos antes de arrancar:

```bash
bash scripts/dev.sh --setup
```

El script:

1. Comprueba prerequisitos (`.env`, `uvicorn`, `node_modules`)
2. Mata procesos en los puertos 8000 / 5173 si ya están ocupados
3. Arranca el **backend** en background → espera a que responda
4. Arranca el **frontend** (Vite dev server) → espera a que responda
5. Abre el navegador automáticamente (macOS)
6. Al hacer **Ctrl+C** mata ambos procesos limpiamente

### URLs

| Servicio | URL |
|----------|-----|
| App (frontend) | http://localhost:5173 |
| Backend API | http://localhost:8000 |
| Swagger / OpenAPI | http://localhost:8000/docs |

### Cambios de código

- **Frontend**: hot-reload automático (Vite).
- **Backend**: **no hay hot-reload**. Haz Ctrl+C y vuelve a ejecutar `bash scripts/dev.sh`.

---

## Login con Salesforce

El backend usa OAuth 2.0 con refresh token. El flujo es:

```
Navegador → /api/salesforce/auth
          → redirect a login.salesforce.com
          → autorizas
          → callback a http://localhost:8000/api/salesforce/oauth/callback
          → sesión guardada en DB (tabla sf_sessions)
          → redirect a http://localhost:5173
```

**La sesión SF expira** si no se usa durante ~15–20 min (el token de acceso de Salesforce caduca y el refresh falla en inactividad). Si ves errores 401 o "Salesforce session required", simplemente vuelve a hacer login.

### Verificar sesión activa

```bash
curl -s http://localhost:8000/api/salesforce/me \
  --cookie "sf_session=<tu_cookie_value>" | jq .
```

---

## Moby AI

Moby usa la API de Anthropic (`claude-sonnet-4-6`). Necesita:

1. **Sesión SF activa** (para consultar datos en tiempo real)
2. `ANTHROPIC_API_KEY` en `backend/.env` (se obtiene automáticamente de AWS Secrets Manager)

### Cómo usarlo

1. Ve a la tab **Chat** en la app
2. Escribe en español o inglés
3. Moby puede ejecutar búsquedas en Explorer, SOQL a Salesforce, y calcular distancias

### Ejemplos de consultas

```
¿Cuántos sitios Stage 2 hay en España?
List the 10 closest sites to Madrid with overnight stay
dame todos los sitios a menos de 150 km de Berlin con farmacia
```

### Streaming

Las respuestas se muestran en tiempo real con el cursor `▋`. Si Moby tarda más de ~5 segundos en responder, probablemente está ejecutando una consulta compleja (extended thinking activado).

---

## Probar las features nuevas

### Feature 1 — Members: filtro multi-nombre

1. Ve a la tab **Members** (`/members`)
2. En "Institution name": escribe `"Munich"` → pulsa **Enter** → aparece chip azul
3. Escribe `"Leuven"` → **Enter** → segundo chip
4. Haz clic en **Search** → solo aparecen instituciones que contienen "Munich" **o** "Leuven"
5. Haz clic en **×** en un chip para eliminarlo
6. **Clear** → todos los filtros se borran

### Feature 2 — Explorer: nearby multi-site

1. Ve a la tab **Explorer** (`/explorer`)
2. Haz una búsqueda (pulsa Search)
3. Marca los **checkboxes** en 2–3 filas de la tabla
4. Aparece el botón violeta **"Nearby (N selected)"** en la toolbar de resultados
5. Haz clic → modal de km → ajusta el radio → **Apply**
6. El panel lateral muestra todos los sites de INNODIA a X km de **cualquiera** de los sites seleccionados
7. La cabecera del panel dice `"N selected sites"`

---

## Logs y depuración

### Ver logs en tiempo real

```bash
# Backend
tail -f /tmp/cts-backend.log

# Frontend
tail -f /tmp/cts-frontend.log
```

### Últimas 50 líneas

```bash
tail -50 /tmp/cts-backend.log
tail -50 /tmp/cts-frontend.log
```

### Estado de los servidores

```bash
bash scripts/dev.sh --status
```

### Parar los servidores (desde otra terminal)

```bash
bash scripts/dev.sh --stop
```

### Probar el API directamente

```bash
# Health check
curl http://localhost:8000/health

# Explorer search (sin autenticación en local si no hay SF)
curl -s -X POST http://localhost:8000/api/explorer/search \
  -H "Content-Type: application/json" \
  -d '{"filters":{"logic":"AND","rules":[]},"columns":[]}' | jq '.rows | length'
```

### Tests unitarios del backend

```bash
python -m pytest backend/tests/ -v
# (152 tests, no necesitan red)
```

---

## Referencia rápida

```bash
# Primera vez
aws sso login --profile juan
bash scripts/dev.sh --setup

# Diario
bash scripts/dev.sh

# Diario + refrescar .env
bash scripts/dev.sh --setup

# SSO expirado
bash scripts/dev.sh --login     # hace sso login + setup + arrancar

# Parar
Ctrl+C   (o)   bash scripts/dev.sh --stop

# Ver estado
bash scripts/dev.sh --status

# Ver logs
tail -f /tmp/cts-backend.log

# Tests backend
python -m pytest backend/tests/ -v

# Tests E2E Playwright (frontend corriendo)
cd frontend && npx playwright test --reporter=list
```

---

## Troubleshooting

### `backend/.env not found`

```bash
aws sso login --profile juan
python3 scripts/gen_local_env.py
```

### `Error: An error occurred (ExpiredTokenException)`

El token SSO ha expirado (duran ~8 horas):

```bash
bash scripts/dev.sh --login
```

### Puerto ya en uso

El script lo resuelve automáticamente. Si persiste:

```bash
lsof -ti tcp:8000 | xargs kill -9
lsof -ti tcp:5173 | xargs kill -9
```

### Backend arranca pero da `503 / 500` en los endpoints de Explorer

La sesión de Salesforce ha expirado. Ve a `http://localhost:5173` → Login.

### Moby no responde / error de Anthropic

Comprueba que `ANTHROPIC_API_KEY` está en `backend/.env`:

```bash
grep ANTHROPIC_API_KEY backend/.env
```

Si falta, regenera el `.env` con `bash scripts/dev.sh --setup`.

### El frontend muestra datos de producción

Normal — el backend local se conecta a la misma base de datos PostgreSQL de producción (AWS RDS). Los cambios que hagas en la UI se reflejan en los datos reales. Ten cuidado al probar escrituras.

### `uvicorn: command not found`

```bash
pip install -r backend/requirements.txt
# o:
pip install uvicorn
```

### `npm: command not found` / Node version error

```bash
node --version   # debe ser 18+
# Si usas nvm:
nvm use 18
```
