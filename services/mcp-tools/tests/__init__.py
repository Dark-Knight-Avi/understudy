"""Tests for the MCP tool server.

Nothing here starts Typst, ComfyUI, RAGFlow or SearXNG. Every backing service is
mocked at its own boundary -- `httpx.MockTransport` for the four HTTP ones, a
monkeypatched `subprocess.run` for Typst -- so the suite runs on a laptop with
none of the platform installed, which is the only way it gets run often.
"""
