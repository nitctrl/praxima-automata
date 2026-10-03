"""The voice agent: everything a call runs. The platform backend never imports this.

It meets the backend only through `praxima.contracts` (the release snapshot) and the
database's SECURITY DEFINER runtime functions. Release path: worker, release, tools,
prompts, speech. `legacy/` and `dev/` are the old single-clinic path, kept until removed.
"""
