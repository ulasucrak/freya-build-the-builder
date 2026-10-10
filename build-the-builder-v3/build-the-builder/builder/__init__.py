"""Generic builder: brief file(s) + tool API file -> Freya phone-agent workflow.

Modules:
  llm      thin Claude wrapper (Agent SDK, fallback `claude -p`) with a call-trace hook
  extract  brief(s) + openapi -> policy.json (contract v1)
"""
