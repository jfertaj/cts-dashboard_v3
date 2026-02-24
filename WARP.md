# WARP.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## Project Overview

CTS Dashboard is a clinical trial site qualification and management system for INNODIA. It's a full-stack application with a FastAPI backend and React (Vite) frontend, integrating with Salesforce for site data and using PostgreSQL for persistence.

## Architecture

### Backend (`backend/`)
- **Framework**: FastAPI with Uvicorn
- **Database**: PostgreSQL with SQLAlchemy 2.0 ORM and Alembic migrations
- **Authentication**: Salesforce OAuth2 flow with signed cookies
- **Key integrations**: 
  - Salesforce (simple-salesforce)
  - OpenAI API for AI-powered chat
  - Google Maps API for geocoding

**Core modules**:
- `app/main.py` - FastAPI application entry point, router registration, CORS middleware
- `app/database.py` - SQLAlchemy engine, session factory, and Base declarative model
- `app/startup.py` - Database initialization (`init_db()`) and background tasks
- `app/models/` - SQLAlchemy models: `Site`, `SiteQual`, `Questionnaire`, `Question`, `Section`, `Response`, `GeonameCity`
- `app/routers/` - API endpoints grouped by domain:
  - `qualification.py` - Upload/parse qualification checklists (Excel), manage sites/questionnaires
  - `salesforce_explorer.py` - Main Explorer API: field metadata, filtering, aggregations, Salesforce opportunity sync
  - `salesforce_auth.py` - OAuth login/callback/logout
  - `salesforce_accounts.py` - Account CRUD operations
  - `ai_chat.py` - OpenAI chat integration with SQL query generation
  - `explorer_bridge.py` - Bridge between AI chat and Explorer data
  - `geo.py` - Geocoding endpoints
- `app/services/` - Reusable business logic (OAuth, geocoding, Salesforce utilities)
- `app/parser/qualification.py` - Excel checklist parsing logic

**Database pattern**: Uses `get_db()` dependency injection for SQLAlchemy sessions. Alembic handles schema migrations.

**Salesforce session management**: Uses signed cookies (`sf_session_id`) to store OAuth tokens. Helper functions in `app/services/salesforce_oauth.py` handle session validation and SF client instantiation.

### Frontend (`frontend/`)
- **Framework**: React 18 with TypeScript
- **Build tool**: Vite (dev server with HMR, production builds)
- **UI**: Ant Design + Tailwind CSS
- **Routing**: React Router DOM v7 with clean URLs (`/explorer`, `/chat`, `/` for upload)
- **Data fetching**: SWR for caching, Axios for HTTP
- **Maps**: Google Maps (@react-google-maps/api) and Leaflet (react-leaflet) with clustering

**Structure**:
- `src/App.tsx` - Main app with tab navigation (Upload, Explorer, Chat)
- `src/pages/` - Top-level views:
  - `UploadLinkView.tsx` - Excel upload for qualification checklists
  - `ExplorerView.tsx` - Interactive data explorer with filters, charts, and maps
  - `ChatView.tsx` - AI chat interface for natural language queries
- `src/components/` - Reusable UI:
  - `FilterBuilder.tsx` - Dynamic filter creation UI
  - `MapView.tsx` - Google Maps with marker clustering
  - `ChartModal.tsx` - Chart visualization modal
  - `AIResultTable.tsx` - Table display for AI query results
  - `SalesforceLinker.tsx` - Link sites to Salesforce accounts
- `src/lib/` - API client utilities
- `src/types/` - TypeScript type definitions

**Dev proxy**: Vite dev server proxies `/api` requests to `http://localhost:8000` (FastAPI backend).

## Development Commands

### Full Stack (Docker Compose)

Start both backend and frontend containers:
```bash
docker-compose up
```

Rebuild and start (after dependency changes):
```bash
docker-compose up --build
```

Stop containers:
```bash
docker-compose down
```

Access:
- Frontend: http://localhost:8080
- Backend API: http://localhost:8000
- API docs: http://localhost:8000/docs

### Backend (Local Development)

**Setup**:
```bash
cd backend
python -m venv venv
source venv/bin/activate  # or venv/bin/activate.fish
pip install -r requirements.txt
```

**Run dev server** (auto-reload on changes):
```bash
cd backend
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**Database migrations**:
```bash
cd backend
# Create new migration after model changes
alembic revision --autogenerate -m "description"
# Apply migrations
alembic upgrade head
# Rollback one migration
alembic downgrade -1
```

**Database utilities**:
```bash
# Reset database (drops all tables, recreates schema)
cd backend
python -c "from app.database import Base, engine; Base.metadata.drop_all(engine); Base.metadata.create_all(engine)"
```

### Frontend (Local Development)

**Setup**:
```bash
cd frontend
npm install
```

**Run dev server** (with hot reload):
```bash
cd frontend
npm run dev
```
Opens on http://localhost:8080 with Vite HMR.

**Build for production**:
```bash
cd frontend
npm run build
```
Output in `frontend/dist/`.

**Preview production build**:
```bash
cd frontend
npm run preview
```

### Linting/Type Checking

No explicit lint/typecheck commands are configured in package.json. TypeScript checking happens during `npm run build` via Vite's build process.

## Environment Variables

Required variables in `.env` at repository root:

**Backend**:
- `DATABASE_URL` - PostgreSQL connection string (format: `postgresql+psycopg://user:pass@host:port/db`)
- `SF_CLIENT_ID`, `SF_CLIENT_SECRET`, `SF_USERNAME`, `SF_PASSWORD` - Salesforce OAuth credentials
- `SF_REDIRECT_URI` - OAuth callback URL (e.g., `http://localhost:8000/api/salesforce/oauth/callback`)
- `SF_DOMAIN` - Salesforce instance domain
- `GOOGLE_MAPS_API_KEY` - For geocoding
- `OPENAI_API_KEY` - For AI chat features
- `FRONTEND_ORIGINS` - Comma-separated CORS origins (e.g., `http://localhost:8080`)

**Frontend** (Vite will load from `.env`):
- `VITE_API_BASE` - Backend API URL (e.g., `http://localhost:8000` or empty string if using proxy)
- `VITE_GOOGLE_MAPS_API_KEY` - Google Maps API key

## Key Workflows

### Uploading Qualification Checklists
1. User uploads Excel file via `/` (UploadLinkView)
2. Backend (`POST /api/qualification/upload`) parses with `app/parser/qualification.py`
3. Creates `Questionnaire` + `Section` + `Question` records
4. Geocodes sites if country/city provided
5. Stores site qualification data in `site_qual` table (JSONB)

### Explorer Data Flow
1. Frontend fetches field metadata: `GET /api/explorer/fields`
2. User builds filters in FilterBuilder component
3. Frontend sends filter criteria: `POST /api/explorer/opportunities`
4. Backend:
   - Fetches Salesforce opportunities (batch with extras)
   - Enriches with qualification data from `site_qual`
   - Applies filters using `_eval_qual_rule()` logic
5. Frontend displays in table, map, and chart visualizations

### Salesforce Sync
- Manual trigger: `POST /api/salesforce/sync` (via SalesforceLinker component)
- Links local `Site` records to Salesforce `Account` records
- Stores mapping in `site.salesforce_account_id`

### AI Chat
1. User sends natural language query to `POST /api/chat` (ai_chat router)
2. OpenAI generates SQL query based on schema
3. Backend executes query against PostgreSQL
4. Results returned as table data
5. Frontend displays in AIResultTable

## Database Models

**Core entities**:
- `Site` - Clinical trial sites (name, country, city, coordinates, Salesforce link)
- `SiteQual` - JSONB qualification data per site (flattened questionnaire responses)
- `Questionnaire` - Top-level checklist container (filename, upload hash)
- `Section` - Questionnaire sections
- `Question` - Individual questions with slugified keys
- `Response` - User answers to questions
- `GeonameCity` - Geocoding reference data (cities500.txt)

**Important**: `SiteQual.qual_data` is JSONB with flattened structure where question slugs are keys. This allows efficient querying of qualification criteria in the Explorer.

## Common Patterns

### Adding a New API Endpoint
1. Create/update router in `backend/app/routers/`
2. Define Pydantic models for request/response if needed
3. Register router in `backend/app/main.py` via `app.include_router()`
4. For Salesforce-authenticated endpoints, use `get_salesforce_from_session_id()` helper

### Adding a New Frontend Component
1. Create `.tsx` file in `frontend/src/components/`
2. Use Ant Design components for consistency
3. Apply Tailwind utilities for styling
4. Import into parent page/component

### Adding Database Fields
1. Modify model in `backend/app/models/`
2. Generate migration: `cd backend && alembic revision --autogenerate -m "add field"`
3. Review generated migration in `backend/alembic/versions/`
4. Apply: `alembic upgrade head`

## Deployment

Uses AWS ECS with Docker images. Deployment scripts in repository root:
- `builld_push_ECR_and_deploy_images_in_ECS.sh` - Build, push to ECR, deploy to ECS
- `deploy_container_and_update_container_cts-dashboard.sh` - Update running ECS tasks

**Production differences**:
- Frontend uses `nginx.aws.conf` (serves from Nginx, proxies `/api` to backend)
- Backend runs without `--reload` flag
- Environment variables injected via ECS task definitions

## Salesforce Integration Details

**OAuth flow**:
1. `GET /api/salesforce/oauth/login` - Redirects to Salesforce authorization
2. Salesforce redirects back to `GET /api/salesforce/oauth/callback`
3. Backend exchanges code for access/refresh tokens
4. Stores tokens in signed cookie (`sf_session_id`)
5. Cookie used for subsequent authenticated requests

**Session validation**: Most Salesforce endpoints check cookie, unsign to get session ID, fetch tokens from in-memory cache, create SF client.

## Google Maps Configuration

Frontend supports both Google Maps (production) and Leaflet (fallback). The active map library is controlled by component props. Google Maps requires `GOOGLE_MAPS_API_KEY` and uses marker clustering via `@googlemaps/markerclusterer`.

## Notes

- No automated tests are currently configured
- Database connection uses connection pooling with pre-ping and 30-minute recycle
- Alembic migrations are in `backend/alembic/versions/`
- Frontend routing supports clean URLs (`/explorer`, `/chat`) and legacy query params (`?tab=explorer`)
- AI chat feature generates SQL but is restricted to configured schemas via `AI_SQL_SCHEMA_ALLOW`
