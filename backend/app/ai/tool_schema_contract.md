
# Tool Schema Abstraction Contract

## Canonical internal definition
Source: backend/app/copilot.py — TOOL_SPECS (READ + WRITE). Each spec has:
- name
- description
- input_schema (JSON Schema: type, properties, required, additionalProperties, enum)

This is the authority. No simplification may widen the accepted argument set.

## Provider rendering
- render_openai() in tool_schema.py produces {"type":"function","function":{"name",...,"parameters":input_schema}}
- render_anthropic() produces Anthropic format with same schema.

Both derive from the same _filtered_specs(role) list.

## Simplified presentation (future / optional)
If a weak local model needs a reduced schema (e.g., fewer optional fields shown,
simplified descriptions, reduced nested objects), the simplification must:
1. Only affect the PRESENTED wire format (render output)
2. Never change server-side validation (validate_tool_args uses full canonical schema)
3. Never add new properties or relax additionalProperties
4. Never remove required fields from the canonical validation
5. Be explicit (e.g., profile flag simplified_schema=True) rather than automatic

Tests required before any simplification is used:
- argument accuracy unchanged after simplification
- RBAC unchanged
- server-side validation rejects anything outside canonical schema
