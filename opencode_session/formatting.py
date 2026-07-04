import json
import sys


def compact_value(value):
    if value is None or value == "":
        return "-"
    text = str(value)
    if any(character.isspace() for character in text):
        return json.dumps(text)
    return text


def compact_bool(value):
    if value is True:
        return "true"
    if value is False:
        return "false"
    return value


def compact_list(values):
    if not values:
        return None
    return ",".join(str(value) for value in values)


def format_table(headers, rows):
    lines = ["\t".join(headers)]
    lines.extend("\t".join(compact_value(value) for value in row) for row in rows)
    return "\n".join(lines)


def write_raw(body):
    sys.stdout.write(body)
