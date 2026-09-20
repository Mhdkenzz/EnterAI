"""Provider-agnostic AI execution layer: response normalization, capability
profiles, tool-schema rendering, and the textual tool-call compatibility
adapter. This package knows how to talk to and normalize any LLM; it has no
knowledge of roles, WorkspaceTools, or confirmation tokens -- that boundary
stays in app/copilot.py, which is this package's only consumer.
"""
