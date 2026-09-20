from app.ai.textual_toolcall_adapter import adapt_textual_tool_calls
from app.ai.tool_validation import validate_tool_args

GET_PROJECTS = {"type": "function", "function": {
    "name": "get_projects", "description": "List projects.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}}
CREATE_TASK = {"type": "function", "function": {
    "name": "create_task", "description": "Propose creating a task.",
    "parameters": {"type": "object", "properties": {
        "project_id": {"type": "string"}, "title": {"type": "string"},
        "priority": {"type": "string", "enum": ["low", "medium", "high"]},
    }, "required": ["project_id", "title"], "additionalProperties": False},
}}
AVAILABLE = [GET_PROJECTS, CREATE_TASK]


def test_adapter_recovers_json_quoted_tool_call():
    content = 'Sure, let me check.\n<tool_call>{"name": "get_projects", "arguments": {}}</tool_call>'
    calls, cleaned = adapt_textual_tool_calls(content, AVAILABLE)
    assert len(calls) == 1
    assert calls[0]["name"] == "get_projects"
    assert calls[0]["args"] == {}
    assert "<tool_call>" not in (cleaned or "")


def test_adapter_recovers_python_single_quoted_tool_call():
    # Regression: Falcon's observed leak format uses Python-dict single quotes,
    # which is invalid JSON but a valid Python literal.
    content = "<tool_call>\n{'name': 'create_task', 'arguments': {'project_id': 'proj-1', 'title': 'Ship it'}}\n</tool_call>"
    calls, cleaned = adapt_textual_tool_calls(content, AVAILABLE)
    assert len(calls) == 1
    assert calls[0]["name"] == "create_task"
    assert calls[0]["args"] == {"project_id": "proj-1", "title": "Ship it"}
    assert cleaned == "" or cleaned is None


def test_adapter_recovers_a_well_formed_call_under_fabliqs_proposal_tag():
    # fabliq's own observed leaks (see below) never happened to contain a
    # parseable call, but the tag family must still be recognized structurally
    # the same way <tool_call> is, for any model that leaks a real call this way.
    content = '<proposal>{"name": "get_projects", "arguments": {}}</proposal>'
    calls, cleaned = adapt_textual_tool_calls(content, AVAILABLE)
    assert len(calls) == 1
    assert calls[0]["name"] == "get_projects"


def test_adapter_recovers_a_well_formed_call_under_fabliqs_function_call_tag():
    content = '<function-call>{"name": "get_projects", "arguments": {}}</function-call>'
    calls, cleaned = adapt_textual_tool_calls(content, AVAILABLE)
    assert len(calls) == 1
    assert calls[0]["name"] == "get_projects"


def test_adapter_leaves_fabliqs_actual_observed_prose_leaks_untouched():
    # Regression, using the exact real strings from this session's fabliq battery
    # (s3_basic_overloaded_team / s3_halluc_empty_project): free prose inside a
    # recognized tag, not a {"name", "arguments"} object. Fail-closed means this
    # must NOT be force-fit into a fabricated call, and the original text must
    # survive unchanged -- not silently stripped either.
    proposal_leak = (
        "This suggests that Product Launch EMEA may currently be overloaded.\n\n"
        "<proposal>\nPropose creating a task to investigate resource allocation for "
        'the "Product Launch EMEA" project and reassign tasks if needed.\n</proposal>'
    )
    calls, cleaned = adapt_textual_tool_calls(proposal_leak, AVAILABLE)
    assert calls == []
    assert cleaned == proposal_leak

    function_call_leak = (
        "there are **no tasks** currently associated with this project.\n\n"
        "<function-call>\nNo further tool calls needed — task list inferred from "
        "project description.\n</function-call>"
    )
    calls, cleaned = adapt_textual_tool_calls(function_call_leak, AVAILABLE)
    assert calls == []
    assert cleaned == function_call_leak


def test_adapter_rejects_tool_name_not_in_this_turns_allowlist():
    # create_team isn't in AVAILABLE (e.g. this turn's role-filtered list, as a
    # member who can't create teams) -- must not be recovered even though it
    # parses cleanly, mirroring the real RBAC allow-list this always derives from.
    content = '<tool_call>{"name": "create_team", "arguments": {"name": "Security"}}</tool_call>'
    calls, cleaned = adapt_textual_tool_calls(content, AVAILABLE)
    assert calls == []
    assert cleaned == content


def test_adapter_rejects_schema_invalid_arguments_and_leaves_content_untouched():
    # Missing the required "title" field.
    content = '<tool_call>{"name": "create_task", "arguments": {"project_id": "proj-1"}}</tool_call>'
    calls, cleaned = adapt_textual_tool_calls(content, AVAILABLE)
    assert calls == []
    assert cleaned == content


def test_adapter_returns_no_calls_for_ordinary_text_reply():
    content = "Product Launch is at risk and 60% complete."
    calls, cleaned = adapt_textual_tool_calls(content, AVAILABLE)
    assert calls == []
    assert cleaned == content


def test_validate_tool_args_rejects_unknown_keys_when_additionalProperties_false():
    schema = {"type": "object", "properties": {}, "additionalProperties": False}
    assert validate_tool_args({"unexpected": "value"}, schema) is None
    assert validate_tool_args({}, schema) == {}


def test_validate_tool_args_rejects_missing_required_keys():
    schema = {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}
    assert validate_tool_args({}, schema) is None
    assert validate_tool_args({"task_id": "t-1"}, schema) == {"task_id": "t-1"}


def test_validate_tool_args_rejects_enum_mismatch():
    schema = {"type": "object", "properties": {"priority": {"type": "string", "enum": ["low", "medium", "high"]}}}
    assert validate_tool_args({"priority": "urgent"}, schema) is None
    assert validate_tool_args({"priority": "high"}, schema) == {"priority": "high"}
