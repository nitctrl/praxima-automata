-- Fixed-vocabulary administrative outcomes: never accept a transcript or free-text summary.
CREATE FUNCTION clinic_private.note_call(session_id uuid, topic text, outcome text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path='' AS $$
DECLARE tenant uuid := clinic_private.runtime_clinic(); s public.call_sessions;
    label text; description text;
BEGIN
    IF topic IS NULL OR topic NOT IN ('connected','availability','doctors','hours','fees',
        'location','faq','clarify','voice') OR outcome IS NULL OR outcome NOT IN
        ('success','ambiguous','not_found','unavailable','forbidden','failed') THEN
        RAISE EXCEPTION 'Invalid administrative outcome' USING ERRCODE='23514';
    END IF;
    SELECT * INTO s FROM public.call_sessions WHERE clinic_id=tenant AND id=session_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Session unavailable' USING ERRCODE='42501'; END IF;
    IF s.ended_at IS NOT NULL THEN RETURN; END IF;
    label := CASE topic WHEN 'connected' THEN 'Call connected'
        WHEN 'availability' THEN 'Doctor availability lookup'
        WHEN 'doctors' THEN 'Doctor directory lookup' WHEN 'hours' THEN 'Clinic hours lookup'
        WHEN 'fees' THEN 'Published fee lookup' WHEN 'location' THEN 'Location lookup'
        WHEN 'faq' THEN 'Approved FAQ lookup' WHEN 'clarify' THEN 'Clarification'
        ELSE 'Voice service' END;
    description := CASE outcome WHEN 'success' THEN 'completed'
        WHEN 'ambiguous' THEN 'clarification required' WHEN 'not_found' THEN 'not found'
        WHEN 'unavailable' THEN 'information unavailable' WHEN 'forbidden' THEN 'safety routed'
        ELSE 'failed' END;
    UPDATE public.call_sessions SET
        short_administrative_summary=CASE WHEN disposition='request_collected'
            THEN short_administrative_summary ELSE label || ': ' || description || '. '
            || 'No appointment confirmed by the agent.' END,
        failure_code=CASE WHEN outcome='failed' THEN 'voice_failed' ELSE failure_code END,
        disposition=CASE WHEN outcome='failed' AND disposition='started' THEN 'failed'
            ELSE disposition END
        WHERE id=session_id;
    INSERT INTO public.call_events(clinic_id,call_session_id,event_key,event_type)
        VALUES(tenant,session_id,gen_random_uuid(),'admin_' || topic || '_' || outcome);
END;
$$;
REVOKE ALL ON FUNCTION clinic_private.note_call(uuid,text,text) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION clinic_private.note_call(uuid,text,text) TO clinic_runtime;