-- Restricted lifecycle/request functions. No generic runtime table mutations.
ALTER TABLE public.call_sessions ADD COLUMN deadline_at timestamptz;
ALTER TABLE public.call_sessions ADD COLUMN heartbeat_at timestamptz;
ALTER TABLE public.call_sessions ADD COLUMN consent_profile_reuse boolean NOT NULL DEFAULT false;
CREATE INDEX running_call_deadlines ON public.call_sessions(clinic_id,deadline_at)
    WHERE ended_at IS NULL;

CREATE FUNCTION clinic_private.runtime_clinic() RETURNS uuid
LANGUAGE sql STABLE SET search_path='' AS $$
    SELECT nullif(current_setting('app.clinic_id',true),'')::uuid;
$$;

CREATE FUNCTION clinic_private.start_call(phone uuid, expected_version uuid, provider_name text,
    account_ref text, call_ref text, room_ref text, test_call boolean DEFAULT false)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE tenant uuid := clinic_private.runtime_clinic(); c public.clinics; old public.call_sessions;
    result public.call_sessions; total_minutes numeric;
BEGIN
    IF tenant IS NULL OR length(call_ref) NOT BETWEEN 1 AND 200
        OR length(account_ref) NOT BETWEEN 1 AND 200 OR length(room_ref) NOT BETWEEN 1 AND 200 THEN
        RAISE EXCEPTION 'Invalid call context' USING ERRCODE='23514';
    END IF;
    SELECT * INTO c FROM public.clinics WHERE id=tenant AND status='active' FOR NO KEY UPDATE;
    IF NOT FOUND OR NOT EXISTS(SELECT 1 FROM public.phone_numbers WHERE id=phone AND clinic_id=tenant
        AND provider=provider_name AND status='active' AND inbound_enabled) THEN
        RAISE EXCEPTION 'Invalid call route' USING ERRCODE='42501';
    END IF;
    SELECT * INTO old FROM public.call_sessions WHERE provider=provider_name
        AND provider_account_reference=account_ref AND provider_call_id=call_ref;
    IF FOUND THEN
        IF old.clinic_id <> tenant OR old.phone_number_id <> phone OR old.livekit_room_id <> room_ref
            OR old.ended_at IS NOT NULL OR old.deadline_at <= now() THEN
            RAISE EXCEPTION 'Call cannot be resumed' USING ERRCODE='42501';
        END IF;
        RETURN jsonb_build_object('id',old.id,'configuration_version_id',old.configuration_version_id,
            'deadline_at',old.deadline_at);
    END IF;
    IF c.active_configuration_version_id IS DISTINCT FROM expected_version
        OR NOT EXISTS(SELECT 1 FROM public.configuration_versions WHERE id=expected_version
            AND clinic_id=tenant AND schema_version=2 AND status='published') THEN
        RAISE EXCEPTION 'Published version changed or unsupported' USING ERRCODE='40001';
    END IF;
    IF (SELECT count(*) FROM public.call_sessions WHERE clinic_id=tenant AND ended_at IS NULL
        AND deadline_at>now()) >= 4 THEN
        RAISE EXCEPTION 'Clinic concurrency limit reached' USING ERRCODE='54000';
    END IF;
    SELECT coalesce(sum(CASE WHEN ended_at IS NULL THEN
        extract(epoch FROM deadline_at-started_at) ELSE coalesce(duration_seconds,0) END)/60,0)
    INTO total_minutes FROM public.call_sessions WHERE clinic_id=tenant AND NOT is_test
        AND started_at>=date_trunc('month',now());
    IF NOT test_call AND total_minutes + c.maximum_call_duration_seconds/60.0 > c.monthly_minute_limit THEN
        RAISE EXCEPTION 'Clinic usage limit reached' USING ERRCODE='54000';
    END IF;
    INSERT INTO public.call_sessions(clinic_id,phone_number_id,provider,provider_account_reference,
        provider_call_id,livekit_room_id,configuration_version_id,answered_at,heartbeat_at,
        deadline_at,is_test,retention_until)
    VALUES(tenant,phone,provider_name,account_ref,call_ref,room_ref,expected_version,now(),now(),
        now()+make_interval(secs=>c.maximum_call_duration_seconds),test_call,now()+interval '30 days')
    RETURNING * INTO result;
    INSERT INTO public.call_events(clinic_id,call_session_id,event_key,event_type)
        VALUES(tenant,result.id,gen_random_uuid(),'started');
    RETURN jsonb_build_object('id',result.id,'configuration_version_id',expected_version,
        'deadline_at',result.deadline_at);
END;
$$;

CREATE FUNCTION clinic_private.update_call(session_id uuid, action text, event_id uuid)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE tenant uuid := clinic_private.runtime_clinic(); s public.call_sessions; kind text;
BEGIN
    IF action NOT IN ('heartbeat','ended','timeout','tool_failed','transfer_failed','safety_routed') THEN
        RAISE EXCEPTION 'Unsupported event' USING ERRCODE='23514';
    END IF;
    SELECT * INTO s FROM public.call_sessions WHERE clinic_id=tenant AND id=session_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Session unavailable' USING ERRCODE='42501'; END IF;
    IF s.ended_at IS NOT NULL THEN RETURN false; END IF;
    IF action='heartbeat' AND s.deadline_at>now() THEN
        UPDATE public.call_sessions SET heartbeat_at=now() WHERE id=session_id;
        RETURN true;
    END IF;
    kind := CASE WHEN s.deadline_at<=now() THEN 'timeout' ELSE action END;
    IF kind='heartbeat' THEN kind:='timeout'; END IF;
    INSERT INTO public.call_events(clinic_id,call_session_id,event_key,event_type)
        VALUES(tenant,session_id,event_id,kind) ON CONFLICT(clinic_id,call_session_id,event_key) DO NOTHING;
    UPDATE public.call_sessions SET
        safety_flag=safety_flag OR kind='safety_routed',
        transfer_attempted=transfer_attempted OR kind='transfer_failed',
        failure_code=CASE WHEN kind IN ('timeout','tool_failed','transfer_failed') THEN kind ELSE failure_code END,
        ended_at=CASE WHEN kind IN ('ended','timeout') THEN now() ELSE ended_at END,
        duration_seconds=CASE WHEN kind IN ('ended','timeout') THEN
            greatest(0,extract(epoch FROM now()-started_at)::integer) ELSE duration_seconds END,
        disposition=CASE WHEN kind='timeout' THEN 'timeout'
            WHEN kind='ended' AND disposition='started' THEN 'completed' ELSE disposition END,
        heartbeat_at=now()
        WHERE id=session_id;
    RETURN kind NOT IN ('ended','timeout');
END;
$$;

CREATE FUNCTION clinic_private.create_request(session_id uuid, request_id uuid, request_kind text,
    name_encrypted bytea, phone_encrypted bytea, key_version text, details jsonb)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE tenant uuid := clinic_private.runtime_clinic(); s public.call_sessions; snapshot jsonb;
    doctor uuid := (details->>'doctor_id')::uuid; service uuid := (details->>'service_id')::uuid;
    request_date date := (details->>'preferred_date')::date; existing uuid; local_today date;
BEGIN
    SELECT * INTO s FROM public.call_sessions WHERE clinic_id=tenant AND id=session_id FOR UPDATE;
    IF NOT FOUND OR s.ended_at IS NOT NULL OR s.deadline_at<=now() THEN
        RAISE EXCEPTION 'Session unavailable' USING ERRCODE='42501';
    END IF;
    IF request_kind NOT IN ('appointment','callback') OR name_encrypted IS NULL
        OR phone_encrypted IS NULL OR length(name_encrypted) NOT BETWEEN 29 AND 1024
        OR length(phone_encrypted) NOT BETWEEN 29 AND 128
        OR key_version IS NULL OR length(key_version) NOT BETWEEN 1 AND 80
        OR details - ARRAY['doctor_id','service_id','preferred_date','preferred_time_start',
            'preferred_time_end','is_new_patient','requested_time','reason_category'] <> '{}'::jsonb THEN
        RAISE EXCEPTION 'Invalid request fields' USING ERRCODE='23514';
    END IF;
    SELECT v.snapshot INTO snapshot FROM public.configuration_versions v
        WHERE clinic_id=tenant AND id=s.configuration_version_id AND status IN ('published','superseded');
    IF snapshot IS NULL THEN RAISE EXCEPTION 'Configuration unavailable' USING ERRCODE='42501'; END IF;
    local_today := (now() AT TIME ZONE (snapshot->>'timezone'))::date;
    IF request_kind='appointment' THEN
        IF request_date IS NULL OR request_date<local_today OR request_date>local_today+366
            OR (doctor IS NULL AND service IS NULL) THEN
            RAISE EXCEPTION 'Invalid appointment request' USING ERRCODE='23514';
        END IF;
        IF doctor IS NOT NULL AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(snapshot->'doctors') d
            WHERE (d->>'id')::uuid=doctor AND (d->>'effective_from')::date<=request_date
                AND ((d->>'effective_until') IS NULL OR request_date<(d->>'effective_until')::date))
            OR service IS NOT NULL AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(snapshot->'services') d
            WHERE (d->>'id')::uuid=service AND (d->>'effective_from')::date<=request_date
                AND ((d->>'effective_until') IS NULL OR request_date<(d->>'effective_until')::date)) THEN
            RAISE EXCEPTION 'Reference not in pinned configuration' USING ERRCODE='23514';
        END IF;
        INSERT INTO public.appointment_requests(id,clinic_id,call_session_id,idempotency_key,
            patient_name_ciphertext,callback_number_ciphertext,pii_key_version,is_new_patient,
            doctor_id,service_id,preferred_date,preferred_time_start,preferred_time_end)
        VALUES(request_id,tenant,session_id,request_id,name_encrypted,phone_encrypted,key_version,
            coalesce((details->>'is_new_patient')::boolean,true),doctor,service,request_date,
            (details->>'preferred_time_start')::time,(details->>'preferred_time_end')::time)
        ON CONFLICT(clinic_id,call_session_id,idempotency_key) DO NOTHING;
        SELECT id INTO existing FROM public.appointment_requests
            WHERE clinic_id=tenant AND call_session_id=session_id AND idempotency_key=request_id;
    ELSE
        IF details->>'requested_time' IS NOT NULL AND (details->>'requested_time')::timestamptz<now() THEN
            RAISE EXCEPTION 'Callback time is past' USING ERRCODE='23514';
        END IF;
        INSERT INTO public.callback_requests(id,clinic_id,call_session_id,idempotency_key,
            name_ciphertext,callback_number_ciphertext,pii_key_version,requested_time,reason_category)
        VALUES(request_id,tenant,session_id,request_id,name_encrypted,phone_encrypted,key_version,
            (details->>'requested_time')::timestamptz,details->>'reason_category')
        ON CONFLICT(clinic_id,call_session_id,idempotency_key) DO NOTHING;
        SELECT id INTO existing FROM public.callback_requests
            WHERE clinic_id=tenant AND call_session_id=session_id AND idempotency_key=request_id;
    END IF;
    UPDATE public.call_sessions SET disposition='request_collected',
        short_administrative_summary='Administrative request received; staff confirmation required.'
        WHERE id=session_id;
    INSERT INTO public.audit_logs(clinic_id,action,resource_type,resource_id,correlation_id)
        VALUES(tenant,'request_received',request_kind,existing,request_id);
    RETURN existing;
END;
$$;

CREATE FUNCTION clinic_private.record_usage(session_id uuid, event_id uuid, input_tokens integer,
    output_tokens integer, stt_seconds numeric, tts_characters integer)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE tenant uuid := clinic_private.runtime_clinic();
BEGIN
    IF NOT EXISTS(SELECT 1 FROM public.call_sessions WHERE clinic_id=tenant AND id=session_id) THEN
        RAISE EXCEPTION 'Session unavailable' USING ERRCODE='42501';
    END IF;
    INSERT INTO public.usage_records(clinic_id,call_session_id,event_key,llm_input_tokens,
        llm_output_tokens,stt_seconds,tts_characters,rate_version)
    VALUES(tenant,session_id,event_id,input_tokens,output_tokens,stt_seconds,tts_characters,
        'unpriced-units-v1') ON CONFLICT(clinic_id,call_session_id,event_key) DO NOTHING;
END;
$$;

-- Reconciler is maintenance-only, not a model tool or a global runtime privilege.
CREATE FUNCTION clinic_private.reconcile_calls() RETURNS integer
LANGUAGE plpgsql SET search_path='' AS $$
DECLARE affected integer;
BEGIN
    UPDATE public.call_sessions SET ended_at=now(),disposition='worker_lost',failure_code='worker_lost',
        duration_seconds=greatest(0,extract(epoch FROM least(now(),deadline_at)-started_at)::integer)
        WHERE ended_at IS NULL AND deadline_at IS NOT NULL
            AND (deadline_at<=now() OR heartbeat_at<now()-interval '2 minutes');
    GET DIAGNOSTICS affected=ROW_COUNT;
    RETURN affected;
END;
$$;

CREATE FUNCTION public.clinic_preview(target uuid, source uuid DEFAULT NULL)
RETURNS TABLE(snapshot jsonb,digest text) LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT * FROM clinic_private.preview_configuration(target,source);
$$;
CREATE FUNCTION public.clinic_publish(target uuid, expected_digest text, expected_active uuid,
    source uuid DEFAULT NULL) RETURNS TABLE(version_id uuid,version_number integer,snapshot jsonb)
LANGUAGE sql SECURITY INVOKER SET search_path='' AS $$
    SELECT * FROM clinic_private.publish_configuration(target,expected_digest,expected_active,source);
$$;

CREATE FUNCTION public.clinic_request_status(target uuid, request_id uuid, request_kind text,
    new_status text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE affected integer;
BEGIN
    IF NOT clinic_private.has_membership(target,ARRAY['owner','manager','receptionist']) THEN
        RAISE EXCEPTION 'Request management forbidden' USING ERRCODE='42501';
    END IF;
    IF new_status NOT IN ('new','contacted','confirmed_externally','closed','cancelled') THEN
        RAISE EXCEPTION 'Invalid request status' USING ERRCODE='23514';
    END IF;
    IF request_kind='appointment' THEN
        UPDATE public.appointment_requests SET status=new_status WHERE clinic_id=target AND id=request_id;
    ELSIF request_kind='callback' AND new_status<>'confirmed_externally' THEN
        UPDATE public.callback_requests SET status=new_status WHERE clinic_id=target AND id=request_id;
    ELSE RAISE EXCEPTION 'Invalid request type' USING ERRCODE='23514'; END IF;
    GET DIAGNOSTICS affected=ROW_COUNT;
    IF affected<>1 THEN RAISE EXCEPTION 'Request unavailable' USING ERRCODE='42501'; END IF;
    INSERT INTO public.audit_logs(clinic_id,actor_id,action,resource_type,resource_id,correlation_id)
        VALUES(target,auth.uid(),'request_status_'||new_status,request_kind,request_id,gen_random_uuid());
END;
$$;

-- Staff identities may manage memberships only as an existing clinic owner.
CREATE FUNCTION public.clinic_membership(target uuid, user_id uuid, new_role text, active boolean)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    PERFORM 1 FROM public.clinics WHERE id=target FOR NO KEY UPDATE;
    IF NOT clinic_private.has_membership(target,ARRAY['owner']) OR new_role NOT IN
        ('owner','manager','receptionist','viewer') THEN
        RAISE EXCEPTION 'Membership management forbidden' USING ERRCODE='42501';
    END IF;
    IF user_id=auth.uid() THEN
        RAISE EXCEPTION 'Cannot change own membership' USING ERRCODE='42501';
    END IF;
    INSERT INTO public.clinic_users(clinic_id,auth_user_id,role,status)
        VALUES(target,user_id,new_role,CASE WHEN active THEN 'active' ELSE 'inactive' END)
        ON CONFLICT(clinic_id,auth_user_id) DO UPDATE SET role=excluded.role,status=excluded.status;
    INSERT INTO public.audit_logs(clinic_id,actor_id,action,resource_type,resource_id,correlation_id)
        VALUES(target,auth.uid(),'membership_changed','auth_user',user_id,gen_random_uuid());
END;
$$;

-- Missing authoring grants for new dashboard editors; fee history is append only.
GRANT INSERT ON public.doctor_services TO authenticated;
CREATE POLICY manager_fee_insert ON public.doctor_services FOR INSERT TO authenticated
    WITH CHECK(clinic_private.has_membership(clinic_id,ARRAY['owner','manager']));
GRANT INSERT,UPDATE ON public.special_date_schedules TO authenticated;
CREATE POLICY manager_special_insert ON public.special_date_schedules FOR INSERT TO authenticated
    WITH CHECK(clinic_private.has_membership(clinic_id,ARRAY['owner','manager']));
CREATE POLICY manager_special_update ON public.special_date_schedules FOR UPDATE TO authenticated
    USING(clinic_private.has_membership(clinic_id,ARRAY['owner','manager']))
    WITH CHECK(clinic_private.has_membership(clinic_id,ARRAY['owner','manager']));

CREATE FUNCTION clinic_private.audit_authoring() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    IF auth.uid() IS NOT NULL THEN
        INSERT INTO public.audit_logs(clinic_id,actor_id,action,resource_type,resource_id,correlation_id)
        VALUES(NEW.clinic_id,auth.uid(),lower(TG_OP),TG_TABLE_NAME,NEW.id,gen_random_uuid());
    END IF;
    RETURN NEW;
END;
$$;
DO $$ DECLARE t text; BEGIN
    FOREACH t IN ARRAY ARRAY['doctors','services','locations','doctor_services','weekly_schedules',
        'special_date_schedules','schedule_exceptions','temporary_notices','approved_faqs'] LOOP
        EXECUTE format('CREATE TRIGGER audit_authoring AFTER INSERT OR UPDATE ON public.%I '
            || 'FOR EACH ROW EXECUTE FUNCTION clinic_private.audit_authoring()',t);
    END LOOP;
END; $$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA clinic_private FROM PUBLIC,anon,authenticated,clinic_runtime;
GRANT EXECUTE ON FUNCTION clinic_private.has_membership(uuid,text[]) TO authenticated;
GRANT EXECUTE ON FUNCTION clinic_private.resolve_destination(text,text,text) TO clinic_runtime;
GRANT EXECUTE ON FUNCTION clinic_private.preview_configuration(uuid,uuid) TO authenticated;
GRANT EXECUTE ON FUNCTION clinic_private.publish_configuration(uuid,text,uuid,uuid) TO authenticated;
GRANT EXECUTE ON FUNCTION clinic_private.start_call(uuid,uuid,text,text,text,text,boolean),
    clinic_private.update_call(uuid,text,uuid),
    clinic_private.create_request(uuid,uuid,text,bytea,bytea,text,jsonb),
    clinic_private.record_usage(uuid,uuid,integer,integer,numeric,integer) TO clinic_runtime;
REVOKE ALL ON FUNCTION public.clinic_preview(uuid,uuid),
    public.clinic_publish(uuid,text,uuid,uuid),public.clinic_request_status(uuid,uuid,text,text),
    public.clinic_membership(uuid,uuid,text,boolean) FROM PUBLIC,anon,authenticated,clinic_runtime;
GRANT EXECUTE ON FUNCTION public.clinic_preview(uuid,uuid),
    public.clinic_publish(uuid,text,uuid,uuid),public.clinic_request_status(uuid,uuid,text,text),
    public.clinic_membership(uuid,uuid,text,boolean) TO authenticated;
NOTIFY pgrst,'reload schema';