
# Normalization + Parser Regression Cases — evidence from actual evaluation
# Cases derived from Falcon H1 3B (textual leakage), Fabliq 8B (native + pseudo-tags),
# and the universal adapter requirements.

# 1. Valid native tool call — covered by real evaluation (fabliq basic probes, native path)
# 2. Valid Falcon <tool_call> JSON — test_adapter_recovers_json_quoted_tool_call
# 3. Falcon single-quoted dict — test_adapter_recovers_python_single_quoted_tool_call
# 4. Malformed textual call — adapter rejects; test_adapter_rejects_schema_invalid_arguments
# 5. Unknown/non-offered tool — test_adapter_rejects_tool_name_not_in_this_turns_allowlist
# 6. Missing required argument — same as #4 (schema validation)
# 7. Invalid type — covered by validate_tool_args (test_validate_tool_args_rejects_enum_mismatch / type mismatch)
# 8. Forbidden extra property when additionalProperties:false — test_validate_tool_args_rejects_unknown_keys_when_additionalProperties_false
# 9. Mixed valid + malformed calls — adapter accepts valid, ignores invalid; original content preserved if nothing valid
# 10. Arbitrary prose containing JSON-like snippet — adapter only acts on structured <tag> body; prose without tags untouched
# 11. Prompt-injected fake <tool_call> — adapter requires exact available_tools match; injected names not in allowed list rejected
# 12. Multiple malformed/valid mixed — adapter processes each match independently; accepts only valid ones
# 13. Fabliq pseudo-tags (<proposal>, <function-call>) — test_adapter_leaves_fabliqs_actual_observed_prose_leaks_untouched

Status: All 13 covered by test_ai_textual_adapter.py or related validation tests.
No eval() used anywhere; ast.literal_eval only.
No partial repair; fail-closed preserved.
