import re


CONDITION_RULE_SECTIONS = ("triggers", "one_time_events", "cyclic_events")
_COMPARISONS = {"==", "!=", "<=", ">=", "<", ">"}
_BOUNDARIES = _COMPARISONS | {"(", ")", "AND", "OR", "&&", "||"}
_TOKEN = re.compile(r'''\s+|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|==|!=|<=|>=|&&|\|\||[()<>]|[^\s()<>!=&|"']+''')
_PATH = re.compile(r"\w+(?:\.\w+)*")


def _parse_condition(condition):
    if not isinstance(condition, str):
        raise ValueError("Condition must be a string")
    tokens = []
    offset = 0
    while offset < len(condition):
        match = _TOKEN.match(condition, offset)
        if match is None:
            raise ValueError(f"Unexpected character or unclosed quote at position {offset + 1}")
        if not match.group().isspace():
            tokens.append((match.group(), match.start(), match.end()))
        offset = match.end()
    if not tokens:
        return ("all", [])
    index = 0

    def current():
        return tokens[index][0] if index < len(tokens) else None

    def fail(message):
        position = tokens[index][1] if index < len(tokens) else len(condition)
        raise ValueError(f"{message} at position {position + 1}")

    def primary():
        nonlocal index
        if current() == "(":
            index += 1
            result = disjunction()
            if current() != ")":
                fail("Closing parenthesis expected")
            index += 1
            return result
        path = current()
        if path is None or path in _BOUNDARIES or not _PATH.fullmatch(path):
            fail("State path or grouped condition expected")
        index += 1
        if re.fullmatch(r"event\.\w+\.fired", path) and current() in {None, ")", "AND", "OR", "&&", "||"}:
            return ("leaf", f"{path} == true")
        operator = current()
        if operator not in _COMPARISONS:
            fail("Comparison operator expected")
        index += 1
        if current() is None or current() in _BOUNDARIES:
            fail("Comparison value expected")
        start = tokens[index][1]
        if current().startswith(('"', "'")):
            end = tokens[index][2]
            index += 1
        else:
            while current() is not None and current() not in _BOUNDARIES:
                if current().startswith(('"', "'")):
                    fail("Quoted values must stand alone")
                end = tokens[index][2]
                index += 1
        return ("leaf", f"{path} {operator} {condition[start:end]}")

    def conjunction():
        nonlocal index
        children = [primary()]
        while current() in {"AND", "&&"}:
            index += 1
            children.append(primary())
        return ("all", children) if len(children) > 1 else children[0]

    def disjunction():
        nonlocal index
        children = [conjunction()]
        while current() in {"OR", "||"}:
            index += 1
            children.append(conjunction())
        return ("any", children) if len(children) > 1 else children[0]

    result = disjunction()
    if index != len(tokens):
        fail("Unexpected token after condition")
    return result


def condition_error(condition):
    try:
        _parse_condition(condition)
    except ValueError as error:
        return str(error)
    except RecursionError:
        return "Condition nesting is too deep"
    return ""


def evaluate_condition(condition, compare):
    try:
        expression = _parse_condition(condition)
    except (ValueError, RecursionError):
        return False

    def evaluate(node):
        kind, content = node
        if kind == "leaf":
            return bool(compare(content))
        aggregate = all if kind == "all" else any
        return aggregate(evaluate(child) for child in content)

    return evaluate(expression)
