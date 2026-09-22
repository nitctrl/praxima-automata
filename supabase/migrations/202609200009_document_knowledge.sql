-- Phase 1 documents: staff-reviewed public prose becomes published snapshot sections.
-- Original uploads stay bounded and tenant-scoped; unreviewed text never reaches a caller.

ALTER TABLE public.knowledge_documents
    ADD COLUMN title text NOT NULL DEFAULT '',
    ADD COLUMN doctor_id uuid,
    ADD COLUMN sections jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN extraction_warnings text[] NOT NULL DEFAULT '{}',
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE public.knowledge_documents
    ADD CONSTRAINT document_doctor_same_clinic FOREIGN KEY (clinic_id, doctor_id)
        REFERENCES public.doctors (clinic_id, id) ON DELETE RESTRICT,
    -- Small pilot uploads only: the review UI and snapshot stay bounded and fast.
    ADD CONSTRAINT document_sections_bounded
        CHECK (jsonb_typeof(sections) = 'array' AND jsonb_array_length(sections) <= 200
            AND length(sections::text) <= 400000);

CREATE TRIGGER lock_authoring BEFORE INSERT OR UPDATE ON public.knowledge_documents
    FOR EACH ROW EXECUTE FUNCTION clinic_private.lock_authoring_clinic();
CREATE TRIGGER touch_updated_at BEFORE UPDATE ON public.knowledge_documents
    FOR EACH ROW EXECUTE FUNCTION clinic_private.touch_updated_at();

-- Drafts can still contain private material before redaction, so only managers read them.
DROP POLICY member_read ON public.knowledge_documents;
CREATE POLICY manager_read ON public.knowledge_documents FOR SELECT TO authenticated
    USING (clinic_private.has_membership(clinic_id, ARRAY['owner','manager']));
GRANT INSERT, UPDATE ON public.knowledge_documents TO authenticated;
CREATE POLICY manager_insert ON public.knowledge_documents FOR INSERT TO authenticated
    WITH CHECK (clinic_private.has_membership(clinic_id, ARRAY['owner','manager']));
CREATE POLICY manager_update ON public.knowledge_documents FOR UPDATE TO authenticated
    USING (clinic_private.has_membership(clinic_id, ARRAY['owner','manager']))
    WITH CHECK (clinic_private.has_membership(clinic_id, ARRAY['owner','manager']));

DO $$ DECLARE c text; BEGIN
    FOR c IN SELECT conname FROM pg_constraint
        WHERE conrelid = 'public.configuration_versions'::regclass AND contype = 'c'
            AND pg_get_constraintdef(oid) ILIKE '%schema_version%' LOOP
        EXECUTE format('ALTER TABLE public.configuration_versions DROP CONSTRAINT %I', c);
    END LOOP;
END; $$;
ALTER TABLE public.configuration_versions ADD CONSTRAINT configuration_versions_schema_version_v3
    CHECK (schema_version IN (1,2,3));

-- Schema 3 = schema 2 plus reviewed document sections. Structured rows stay authoritative.
CREATE OR REPLACE FUNCTION clinic_private.build_snapshot(target uuid) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
DECLARE result jsonb; table_name text; keys text[]; filter_sql text; rows_json jsonb;
BEGIN
    SELECT jsonb_build_object('schema_version',3,'clinic_id',id,'name',name,'timezone',timezone,
        'default_language',default_language,'supported_languages',supported_languages,
        'greeting',greeting,'emergency_message',emergency_message,'fallback_message',fallback_message,
        'transfer_enabled',transfer_enabled)
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

CREATE OR REPLACE FUNCTION clinic_private.validate_snapshot(s jsonb, target uuid) RETURNS void
LANGUAGE plpgsql SET search_path = '' AS $$
DECLARE group_name text; item jsonb; other jsonb; ref_key text; ref_group text; total integer;
BEGIN
    IF s->>'schema_version' NOT IN ('2','3') OR (s->>'clinic_id')::uuid <> target
        OR s->>'transfer_enabled' <> 'false'
        OR length(trim(s->>'emergency_message')) NOT BETWEEN 1 AND 2000
        OR NOT EXISTS (SELECT 1 FROM pg_catalog.pg_timezone_names WHERE name=s->>'timezone')
        OR NOT (s->'supported_languages' ? (s->>'default_language'))
        OR jsonb_array_length(s->'locations')=0
        OR NOT EXISTS (SELECT 1 FROM jsonb_array_elements(s->'weekly_schedules') r
            WHERE r->>'doctor_id' IS NULL) THEN
        RAISE EXCEPTION 'Invalid clinic settings or missing hours; transfers disabled in Phase 2'
            USING ERRCODE='23514';
    END IF;
    FOREACH group_name IN ARRAY ARRAY['doctors','services','locations','doctor_services',
        'weekly_schedules','special_date_schedules','schedule_exceptions','temporary_notices',
        'approved_faqs'] LOOP
        IF jsonb_array_length(s->group_name)>2000 THEN
            RAISE EXCEPTION 'Snapshot too large' USING ERRCODE='23514';
        END IF;
        FOR item IN SELECT * FROM jsonb_array_elements(s->group_name) LOOP
            FOR ref_key,ref_group IN VALUES ('doctor_id','doctors'),('service_id','services'),
                ('location_id','locations') LOOP
                IF item->>ref_key IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM jsonb_array_elements(s->ref_group) r
                    WHERE r->>'id'=item->>ref_key) THEN
                    RAISE EXCEPTION 'Reference to inactive or missing content' USING ERRCODE='23514';
                END IF;
            END LOOP;
        END LOOP;
    END LOOP;
    -- NULL doctor scope is compared explicitly. Adjacent half-open intervals are valid.
    IF EXISTS (SELECT 1 FROM jsonb_array_elements(s->'weekly_schedules') a,
        jsonb_array_elements(s->'weekly_schedules') b
        WHERE a->>'id' < b->>'id' AND a->>'doctor_id' IS NOT DISTINCT FROM b->>'doctor_id'
        AND a->>'location_id'=b->>'location_id' AND a->>'day_of_week'=b->>'day_of_week'
        AND (a->>'start_time')::time < (b->>'end_time')::time
        AND (b->>'start_time')::time < (a->>'end_time')::time
        AND daterange((a->>'effective_from')::date,(a->>'effective_until')::date,'[)')
            && daterange((b->>'effective_from')::date,(b->>'effective_until')::date,'[)')) THEN
        RAISE EXCEPTION 'Overlapping weekly hours' USING ERRCODE='23514';
    END IF;
    FOREACH group_name IN ARRAY ARRAY['special_date_schedules','schedule_exceptions'] LOOP
        FOR item IN SELECT * FROM jsonb_array_elements(s->group_name) LOOP
            FOR other IN SELECT * FROM jsonb_array_elements(s->group_name) LOOP
                IF item->>'id' < other->>'id'
                    AND item->>'doctor_id' IS NOT DISTINCT FROM other->>'doctor_id'
                    AND item->>'location_id'=other->>'location_id'
                    AND coalesce(item->>'schedule_date',item->>'exception_date') =
                        coalesce(other->>'schedule_date',other->>'exception_date')
                    AND (item->>'status'='unavailable' OR other->>'status'='unavailable'
                        OR ((item->>'start_time')::time < (other->>'end_time')::time
                        AND (other->>'start_time')::time < (item->>'end_time')::time)) THEN
                    RAISE EXCEPTION 'Conflicting date-specific hours' USING ERRCODE='23514';
                END IF;
            END LOOP;
        END LOOP;
    END LOOP;
    FOR item IN SELECT * FROM jsonb_array_elements(s->'temporary_notices') LOOP
        IF item->>'notice_type' NOT IN ('closure','manual_closure','doctor_unavailable',
            'service_unavailable','no_walk_ins','information')
            OR (item->>'notice_type'='doctor_unavailable' AND item->>'doctor_id' IS NULL)
            OR (item->>'notice_type'='service_unavailable' AND item->>'service_id' IS NULL) THEN
            RAISE EXCEPTION 'Unsupported notice scope/type' USING ERRCODE='23514';
        END IF;
    END LOOP;
    IF s->>'schema_version'='3' THEN
        IF jsonb_typeof(s->'document_sections') <> 'array'
            OR jsonb_array_length(s->'document_sections')>200 THEN
            RAISE EXCEPTION 'Invalid document sections' USING ERRCODE='23514';
        END IF;
        total := 0;
        FOR item IN SELECT * FROM jsonb_array_elements(s->'document_sections') LOOP
            total := total + length(item->>'text');
            IF item - ARRAY['id','document_id','document_title','document_version','topic',
                'heading','text','doctor_id','keywords'] <> '{}'::jsonb
                OR item->>'id' IS NULL OR item->>'document_id' IS NULL OR item->>'text' IS NULL
                OR length(item->>'text') NOT BETWEEN 1 AND 4000
                OR coalesce(length(item->>'heading'),0) > 200
                OR coalesce(length(item->>'document_title'),0) NOT BETWEEN 1 AND 200
                OR jsonb_typeof(item->'keywords') <> 'array'
                OR (item->>'doctor_id' IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM jsonb_array_elements(s->'doctors') d
                    WHERE d->>'id'=item->>'doctor_id')) THEN
                RAISE EXCEPTION 'Invalid or unreferenced document section' USING ERRCODE='23514';
            END IF;
        END LOOP;
        IF total > 200000 THEN
            RAISE EXCEPTION 'Published documents exceed the supported size' USING ERRCODE='23514';
        END IF;
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION clinic_private.preview_configuration(target uuid, source uuid DEFAULT NULL)
RETURNS TABLE(snapshot jsonb, digest text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE s jsonb;
BEGIN
    IF NOT clinic_private.has_membership(target,ARRAY['owner','manager']) THEN
        RAISE EXCEPTION 'Publication forbidden' USING ERRCODE='42501';
    END IF;
    IF source IS NULL THEN s := clinic_private.build_snapshot(target);
    ELSE
        SELECT v.snapshot INTO s FROM public.configuration_versions v
            WHERE clinic_id=target AND id=source AND schema_version IN (2,3)
                AND status IN ('published','superseded');
        IF s IS NULL THEN RAISE EXCEPTION 'Source unavailable' USING ERRCODE='23514'; END IF;
    END IF;
    PERFORM clinic_private.validate_snapshot(s,target);
    RETURN QUERY SELECT s,encode(sha256(convert_to(s::text,'UTF8')),'hex');
END;
$$;

-- Rolling back to a schema 2 version keeps that version's own schema number.
CREATE OR REPLACE FUNCTION clinic_private.publish_configuration(target uuid, expected_digest text,
    expected_active uuid, source uuid DEFAULT NULL)
RETURNS TABLE(version_id uuid, version_number integer, snapshot jsonb)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE s jsonb; d text; active uuid; next_number integer; new_id uuid := gen_random_uuid();
BEGIN
    -- Membership and authorship changes are rechecked after acquiring the lock.
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'Publication requires READ COMMITTED' USING ERRCODE='25000';
    END IF;
    SELECT active_configuration_version_id INTO active FROM public.clinics
        WHERE id=target FOR NO KEY UPDATE;
    IF NOT clinic_private.has_membership(target,ARRAY['owner','manager']) THEN
        RAISE EXCEPTION 'Publication forbidden' USING ERRCODE='42501';
    END IF;
    IF active IS DISTINCT FROM expected_active THEN
        RAISE EXCEPTION 'Active publication changed; preview again' USING ERRCODE='40001';
    END IF;
    SELECT p.snapshot,p.digest INTO s,d FROM clinic_private.preview_configuration(target,source) p;
    IF expected_digest IS DISTINCT FROM d THEN
        RAISE EXCEPTION 'Preview content changed; preview again' USING ERRCODE='40001';
    END IF;
    SELECT coalesce(max(v.version_number),0)+1 INTO next_number
        FROM public.configuration_versions v WHERE clinic_id=target;
    UPDATE public.configuration_versions SET status='superseded'
        WHERE clinic_id=target AND status='published';
    INSERT INTO public.configuration_versions
        (id,clinic_id,version_number,status,snapshot,schema_version,prompt_version,
            published_by,published_at,source_version_id)
        VALUES(new_id,target,next_number,'published',s,(s->>'schema_version')::integer,
            'admin-tools-v'||(s->>'schema_version'),auth.uid(),now(),source);
    UPDATE public.clinics SET active_configuration_version_id=new_id WHERE id=target;
    INSERT INTO public.audit_logs(clinic_id,actor_id,action,resource_type,resource_id,correlation_id)
        VALUES(target,auth.uid(),CASE WHEN source IS NULL THEN 'configuration_published'
            ELSE 'configuration_rollback_published' END,'configuration_version',new_id,gen_random_uuid());
    RETURN QUERY SELECT new_id,next_number,s;
END;
$$;
