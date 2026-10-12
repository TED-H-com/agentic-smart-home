"""Test-only model routing. Never packaged into a service image.

The real CLI, broker, Hermes runtime, AIAgent and MCP remain in use. Only their
cloud model endpoint is redirected to the disposable local protocol server.
"""
import asyncio
import os


def main():
    endpoint = os.environ["E2E_MODEL_URL"]
    assert endpoint.startswith("http://127.0.0.1:")
    if os.environ["E2E_ROLE"] == "runner":
        from agentic_runner.app import serve
        from agentic_runner.providers import Providers

        original = Providers.command

        def command(self, provider, schema, output, model):
            assert provider == "codex"
            return original(self, provider, schema, output, model)[:-1] + [
                "--disable", "enable_request_compression",
                "-c", 'model_provider="e2e"',
                "-c", 'model_providers.e2e.name="Local E2E fixture"',
                "-c", f'model_providers.e2e.base_url="{endpoint}/v1"',
                "-c", 'model_providers.e2e.wire_api="responses"',
                "-c", "model_providers.e2e.requires_openai_auth=false", "-",
            ]

        Providers.command = command
        asyncio.run(serve())
    else:
        from agentic_hermes.app import Runtime, main

        original = Runtime.child_environment

        def environment(self, scratch):
            return {**original(self, scratch), "AGENTIC_PROVIDER_URL": endpoint + "/v1"}

        Runtime.child_environment = environment
        main()


if __name__ == "__main__":
    main()
