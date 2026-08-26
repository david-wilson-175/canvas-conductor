"""Tests for the `student-groups` command group (CSV-driven course-level
student groups: categories, groups, memberships).

The interesting surface is `apply`: CSV parsing (identifier-column detection
and the Canvas prefix each one needs) plus find-or-create idempotency against
existing course state. The read commands and the two destructive ones get
smoke coverage.
"""
from __future__ import annotations

import json

from typer.testing import CliRunner

from canvas_conductor.cli import app

runner = CliRunner()


def _config(write_config):
    write_config(
        """
[courses.t]
id = 99
"""
    )


def _csv(tmp_path, text, name="roster.csv", encoding="utf-8"):
    path = tmp_path / name
    path.write_text(text, encoding=encoding)
    return str(path)


def _posts(mock_responses):
    return [c for c in mock_responses.calls if c.request.method == "POST"]


def _post_bodies(mock_responses):
    return [json.loads(c.request.body) for c in _posts(mock_responses)]


def _deletes(mock_responses):
    return [c for c in mock_responses.calls if c.request.method == "DELETE"]


def _membership_posts(mock_responses):
    return [c for c in _posts(mock_responses) if "/memberships" in c.request.url]


def _member_ids(mock_responses):
    return sorted(
        json.loads(c.request.body)["user_id"] for c in _membership_posts(mock_responses)
    )


def _stub_course(mock_responses, api, categories=None, groups=None, users=None):
    """Wire the standard find-or-create lookup chain.

    categories: list of category dicts returned for the course
    groups:     {category_id: [group dicts]}
    users:      {group_id: [user dicts]} already in each group
    """
    mock_responses.get(f"{api}/courses/99/group_categories", json=categories or [])
    for cat_id, gs in (groups or {}).items():
        mock_responses.get(f"{api}/group_categories/{cat_id}/groups", json=gs)
    for gid, us in (users or {}).items():
        mock_responses.get(f"{api}/groups/{gid}/users", json=us)


# --------------------------------------------------------------------------
# apply — CSV parsing and identifier detection
# --------------------------------------------------------------------------


def test_apply_dry_run_makes_no_requests(write_config, mock_responses, api, tmp_path):
    _config(write_config)
    path = _csv(tmp_path, "sis_user_id,group_name\n123,Team Alpha\n124,Team Alpha\n")

    result = runner.invoke(
        app,
        ["student-groups", "apply", "-f", path, "--category", "Project Teams", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert len(mock_responses.calls) == 0
    assert "Team Alpha (2 members)" in result.output
    assert "Project Teams" in result.output


def test_apply_prefixes_sis_user_id(write_config, mock_responses, api, tmp_path):
    _config(write_config)
    path = _csv(tmp_path, "sis_user_id,group_name\n123,Team Alpha\n")
    _stub_course(mock_responses, api, groups={7: []}, users={70: []})
    mock_responses.post(f"{api}/courses/99/group_categories", json={"id": 7, "name": "PT"})
    mock_responses.post(f"{api}/group_categories/7/groups", json={"id": 70, "name": "Team Alpha"})
    mock_responses.post(f"{api}/groups/70/memberships", json={"id": 900})

    result = runner.invoke(
        app,
        ["student-groups", "apply", "-f", path, "--category", "PT", "-y"],
    )
    assert result.exit_code == 0, result.output
    assert _member_ids(mock_responses) == ["sis_user_id:123"]


def test_apply_prefixes_email_as_sis_login_id(write_config, mock_responses, api, tmp_path):
    _config(write_config)
    path = _csv(tmp_path, "email,group_name\nstu@byu.edu,Pair 1\n")
    _stub_course(mock_responses, api, groups={7: []}, users={70: []})
    mock_responses.post(f"{api}/courses/99/group_categories", json={"id": 7, "name": "Lab"})
    mock_responses.post(f"{api}/group_categories/7/groups", json={"id": 70, "name": "Pair 1"})
    mock_responses.post(f"{api}/groups/70/memberships", json={"id": 900})

    result = runner.invoke(
        app, ["student-groups", "apply", "-f", path, "--category", "Lab", "-y"]
    )
    assert result.exit_code == 0, result.output
    assert _member_ids(mock_responses) == ["sis_login_id:stu@byu.edu"]


def test_apply_canvas_user_id_gets_no_prefix(write_config, mock_responses, api, tmp_path):
    _config(write_config)
    path = _csv(tmp_path, "user_id,group_name\n5150,Team Alpha\n")
    _stub_course(mock_responses, api, groups={7: []}, users={70: []})
    mock_responses.post(f"{api}/courses/99/group_categories", json={"id": 7, "name": "PT"})
    mock_responses.post(f"{api}/group_categories/7/groups", json={"id": 70, "name": "Team Alpha"})
    mock_responses.post(f"{api}/groups/70/memberships", json={"id": 900})

    result = runner.invoke(
        app, ["student-groups", "apply", "-f", path, "--category", "PT", "-y"]
    )
    assert result.exit_code == 0, result.output
    assert _member_ids(mock_responses) == ["5150"]


def test_apply_identifier_priority_prefers_user_id(write_config, mock_responses, api, tmp_path):
    """user_id outranks sis_user_id when a CSV carries both."""
    _config(write_config)
    path = _csv(tmp_path, "sis_user_id,user_id,group_name\n123,5150,Team Alpha\n")
    _stub_course(mock_responses, api, groups={7: []}, users={70: []})
    mock_responses.post(f"{api}/courses/99/group_categories", json={"id": 7, "name": "PT"})
    mock_responses.post(f"{api}/group_categories/7/groups", json={"id": 70, "name": "Team Alpha"})
    mock_responses.post(f"{api}/groups/70/memberships", json={"id": 900})

    result = runner.invoke(
        app, ["student-groups", "apply", "-f", path, "--category", "PT", "-y"]
    )
    assert result.exit_code == 0, result.output
    assert _member_ids(mock_responses) == ["5150"]


def test_apply_headers_are_case_insensitive_and_bom_tolerant(
    write_config, mock_responses, api, tmp_path
):
    """Excel exports carry a BOM and often title-case headers."""
    _config(write_config)
    path = _csv(
        tmp_path,
        "SIS_User_ID,Group_Name,Category\n123,Team Alpha,Project Teams\n",
        encoding="utf-8-sig",
    )
    result = runner.invoke(app, ["student-groups", "apply", "-f", path, "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Project Teams" in result.output
    assert "Team Alpha (1 member)" in result.output


def test_apply_category_column_overrides_default(write_config, mock_responses, api, tmp_path):
    _config(write_config)
    path = _csv(
        tmp_path,
        "sis_user_id,group_name,category\n"
        "123,Team Alpha,Project Teams\n"
        "124,Pair 1,Lab Pairs\n",
    )
    result = runner.invoke(
        app,
        ["student-groups", "apply", "-f", path, "--category", "Ignored", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "Project Teams" in result.output
    assert "Lab Pairs" in result.output
    assert "2 membership row(s) across 2 group(s) in 2 categories" in result.output


def test_apply_skips_rows_missing_required_fields(write_config, mock_responses, api, tmp_path):
    _config(write_config)
    path = _csv(
        tmp_path,
        "sis_user_id,group_name\n"
        "123,Team Alpha\n"
        ",Team Alpha\n"      # no id
        "125,\n"             # no group
        "126,Team Alpha\n",
    )
    result = runner.invoke(
        app, ["student-groups", "apply", "-f", path, "--category", "PT", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "2 membership row(s)" in result.output


def test_apply_requires_group_name_column(write_config, mock_responses, api, tmp_path):
    _config(write_config)
    path = _csv(tmp_path, "sis_user_id,team\n123,Team Alpha\n")
    result = runner.invoke(
        app, ["student-groups", "apply", "-f", path, "--category", "PT", "--dry-run"]
    )
    assert result.exit_code != 0
    assert "group_name" in result.output


def test_apply_requires_an_identifier_column(write_config, mock_responses, api, tmp_path):
    _config(write_config)
    path = _csv(tmp_path, "name,group_name\nAlice,Team Alpha\n")
    result = runner.invoke(
        app, ["student-groups", "apply", "-f", path, "--category", "PT", "--dry-run"]
    )
    assert result.exit_code != 0
    assert "identifier column" in result.output


def test_apply_requires_a_category_from_somewhere(write_config, mock_responses, api, tmp_path):
    _config(write_config)
    path = _csv(tmp_path, "sis_user_id,group_name\n123,Team Alpha\n")
    result = runner.invoke(app, ["student-groups", "apply", "-f", path, "--dry-run"])
    assert result.exit_code != 0
    assert "category" in result.output.lower()


def test_apply_reports_missing_csv(write_config, mock_responses, api, tmp_path):
    _config(write_config)
    result = runner.invoke(
        app,
        ["student-groups", "apply", "-f", str(tmp_path / "nope.csv"), "--category", "PT"],
    )
    assert result.exit_code != 0
    assert "CSV not found" in result.output


# --------------------------------------------------------------------------
# apply — idempotency against existing course state
# --------------------------------------------------------------------------


def test_apply_reuses_existing_category_and_group(write_config, mock_responses, api, tmp_path):
    """Re-running against fully-populated state creates nothing."""
    _config(write_config)
    path = _csv(tmp_path, "sis_user_id,group_name\n123,Team Alpha\n")
    _stub_course(
        mock_responses,
        api,
        categories=[{"id": 7, "name": "PT"}],
        groups={7: [{"id": 70, "name": "Team Alpha"}]},
        users={70: [{"id": 5150, "sis_user_id": "123", "login_id": "stu@byu.edu"}]},
    )

    result = runner.invoke(
        app, ["student-groups", "apply", "-f", path, "--category", "PT", "-y"]
    )
    assert result.exit_code == 0, result.output
    assert len(_posts(mock_responses)) == 0
    assert "categories created:   0" in result.output
    assert "groups created:       0" in result.output
    assert "memberships created:  0" in result.output
    assert "memberships skipped:  1" in result.output


def test_apply_dedupes_member_by_any_identity_form(write_config, mock_responses, api, tmp_path):
    """A CSV keyed on email must match a member Canvas reports by login_id."""
    _config(write_config)
    path = _csv(tmp_path, "email,group_name\nstu@byu.edu,Team Alpha\n")
    _stub_course(
        mock_responses,
        api,
        categories=[{"id": 7, "name": "PT"}],
        groups={7: [{"id": 70, "name": "Team Alpha"}]},
        users={70: [{"id": 5150, "sis_user_id": "123", "login_id": "stu@byu.edu"}]},
    )

    result = runner.invoke(
        app, ["student-groups", "apply", "-f", path, "--category", "PT", "-y"]
    )
    assert result.exit_code == 0, result.output
    assert len(_membership_posts(mock_responses)) == 0
    assert "memberships skipped:  1" in result.output


def test_apply_adds_only_the_missing_member(write_config, mock_responses, api, tmp_path):
    _config(write_config)
    path = _csv(tmp_path, "sis_user_id,group_name\n123,Team Alpha\n999,Team Alpha\n")
    _stub_course(
        mock_responses,
        api,
        categories=[{"id": 7, "name": "PT"}],
        groups={7: [{"id": 70, "name": "Team Alpha"}]},
        users={70: [{"id": 5150, "sis_user_id": "123"}]},
    )
    mock_responses.post(f"{api}/groups/70/memberships", json={"id": 901})

    result = runner.invoke(
        app, ["student-groups", "apply", "-f", path, "--category", "PT", "-y"]
    )
    assert result.exit_code == 0, result.output
    assert _member_ids(mock_responses) == ["sis_user_id:999"]
    assert "memberships created:  1" in result.output
    assert "memberships skipped:  1" in result.output


def test_apply_creates_category_with_self_signup_and_limit(
    write_config, mock_responses, api, tmp_path
):
    _config(write_config)
    path = _csv(tmp_path, "sis_user_id,group_name\n123,Team Alpha\n")
    _stub_course(mock_responses, api, groups={7: []}, users={70: []})
    mock_responses.post(f"{api}/courses/99/group_categories", json={"id": 7, "name": "PT"})
    mock_responses.post(f"{api}/group_categories/7/groups", json={"id": 70, "name": "Team Alpha"})
    mock_responses.post(f"{api}/groups/70/memberships", json={"id": 900})

    result = runner.invoke(
        app,
        [
            "student-groups", "apply", "-f", path, "--category", "PT",
            "--self-signup", "--group-limit", "4", "-y",
        ],
    )
    assert result.exit_code == 0, result.output
    cat_body = next(
        json.loads(c.request.body)
        for c in _posts(mock_responses)
        if c.request.url.endswith("/group_categories")
    )
    assert cat_body == {"name": "PT", "self_signup": "enabled", "group_limit": 4}
    assert "categories created:   1" in result.output


def test_apply_reports_membership_failures_and_exits_nonzero(
    write_config, mock_responses, api, tmp_path
):
    _config(write_config)
    path = _csv(tmp_path, "sis_user_id,group_name\n123,Team Alpha\n")
    _stub_course(
        mock_responses,
        api,
        categories=[{"id": 7, "name": "PT"}],
        groups={7: [{"id": 70, "name": "Team Alpha"}]},
        users={70: []},
    )
    mock_responses.post(
        f"{api}/groups/70/memberships",
        json={"errors": [{"message": "already in another group"}]},
        status=400,
    )

    result = runner.invoke(
        app, ["student-groups", "apply", "-f", path, "--category", "PT", "-y"]
    )
    assert result.exit_code == 1, result.output
    assert "failures:" in result.output
    assert "Team Alpha" in result.output


# --------------------------------------------------------------------------
# read commands
# --------------------------------------------------------------------------


def test_categories_lists_course_categories(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/group_categories",
        json=[{"id": 7, "name": "Project Teams", "self_signup": None, "group_count": 3}],
    )
    result = runner.invoke(app, ["student-groups", "categories"])
    assert result.exit_code == 0, result.output
    assert "Project Teams" in result.output


def test_list_groups_supports_json_output(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/groups",
        json=[{"id": 70, "name": "Team Alpha", "group_category_id": 7, "members_count": 4}],
    )
    result = runner.invoke(app, ["student-groups", "list", "-o", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload[0]["name"] == "Team Alpha"


def test_members_lists_group_users(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/groups/70/users",
        json=[{"id": 5150, "name": "Alice", "sis_user_id": "123", "login_id": "a@byu.edu"}],
    )
    result = runner.invoke(app, ["student-groups", "members", "--group", "70"])
    assert result.exit_code == 0, result.output
    assert "Alice" in result.output


# --------------------------------------------------------------------------
# auto-assign / delete-category
# --------------------------------------------------------------------------


def test_auto_assign_dry_run_makes_no_request(write_config, mock_responses, api):
    _config(write_config)
    result = runner.invoke(
        app, ["student-groups", "auto-assign", "--category-id", "7", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert len(mock_responses.calls) == 0


def test_auto_assign_posts_to_assign_unassigned(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.post(
        f"{api}/group_categories/7/assign_unassigned_members",
        json={"id": 4242, "url": "https://test.instructure.com/api/v1/progress/4242"},
    )
    result = runner.invoke(
        app, ["student-groups", "auto-assign", "--category-id", "7", "-y"]
    )
    assert result.exit_code == 0, result.output
    assert len(_posts(mock_responses)) == 1
    assert "progress/4242" in result.output


def test_delete_category_defaults_to_dry_run(write_config, mock_responses, api):
    """`delete-category` is the one destructive command that is safe by default."""
    _config(write_config)
    result = runner.invoke(app, ["student-groups", "delete-category", "--id", "7"])
    assert result.exit_code == 0, result.output
    assert len(_deletes(mock_responses)) == 0


def test_delete_category_commit_sends_delete(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.delete(f"{api}/group_categories/7", json={"id": 7})
    result = runner.invoke(
        app, ["student-groups", "delete-category", "--id", "7", "--commit", "-y"]
    )
    assert result.exit_code == 0, result.output
    assert len(_deletes(mock_responses)) == 1
    assert "Deleted category 7" in result.output
