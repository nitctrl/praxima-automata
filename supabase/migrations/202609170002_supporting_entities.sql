CREATE TABLE public.special_date_schedules (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id uuid NOT NULL REFERENCES public.clinics ON DELETE RESTRICT,
    doctor_id uuid,
    location_id uuid NOT NULL,
    schedule_date date NOT NULL,
    start_time time NOT NULL,
    end_time time NOT NULL CHECK (end_time > start_time),
    publication_status text NOT NULL DEFAULT 'draft'
        CHECK (publication_status IN ('draft','published','archived')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, id),
    FOREIGN KEY (clinic_id, doctor_id) REFERENCES public.doctors (clinic_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (clinic_id, location_id) REFERENCES public.locations (clinic_id, id) ON DELETE RESTRICT
);

CREATE TABLE public.caller_profiles (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id uuid NOT NULL REFERENCES public.clinics ON DELETE RESTRICT,
    phone_ciphertext bytea NOT NULL,
    phone_lookup_hmac bytea NOT NULL,
    display_name_ciphertext bytea NOT NULL,
    pii_key_version text NOT NULL,
    preferred_language text,
    consent_status text NOT NULL CHECK (consent_status IN ('granted','withdrawn')),
    consented_at timestamptz NOT NULL,
    last_confirmed_at timestamptz NOT NULL,
    retention_until timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, id), UNIQUE (clinic_id, phone_lookup_hmac),
    CHECK (retention_until > consented_at)
);
ALTER TABLE public.call_sessions ADD COLUMN caller_profile_id uuid;
ALTER TABLE public.call_sessions ADD CONSTRAINT call_profile_same_clinic
    FOREIGN KEY (clinic_id, caller_profile_id)
    REFERENCES public.caller_profiles (clinic_id, id) ON DELETE RESTRICT;
ALTER TABLE public.appointment_requests ADD COLUMN caller_profile_id uuid;
ALTER TABLE public.appointment_requests ADD CONSTRAINT request_profile_same_clinic
    FOREIGN KEY (clinic_id, caller_profile_id)
    REFERENCES public.caller_profiles (clinic_id, id) ON DELETE RESTRICT;

CREATE TABLE public.consent_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id uuid NOT NULL REFERENCES public.clinics ON DELETE RESTRICT,
    caller_profile_id uuid,
    call_session_id uuid NOT NULL,
    consent_type text NOT NULL CHECK (consent_type IN ('profile_reuse','recording')),
    action text NOT NULL CHECK (action IN ('granted','denied','withdrawn')),
    notice_version text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, id),
    FOREIGN KEY (clinic_id, caller_profile_id)
        REFERENCES public.caller_profiles (clinic_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (clinic_id, call_session_id)
        REFERENCES public.call_sessions (clinic_id, id) ON DELETE RESTRICT
);

CREATE TABLE public.call_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id uuid NOT NULL REFERENCES public.clinics ON DELETE RESTRICT,
    call_session_id uuid NOT NULL,
    event_key uuid NOT NULL,
    event_type text NOT NULL CHECK (event_type IN (
        'started','ended','tool_failed','transfer_failed','safety_routed','timeout')),
    -- No free-form transcript/user payload accepted in Phase 1.
    sanitized_payload jsonb NOT NULL DEFAULT '{}' CHECK (sanitized_payload = '{}'::jsonb),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, id), UNIQUE (clinic_id, call_session_id, event_key),
    FOREIGN KEY (clinic_id, call_session_id)
        REFERENCES public.call_sessions (clinic_id, id) ON DELETE RESTRICT
);

CREATE TABLE public.callback_requests (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id uuid NOT NULL REFERENCES public.clinics ON DELETE RESTRICT,
    call_session_id uuid NOT NULL,
    caller_profile_id uuid,
    idempotency_key uuid NOT NULL,
    name_ciphertext bytea,
    callback_number_ciphertext bytea,
    pii_key_version text,
    pii_erased_at timestamptz,
    requested_time timestamptz,
    reason_category text NOT NULL CHECK (reason_category IN (
        'appointment','hours','fees','registration','human_requested','other_admin')),
    status text NOT NULL DEFAULT 'new'
        CHECK (status IN ('new','contacted','closed','cancelled')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, id), UNIQUE (clinic_id, call_session_id, idempotency_key),
    CHECK ((name_ciphertext IS NOT NULL AND callback_number_ciphertext IS NOT NULL
        AND pii_key_version IS NOT NULL) OR pii_erased_at IS NOT NULL),
    FOREIGN KEY (clinic_id, caller_profile_id)
        REFERENCES public.caller_profiles (clinic_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (clinic_id, call_session_id)
        REFERENCES public.call_sessions (clinic_id, id) ON DELETE RESTRICT
);

CREATE TABLE public.usage_records (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id uuid NOT NULL REFERENCES public.clinics ON DELETE RESTRICT,
    call_session_id uuid NOT NULL,
    event_key uuid NOT NULL,
    telephony_seconds integer NOT NULL DEFAULT 0 CHECK (telephony_seconds >= 0),
    stt_seconds numeric(12,3) NOT NULL DEFAULT 0 CHECK (stt_seconds >= 0),
    llm_input_tokens integer NOT NULL DEFAULT 0 CHECK (llm_input_tokens >= 0),
    llm_output_tokens integer NOT NULL DEFAULT 0 CHECK (llm_output_tokens >= 0),
    tts_characters integer NOT NULL DEFAULT 0 CHECK (tts_characters >= 0),
    telephony_cost numeric(12,6) NOT NULL DEFAULT 0 CHECK (telephony_cost >= 0),
    stt_cost numeric(12,6) NOT NULL DEFAULT 0 CHECK (stt_cost >= 0),
    llm_cost numeric(12,6) NOT NULL DEFAULT 0 CHECK (llm_cost >= 0),
    tts_cost numeric(12,6) NOT NULL DEFAULT 0 CHECK (tts_cost >= 0),
    currency text NOT NULL DEFAULT 'INR' CHECK (currency ~ '^[A-Z]{3}$'),
    rate_version text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, id), UNIQUE (clinic_id, call_session_id, event_key),
    FOREIGN KEY (clinic_id, call_session_id)
        REFERENCES public.call_sessions (clinic_id, id) ON DELETE RESTRICT
);

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['special_date_schedules','caller_profiles','consent_events',
        'call_events','callback_requests','usage_records'] LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC, anon, authenticated, clinic_runtime', t);
        EXECUTE format('GRANT SELECT ON public.%I TO authenticated', t);
        EXECUTE format('CREATE POLICY member_read ON public.%I FOR SELECT TO authenticated '
            || 'USING (clinic_private.has_membership(clinic_id, '
            || 'ARRAY[''owner'',''manager'',''receptionist'',''viewer'']))', t);
        EXECUTE format('CREATE INDEX ON public.%I (clinic_id)', t);
    END LOOP;
    FOREACH t IN ARRAY ARRAY['consent_events','call_events','usage_records'] LOOP
        EXECUTE format('CREATE TRIGGER append_only BEFORE UPDATE OR DELETE ON public.%I '
            || 'FOR EACH ROW EXECUTE FUNCTION clinic_private.append_only()', t);
    END LOOP;
    FOREACH t IN ARRAY ARRAY['caller_profiles','callback_requests'] LOOP
        EXECUTE format('CREATE TRIGGER touch_updated_at BEFORE UPDATE ON public.%I '
            || 'FOR EACH ROW EXECUTE FUNCTION clinic_private.touch_updated_at()', t);
    END LOOP;
END;
$$;

-- Conservative writes: all session/PII/consent/request operations are denied until
-- Phase 3 provides encryption, confirmation state and bounded authorized services.

-- Scope locks serialize fee changes (including nullable/endless intervals) even
-- without the btree_gist extension. Direct fee writes are migration-only for now.
CREATE FUNCTION clinic_private.prevent_fee_overlap() RETURNS trigger
LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
    PERFORM 1 FROM public.clinics WHERE id = NEW.clinic_id FOR UPDATE;
    IF NEW.status = 'active' AND EXISTS (
        SELECT 1 FROM public.doctor_services f
        WHERE f.clinic_id = NEW.clinic_id AND f.doctor_id = NEW.doctor_id
            AND f.service_id = NEW.service_id AND f.id <> NEW.id AND f.status = 'active'
            AND daterange(f.effective_from, f.effective_until, '[)')
                && daterange(NEW.effective_from, NEW.effective_until, '[)')
    ) THEN
        RAISE EXCEPTION 'Overlapping fee intervals' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER fee_overlap BEFORE INSERT OR UPDATE ON public.doctor_services
    FOR EACH ROW EXECUTE FUNCTION clinic_private.prevent_fee_overlap();
REVOKE ALL ON FUNCTION clinic_private.prevent_fee_overlap() FROM PUBLIC;