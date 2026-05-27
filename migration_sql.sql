-- Run this SQL in Supabase SQL Editor (https://fctpcvqkqokzizjcirpl.supabase.co)
-- before running the seed script.

-- 1. Add user_id to patients table (link patient record to user account)
ALTER TABLE patients
ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(user_id) ON DELETE SET NULL UNIQUE;

-- 2. Add PENDING and CONFIRMED to result_status enum
ALTER TYPE result_status ADD VALUE IF NOT EXISTS 'PENDING';
ALTER TYPE result_status ADD VALUE IF NOT EXISTS 'CONFIRMED';

-- 3. Add patient portal columns to analysis_results
ALTER TABLE analysis_results
ADD COLUMN IF NOT EXISTS patient_id UUID REFERENCES patients(patient_id) ON DELETE RESTRICT;

ALTER TABLE analysis_results
ADD COLUMN IF NOT EXISTS cell_counts JSONB;

ALTER TABLE analysis_results
ADD COLUMN IF NOT EXISTS interpretation TEXT;

ALTER TABLE analysis_results
ADD COLUMN IF NOT EXISTS medtech_id UUID REFERENCES users(user_id) ON DELETE RESTRICT;

ALTER TABLE analysis_results
ADD COLUMN IF NOT EXISTS medtech_name VARCHAR(255);

ALTER TABLE analysis_results
ADD COLUMN IF NOT EXISTS pathologist_name VARCHAR(255);

ALTER TABLE analysis_results
ADD COLUMN IF NOT EXISTS pathologist_license VARCHAR(100);

ALTER TABLE analysis_results
ADD COLUMN IF NOT EXISTS released_at TIMESTAMPTZ;

-- 4. Create result_views table (audit trail for patient result views)
CREATE TABLE IF NOT EXISTS result_views (
    view_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    result_id UUID NOT NULL REFERENCES analysis_results(result_id) ON DELETE CASCADE,
    patient_id UUID NOT NULL REFERENCES patients(patient_id) ON DELETE CASCADE,
    viewed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 5. Disable RLS on result_views
ALTER TABLE result_views DISABLE ROW LEVEL SECURITY;
ALTER TABLE analysis_results DISABLE ROW LEVEL SECURITY;

-- Verify:
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_name = 'analysis_results'
  AND column_name IN ('patient_id', 'cell_counts', 'interpretation', 'medtech_name', 'pathologist_name', 'pathologist_license', 'released_at');

SELECT column_name
FROM information_schema.columns
WHERE table_name = 'patients' AND column_name = 'user_id';

SELECT table_name FROM information_schema.tables WHERE table_name = 'result_views';
