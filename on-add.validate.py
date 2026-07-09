#!/usr/bin/env python3

# TaskWarrior hook that validates task hygiene on add/modify, per the
# taxonomy in nt/p/wiki/plan/53732d46-task-categorization-system.md and
# nt/p/wiki/plan/58c5884d-task-categorization-extended-tags.md.
#
# Input: on-add gets 1 JSON line (new task); on-modify gets 2 JSON lines
# (original, modified). Both lines are used to diff annotations and detect
# status transitions.
#
# Skip: tag "todo" skips everything. On-add with non-pending status skips
# everything (`task log`). On-modify with non-pending status runs only the
# closing checks (see below).
#
# HARD checks — reject (exit 1, single "validate: ..." line):
#   For pending tasks (add, or modify ending pending):
#     1. priority present, one of L/M/H
#     2. (due AND scheduled) present, OR wait present
#     3. exactly one duty tag of {feat, chore, fix, etc}
#     4. at most one person tag of {self, frnds, rltvs, fllw}
#     5. project present and matching ^(p|j|w)(\.[a-z0-9]+)*$
#     6. NEW annotations (diff vs original; all of them on add) must start
#        with one of cm:/blc:/bl:/cl:/wt:
#   For modify ending completed/deleted (only these two checks run):
#     7. the new-annotation prefix check above
#     8. if transitioning pending -> completed/deleted: at least one
#        annotation starting "cl:" must be present
#        (flow: `task <uuid> annotate 'cl: reason'` then done/delete)
#
# SOFT checks — task passes (exit 0) but a "validate-warn: ..." feedback
# line follows the task JSON (pending tasks only):
#   a. estimate missing/zero (default PT0H0M counts as missing) or not a
#      multiple of 15 minutes (wiki: 0.25 hour precision)
#   b. effort missing or not one of L/M/H
#   c. no activity tag ({adm,plan,...,ing}; lrn.* subtags count as lrn)
#   d. +_blc without a "blc:" annotation; +_bl without a "bl:" annotation
#   e. +srv combined with a w / w.* project (wiki: not applied with w:*)
#   f. tags outside the known vocabulary (scope list grows organically)
#   g. missing sprint UDA (default covers adds; legacy may lack it)
#
# Never mutates the task: on pass, echoes the (modified) task JSON unchanged.

import json
import re
import sys

DUTY_TAGS = {"feat", "chore", "fix", "etc"}
PERSON_TAGS = {"self", "frnds", "rltvs", "fllw"}
ACTIVITY_TAGS = {
    "adm", "plan", "mntn", "hlth", "fin", "pcare", "srv", "lrn",
    "blog", "go", "meet", "inv", "doc", "ing",
}
SCOPE_TAGS = {"cicd", "dev", "stag", "prod", "vpn", "sso", "docs"}
STATE_TAGS = {"ph"}
META_TAGS = {"_blc", "_bl"}
ESCAPE_TAGS = {"todo"}
KNOWN_TAGS = (
    DUTY_TAGS | PERSON_TAGS | ACTIVITY_TAGS | SCOPE_TAGS
    | STATE_TAGS | META_TAGS | ESCAPE_TAGS
)
LEVELS = {"L", "M", "H"}
PROJECT_RE = re.compile(r"^(p|j|w)(\.[a-z0-9]+)*$")
QUARTER_HOUR = 15 * 60
ANNOTATION_PREFIXES = ("cm:", "blc:", "bl:", "cl:", "wt:")

_DURATION_RE = re.compile(
    r"^P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)W)?(?:(\d+)D)?"
    r"(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?$"
)


def duration_seconds(value) -> float:
    """
    Parses an estimate value into seconds, defensively.

    Accepts ISO-8601 durations (PT0H0M, PT15M, P1DT2H, ...), integer/float
    seconds, or digit strings. Returns 0.0 for anything unparseable.
    """
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return 0.0
    value = value.strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return float(value)
    match = _DURATION_RE.fullmatch(value.upper())
    if not match:
        return 0.0
    years, months, weeks, days, hours, minutes, seconds = (
        float(g) if g else 0.0 for g in match.groups()
    )
    return (
        years * 365 * 86400
        + months * 30 * 86400
        + weeks * 7 * 86400
        + days * 86400
        + hours * 3600
        + minutes * 60
        + seconds
    )


def is_activity_tag(tag: str) -> bool:
    return tag in ACTIVITY_TAGS or tag.startswith("lrn.")


def annotation_set(task: dict) -> set:
    return {
        (a.get("entry"), a.get("description", ""))
        for a in (task.get("annotations") or [])
        if isinstance(a, dict)
    }


def emit_and_exit(task: dict, warnings: list):
    print(json.dumps(task, ensure_ascii = False))
    if warnings:
        print("validate-warn: " + "; ".join(warnings))
    sys.exit(0)


input_lines = sys.stdin.readlines()
task_data = json.loads(input_lines[-1])
original_data = json.loads(input_lines[0]) if len(input_lines) > 1 else None
is_modify = original_data is not None

tags = set(task_data.get("tags") or [])
status = task_data.get("status", "pending")

if "todo" in tags:
    emit_and_exit(task_data, [])

# New annotations: diff against original on modify, everything on add.
if is_modify:
    new_annotations = annotation_set(task_data) - annotation_set(original_data)
else:
    new_annotations = annotation_set(task_data)

annotation_violations = []
for _, text in sorted(new_annotations, key = lambda pair: (str(pair[0]), pair[1])):
    if not text.startswith(ANNOTATION_PREFIXES):
        shown = text if len(text) <= 40 else text[:37] + "..."
        annotation_violations.append(
            "annotation '{}' lacks prefix (cm:/blc:/bl:/cl:/wt:)".format(shown)
        )

if status != "pending":
    if not is_modify:
        # `task log` etc. — no way to annotate at creation time; skip all.
        emit_and_exit(task_data, [])
    if status not in ("completed", "deleted"):
        # recurring templates and other exotic statuses — skip.
        emit_and_exit(task_data, [])
    # Closing path: only annotation-prefix and cl: checks.
    violations = list(annotation_violations)
    original_status = original_data.get("status", "pending")
    if original_status == "pending":
        all_annotation_texts = [text for _, text in annotation_set(task_data)]
        if not any(t.startswith("cl:") for t in all_annotation_texts):
            violations.append("done/delete requires cl: annotation")
    if violations:
        print("validate: " + "; ".join(violations))
        sys.exit(1)
    emit_and_exit(task_data, [])

# --- pending task: full validation ---

violations = []
warnings = []

if task_data.get("priority") not in LEVELS:
    violations.append("missing priority")

if not (("due" in task_data and "scheduled" in task_data) or "wait" in task_data):
    violations.append("need due+scheduled or wait")

duty_count = len(tags & DUTY_TAGS)
if duty_count != 1:
    violations.append(
        "need exactly one of +feat/+chore/+fix/+etc (found {})".format(duty_count)
    )

person_count = len(tags & PERSON_TAGS)
if person_count > 1:
    violations.append(
        "at most one of +self/+frnds/+rltvs/+fllw (found {})".format(person_count)
    )

project = task_data.get("project")
if not project:
    violations.append("missing project")
elif not PROJECT_RE.match(project):
    violations.append("invalid project '{}' (want p / j.* / w.*)".format(project))

violations.extend(annotation_violations)

if violations:
    print("validate: " + "; ".join(violations))
    sys.exit(1)

# --- soft checks: warn but do not block ---

estimate_seconds = duration_seconds(task_data.get("estimate"))
if estimate_seconds <= 0:
    warnings.append("missing estimate")
elif estimate_seconds % QUARTER_HOUR != 0:
    warnings.append("estimate not a multiple of 15min")

if task_data.get("effort") not in LEVELS:
    warnings.append("missing effort")

if not any(is_activity_tag(tag) for tag in tags):
    warnings.append("no activity tag")

annotation_texts = [text for _, text in annotation_set(task_data)]
for meta_tag, prefix in (("_blc", "blc:"), ("_bl", "bl:")):
    if meta_tag in tags and not any(t.startswith(prefix) for t in annotation_texts):
        warnings.append("+{} without {} annotation".format(meta_tag, prefix))

if "srv" in tags and project is not None and (project == "w" or project.startswith("w.")):
    warnings.append("+srv not applicable with w.* project")

unknown_tags = sorted(tag for tag in tags if tag not in KNOWN_TAGS and not tag.startswith("lrn."))
if unknown_tags:
    warnings.append("unknown tag(s): {}".format(", ".join(unknown_tags)))

if task_data.get("sprint") in (None, ""):
    warnings.append("missing sprint")

emit_and_exit(task_data, warnings)
