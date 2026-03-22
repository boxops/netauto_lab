import csv
import hashlib
import json


def load_csv(fp, key=None, dialect=None):
    if dialect is None and fp.seekable():
        # Peek at first 1MB to sniff the delimiter and other dialect details
        peek = fp.read(1024**2)
        fp.seek(0)
        try:
            dialect = csv.Sniffer().sniff(peek, delimiters=",\t;")
        except csv.Error:
            # Oh well, we tried. Fallback to the default.
            pass
    fp = csv.reader(fp, dialect=(dialect or "excel"))
    headings = next(fp)
    rows = [dict(zip(headings, line)) for line in fp]
    if key:
        keyfn = lambda r: r[key]
    else:
        keyfn = lambda r: hashlib.sha1(
            json.dumps(r, sort_keys=True).encode("utf8")
        ).hexdigest()
    return {keyfn(r): r for r in rows}


def compare_csv_files(previous, current, show_unchanged=False):
    result = {
        "added": [],
        "removed": [],
        "changed": [],
        "columns_added": [],
        "columns_removed": [],
    }
    # Have the columns changed?
    previous_columns = set(next(iter(previous.values())).keys())
    current_columns = set(next(iter(current.values())).keys())
    ignore_columns = None
    if previous_columns != current_columns:
        result["columns_added"] = [
            c for c in current_columns if c not in previous_columns
        ]
        result["columns_removed"] = [
            c for c in previous_columns if c not in current_columns
        ]
        ignore_columns = current_columns.symmetric_difference(previous_columns)
    # Have any rows been removed or added?
    removed = [id for id in previous if id not in current]
    added = [id for id in current if id not in previous]
    # How about changed?
    removed_or_added = set(removed) | set(added)
    potential_changes = [id for id in current if id not in removed_or_added]
    changed = [id for id in potential_changes if current[id] != previous[id]]
    if added:
        result["added"] = [current[id] for id in added]
    if removed:
        result["removed"] = [previous[id] for id in removed]
    if changed:
        for id in changed:
            diffs = list(diff(previous[id], current[id], ignore=ignore_columns))
            if diffs:
                changes = {
                    "key": id,
                    "changes": {
                        # field can be a list if id contained '.' - #7
                        field[0] if isinstance(field, list) else field: [
                            prev_value,
                            current_value,
                        ]
                        for _, field, (prev_value, current_value) in diffs
                    },
                }
                if show_unchanged:
                    changes["unchanged"] = {
                        field: value
                        for field, value in previous[id].items()
                        if field not in changes["changes"] and field != "id"
                    }
                result["changed"].append(changes)
    return result
