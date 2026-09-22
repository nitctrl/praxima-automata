-- Administrative data only. No patient/clinical record or audio storage.
-- Executed transactionally by scripts/database.py. Never run against an unspecified DB.
CREATE SCHEMA clinic_private;
REVOKE ALL ON SCHEMA clinic_private FROM PUBLIC, anon, authenticated;

CREATE ROLE clinic_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
GRANT clinic_runtime TO postgres;
GRANT USAGE ON SCHEMA public, clinic_private TO clinic_runtime;
GRANT USAGE ON SCHEMA clinic_private TO authenticated;

CREATE TABLE public.clinics (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
    slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,99}$'),
    timezone text NOT NULL DEFAULT 'Asia/Kolkata',
    default_language text NOT NULL DEFAULT 'hi-IN',
    supported_languages text[] NOT NULL DEFAULT ARRAY['hi-IN','en-IN'],
    status text NOT NULL DEFAULT 'inactive' CHECK (status IN ('active','inactive','suspended')),
    greeting text NOT NULL,
    emergency_message text NOT NULL CHECK (length(emergency_message) > 0),
    fallback_message text NOT NULL,
    transfer_enabled boolean NOT NULL DEFAULT false,
    transfer_secret_reference text,
    transfer_hours jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(transfer_hours) = 'object'),
    maximum_call_duration_seconds integer NOT NULL DEFAULT 600
        CHECK (maximum_call_duration_seconds BETWEEN 30 AND 3600),
    monthly_minute_limit integer NOT NULL DEFAULT 1000 CHECK (monthly_minute_limit >= 0),
    recording_enabled boolean NOT NULL DEFAULT false CHECK (recording_enabled = false),
    active_configuration_version_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (cardinality(supported_languages) > 0 AND default_language = ANY(supported_languages)),
    CHECK (NOT transfer_enabled OR transfer_secret_reference IS NOT NULL)
);

CREATE TABLE public.clinic_users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id uuid NOT NULL REFERENCES public.clinics ON DELETE RESTRICT,
    auth_user_id uuid NOT NULL REFERENCES auth.users ON DELETE RESTRICT,
    role text NOT NULL CHECK (role IN ('owner','manager','receptionist','viewer')),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, id), UNIQUE (clinic_id, auth_user_id)
);
CREATE INDEX clinic_users_auth_lookup ON public.clinic_users (auth_user_id, status, clinic_id);

CREATE TABLE public.configuration_versions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id uuid NOT NULL REFERENCES public.clinics ON DELETE RESTRICT,
    version_number integer NOT NULL CHECK (version_number > 0),
    status text NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','published','superseded','archived')),
    snapshot jsonb NOT NULL CHECK (jsonb_typeof(snapshot) = 'object'),
    schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version = 1),
    prompt_version text NOT NULL,
    published_by uuid REFERENCES auth.users ON DELETE RESTRICT,
    published_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, id), UNIQUE (clinic_id, version_number),
    CHECK (status = 'draft' OR published_at IS NOT NULL)
);
CREATE UNIQUE INDEX one_published_version ON public.configuration_versions (clinic_id)
    WHERE status = 'published';
ALTER TABLE public.clinics ADD CONSTRAINT active_configuration_same_clinic
    FOREIGN KEY (id, active_configuration_version_id)
    REFERENCES public.configuration_versions (clinic_id, id) ON DELETE RESTRICT;

CREATE TABLE public.phone_numbers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id uuid NOT NULL REFERENCES public.clinics ON DELETE RESTRICT,
    provider text NOT NULL CHECK (provider IN ('plivo','test')),
    e164_number text NOT NULL CHECK (e164_number ~ '^\+[1-9][0-9]{7,14}$'),
    provider_reference text,
    trusted_trunk_id text NOT NULL CHECK (length(trusted_trunk_id) BETWEEN 1 AND 100),
    inbound_enabled boolean NOT NULL DEFAULT true,
    outbound_enabled boolean NOT NULL DEFAULT false CHECK (outbound_enabled = false),
    status text NOT NULL DEFAULT 'inactive' CHECK (status IN ('active','inactive')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, id)
);
CREATE UNIQUE INDEX unique_active_called_number ON public.phone_numbers (e164_number)
    WHERE status = 'active' AND inbound_enabled;
CREATE INDEX phone_numbers_clinic ON public.phone_numbers (clinic_id);

CREATE TABLE public.locations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id uuid NOT NULL REFERENCES public.clinics ON DELETE RESTRICT,
    name text NOT NULL,
    address text NOT NULL,
    landmark text,
    directions text,
    map_url text CHECK (map_url IS NULL OR map_url LIKE 'https://%'),
    parking_information text,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive')),
    effective_from date NOT NULL DEFAULT CURRENT_DATE,
    effective_until date,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, id), CHECK (effective_until IS NULL OR effective_until > effective_from)
);

CREATE TABLE public.doctors (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id uuid NOT NULL REFERENCES public.clinics ON DELETE RESTRICT,
    display_name text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 200),
    normalized_name text NOT NULL,
    aliases text[] NOT NULL DEFAULT '{}',
    speciality text NOT NULL,
    languages text[] NOT NULL DEFAULT '{}',
    short_public_bio text NOT NULL DEFAULT '',
    accepts_new_patients boolean NOT NULL DEFAULT true,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive')),
    effective_from date NOT NULL DEFAULT CURRENT_DATE,
    effective_until date,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, id), CHECK (effective_until IS NULL OR effective_until > effective_from)
);
CREATE INDEX doctors_name_lookup ON public.doctors (clinic_id, normalized_name, status);

CREATE TABLE public.services (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id uuid NOT NULL REFERENCES public.clinics ON DELETE RESTRICT,
    name text NOT NULL,
    normalized_name text NOT NULL,
    aliases text[] NOT NULL DEFAULT '{}',
    short_approved_description text NOT NULL,
    appointment_required boolean NOT NULL DEFAULT true,
    active boolean NOT NULL DEFAULT true,
    effective_from date NOT NULL DEFAULT CURRENT_DATE,
    effective_until date,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, id), CHECK (effective_until IS NULL OR effective_until > effective_from)
);

CREATE TABLE public.doctor_services (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id uuid NOT NULL REFERENCES public.clinics ON DELETE RESTRICT,
    doctor_id uuid NOT NULL,
    service_id uuid NOT NULL,
    current_fee numeric(12,2) NOT NULL CHECK (current_fee >= 0),
    currency text NOT NULL DEFAULT 'INR' CHECK (currency ~ '^[A-Z]{3}$'),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive')),
    effective_from date NOT NULL,
    effective_until date,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, id), UNIQUE (clinic_id, doctor_id, service_id, effective_from),
    FOREIGN KEY (clinic_id, doctor_id) REFERENCES public.doctors (clinic_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (clinic_id, service_id) REFERENCES public.services (clinic_id, id) ON DELETE RESTRICT,
    CHECK (effective_until IS NULL OR effective_until > effective_from)
);

CREATE TABLE public.weekly_schedules (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id uuid NOT NULL REFERENCES public.clinics ON DELETE RESTRICT,
    doctor_id uuid,
    location_id uuid NOT NULL,
    day_of_week smallint NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
    start_time time NOT NULL,
    end_time time NOT NULL,
    availability_type text NOT NULL DEFAULT 'consultation'
        CHECK (availability_type IN ('consultation','clinic_hours','no_walk_ins')),
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','inactive')),
    effective_from date NOT NULL DEFAULT CURRENT_DATE,
    effective_until date,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, id), CHECK (end_time > start_time),
    CHECK (effective_until IS NULL OR effective_until > effective_from),
    FOREIGN KEY (clinic_id, doctor_id) REFERENCES public.doctors (clinic_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (clinic_id, location_id) REFERENCES public.locations (clinic_id, id) ON DELETE RESTRICT
);

CREATE TABLE public.schedule_exceptions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id uuid NOT NULL REFERENCES public.clinics ON DELETE RESTRICT,
    doctor_id uuid,
    location_id uuid NOT NULL,
    exception_date date NOT NULL,
    status text NOT NULL CHECK (status IN ('available','unavailable','modified_hours')),
    start_time time,
    end_time time,
    public_message text NOT NULL,
    internal_note text NOT NULL DEFAULT '',
    publication_status text NOT NULL DEFAULT 'draft'
        CHECK (publication_status IN ('draft','published','archived')),
    created_by uuid REFERENCES auth.users ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, id),
    CHECK ((status = 'unavailable' AND start_time IS NULL AND end_time IS NULL)
        OR (start_time IS NOT NULL AND end_time IS NOT NULL AND end_time > start_time)),
    FOREIGN KEY (clinic_id, doctor_id) REFERENCES public.doctors (clinic_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (clinic_id, location_id) REFERENCES public.locations (clinic_id, id) ON DELETE RESTRICT
);

CREATE TABLE public.temporary_notices (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id uuid NOT NULL REFERENCES public.clinics ON DELETE RESTRICT,
    location_id uuid, doctor_id uuid, service_id uuid,
    notice_type text NOT NULL,
    public_message text NOT NULL,
    internal_note text NOT NULL DEFAULT '',
    starts_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL CHECK (expires_at > starts_at),
    priority integer NOT NULL DEFAULT 0 CHECK (priority BETWEEN 0 AND 100),
    publication_status text NOT NULL DEFAULT 'draft'
        CHECK (publication_status IN ('draft','published','archived')),
    created_by uuid REFERENCES auth.users ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, id),
    FOREIGN KEY (clinic_id, doctor_id) REFERENCES public.doctors (clinic_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (clinic_id, service_id) REFERENCES public.services (clinic_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (clinic_id, location_id) REFERENCES public.locations (clinic_id, id) ON DELETE RESTRICT
);
CREATE INDEX effective_notices ON public.temporary_notices (clinic_id, starts_at, expires_at);

CREATE TABLE public.approved_faqs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id uuid NOT NULL REFERENCES public.clinics ON DELETE RESTRICT,
    category text NOT NULL,
    canonical_question text NOT NULL,
    alternative_phrasings text[] NOT NULL DEFAULT '{}',
    approved_answer text NOT NULL,
    effective_from date NOT NULL DEFAULT CURRENT_DATE,
    effective_until date,
    publication_status text NOT NULL DEFAULT 'draft'
        CHECK (publication_status IN ('draft','published','archived')),
    created_by uuid REFERENCES auth.users ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, id), CHECK (effective_until IS NULL OR effective_until > effective_from)
);

CREATE TABLE public.knowledge_documents (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id uuid NOT NULL REFERENCES public.clinics ON DELETE RESTRICT,
    storage_path text NOT NULL CHECK (storage_path LIKE clinic_id::text || '/%'),
    original_filename text NOT NULL,
    mime_type text NOT NULL,
    document_category text NOT NULL,
    checksum text NOT NULL,
    status text NOT NULL DEFAULT 'draft'
        CHECK (status IN ('processing','draft','needs_review','published','rejected','archived')),
    effective_from timestamptz NOT NULL DEFAULT now(),
    effective_until timestamptz,
    version integer NOT NULL CHECK (version > 0),
    supersedes_document_id uuid,
    extracted_text text,
    uploaded_by uuid REFERENCES auth.users ON DELETE RESTRICT,
    reviewed_by uuid REFERENCES auth.users ON DELETE RESTRICT,
    published_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, id), UNIQUE (clinic_id, storage_path),
    CHECK (effective_until IS NULL OR effective_until > effective_from),
    CHECK (status <> 'published' OR published_at IS NOT NULL),
    FOREIGN KEY (clinic_id, supersedes_document_id)
        REFERENCES public.knowledge_documents (clinic_id, id) ON DELETE RESTRICT
);
-- Embeddings deliberately deferred to Phase 5; no dummy vectors or untested search API.

CREATE TABLE public.call_sessions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id uuid NOT NULL REFERENCES public.clinics ON DELETE RESTRICT,
    phone_number_id uuid NOT NULL,
    provider text NOT NULL,
    provider_account_reference text NOT NULL,
    provider_call_id text NOT NULL,
    livekit_room_id text NOT NULL,
    configuration_version_id uuid NOT NULL,
    caller_number_ciphertext bytea,
    pii_key_version text,
    started_at timestamptz NOT NULL DEFAULT now(),
    answered_at timestamptz,
    ended_at timestamptz,
    duration_seconds integer CHECK (duration_seconds >= 0),
    detected_languages text[] NOT NULL DEFAULT '{}',
    primary_intent text,
    disposition text NOT NULL DEFAULT 'started',
    transfer_attempted boolean NOT NULL DEFAULT false,
    transfer_succeeded boolean NOT NULL DEFAULT false,
    safety_flag boolean NOT NULL DEFAULT false,
    short_administrative_summary text,
    failure_code text,
    estimated_cost numeric(12,6) CHECK (estimated_cost >= 0),
    is_test boolean NOT NULL DEFAULT false,
    retention_until timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, id), UNIQUE (provider, provider_account_reference, provider_call_id),
    CHECK (ended_at IS NULL OR ended_at >= started_at),
    CHECK (NOT transfer_succeeded OR transfer_attempted),
    CHECK (caller_number_ciphertext IS NULL OR pii_key_version IS NOT NULL),
    FOREIGN KEY (clinic_id, phone_number_id)
        REFERENCES public.phone_numbers (clinic_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (clinic_id, configuration_version_id)
        REFERENCES public.configuration_versions (clinic_id, id) ON DELETE RESTRICT
);
CREATE INDEX calls_recent ON public.call_sessions (clinic_id, started_at DESC);

CREATE TABLE public.appointment_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id uuid NOT NULL REFERENCES public.clinics ON DELETE RESTRICT,
    call_session_id uuid NOT NULL,
    idempotency_key uuid NOT NULL,
    patient_name_ciphertext bytea,
    callback_number_ciphertext bytea,
    pii_key_version text,
    pii_erased_at timestamptz,
    is_new_patient boolean NOT NULL,
    doctor_id uuid,
    service_id uuid,
    preferred_date date NOT NULL,
    preferred_time_start time,
    preferred_time_end time,
    status text NOT NULL DEFAULT 'new'
        CHECK (status IN ('new','contacted','confirmed_externally','closed','cancelled')),
    receptionist_note_ciphertext bytea,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, id), UNIQUE (clinic_id, call_session_id, idempotency_key),
    CHECK ((patient_name_ciphertext IS NOT NULL AND callback_number_ciphertext IS NOT NULL
        AND pii_key_version IS NOT NULL) OR pii_erased_at IS NOT NULL),
    CHECK (doctor_id IS NOT NULL OR service_id IS NOT NULL),
    CHECK (preferred_time_end IS NULL OR (preferred_time_start IS NOT NULL
        AND preferred_time_end > preferred_time_start)),
    FOREIGN KEY (clinic_id, call_session_id)
        REFERENCES public.call_sessions (clinic_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (clinic_id, doctor_id) REFERENCES public.doctors (clinic_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (clinic_id, service_id) REFERENCES public.services (clinic_id, id) ON DELETE RESTRICT
);
CREATE INDEX requests_pending ON public.appointment_requests (clinic_id, status, created_at);

CREATE TABLE public.audit_logs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id uuid NOT NULL REFERENCES public.clinics ON DELETE RESTRICT,
    actor_id uuid REFERENCES auth.users ON DELETE RESTRICT,
    action text NOT NULL,
    resource_type text NOT NULL,
    resource_id uuid NOT NULL,
    correlation_id uuid NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, id)
);

CREATE FUNCTION clinic_private.touch_updated_at() RETURNS trigger
LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

CREATE FUNCTION clinic_private.validate_clinic() RETURNS trigger
LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_catalog.pg_timezone_names WHERE name = NEW.timezone) THEN
        RAISE EXCEPTION 'Invalid clinic timezone' USING ERRCODE = '23514';
    END IF;
    IF NEW.active_configuration_version_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM public.configuration_versions
        WHERE clinic_id = NEW.id AND id = NEW.active_configuration_version_id
            AND status = 'published'
    ) THEN
        RAISE EXCEPTION 'Active version must be published for this clinic' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER clinic_validation BEFORE INSERT OR UPDATE ON public.clinics
    FOR EACH ROW EXECUTE FUNCTION clinic_private.validate_clinic();

CREATE FUNCTION clinic_private.immutable_publication() RETURNS trigger
LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
    IF OLD.status <> 'draft' THEN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'Published history cannot be deleted' USING ERRCODE = '23514';
        END IF;
        IF (to_jsonb(OLD) - 'status') IS DISTINCT FROM (to_jsonb(NEW) - 'status')
            OR NEW.status = 'draft' THEN
            RAISE EXCEPTION 'Published content is immutable' USING ERRCODE = '23514';
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER immutable_configuration BEFORE UPDATE OR DELETE ON public.configuration_versions
    FOR EACH ROW EXECUTE FUNCTION clinic_private.immutable_publication();

CREATE FUNCTION clinic_private.append_only() RETURNS trigger
LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
    RAISE EXCEPTION 'Append-only audit history' USING ERRCODE = '23514';
END;
$$;
CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON public.audit_logs
    FOR EACH ROW EXECUTE FUNCTION clinic_private.append_only();

-- Definer avoids recursive membership RLS. Identity comes from Supabase's verified
-- JWT context, not a function parameter or mutable user_metadata.
CREATE FUNCTION clinic_private.has_membership(target uuid, roles text[]) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
    SELECT EXISTS (
        SELECT 1 FROM public.clinic_users m JOIN public.clinics c ON c.id = m.clinic_id
        WHERE m.clinic_id = target AND m.auth_user_id = auth.uid()
            AND m.status = 'active' AND c.status = 'active' AND m.role = ANY(roles)
    );
$$;

-- Runtime resolver exposes no facts for a guessed clinic UUID. It is not granted
-- to browser/API roles; the trusted SIP adapter supplies the destination + trunk.
CREATE FUNCTION clinic_private.resolve_destination(p_provider text, p_number text, p_trunk text)
RETURNS TABLE (clinic_id uuid, phone_number_id uuid, configuration_version_id uuid,
    timezone text, supported_languages text[])
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
    SELECT c.id, p.id, v.id, v.snapshot->>'timezone',
        ARRAY(SELECT jsonb_array_elements_text(v.snapshot->'supported_languages'))
    FROM public.phone_numbers p
    JOIN public.clinics c ON c.id = p.clinic_id
    JOIN public.configuration_versions v ON v.clinic_id = c.id
        AND v.id = c.active_configuration_version_id AND v.status = 'published'
    WHERE p.provider = p_provider AND p.e164_number = p_number
        AND p.trusted_trunk_id = p_trunk AND p.status = 'active' AND p.inbound_enabled
        AND c.status = 'active';
$$;

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['clinics','clinic_users','configuration_versions','phone_numbers',
        'locations','doctors','services','doctor_services','weekly_schedules','schedule_exceptions',
        'temporary_notices','approved_faqs','knowledge_documents','call_sessions',
        'appointment_requests','audit_logs'] LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC, anon, authenticated, clinic_runtime', t);
        EXECUTE format('GRANT SELECT ON public.%I TO authenticated', t);
        EXECUTE format(
            'CREATE POLICY member_read ON public.%I FOR SELECT TO authenticated USING '
            || '(clinic_private.has_membership(%I, ARRAY[''owner'',''manager'',''receptionist'',''viewer'']))',
            t, CASE WHEN t = 'clinics' THEN 'id' ELSE 'clinic_id' END);
        IF t <> 'clinics' THEN
            EXECUTE format('CREATE INDEX ON public.%I (clinic_id)', t);
        END IF;
    END LOOP;
    -- Authoring only: these rows NEVER feed active calls directly.
    FOREACH t IN ARRAY ARRAY['locations','doctors','services','weekly_schedules',
        'schedule_exceptions','temporary_notices','approved_faqs'] LOOP
        EXECUTE format('GRANT INSERT, UPDATE ON public.%I TO authenticated', t);
        EXECUTE format('CREATE POLICY manager_insert ON public.%I FOR INSERT TO authenticated '
            || 'WITH CHECK (clinic_private.has_membership(clinic_id, ARRAY[''owner'',''manager'']))', t);
        EXECUTE format('CREATE POLICY manager_update ON public.%I FOR UPDATE TO authenticated '
            || 'USING (clinic_private.has_membership(clinic_id, ARRAY[''owner'',''manager''])) '
            || 'WITH CHECK (clinic_private.has_membership(clinic_id, ARRAY[''owner'',''manager'']))', t);
    END LOOP;
    FOREACH t IN ARRAY ARRAY['clinics','clinic_users','locations','doctors','services',
        'weekly_schedules','schedule_exceptions','approved_faqs','appointment_requests'] LOOP
        EXECUTE format('CREATE TRIGGER touch_updated_at BEFORE UPDATE ON public.%I '
            || 'FOR EACH ROW EXECUTE FUNCTION clinic_private.touch_updated_at()', t);
    END LOOP;
END;
$$;

GRANT SELECT ON public.configuration_versions TO clinic_runtime;
CREATE POLICY runtime_pinned_read ON public.configuration_versions FOR SELECT TO clinic_runtime
    USING (clinic_id = nullif(current_setting('app.clinic_id', true), '')::uuid
        AND status IN ('published','superseded'));

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA clinic_private FROM PUBLIC, anon, authenticated, clinic_runtime;
GRANT EXECUTE ON FUNCTION clinic_private.has_membership(uuid, text[]) TO authenticated;
GRANT EXECUTE ON FUNCTION clinic_private.resolve_destination(text, text, text) TO clinic_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA clinic_private REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

-- No application grants for membership administration, phone assignment, publishing,
-- requests, or fee-history mutation until their authorized services are implemented.
-- No grants on raw operational authoring tables to clinic_runtime.