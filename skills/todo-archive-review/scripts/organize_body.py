#!/usr/bin/env python3
"""Validate connector property evidence and emit plain Organize fields from SQL."""
import json
import re
import sys


def canonical_body(fields, row, properties):
    if (
        not isinstance(fields, list)
        or len(fields) != 3
        or any(not isinstance(field, str) or not field.strip() for field in fields)
        or len(set(fields)) != 3
        or not isinstance(row, dict)
        or not isinstance(properties, dict)
    ):
        raise ValueError
    body = {}
    for index, field in enumerate(fields):
        value = row[field]
        rendered = properties[field]
        if value is None and index > 0:
            value = ""
        if not isinstance(value, str) or not isinstance(rendered, str):
            raise ValueError
        if value != rendered:
            # Accept the observed paragraph envelope or Markdown escaping only when the SQL text corroborates it.
            paragraph = re.fullmatch(r"<p>\n(.*?)\n</p>", rendered, flags=re.DOTALL)
            shown = paragraph[1] if paragraph else rendered
            if re.sub(r"\\([\\`*_{}\[\]()#+\-.!>])", r"\1", shown).strip() != value:
                raise ValueError
        body[field] = value.strip() if index else value
    return body


if __name__ == "__main__":
    try:
        payload = json.load(sys.stdin)
        body = canonical_body(payload["fields"], payload["row"], payload["properties"])
    except (ValueError, KeyError, TypeError):
        sys.exit("Organize body evidence is missing, malformed, or inconsistent.")
    print(json.dumps(body, ensure_ascii=False))
