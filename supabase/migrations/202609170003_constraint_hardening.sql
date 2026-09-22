-- Exclusion constraints protect fee intervals even with stale snapshots under
-- REPEATABLE READ. No broad clinic-row lock or writer cooperation required.
CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA extensions;
SET LOCAL search_path = public, extensions, pg_catalog;
ALTER TABLE public.doctor_services ADD CONSTRAINT no_overlapping_active_fees
    EXCLUDE USING gist (
        clinic_id WITH =, doctor_id WITH =, service_id WITH =,
        daterange(effective_from, effective_until, '[)') WITH &&
    ) WHERE (status = 'active');
DROP TRIGGER fee_overlap ON public.doctor_services;
DROP FUNCTION clinic_private.prevent_fee_overlap();

-- Clear potentially inherited Supabase creator-default grants explicitly. A
-- per-schema default REVOKE alone cannot remove global default EXECUTE grants.
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA clinic_private
    FROM PUBLIC, anon, authenticated, clinic_runtime;
GRANT EXECUTE ON FUNCTION clinic_private.has_membership(uuid, text[]) TO authenticated;
GRANT EXECUTE ON FUNCTION clinic_private.resolve_destination(text, text, text) TO clinic_runtime;
-- Future migrations must explicitly revoke/grant each new private function.
RESET search_path;