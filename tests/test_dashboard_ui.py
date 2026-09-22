from pathlib import Path


def test_dashboard_exposes_only_the_simple_workflows():
    script = (Path(__file__).parents[1] / "src/clinic/web/app.js").read_text()
    navigation = script.split("const categories=", 1)[0]
    for page in ["Home", "Knowledge", "Live Updates", "Agent Test", "Requests", "Calls"]:
        assert page in navigation
    for legacy in [
        "doctors:", "services:", "doctor_services:", "weekly_schedules:",
        "special_date_schedules:", "schedule_exceptions:", "locations:",
    ]:
        assert legacy not in navigation
