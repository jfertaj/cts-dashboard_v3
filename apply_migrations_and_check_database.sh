# reset database
./reset_cts-dashboard.sh



# Check taht database is storing information
# ✅ 1. Open a bash shell inside the container
docker exec -it cts-dashboard-db-1 bash

# ✅ 2. Connect to Postgres using psql
psql -U ctsuser -d ctsdb

# ✅ 2. Run the sanity check SQL queries:
SELECT COUNT(*) FROM responses;
SELECT COUNT(*) FROM questions;
SELECT COUNT(*) FROM sections;
SELECT COUNT(*) FROM questionnaires;
SELECT COUNT(*) FROM sites;

-- View sample data
SELECT * FROM sections LIMIT 5;
SELECT * FROM responses LIMIT 5;
SELECT * FROM questions LIMIT 5;


-- See site
SELECT * FROM sites;

-- See questionnaire
SELECT * FROM questionnaires;

-- See sections
SELECT * FROM sections LIMIT 5;;

-- See questions
SELECT * FROM questions LIMIT 5;;

-- See responses
SELECT * FROM responses LIMIT 5;


# After changing backend
docker compose restart backend

# Confirm endpoint works
curl http://localhost:8000/api/responses/

# reset database
./reset_cts-dashboard.sh

# Stop and delete database
docker compose down -v

# Rebuild everything
docker compose build
docker compose up -d


docker compose exec backend env PYTHONPATH=./ python app/init_db.py

# Apply initial schema with alembic
docker compose exec backend env PYTHONPATH=./ alembic -c app/alembic.ini upgrade head 

# Apply migrations from scratch
alias alembic-docker='docker compose exec backend env PYTHONPATH=./ alembic -c app/alembic.ini'
alembic-docker upgrade head

# Once models.py is changed we need to do a migration
# But before we need to reset everything
./reset_cts-dashboard.sh
# Apply the migration
docker compose exec backend env PYTHONPATH=./ alembic -c app/alembic.ini revision --autogenerate -m "add order to section"
docker compose exec backend env PYTHONPATH=./ alembic -c app/alembic.ini revision --autogenerate -m "update question and section ordering fields"
docker compose exec backend env PYTHONPATH=./ alembic -c app/alembic.ini revision --autogenerate -m "update profiling and models with subsections"
docker compose exec backend env PYTHONPATH=./ alembic -c app/alembic.ini revision --autogenerate -m "Add section_order to sections table"
docker compose exec backend env PYTHONPATH=./ alembic -c app/alembic.ini revision --autogenerate -m "Add file_hash to questionnaires table"

# Check migration
docker compose exec backend /bin/bash
cd /app/app/alembic/versions/
ls

cat /app/app/alembic/versions/d1c3dc71c0e4_add_section_order_to_sections_table.py


# Apply the migration
docker compose exec backend env PYTHONPATH=./ alembic -c app/alembic.ini upgrade head


# Check database schema
docker compose exec db psql -U ctsuser -d ctsdb

\d sections

SELECT * FROM sections;


# Find duplicates by file_name
SELECT file_name, COUNT(*) 
FROM questionnaires 
GROUP BY file_name 
HAVING COUNT(*) > 1;

# SQL Cleanup Plan
# ✅ Step 1: Find questionnaire.ids to keep

-- This will return just 1 ID (the original)
SELECT MIN(id) AS keep_id
FROM questionnaires
WHERE file_name = 'CTS_Profiling Questionnaire_v2.0 18 June 2024 - Lisbon.xlsx';


# ✅ Step 2: Delete Dependent Records (in correct order)

## 🔴 1. Delete responses
DELETE FROM responses
WHERE question_id IN (
  SELECT q.id
  FROM questions q
  JOIN sections s ON q.section_id = s.id
  WHERE s.questionnaire_id IN (
    SELECT id FROM questionnaires
    WHERE file_name = 'CTS_Profiling Questionnaire_v2.0 18 June 2024 - Lisbon.xlsx'
    AND id != 2  -- 👈 ID to keep
  )
);


## 🔴 2. DeleDELETE FROM questions
DELETE FROM questions
WHERE section_id IN (
  SELECT id FROM sections
  WHERE questionnaire_id IN (
    SELECT id FROM questionnaires
    WHERE file_name = 'CTS_Profiling Questionnaire_v2.0 18 June 2024 - Lisbon.xlsx'
    AND id != 2
  )
);

## 🔴 3. Delete sections
DELETE FROM sections
WHERE questionnaire_id IN (
  SELECT id FROM questionnaires
  WHERE file_name = 'CTS_Profiling Questionnaire_v2.0 18 June 2024 - Lisbon.xlsx'
  AND id != 2
);


## 🔴 4. Delete Duplicate Questionnaires
DELETE FROM questionnaires
WHERE file_name = 'CTS_Profiling Questionnaire_v2.0 18 June 2024 - Lisbon.xlsx'
AND id != 2;

## ✅ Final Check

# Now confirm that duplicates are gone:
SELECT file_name, COUNT(*)
FROM questionnaires
GROUP BY file_name
HAVING COUNT(*) > 1;


# Full reset of Alembic in Docker

docker compose exec backend sh -c "rm -f app/migrations/versions/*.py"
docker compose exec db psql -U ctsuser -d ctsdb
DELETE FROM alembic_version;
docker compose exec backend env PYTHONPATH=./ alembic -c app/alembic.ini revision --autogenerate -m "Initial clean migration with file_hash"


