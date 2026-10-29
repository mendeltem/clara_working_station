"""Parsing of Qwen/Hermes tool-call text and <think> blocks, including chunk boundaries."""
import json
import random

from clara.toolcalls import StreamRouter, extract_tool_calls, parse_tool_call_json, split_think

QWEN_OUTPUT = ('<think>\nI need to read the file first.\n</think>\n\nLet me look.\n<tool_call>\n'
               '{"name": "read_file", "arguments": {"path": "hello.txt", "limit": 20}}\n</tool_call>')


def test_parse_simple_call():
    tc = parse_tool_call_json('{"name": "bash", "arguments": {"command": "ls -la"}}')
    assert tc.name == "bash" and tc.arguments == {"command": "ls -la"}


def test_parse_arguments_given_as_string_and_trailing_comma():
    tc = parse_tool_call_json('{"name": "bash", "arguments": "{\\"command\\": \\"pwd\\",}"}')
    assert tc.arguments == {"command": "pwd"}


def test_parse_rejects_non_calls():
    assert parse_tool_call_json("just text") is None
    assert parse_tool_call_json('{"arguments": {}}') is None


def test_extract_calls_and_remaining_text():
    calls, rest = extract_tool_calls(QWEN_OUTPUT.split("</think>")[1])
    assert len(calls) == 1 and calls[0].name == "read_file"
    assert rest == "Let me look."


def test_extract_unclosed_call_when_model_stopped_early():
    calls, rest = extract_tool_calls('text\n<tool_call>\n{"name": "search", "arguments": {"pattern": "x"}}')
    assert calls and calls[0].name == "search" and rest == "text"


def test_split_think_handles_closed_and_unclosed():
    r, v = split_think("<think>a\nb</think>answer")
    assert r == "a\nb" and v == "answer"
    r, v = split_think("<think>cut off")
    assert r == "cut off" and v == ""


def _route(text: str, chunks):
    router = StreamRouter()
    events = []
    for ch in chunks:
        events.extend(router.push(ch))
    calls, visible, reasoning = router.finish()
    return events, calls, visible, reasoning


def test_router_same_result_for_any_chunking():
    random.seed(7)
    expected = None
    for trial in range(40):
        pos, chunks = 0, []
        while pos < len(QWEN_OUTPUT):
            n = random.randint(1, 9)
            chunks.append(QWEN_OUTPUT[pos:pos + n])
            pos += n
        events, calls, visible, reasoning = _route(QWEN_OUTPUT, chunks)
        result = (json.dumps([(c.name, c.arguments) for c in calls]), visible, reasoning,
                  "".join(t for k, t in events if k == "text").strip(),
                  "".join(t for k, t in events if k == "reasoning").strip())
        if expected is None:
            expected = result
        assert result == expected, f"chunking {chunks} changed the result"
    assert expected[0] == json.dumps([("read_file", {"path": "hello.txt", "limit": 20})])
    assert expected[1] == "Let me look."
    assert expected[2] == "I need to read the file first."
    assert expected[3] == "Let me look."               # tool-call text never reaches the screen
    assert expected[4] == "I need to read the file first."


def test_router_plain_text_with_angle_bracket():
    events, calls, visible, reasoning = _route("", ["x < y and ", "a <b> c"])
    assert "".join(t for k, t in events) == "x < y and a <b> c" and not calls


def test_router_two_calls_in_one_message():
    text = ('<tool_call>\n{"name": "read_file", "arguments": {"path": "a"}}\n</tool_call>\n'
            '<tool_call>\n{"name": "read_file", "arguments": {"path": "b"}}\n</tool_call>')
    events, calls, visible, reasoning = _route(text, [text])
    assert [c.arguments["path"] for c in calls] == ["a", "b"] and visible == ""
