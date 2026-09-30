-- Tenant-configured appointment slots, agent bookings and WhatsApp delivery.
-- Platform administrators own telephony/WhatsApp onboarding; a clinic never sets its own numbers.
-- Caller name/number stay encrypted application-side; SQL only moves ciphertext.

ALTER TABLE public.clinics ADD COLUMN slot_minutes smallint NOT NULL DEFAULT 30
    CHECK (slot_minutes BETWEEN 5 AND 240);

CREATE TABLE public.clinic_integrations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id uuid NOT NULL REFERENCES public.clinics ON DELETE RESTRICT,
    kind text NOT NULL CHECK (kind IN ('whatsapp','calendar')),
    provider text NOT NULL CHECK (provider IN ('open-wa','meta','internal')),
    external_reference text NOT NULL DEFAULT '' CHECK (length(external_reference) <= 120),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('inactive','pending','active')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, id), UNIQUE (clinic_id, kind),
    -- A WhatsApp integration must carry the tenant's own E.164 business number.
    CHECK (kind <> 'whatsapp' OR external_reference ~ '^\+[1-9][0-9]{7,14}$')
);

CREATE TABLE public.calendar_bookings (
    id uuid PRIMARY KEY,
    clinic_id uuid NOT NULL REFERENCES public.clinics ON DELETE RESTRICT,
    doctor_id uuid,
    service_id uuid,
    requested_date date NOT NULL,
    start_time time NOT NULL,
    end_time time NOT NULL,
    status text NOT NULL DEFAULT 'booked' CHECK (status IN ('booked','cancelled')),
    patient_name_ciphertext bytea,
    callback_number_ciphertext bytea,
    pii_key_version text,
    source text NOT NULL DEFAULT 'agent' CHECK (length(source) BETWEEN 1 AND 40),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, id),
    FOREIGN KEY (clinic_id, doctor_id) REFERENCES public.doctors (clinic_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (clinic_id, service_id) REFERENCES public.services (clinic_id, id) ON DELETE RESTRICT,
    CHECK (end_time > start_time)
);
-- One live booking per doctor and start time; clinic-wide slots share the nil UUID bucket.
CREATE UNIQUE INDEX calendar_bookings_one_per_slot ON public.calendar_bookings
    (clinic_id, coalesce(doctor_id, '00000000-0000-0000-0000-000000000000'::uuid),
     requested_date, start_time) WHERE status = 'booked';
CREATE INDEX calendar_bookings_day ON public.calendar_bookings
    (clinic_id, requested_date DESC, start_time);

CREATE TABLE public.whatsapp_messages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    clinic_id uuid NOT NULL REFERENCES public.clinics ON DELETE RESTRICT,
    booking_id uuid,
    audience text NOT NULL CHECK (audience IN ('caller','clinic')),
    recipient_ciphertext bytea NOT NULL,
    pii_key_version text NOT NULL,
    body text NOT NULL CHECK (length(body) BETWEEN 1 AND 1000),
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','sent','failed')),
    attempts smallint NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 20),
    last_error text NOT NULL DEFAULT '' CHECK (length(last_error) <= 200),
    sent_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (clinic_id, id),
    FOREIGN KEY (clinic_id, booking_id)
        REFERENCES public.calendar_bookings (clinic_id, id) ON DELETE RESTRICT
);
CREATE INDEX whatsapp_messages_pending ON public.whatsapp_messages (clinic_id, created_at)
    WHERE status = 'queued';

-- ── Runtime (voice agent) surface ───────────────────────────────────────────

-- Only taken start times leave the database; free slots are derived from the pinned
-- publication snapshot so a live call never depends on unpublished rows.
CREATE FUNCTION clinic_private.booked_slots(target_date date, doctor uuid DEFAULT NULL)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
    SELECT coalesce(jsonb_agg(to_char(start_time,'HH24:MI') ORDER BY start_time), '[]'::jsonb)
    FROM public.calendar_bookings
    WHERE clinic_id = clinic_private.runtime_clinic() AND requested_date = target_date
        AND status = 'booked' AND (doctor IS NULL OR doctor_id = doctor);
$$;

CREATE FUNCTION clinic_private.book_calendar_slot(booking_key uuid, target_date date,
    slot_start time, slot_end time, doctor uuid DEFAULT NULL, service uuid DEFAULT NULL,
    name_ct bytea DEFAULT NULL, phone_ct bytea DEFAULT NULL, key_version text DEFAULT NULL,
    source text DEFAULT 'agent')
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE tenant uuid := clinic_private.runtime_clinic(); c public.clinics; local_today date;
    booking public.calendar_bookings; whatsapp text;
BEGIN
    SELECT * INTO c FROM public.clinics WHERE id = tenant AND status = 'active';
    IF NOT FOUND OR booking_key IS NULL OR target_date IS NULL OR slot_start IS NULL
        OR slot_end IS NULL OR slot_end <= slot_start THEN
        RAISE EXCEPTION 'Invalid booking request' USING ERRCODE = '23514';
    END IF;
    local_today := (now() AT TIME ZONE c.timezone)::date;
    IF target_date < local_today OR target_date > local_today + 366 THEN
        RAISE EXCEPTION 'Requested date unavailable' USING ERRCODE = '23514';
    END IF;
    IF doctor IS NOT NULL AND NOT EXISTS (SELECT 1 FROM public.doctors d
        WHERE d.clinic_id = tenant AND d.id = doctor AND d.status = 'active'
            AND d.effective_from <= target_date
            AND (d.effective_until IS NULL OR target_date < d.effective_until)) THEN
        RAISE EXCEPTION 'Doctor outside the active clinic configuration' USING ERRCODE = '23514';
    END IF;
    IF service IS NOT NULL AND NOT EXISTS (SELECT 1 FROM public.services s
        WHERE s.clinic_id = tenant AND s.id = service AND s.active
            AND s.effective_from <= target_date
            AND (s.effective_until IS NULL OR target_date < s.effective_until)) THEN
        RAISE EXCEPTION 'Service outside the active clinic configuration' USING ERRCODE = '23514';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM public.clinic_integrations
        WHERE clinic_id = tenant AND kind = 'calendar' AND status = 'active') THEN
        RAISE EXCEPTION 'Calendar integration missing' USING ERRCODE = '42501';
    END IF;

    -- A retried tool call must never create a second booking for the same caller intent.
    SELECT * INTO booking FROM public.calendar_bookings
        WHERE clinic_id = tenant AND id = booking_key;
    IF NOT FOUND THEN
        BEGIN
            INSERT INTO public.calendar_bookings(id, clinic_id, doctor_id, service_id,
                requested_date, start_time, end_time, patient_name_ciphertext,
                callback_number_ciphertext, pii_key_version, source)
            VALUES (booking_key, tenant, doctor, service, target_date, slot_start, slot_end,
                name_ct, phone_ct, key_version, source)
            RETURNING * INTO booking;
        EXCEPTION WHEN unique_violation THEN
            RETURN jsonb_build_object('status','taken');
        END;
        INSERT INTO public.audit_logs(clinic_id, action, resource_type, resource_id, correlation_id)
            VALUES (tenant, 'calendar_slot_booked', 'calendar_bookings', booking.id, booking_key);
    END IF;

    SELECT external_reference INTO whatsapp FROM public.clinic_integrations
        WHERE clinic_id = tenant AND kind = 'whatsapp' AND status = 'active';
    RETURN jsonb_build_object('status','booked', 'id', booking.id,
        'requested_date', booking.requested_date,
        'start_time', to_char(booking.start_time,'HH24:MI'),
        'end_time', to_char(booking.end_time,'HH24:MI'),
        'clinic_whatsapp', whatsapp, 'clinic_name', c.name);
END;
$$;

-- Encryption happens in the application; this only validates and stores the batch.
CREATE FUNCTION clinic_private.queue_whatsapp(booking uuid, items jsonb)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE tenant uuid := clinic_private.runtime_clinic(); inserted integer;
BEGIN
    IF jsonb_typeof(items) <> 'array' OR jsonb_array_length(items) NOT BETWEEN 1 AND 4
        OR NOT EXISTS (SELECT 1 FROM public.calendar_bookings
            WHERE clinic_id = tenant AND id = booking) THEN
        RAISE EXCEPTION 'Invalid message batch' USING ERRCODE = '23514';
    END IF;
    INSERT INTO public.whatsapp_messages(clinic_id, booking_id, audience,
        recipient_ciphertext, pii_key_version, body)
    SELECT tenant, booking, i->>'audience', decode(i->>'recipient','base64'),
        i->>'key_version', i->>'body'
    FROM jsonb_array_elements(items) i;
    GET DIAGNOSTICS inserted = ROW_COUNT;
    RETURN inserted;
END;
$$;

CREATE FUNCTION clinic_private.whatsapp_outbox(maximum integer DEFAULT 10)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
    SELECT coalesce(jsonb_agg(jsonb_build_object('id', id, 'audience', audience,
        'recipient', encode(recipient_ciphertext,'base64'), 'key_version', pii_key_version,
        'booking_id', booking_id, 'body', body)), '[]'::jsonb)
    FROM (SELECT * FROM public.whatsapp_messages
        WHERE clinic_id = clinic_private.runtime_clinic() AND status = 'queued'
        ORDER BY created_at LIMIT greatest(1, least(coalesce(maximum, 10), 50))) q;
$$;

CREATE FUNCTION clinic_private.whatsapp_mark(message uuid, delivered boolean,
    reason text DEFAULT '')
RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path = '' AS $$
    UPDATE public.whatsapp_messages
    SET attempts = attempts + 1,
        status = CASE WHEN delivered THEN 'sent'
            WHEN attempts + 1 >= 5 THEN 'failed' ELSE 'queued' END,
        last_error = CASE WHEN delivered THEN '' ELSE left(coalesce(reason, ''), 200) END,
        sent_at = CASE WHEN delivered THEN now() ELSE sent_at END
    WHERE clinic_id = clinic_private.runtime_clinic() AND id = message AND status = 'queued';
$$;

-- ── Platform administration: onboarding a tenant and its numbers ────────────

CREATE FUNCTION public.clinic_platform_onboard(clinic_name text, owner_email text,
    called_number text, whatsapp_number text, zone text DEFAULT 'Asia/Kolkata',
    trunk text DEFAULT 'pending-trunk-assignment')
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE tenant uuid; owner uuid; base_slug text; unique_slug text; place uuid; day integer;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM clinic_private.platform_admins
        WHERE auth_user_id = auth.uid() AND active) THEN
        RAISE EXCEPTION 'Platform access forbidden' USING ERRCODE = '42501';
    END IF;
    IF length(trim(coalesce(clinic_name, ''))) NOT BETWEEN 1 AND 200
        OR coalesce(called_number, '') !~ '^\+[1-9][0-9]{7,14}$'
        OR coalesce(whatsapp_number, '') !~ '^\+[1-9][0-9]{7,14}$'
        OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_timezone_names WHERE name = zone) THEN
        RAISE EXCEPTION 'Provide a clinic name, valid E.164 numbers and a real timezone'
            USING ERRCODE = '23514';
    END IF;
    base_slug := trim(both '-' from
        left(regexp_replace(lower(trim(clinic_name)), '[^a-z0-9]+', '-', 'g'), 80));
    IF length(base_slug) < 2 THEN base_slug := 'clinic'; END IF;
    unique_slug := base_slug;
    WHILE EXISTS (SELECT 1 FROM public.clinics WHERE slug = unique_slug) LOOP
        unique_slug := base_slug || '-' || substr(gen_random_uuid()::text, 1, 6);
    END LOOP;

    INSERT INTO public.clinics(name, slug, timezone, status, greeting, emergency_message,
        fallback_message)
    VALUES (trim(clinic_name), unique_slug, zone, 'active',
        'Thank you for calling ' || trim(clinic_name) || '. This is an automated assistant.',
        'This assistant cannot give medical advice. For an emergency, contact local '
            || 'emergency services immediately.',
        'I do not have confirmed information for that. Please contact reception.')
    RETURNING id INTO tenant;

    INSERT INTO public.locations(clinic_id, name, address)
        VALUES (tenant, 'Main location', 'Address not yet provided') RETURNING id INTO place;
    -- Default clinic hours make a new tenant publishable immediately; staff edit them after.
    FOR day IN 0..6 LOOP
        INSERT INTO public.weekly_schedules(clinic_id, location_id, day_of_week,
            start_time, end_time, availability_type)
        VALUES (tenant, place, day, '09:00', '17:00', 'clinic_hours');
    END LOOP;

    INSERT INTO public.phone_numbers(clinic_id, provider, e164_number, trusted_trunk_id, status)
        VALUES (tenant, 'plivo', called_number, trunk, 'active');
    INSERT INTO public.clinic_integrations(clinic_id, kind, provider, external_reference, status)
        VALUES (tenant, 'calendar', 'internal', 'internal-calendar', 'active'),
               (tenant, 'whatsapp', 'open-wa', whatsapp_number, 'pending');

    SELECT id INTO owner FROM auth.users WHERE lower(email) = lower(trim(coalesce(owner_email,'')));
    IF owner IS NOT NULL THEN
        INSERT INTO public.clinic_users(clinic_id, auth_user_id, role)
            VALUES (tenant, owner, 'owner');
    END IF;
    INSERT INTO public.audit_logs(clinic_id, actor_id, action, resource_type, resource_id,
        correlation_id)
        VALUES (tenant, auth.uid(), 'clinic_onboarded', 'clinics', tenant, gen_random_uuid());
    RETURN jsonb_build_object('clinic_id', tenant, 'slug', unique_slug,
        'owner_linked', owner IS NOT NULL);
END;
$$;

CREATE FUNCTION public.clinic_platform_integration(target uuid, integration_kind text,
    reference text, new_status text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE result public.clinic_integrations;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM clinic_private.platform_admins
        WHERE auth_user_id = auth.uid() AND active) THEN
        RAISE EXCEPTION 'Platform access forbidden' USING ERRCODE = '42501';
    END IF;
    IF integration_kind NOT IN ('whatsapp','calendar')
        OR new_status NOT IN ('inactive','pending','active')
        OR (integration_kind = 'whatsapp'
            AND coalesce(reference, '') !~ '^\+[1-9][0-9]{7,14}$') THEN
        RAISE EXCEPTION 'Provide a valid integration kind, E.164 number and status'
            USING ERRCODE = '23514';
    END IF;
    INSERT INTO public.clinic_integrations(clinic_id, kind, provider, external_reference, status)
    VALUES (target, integration_kind,
        CASE WHEN integration_kind = 'whatsapp' THEN 'open-wa' ELSE 'internal' END,
        reference, new_status)
    ON CONFLICT (clinic_id, kind) DO UPDATE
        SET external_reference = excluded.external_reference, status = excluded.status
    RETURNING * INTO result;
    INSERT INTO public.audit_logs(clinic_id, actor_id, action, resource_type, resource_id,
        correlation_id)
        VALUES (target, auth.uid(), 'integration_configured', 'clinic_integrations', result.id,
            gen_random_uuid());
    RETURN jsonb_build_object('id', result.id, 'kind', result.kind, 'status', result.status);
END;
$$;

-- Staff may retry a failed notification; they never see the stored recipient ciphertext.
CREATE FUNCTION public.clinic_whatsapp_retry(target uuid, message uuid)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
BEGIN
    IF NOT clinic_private.has_membership(target, ARRAY['owner','manager']) THEN
        RAISE EXCEPTION 'Retry forbidden' USING ERRCODE = '42501';
    END IF;
    UPDATE public.whatsapp_messages SET status = 'queued', attempts = 0, last_error = ''
        WHERE clinic_id = target AND id = message AND status = 'failed';
END;
$$;

-- ── Settings and snapshot: slot length is tenant-configured ─────────────────

CREATE OR REPLACE FUNCTION public.clinic_settings(target uuid, settings jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    IF NOT clinic_private.has_membership(target,ARRAY['owner','manager']) THEN
        RAISE EXCEPTION 'Settings forbidden' USING ERRCODE='42501';
    END IF;
    IF jsonb_typeof(settings)<>'object' OR settings - ARRAY['greeting','emergency_message',
        'fallback_message','default_language','supported_languages','slot_minutes'] <> '{}'::jsonb
        OR length(settings::text)>10000
        OR (settings ? 'slot_minutes' AND jsonb_typeof(settings->'slot_minutes') <> 'number') THEN
        RAISE EXCEPTION 'Unsupported settings' USING ERRCODE='23514';
    END IF;
    UPDATE public.clinics SET greeting=coalesce(settings->>'greeting',greeting),
        emergency_message=coalesce(settings->>'emergency_message',emergency_message),
        fallback_message=coalesce(settings->>'fallback_message',fallback_message),
        default_language=coalesce(settings->>'default_language',default_language),
        slot_minutes=coalesce((settings->>'slot_minutes')::smallint,slot_minutes),
        supported_languages=CASE WHEN settings ? 'supported_languages' THEN
            ARRAY(SELECT jsonb_array_elements_text(settings->'supported_languages'))
            ELSE supported_languages END WHERE id=target;
    INSERT INTO public.audit_logs(clinic_id,actor_id,action,resource_type,resource_id,correlation_id)
        VALUES(target,auth.uid(),'settings_draft_updated','clinics',target,gen_random_uuid());
END;
$$;

-- Schema 3 plus the tenant's slot length. Structured rows stay authoritative.
CREATE OR REPLACE FUNCTION clinic_private.build_snapshot(target uuid) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
DECLARE result jsonb; table_name text; keys text[]; filter_sql text; rows_json jsonb;
BEGIN
    SELECT jsonb_build_object('schema_version',3,'clinic_id',id,'name',name,'timezone',timezone,
        'default_language',default_language,'supported_languages',supported_languages,
        'greeting',greeting,'emergency_message',emergency_message,'fallback_message',fallback_message,
        'transfer_enabled',transfer_enabled,'slot_minutes',slot_minutes)
    INTO result FROM public.clinics WHERE id=target AND status='active';
    IF result IS NULL THEN RAISE EXCEPTION 'Clinic unavailable' USING ERRCODE='42501'; END IF;
    -- Static allowlists: new columns never enter snapshots implicitly.
    FOR table_name,keys,filter_sql IN SELECT * FROM (VALUES
        ('doctors', ARRAY['id','display_name','aliases','speciality','languages','short_public_bio',
            'accepts_new_patients','effective_from','effective_until'], 'status = ''active'''),
        ('services', ARRAY['id','name','aliases','short_approved_description','appointment_required',
            'effective_from','effective_until'], 'active'),
        ('locations', ARRAY['id','name','address','landmark','directions','map_url','parking_information',
            'effective_from','effective_until'], 'status = ''active'''),
        ('doctor_services', ARRAY['id','doctor_id','service_id','current_fee','currency',
            'effective_from','effective_until'], 'status = ''active'''),
        ('weekly_schedules', ARRAY['id','doctor_id','location_id','day_of_week','start_time','end_time',
            'availability_type','effective_from','effective_until'], 'status = ''active'''),
        ('special_date_schedules', ARRAY['id','doctor_id','location_id','schedule_date',
            'start_time','end_time'], 'publication_status = ''published'''),
        ('schedule_exceptions', ARRAY['id','doctor_id','location_id','exception_date','status',
            'start_time','end_time','public_message'], 'publication_status = ''published'''),
        ('temporary_notices', ARRAY['id','location_id','doctor_id','service_id','notice_type',
            'public_message','starts_at','expires_at','priority'], 'publication_status = ''published'''),
        ('approved_faqs', ARRAY['id','category','canonical_question','alternative_phrasings',
            'approved_answer','effective_from','effective_until'], 'publication_status = ''published''')
    ) AS projections(t,k,f) LOOP
        EXECUTE format('SELECT coalesce(jsonb_agg(projected ORDER BY row_id),''[]''::jsonb) '
            || 'FROM (SELECT id AS row_id, (SELECT jsonb_object_agg(k,to_jsonb(r)->k) '
            || 'FROM unnest($2) k) AS projected FROM public.%I r WHERE clinic_id=$1 AND %s) q',
            table_name, filter_sql) INTO rows_json USING target,keys;
        result := result || jsonb_build_object(table_name, rows_json);
    END LOOP;
    -- Only reviewed sections of effective published documents; never the raw extracted text.
    SELECT coalesce(jsonb_agg(jsonb_build_object(
            'id', s->>'id', 'document_id', d.id, 'document_title', d.title,
            'document_version', d.version, 'topic', d.document_category,
            'heading', s->>'heading', 'text', s->>'text',
            'doctor_id', coalesce(s->>'doctor_id', d.doctor_id::text),
            'keywords', coalesce(s->'keywords','[]'::jsonb))
        ORDER BY d.created_at, d.id, (s->>'position')::integer), '[]'::jsonb)
    INTO rows_json FROM public.knowledge_documents d
        CROSS JOIN LATERAL jsonb_array_elements(d.sections) s
        WHERE d.clinic_id=target AND d.status='published' AND d.effective_from<=now()
            AND (d.effective_until IS NULL OR d.effective_until>now());
    RETURN result || jsonb_build_object('document_sections', rows_json);
END;
$$;

-- ── Isolation ───────────────────────────────────────────────────────────────

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['clinic_integrations','calendar_bookings','whatsapp_messages'] LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('REVOKE ALL ON public.%I FROM PUBLIC, anon, authenticated, clinic_runtime', t);
        EXECUTE format('CREATE POLICY member_read ON public.%I FOR SELECT TO authenticated USING '
            || '(clinic_private.has_membership(clinic_id, '
            || 'ARRAY[''owner'',''manager'',''receptionist'',''viewer'']))', t);
    END LOOP;
    -- whatsapp_messages is append-and-mark only; it carries no updated_at column.
    FOREACH t IN ARRAY ARRAY['clinic_integrations','calendar_bookings'] LOOP
        EXECUTE format('CREATE TRIGGER touch_updated_at BEFORE UPDATE ON public.%I '
            || 'FOR EACH ROW EXECUTE FUNCTION clinic_private.touch_updated_at()', t);
    END LOOP;
END;
$$;

-- Column grants only: caller ciphertext and recipient ciphertext never reach a browser JWT.
GRANT SELECT ON public.clinic_integrations TO authenticated;
GRANT SELECT (id, clinic_id, doctor_id, service_id, requested_date, start_time, end_time,
    status, source, created_at) ON public.calendar_bookings TO authenticated;
GRANT SELECT (id, clinic_id, booking_id, audience, status, attempts, last_error, sent_at,
    created_at) ON public.whatsapp_messages TO authenticated;

REVOKE ALL ON FUNCTION clinic_private.booked_slots(date,uuid),
    clinic_private.book_calendar_slot(uuid,date,time,time,uuid,uuid,bytea,bytea,text,text),
    clinic_private.queue_whatsapp(uuid,jsonb), clinic_private.whatsapp_outbox(integer),
    clinic_private.whatsapp_mark(uuid,boolean,text)
    FROM PUBLIC, anon, authenticated, clinic_runtime;
GRANT EXECUTE ON FUNCTION clinic_private.booked_slots(date,uuid),
    clinic_private.book_calendar_slot(uuid,date,time,time,uuid,uuid,bytea,bytea,text,text),
    clinic_private.queue_whatsapp(uuid,jsonb), clinic_private.whatsapp_outbox(integer),
    clinic_private.whatsapp_mark(uuid,boolean,text) TO clinic_runtime;

REVOKE ALL ON FUNCTION public.clinic_platform_onboard(text,text,text,text,text,text),
    public.clinic_platform_integration(uuid,text,text,text),
    public.clinic_whatsapp_retry(uuid,uuid) FROM PUBLIC, anon, authenticated, clinic_runtime;
GRANT EXECUTE ON FUNCTION public.clinic_platform_onboard(text,text,text,text,text,text),
    public.clinic_platform_integration(uuid,text,text,text),
    public.clinic_whatsapp_retry(uuid,uuid) TO authenticated;
