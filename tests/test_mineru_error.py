from silica.sources.convert import _mineru_error


def test_extracts_error_field_from_json_blob():
    # The real failure: mineru wrote a JSON task blob; the old [-300:] slice
    # started mid-token ("2:25:16…") and buried the message.
    blob = (
        '{"started_at": "2026-07-21T22:25:16.251918+00:00", '
        '"error": "No module named \'six\'", "queued_ahead": 0}'
    )
    assert _mineru_error(blob) == "No module named 'six'"


def test_raw_text_head_truncated_not_tail():
    assert _mineru_error("boom: " + "x" * 500) == ("boom: " + "x" * 294)


def test_plain_short_message_stripped():
    assert _mineru_error("  segfault  ") == "segfault"


def test_json_without_error_field_falls_back_to_head():
    assert _mineru_error('{"status": "queued"}') == '{"status": "queued"}'
