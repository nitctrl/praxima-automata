-- Let clinic owners/managers rename their clinic (the receptionist's identity)
-- from the dashboard Settings tab, same draft-then-publish flow as other settings.

CREATE OR REPLACE FUNCTION public.clinic_settings(target uuid, settings jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
BEGIN
    IF NOT clinic_private.has_membership(target,ARRAY['owner','manager']) THEN
        RAISE EXCEPTION 'Settings forbidden' USING ERRCODE='42501';
    END IF;
    IF jsonb_typeof(settings)<>'object' OR settings - ARRAY['name','greeting','emergency_message',
        'fallback_message','default_language','supported_languages','slot_minutes'] <> '{}'::jsonb
        OR length(settings::text)>10000
        OR (settings ? 'slot_minutes' AND jsonb_typeof(settings->'slot_minutes') <> 'number')
        OR (settings ? 'name' AND length(settings->>'name') NOT BETWEEN 1 AND 200) THEN
        RAISE EXCEPTION 'Unsupported settings' USING ERRCODE='23514';
    END IF;
    UPDATE public.clinics SET name=coalesce(settings->>'name',name),
        greeting=coalesce(settings->>'greeting',greeting),
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
