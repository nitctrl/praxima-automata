-- Platform administrators are explicitly provisioned by a privileged operator;
-- neither JWT user_metadata nor clinic membership grants platform authority.
CREATE TABLE clinic_private.platform_admins (
    auth_user_id uuid PRIMARY KEY REFERENCES auth.users ON DELETE RESTRICT,
    active boolean NOT NULL DEFAULT true
);
REVOKE ALL ON clinic_private.platform_admins FROM PUBLIC,anon,authenticated,clinic_runtime;

CREATE FUNCTION public.clinic_platform_overview() RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    IF NOT EXISTS(SELECT 1 FROM clinic_private.platform_admins
        WHERE auth_user_id=auth.uid() AND active) THEN
        RAISE EXCEPTION 'Platform access forbidden' USING ERRCODE='42501';
    END IF;
    RETURN jsonb_build_object('clinics',(SELECT coalesce(jsonb_agg(x),'[]') FROM (
        SELECT c.id,c.name,c.status,c.active_configuration_version_id,c.monthly_minute_limit,
            (SELECT coalesce(sum(s.duration_seconds),0) FROM public.call_sessions s
                WHERE s.clinic_id=c.id AND NOT s.is_test AND s.started_at>=date_trunc('month',now()))
                AS month_seconds,
            (SELECT count(*) FROM public.call_sessions s WHERE s.clinic_id=c.id
                AND NOT s.is_test AND s.failure_code IS NOT NULL
                AND s.started_at>now()-interval '7 days') AS recent_errors
        FROM public.clinics c ORDER BY c.name LIMIT 200) x),
        'phones',(SELECT coalesce(jsonb_agg(x),'[]') FROM (
            SELECT id,clinic_id,provider,e164_number,status FROM public.phone_numbers
            ORDER BY created_at DESC LIMIT 200) x));
END;
$$;

CREATE FUNCTION public.clinic_settings(target uuid, settings jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    IF NOT clinic_private.has_membership(target,ARRAY['owner','manager']) THEN
        RAISE EXCEPTION 'Settings forbidden' USING ERRCODE='42501';
    END IF;
    IF jsonb_typeof(settings)<>'object' OR settings - ARRAY['greeting','emergency_message',
        'fallback_message','default_language','supported_languages'] <> '{}'::jsonb
        OR length(settings::text)>10000 THEN
        RAISE EXCEPTION 'Unsupported settings' USING ERRCODE='23514';
    END IF;
    UPDATE public.clinics SET greeting=coalesce(settings->>'greeting',greeting),
        emergency_message=coalesce(settings->>'emergency_message',emergency_message),
        fallback_message=coalesce(settings->>'fallback_message',fallback_message),
        default_language=coalesce(settings->>'default_language',default_language),
        supported_languages=CASE WHEN settings ? 'supported_languages' THEN
            ARRAY(SELECT jsonb_array_elements_text(settings->'supported_languages'))
            ELSE supported_languages END WHERE id=target;
    INSERT INTO public.audit_logs(clinic_id,actor_id,action,resource_type,resource_id,correlation_id)
        VALUES(target,auth.uid(),'settings_draft_updated','clinics',target,gen_random_uuid());
END;
$$;

CREATE FUNCTION public.clinic_request_detail(target uuid, request_id uuid, request_kind text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE result jsonb;
BEGIN
    IF NOT clinic_private.has_membership(target,ARRAY['owner','manager','receptionist']) THEN
        RAISE EXCEPTION 'Sensitive access forbidden' USING ERRCODE='42501';
    END IF;
    IF request_kind='appointment' THEN
        SELECT jsonb_build_object('name',encode(patient_name_ciphertext,'base64'),
            'phone',encode(callback_number_ciphertext,'base64'),'version',pii_key_version,
            'erased',pii_erased_at IS NOT NULL) INTO result FROM public.appointment_requests
            WHERE clinic_id=target AND id=request_id;
    ELSIF request_kind='callback' THEN
        SELECT jsonb_build_object('name',encode(name_ciphertext,'base64'),
            'phone',encode(callback_number_ciphertext,'base64'),'version',pii_key_version,
            'erased',pii_erased_at IS NOT NULL) INTO result FROM public.callback_requests
            WHERE clinic_id=target AND id=request_id;
    END IF;
    IF result IS NULL THEN RAISE EXCEPTION 'Request unavailable' USING ERRCODE='42501'; END IF;
    INSERT INTO public.audit_logs(clinic_id,actor_id,action,resource_type,resource_id,correlation_id)
        VALUES(target,auth.uid(),'sensitive_request_access',request_kind,request_id,gen_random_uuid());
    RETURN result;
END;
$$;

-- Ciphertext is not a substitute for access controls. Only the audited RPC above
-- may retrieve request PII; browser JWTs cannot read protected columns directly.
REVOKE SELECT ON public.appointment_requests,public.callback_requests,public.call_sessions,
    public.caller_profiles FROM authenticated;
GRANT SELECT(id,clinic_id,call_session_id,is_new_patient,doctor_id,service_id,preferred_date,
    preferred_time_start,preferred_time_end,status,created_at,updated_at)
    ON public.appointment_requests TO authenticated;
GRANT SELECT(id,clinic_id,call_session_id,requested_time,reason_category,status,created_at,updated_at)
    ON public.callback_requests TO authenticated;
GRANT SELECT(id,clinic_id,configuration_version_id,started_at,answered_at,ended_at,duration_seconds,
    detected_languages,primary_intent,disposition,transfer_attempted,transfer_succeeded,safety_flag,
    short_administrative_summary,failure_code,estimated_cost,is_test,retention_until)
    ON public.call_sessions TO authenticated;

REVOKE ALL ON FUNCTION public.clinic_platform_overview(),public.clinic_settings(uuid,jsonb),
    public.clinic_request_detail(uuid,uuid,text) FROM PUBLIC,anon,authenticated,clinic_runtime;
GRANT EXECUTE ON FUNCTION public.clinic_platform_overview(),public.clinic_settings(uuid,jsonb),
    public.clinic_request_detail(uuid,uuid,text) TO authenticated;
NOTIFY pgrst,'reload schema';