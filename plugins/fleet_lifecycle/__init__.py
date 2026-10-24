"""Opt-in operator CLI. Discovery registers only; no hooks or model tools."""


def register(ctx):
    from plugins.fleet_lifecycle.controller import setup_cli, command
    ctx.register_cli_command(
        name="fleet-lifecycle", help="Trusted, gated fleet source verification",
        setup_fn=setup_cli, handler_fn=command,
        description="Read-only check, one-plan admission, or one durable lifecycle tick.",
    )
