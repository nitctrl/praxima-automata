"""Explicit, narrow activation and rollback for the user-approved fictional phone test.

Does not change Plivo, trunk settings, other clinics, or any published information.
Run provision, start clinic_sip worker, then switch. rollback restores the old worker.
"""

import argparse
import asyncio
from pathlib import Path
from uuid import uuid4

import psycopg
from dotenv import dotenv_values
from livekit import api

from praxima.dev.activation import development_settings, load_development, verify_history
from praxima.dev.sip_test import (
    CLINIC,
    LIVEKIT_URL,
    NUMBER,
    PROJECT,
    RULE,
    TRUNK,
    WORKER,
    preflight,
)
from praxima.modules.releases.domain.snapshot import Snapshot

ROOT = Path(__file__).resolve().parents[1]


async def routing(action: str) -> None:
    values = dotenv_values(ROOT / ".env")
    if values.get("LIVEKIT_URL") != LIVEKIT_URL:
        raise ValueError("Wrong LiveKit project")
    async with api.LiveKitAPI(
        url=LIVEKIT_URL, api_key=values.get("LIVEKIT_API_KEY"),
        api_secret=values.get("LIVEKIT_API_SECRET"),
    ) as client:
        trunks = await client.sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())
        selected = [t for t in trunks.items if t.sip_trunk_id == TRUNK]
        if len(selected) != 1 or list(selected[0].numbers) != [NUMBER]:
            raise ValueError("Expected single-number trunk required")
        # Prevent a custom SIP header from overriding the built-in routing fields.
        if any(value.startswith("sip.") for value in selected[0].headers_to_attributes.values()):
            raise ValueError("Reserved SIP attribute override")
        rules = await client.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())
        selected_rules = [r for r in rules.items if r.sip_dispatch_rule_id == RULE]
        if len(selected_rules) != 1:
            raise ValueError("Dispatch rule missing")
        rule = selected_rules[0]
        if (
            list(rule.trunk_ids) != [TRUNK]
            or rule.rule.dispatch_rule_individual.room_prefix != "call-"
            or any(key.startswith("sip.") for key in rule.attributes)
            or len(rule.room_config.agents) != 1
            or rule.room_config.agents[0].agent_name not in {"inbound-agent", WORKER}
        ):
            raise ValueError("Unexpected dispatch configuration; no automatic overwrite")
        for other in rules.items:
            competing = not other.trunk_ids or TRUNK in other.trunk_ids
            if other.sip_dispatch_rule_id != RULE and competing:
                raise ValueError("Potential competing dispatch rule; review manually")
        if action in {"switch", "rollback"}:
            target = WORKER if action == "switch" else "inbound-agent"
            if rule.room_config.agents[0].agent_name != target:
                # Preserve every other field; never delete/recreate the rule or trunk.
                rule.room_config.agents[0].agent_name = target
                await client.sip.update_dispatch_rule(RULE, rule)
            verified = await client.sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())
            actual = next(r for r in verified.items if r.sip_dispatch_rule_id == RULE)
            if [a.agent_name for a in actual.room_config.agents] != [target]:
                raise ValueError("Dispatch verification failed")
            print(f"Verified dispatch worker: {target}")
        else:
            name = rule.room_config.agents[0].agent_name
            print(f"Verified single-number trunk; current worker: {name}")


def provision(enable: bool = True) -> None:
    settings = development_settings(ROOT, PROJECT)
    clinic, _ = load_development(ROOT, PROJECT)
    if clinic != CLINIC:
        raise ValueError("Unexpected fixture")
    with psycopg.connect(settings.dsn, connect_timeout=10) as conn:
        conn.execute("SET LOCAL statement_timeout='15s'")
        conn.execute("SET LOCAL lock_timeout='3s'")
        conn.execute("SELECT pg_advisory_xact_lock(1709202601)")
        verify_history(conn, ROOT)
        row = conn.execute(
            "SELECT c.status,v.snapshot FROM public.clinics c "
            "JOIN public.configuration_versions v ON v.id=c.active_configuration_version_id "
            "AND v.clinic_id=c.id AND v.status='published' WHERE c.id=%s FOR UPDATE OF c",
            (CLINIC,),
        ).fetchone()
        if not row or row[0] != "active" or Snapshot.model_validate(row[1]).clinic_id != CLINIC:
            raise ValueError("Published active fictional clinic required")
        routes = conn.execute(
            "SELECT id,clinic_id,provider,trusted_trunk_id FROM public.phone_numbers "
            "WHERE e164_number=%s FOR UPDATE", (NUMBER,),
        ).fetchall()
        if any(r[1:] != (CLINIC, "plivo", TRUNK) for r in routes) or len(routes) > 1:
            raise ValueError("Existing phone ownership differs; refusing takeover")
        if not routes:
            if not enable:
                return
            created = conn.execute(
                "INSERT INTO public.phone_numbers(clinic_id,provider,e164_number,"
                "provider_reference,trusted_trunk_id,status,inbound_enabled) "
                "VALUES(%s,'plivo',%s,'controlled-fictional-sip-test',%s,'active',true) "
                "RETURNING id",
                (CLINIC, NUMBER, TRUNK),
            ).fetchone()
            if created is None:
                raise ValueError("Phone provisioning failed")
            phone = created[0]
        else:
            phone = routes[0][0]
            conn.execute(
                "UPDATE public.phone_numbers SET status=%s,inbound_enabled=%s WHERE id=%s",
                ("active" if enable else "inactive", enable, phone),
            )
        conn.execute(
            "INSERT INTO public.audit_logs"
            "(clinic_id,action,resource_type,resource_id,correlation_id) "
            "VALUES(%s,%s,'phone_numbers',%s,%s)",
            (CLINIC, "sip_test_enabled" if enable else "sip_test_disabled", phone, uuid4()),
        )
    print("Controlled fictional phone route enabled." if enable else "Clinic phone route disabled.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inspect", "provision", "switch", "rollback"])
    parser.add_argument("--confirm-number", required=True, choices=[NUMBER])
    parser.add_argument("--confirm-development-project", required=True, choices=[PROJECT])
    args = parser.parse_args()
    try:
        if args.action == "provision":
            asyncio.run(routing("inspect"))
            provision()
        elif args.action == "switch":
            asyncio.run(preflight(ROOT))
            asyncio.run(routing("switch"))
        elif args.action == "rollback":
            asyncio.run(routing("rollback"))
            provision(False)
        else:
            asyncio.run(routing("inspect"))
    except Exception as exc:
        name = type(exc).__name__
        raise SystemExit(f"SIP operation refused ({name}); details suppressed.") from None


if __name__ == "__main__":
    main()