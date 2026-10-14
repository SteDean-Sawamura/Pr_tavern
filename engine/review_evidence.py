import copy
import json


REVIEW_STATE_FIELDS = (
    "player", "inventory", "npcs", "script_variables", "world_properties",
    "active_persistent_states", "visible_locations",
)


def execution_snapshot(before, candidate, *, start_time, end_time, time_source, definitions):
    values = []

    def collect(path, original, projected, original_exists=True, projected_exists=True):
        include_unchanged = path[0] in {"script_variables", "world_properties"}
        if original_exists == projected_exists and original == projected and not include_unchanged:
            return
        if isinstance(original, dict) and isinstance(projected, dict):
            for key in sorted(original.keys() | projected.keys()):
                collect([*path, key], original.get(key), projected.get(key), key in original, key in projected)
            return
        entry = {"path": path, "before_exists": original_exists, "before": original,
                 "candidate_exists": projected_exists, "candidate": projected,
                 "changed": original_exists != projected_exists or original != projected}
        if path[0] == "script_variables" and len(path) == 2 and path[1] in definitions:
            entry["definition"] = definitions[path[1]]
        values.append(entry)

    for field in REVIEW_STATE_FIELDS:
        collect([field], before.get(field, {}), candidate.get(field, {}), field in before, field in candidate)
    values.sort(key=lambda entry: (not entry["changed"], entry["path"]))
    return copy.deepcopy({
        "scope": "uncommitted_state_tool_projection",
        "excludes": ["scheduled_event_effects", "turn_lifecycle_effects"],
        "time": {"before": start_time, "candidate": end_time, "source": time_source},
        "values": values,
    })


def format_execution_snapshot(snapshot, max_characters=12000):
    result = {key: value for key, value in snapshot.items() if key not in {"values", "state_updates"}}
    result.update(values=[], omitted_values=0)
    if len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) > max_characters - 32:
        return json.dumps({"scope": "uncommitted_state_tool_projection", "unavailable": True,
                           "values": [], "omitted_values": len(snapshot["values"]) + bool(snapshot.get("state_updates"))},
                          separators=(",", ":"))
    values = list(snapshot["values"])
    if snapshot.get("state_updates"):
        values.append({"path": ["state_tool_updates"], "candidate": snapshot["state_updates"],
                       "changed": True, "projection": "tool_arguments_not_additional_effects"})
    values.sort(key=lambda entry: (not entry["changed"], entry["path"]))
    for entry in values:
        result["values"].append(entry)
        if len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) > max_characters - 32:
            result["values"].pop()
            result["omitted_values"] += 1
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
