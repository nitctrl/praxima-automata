-- Forward-only expansion for migration 007's fixed-vocabulary events.
-- Preserve the empty-payload requirement; no caller text is admitted.
ALTER TABLE public.call_events DROP CONSTRAINT call_events_event_type_check;
ALTER TABLE public.call_events ADD CONSTRAINT call_events_event_type_check CHECK (
    event_type IN ('started','ended','tool_failed','transfer_failed','safety_routed','timeout')
    OR event_type ~ '^admin_(connected|availability|doctors|hours|fees|location|faq|clarify|voice)_(success|ambiguous|not_found|unavailable|forbidden|failed)$'
);