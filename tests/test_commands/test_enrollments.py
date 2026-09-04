"""Tests for the `enrollments` command group.

The write commands are guarded by a read-back: Canvas returning 200 is not
treated as success until the enrollment has been re-read from the course
listing. Several tests below exist specifically to pin that down, because
the failure it catches (a 200 OK that wrote nothing) is silent otherwise.
"""
from __future__ import annotations

import json

from typer.testing import CliRunner

from canvas_conductor.cli import app


runner = CliRunner()


def _config(write_config, readonly: bool = False):
    write_config(
        f"""
[courses.sandbox]
id = 99
name = "Sandbox"

[courses.locked]
id = 55
name = "Locked"
readonly = {str(readonly).lower()}
"""
    )


def _enrollment(**overrides):
    base = {
        "id": 1911492,
        "user_id": 4242,
        "course_id": 99,
        "course_section_id": 7,
        "type": "TaEnrollment",
        "role": "TaEnrollment",
        "enrollment_state": "active",
        "user": {"name": "Milla Silvester", "login_id": "milla23"},
    }
    base.update(overrides)
    return base


def _bodies(mock_responses, method: str) -> list[dict]:
    return [
        json.loads(c.request.body)
        for c in mock_responses.calls
        if c.request.method == method and c.request.body
    ]


def _calls(mock_responses, method: str):
    return [c for c in mock_responses.calls if c.request.method == method]


# -- add -----------------------------------------------------------------


def test_add_posts_nested_payload_and_verifies(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.post(f"{api}/courses/99/enrollments", json={"id": 1911492})
    mock_responses.get(f"{api}/courses/99/enrollments", json=[_enrollment()])

    result = runner.invoke(
        app,
        ["enrollments", "add", "-c", "sandbox", "--user", "milla23",
         "--role", "ta", "--state", "active", "-y"],
    )
    assert result.exit_code == 0, result.output

    body = _bodies(mock_responses, "POST")[0]
    assert body == {
        "enrollment": {
            "user_id": "sis_login_id:milla23",
            "type": "TaEnrollment",
            "enrollment_state": "active",
            "notify": True,
        }
    }
    assert "Verified: enrollment 1911492" in result.output


def test_add_payload_uses_no_rails_bracket_keys(write_config, mock_responses, api):
    """`{"enrollment[user_id]": …}` would 200 and enrol nobody."""
    _config(write_config)
    mock_responses.post(f"{api}/courses/99/enrollments", json={"id": 1911492})
    mock_responses.get(f"{api}/courses/99/enrollments", json=[_enrollment()])

    runner.invoke(
        app,
        ["enrollments", "add", "-c", "sandbox", "--user", "milla23",
         "--role", "ta", "--state", "active", "--limit-to-section", "-y"],
    )
    body = _bodies(mock_responses, "POST")[0]

    def walk(node, path=""):
        for key, value in node.items():
            assert "[" not in key and "]" not in key, f"bracket key at {path}{key}"
            if isinstance(value, dict):
                walk(value, f"{path}{key}.")

    walk(body)
    assert list(body) == ["enrollment"], "payload must nest under a single key"


def test_add_maps_every_friendly_role(write_config, mock_responses, api):
    _config(write_config)
    cases = {
        "student": "StudentEnrollment",
        "teacher": "TeacherEnrollment",
        "instructor": "TeacherEnrollment",
        "ta": "TaEnrollment",
        "designer": "DesignerEnrollment",
        "observer": "ObserverEnrollment",
        "TaEnrollment": "TaEnrollment",
    }
    for friendly, canvas_type in cases.items():
        mock_responses.post(f"{api}/courses/99/enrollments", json={"id": 5})
        mock_responses.get(
            f"{api}/courses/99/enrollments",
            json=[_enrollment(id=5, type=canvas_type, enrollment_state="active")],
        )
        result = runner.invoke(
            app,
            ["enrollments", "add", "-c", "sandbox", "--user", "milla23",
             "--role", friendly, "--state", "active", "-y"],
        )
        assert result.exit_code == 0, f"{friendly}: {result.output}"

    sent = [b["enrollment"]["type"] for b in _bodies(mock_responses, "POST")]
    assert sent == list(cases.values())


def test_add_passes_role_id_through(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.post(f"{api}/courses/99/enrollments", json={"id": 77})
    mock_responses.get(
        f"{api}/courses/99/enrollments",
        json=[_enrollment(id=77, type="TaEnrollment", role="Peer Mentor")],
    )
    result = runner.invoke(
        app,
        ["enrollments", "add", "-c", "sandbox", "--user", "milla23",
         "--role-id", "412", "--state", "active", "-y"],
    )
    assert result.exit_code == 0, result.output
    inner = _bodies(mock_responses, "POST")[0]["enrollment"]
    assert inner["role_id"] == 412
    assert "type" not in inner, "no --role given, so no base type should be sent"


def test_add_requires_a_role(write_config):
    _config(write_config)
    result = runner.invoke(
        app, ["enrollments", "add", "-c", "sandbox", "--user", "milla23", "-y"]
    )
    assert result.exit_code != 0
    assert "--role" in result.output


def test_add_dry_run_writes_nothing_and_shows_payload(write_config, mock_responses, api):
    _config(write_config)
    result = runner.invoke(
        app,
        ["enrollments", "add", "-c", "sandbox", "--user", "milla23",
         "--role", "ta", "--state", "active", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "DRY-RUN: POST /courses/99/enrollments" in result.output
    assert '"user_id": "sis_login_id:milla23"' in result.output
    assert not mock_responses.calls


def test_add_section_posts_to_section_endpoint(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.post(f"{api}/sections/7/enrollments", json={"id": 12})
    mock_responses.get(
        f"{api}/courses/99/enrollments",
        json=[_enrollment(id=12, type="DesignerEnrollment")],
    )
    result = runner.invoke(
        app,
        ["enrollments", "add", "-c", "sandbox", "--user", "milla23",
         "--role", "designer", "--state", "active", "--section", "7", "-y"],
    )
    assert result.exit_code == 0, result.output
    assert _calls(mock_responses, "POST")[0].request.url.endswith(
        "/sections/7/enrollments"
    )


def test_add_limit_to_section_and_notify_flags(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.post(f"{api}/courses/99/enrollments", json={"id": 8})
    mock_responses.get(
        f"{api}/courses/99/enrollments",
        json=[_enrollment(id=8, type="StudentEnrollment")],
    )
    runner.invoke(
        app,
        ["enrollments", "add", "-c", "sandbox", "--user", "42", "--role", "student",
         "--state", "active", "--limit-to-section", "--no-notify", "-y"],
    )
    inner = _bodies(mock_responses, "POST")[0]["enrollment"]
    assert inner["limit_privileges_to_course_section"] is True
    assert inner["notify"] is False
    assert inner["user_id"] == "42", "a bare numeric id passes through unprefixed"


def test_add_observer_sends_associated_user(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.post(f"{api}/courses/99/enrollments", json={"id": 9})
    mock_responses.get(
        f"{api}/courses/99/enrollments",
        json=[_enrollment(id=9, type="ObserverEnrollment")],
    )
    runner.invoke(
        app,
        ["enrollments", "add", "-c", "sandbox", "--user", "parent1",
         "--role", "observer", "--associated-user", "kid42", "--state", "active", "-y"],
    )
    inner = _bodies(mock_responses, "POST")[0]["enrollment"]
    assert inner["associated_user_id"] == "sis_login_id:kid42"


def test_add_accepts_explicit_sis_references(write_config, mock_responses, api):
    _config(write_config)
    for ref in ("sis_login_id:milla23", "sis_user_id:00123", "sis_integration_id:x9"):
        mock_responses.post(f"{api}/courses/99/enrollments", json={"id": 3})
        mock_responses.get(
            f"{api}/courses/99/enrollments",
            json=[_enrollment(id=3, type="StudentEnrollment")],
        )
        result = runner.invoke(
            app,
            ["enrollments", "add", "-c", "sandbox", "--user", ref,
             "--role", "student", "--state", "active", "-y"],
        )
        assert result.exit_code == 0, result.output

    sent = [b["enrollment"]["user_id"] for b in _bodies(mock_responses, "POST")]
    assert sent == ["sis_login_id:milla23", "sis_user_id:00123", "sis_integration_id:x9"]


def test_add_rejects_a_human_name(write_config, mock_responses):
    """Name search needs the account endpoint, which 403s for teacher tokens."""
    _config(write_config)
    result = runner.invoke(
        app,
        ["enrollments", "add", "-c", "sandbox", "--user", "Jane Smith",
         "--role", "ta", "-y"],
    )
    assert result.exit_code != 0
    assert "netid" in result.output
    assert not mock_responses.calls


def test_add_rejects_unknown_role(write_config):
    _config(write_config)
    result = runner.invoke(
        app,
        ["enrollments", "add", "-c", "sandbox", "--user", "milla23",
         "--role", "peer-mentor", "-y"],
    )
    assert result.exit_code != 0
    assert "--role-id" in result.output


def test_add_rejects_non_creatable_state(write_config):
    _config(write_config)
    result = runner.invoke(
        app,
        ["enrollments", "add", "-c", "sandbox", "--user", "milla23",
         "--role", "ta", "--state", "completed", "-y"],
    )
    assert result.exit_code != 0
    assert "--state" in result.output


def test_add_ta_warns_about_gradebook(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.post(f"{api}/courses/99/enrollments", json={"id": 1})
    mock_responses.get(f"{api}/courses/99/enrollments", json=[_enrollment(id=1)])
    result = runner.invoke(
        app,
        ["enrollments", "add", "-c", "sandbox", "--user", "milla23",
         "--role", "ta", "--state", "active", "-y"],
    )
    assert "gradebook access" in result.output
    assert "designer" in result.output


def test_add_teacher_requires_confirmation(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.post(f"{api}/courses/99/enrollments", json={"id": 2})
    result = runner.invoke(
        app,
        ["enrollments", "add", "-c", "sandbox", "--user", "milla23",
         "--role", "teacher", "--state", "active"],
        input="n\n",
    )
    assert result.exit_code == 1, result.output
    assert not _calls(mock_responses, "POST")


def test_add_teacher_proceeds_when_confirmed(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.post(f"{api}/courses/99/enrollments", json={"id": 2})
    mock_responses.get(
        f"{api}/courses/99/enrollments",
        json=[_enrollment(id=2, type="TeacherEnrollment")],
    )
    result = runner.invoke(
        app,
        ["enrollments", "add", "-c", "sandbox", "--user", "milla23",
         "--role", "teacher", "--state", "active"],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    assert len(_calls(mock_responses, "POST")) == 1


def test_add_fails_loudly_when_readback_finds_nothing(write_config, mock_responses, api):
    """A 200 OK that wrote nothing must not be reported as success."""
    _config(write_config)
    mock_responses.post(f"{api}/courses/99/enrollments", json={"id": 1911492})
    mock_responses.get(f"{api}/courses/99/enrollments", json=[])

    result = runner.invoke(
        app,
        ["enrollments", "add", "-c", "sandbox", "--user", "milla23",
         "--role", "ta", "--state", "active", "-y"],
    )
    assert result.exit_code == 10, result.output
    assert "Read-back failed" in result.output


def test_add_fails_loudly_on_readback_type_mismatch(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.post(f"{api}/courses/99/enrollments", json={"id": 5})
    mock_responses.get(
        f"{api}/courses/99/enrollments",
        json=[_enrollment(id=5, type="StudentEnrollment")],
    )
    result = runner.invoke(
        app,
        ["enrollments", "add", "-c", "sandbox", "--user", "milla23",
         "--role", "ta", "--state", "active", "-y"],
    )
    assert result.exit_code == 10, result.output
    assert "mismatch" in result.output


def test_add_readback_finds_inactive_enrollment(write_config, mock_responses, api):
    """The default listing hides inactive rows; read-back must ask for them."""
    _config(write_config)
    mock_responses.post(f"{api}/courses/99/enrollments", json={"id": 6})
    mock_responses.get(
        f"{api}/courses/99/enrollments",
        json=[_enrollment(id=6, type="TaEnrollment", enrollment_state="inactive")],
    )
    result = runner.invoke(
        app,
        ["enrollments", "add", "-c", "sandbox", "--user", "milla23",
         "--role", "ta", "--state", "inactive", "-y"],
    )
    assert result.exit_code == 0, result.output
    url = _calls(mock_responses, "GET")[0].request.url
    assert "state%5B%5D=inactive" in url


def test_add_403_gives_actionable_guidance(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.post(
        f"{api}/courses/99/enrollments",
        json={"errors": [{"message": "user not authorized"}]},
        status=403,
    )
    result = runner.invoke(
        app,
        ["enrollments", "add", "-c", "sandbox", "--user", "milla23",
         "--role", "ta", "--state", "active", "-y"],
    )
    assert result.exit_code == 4, result.output
    assert "sis_login_id" in result.output
    assert "Traceback" not in result.output


def test_add_404_mentions_netid_spelling(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.post(
        f"{api}/courses/99/enrollments",
        json={"errors": [{"message": "not found"}]},
        status=404,
    )
    result = runner.invoke(
        app,
        ["enrollments", "add", "-c", "sandbox", "--user", "nosuchuser",
         "--role", "ta", "--state", "active", "-y"],
    )
    assert result.exit_code == 5, result.output
    assert "netid" in result.output


# -- remove --------------------------------------------------------------


def test_remove_requires_task(write_config):
    _config(write_config)
    result = runner.invoke(
        app, ["enrollments", "remove", "-c", "sandbox", "--enrollment-id", "1", "-y"]
    )
    assert result.exit_code != 0
    assert "--task" in result.output


def test_remove_rejects_unknown_task(write_config):
    _config(write_config)
    result = runner.invoke(
        app,
        ["enrollments", "remove", "-c", "sandbox", "--enrollment-id", "1",
         "--task", "destroy", "-y"],
    )
    assert result.exit_code != 0
    assert "conclude" in result.output


def test_remove_delete_requires_confirm_flag(write_config, mock_responses):
    _config(write_config)
    result = runner.invoke(
        app,
        ["enrollments", "remove", "-c", "sandbox", "--enrollment-id", "1",
         "--task", "delete", "-y"],
    )
    assert result.exit_code != 0
    assert "--confirm-delete" in result.output
    assert not mock_responses.calls


def test_remove_conclude_sends_task_and_verifies_state(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/enrollments", json=[_enrollment()])
    mock_responses.delete(f"{api}/courses/99/enrollments/1911492", json={"id": 1911492})
    mock_responses.get(
        f"{api}/courses/99/enrollments",
        json=[_enrollment(enrollment_state="completed")],
    )
    result = runner.invoke(
        app,
        ["enrollments", "remove", "-c", "sandbox", "--enrollment-id", "1911492",
         "--task", "conclude", "-y"],
    )
    assert result.exit_code == 0, result.output
    assert "task=conclude" in _calls(mock_responses, "DELETE")[0].request.url


def test_remove_delete_verifies_absence(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/enrollments", json=[_enrollment()])
    mock_responses.delete(f"{api}/courses/99/enrollments/1911492", json={"id": 1911492})
    mock_responses.get(f"{api}/courses/99/enrollments", json=[])
    result = runner.invoke(
        app,
        ["enrollments", "remove", "-c", "sandbox", "--enrollment-id", "1911492",
         "--task", "delete", "--confirm-delete", "-y"],
    )
    assert result.exit_code == 0, result.output
    assert "is gone from course 99" in result.output


def test_remove_fails_when_deleted_enrollment_survives(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/enrollments", json=[_enrollment()])
    mock_responses.delete(f"{api}/courses/99/enrollments/1911492", json={})
    mock_responses.get(f"{api}/courses/99/enrollments", json=[_enrollment()])
    result = runner.invoke(
        app,
        ["enrollments", "remove", "-c", "sandbox", "--enrollment-id", "1911492",
         "--task", "delete", "--confirm-delete", "-y"],
    )
    assert result.exit_code == 10, result.output
    assert "still present" in result.output


def test_remove_dry_run_writes_nothing(write_config, mock_responses):
    _config(write_config)
    result = runner.invoke(
        app,
        ["enrollments", "remove", "-c", "sandbox", "--enrollment-id", "1911492",
         "--task", "conclude", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "DRY-RUN: DELETE /courses/99/enrollments/1911492?task=conclude" in result.output
    assert not mock_responses.calls


def test_remove_unknown_enrollment_exits_5(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/enrollments", json=[])
    result = runner.invoke(
        app,
        ["enrollments", "remove", "-c", "sandbox", "--enrollment-id", "404404",
         "--task", "conclude", "-y"],
    )
    assert result.exit_code == 5, result.output
    assert not _calls(mock_responses, "DELETE")


# -- reactivate ----------------------------------------------------------


def test_reactivate_verifies_active(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/enrollments", json=[_enrollment(enrollment_state="inactive")]
    )
    mock_responses.put(
        f"{api}/courses/99/enrollments/1911492/reactivate", json={"id": 1911492}
    )
    mock_responses.get(f"{api}/courses/99/enrollments", json=[_enrollment()])
    result = runner.invoke(
        app,
        ["enrollments", "reactivate", "-c", "sandbox", "--enrollment-id", "1911492", "-y"],
    )
    assert result.exit_code == 0, result.output
    assert "Verified: enrollment 1911492" in result.output


def test_reactivate_uses_put_not_post(write_config, mock_responses, api):
    """Canvas has no POST route here; POSTing 404s with an HTML page."""
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/enrollments", json=[_enrollment(enrollment_state="inactive")]
    )
    mock_responses.put(
        f"{api}/courses/99/enrollments/1911492/reactivate", json={"id": 1911492}
    )
    mock_responses.get(f"{api}/courses/99/enrollments", json=[_enrollment()])
    runner.invoke(
        app,
        ["enrollments", "reactivate", "-c", "sandbox", "--enrollment-id", "1911492", "-y"],
    )
    assert [c.request.method for c in mock_responses.calls if c.request.method != "GET"] == [
        "PUT"
    ]


def test_reactivate_dry_run_says_put(write_config, mock_responses):
    _config(write_config)
    result = runner.invoke(
        app,
        ["enrollments", "reactivate", "-c", "sandbox", "--enrollment-id", "1", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "DRY-RUN: PUT /courses/99/enrollments/1/reactivate" in result.output
    assert not mock_responses.calls


def test_destructive_task_403_recommends_conclude(write_config, mock_responses, api):
    """Verified live: delete/deactivate 403 on your own enrollment, conclude doesn't."""
    _config(write_config)
    for task, extra in (
        ("deactivate", []),
        ("inactivate", []),
        ("delete", ["--confirm-delete"]),
    ):
        mock_responses.get(f"{api}/courses/99/enrollments", json=[_enrollment()])
        mock_responses.delete(
            f"{api}/courses/99/enrollments/1911492",
            json={"errors": [{"message": "user not authorized to perform that action"}]},
            status=403,
        )
        result = runner.invoke(
            app,
            ["enrollments", "remove", "-c", "sandbox", "--enrollment-id", "1911492",
             "--task", task, "-y", *extra],
        )
        assert result.exit_code == 4, f"{task}: {result.output}"
        assert "conclude" in result.output, task
        assert "Traceback" not in result.output, task


def test_conclude_403_gives_generic_guidance(write_config, mock_responses, api):
    """conclude isn't subject to the self-enrollment check, so don't blame it."""
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/enrollments", json=[_enrollment()])
    mock_responses.delete(
        f"{api}/courses/99/enrollments/1911492",
        json={"errors": [{"message": "user not authorized"}]},
        status=403,
    )
    result = runner.invoke(
        app,
        ["enrollments", "remove", "-c", "sandbox", "--enrollment-id", "1911492",
         "--task", "conclude", "-y"],
    )
    assert result.exit_code == 4, result.output
    assert "manage_admin_users" not in result.output


def test_html_error_body_is_not_dumped_raw(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/enrollments", json=[_enrollment()])
    mock_responses.delete(
        f"{api}/courses/99/enrollments/1911492",
        body="<!DOCTYPE html>\n<html dir='ltr'><head><title>Page Not Found</title>",
        status=404,
        content_type="text/html",
    )
    result = runner.invoke(
        app,
        ["enrollments", "remove", "-c", "sandbox", "--enrollment-id", "1911492",
         "--task", "conclude", "-y"],
    )
    assert result.exit_code == 5, result.output
    assert "DOCTYPE" not in result.output
    # Rich wraps at 80 cols; match a fragment that can't straddle the break.
    assert "rather than a JSON error" in result.output


def test_update_warns_when_old_enrollment_survives_removal(
    write_config, mock_responses, api
):
    """A failed step 2 leaves the person holding both enrollments."""
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/enrollments", json=[_enrollment()])
    mock_responses.post(f"{api}/sections/7/enrollments", json={"id": 2000})
    mock_responses.get(
        f"{api}/courses/99/enrollments",
        json=[_enrollment(), _enrollment(id=2000, type="DesignerEnrollment")],
    )
    mock_responses.delete(
        f"{api}/courses/99/enrollments/1911492",
        json={"errors": [{"message": "user not authorized to perform that action"}]},
        status=403,
    )
    result = runner.invoke(
        app,
        ["enrollments", "update", "-c", "sandbox", "--enrollment-id", "1911492",
         "--role", "designer", "--task", "deactivate", "-y"],
    )
    assert result.exit_code == 4, result.output
    assert "BOTH" in result.output


def test_reactivate_notes_non_inactive_state(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/enrollments", json=[_enrollment(enrollment_state="completed")]
    )
    mock_responses.put(
        f"{api}/courses/99/enrollments/1911492/reactivate", json={"id": 1911492}
    )
    mock_responses.get(f"{api}/courses/99/enrollments", json=[_enrollment()])
    result = runner.invoke(
        app,
        ["enrollments", "reactivate", "-c", "sandbox", "--enrollment-id", "1911492", "-y"],
    )
    assert "inactive" in result.output


# -- update --------------------------------------------------------------


def test_update_adds_before_removing(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/enrollments", json=[_enrollment()])
    mock_responses.post(f"{api}/sections/7/enrollments", json={"id": 2000})
    mock_responses.get(
        f"{api}/courses/99/enrollments",
        json=[_enrollment(), _enrollment(id=2000, type="DesignerEnrollment")],
    )
    mock_responses.delete(f"{api}/courses/99/enrollments/1911492", json={})
    mock_responses.get(
        f"{api}/courses/99/enrollments",
        json=[
            _enrollment(enrollment_state="completed"),
            _enrollment(id=2000, type="DesignerEnrollment"),
        ],
    )

    result = runner.invoke(
        app,
        ["enrollments", "update", "-c", "sandbox", "--enrollment-id", "1911492",
         "--role", "designer", "--task", "conclude", "-y"],
    )
    assert result.exit_code == 0, result.output

    methods = [c.request.method for c in mock_responses.calls]
    assert methods.index("POST") < methods.index("DELETE"), "add must precede remove"

    inner = _bodies(mock_responses, "POST")[0]["enrollment"]
    assert inner["user_id"] == "4242", "reuses the numeric id from the old enrollment"
    assert inner["type"] == "DesignerEnrollment"
    assert "task=conclude" in _calls(mock_responses, "DELETE")[0].request.url


def test_update_skips_removal_when_add_fails(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/enrollments", json=[_enrollment()])
    mock_responses.post(
        f"{api}/sections/7/enrollments",
        json={"errors": [{"message": "user not authorized"}]},
        status=403,
    )
    result = runner.invoke(
        app,
        ["enrollments", "update", "-c", "sandbox", "--enrollment-id", "1911492",
         "--role", "designer", "--task", "delete", "--confirm-delete", "-y"],
    )
    assert result.exit_code == 4, result.output
    assert not _calls(mock_responses, "DELETE"), "old access must survive a failed add"


def test_update_requires_something_to_change(write_config):
    _config(write_config)
    result = runner.invoke(
        app,
        ["enrollments", "update", "-c", "sandbox", "--enrollment-id", "1",
         "--task", "conclude", "-y"],
    )
    assert result.exit_code != 0
    assert "Nothing to change" in result.output


def test_update_dry_run_shows_both_steps(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/enrollments", json=[_enrollment()])
    result = runner.invoke(
        app,
        ["enrollments", "update", "-c", "sandbox", "--enrollment-id", "1911492",
         "--role", "designer", "--task", "conclude", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "step 1 of 2" in result.output
    assert "step 2 of 2" in result.output
    assert not _calls(mock_responses, "POST")
    assert not _calls(mock_responses, "DELETE")


# -- readonly guard ------------------------------------------------------


def test_readonly_course_blocks_writes(write_config, mock_responses):
    _config(write_config, readonly=True)
    result = runner.invoke(
        app,
        ["enrollments", "add", "-c", "locked", "--user", "milla23",
         "--role", "ta", "--state", "active", "-y"],
    )
    assert result.exit_code == 9, result.output
    assert "readonly" in result.output
    assert not mock_responses.calls


def test_readonly_course_allows_force(write_config, mock_responses, api):
    _config(write_config, readonly=True)
    mock_responses.post(f"{api}/courses/55/enrollments", json={"id": 1})
    mock_responses.get(f"{api}/courses/55/enrollments", json=[_enrollment(id=1)])
    result = runner.invoke(
        app,
        ["enrollments", "add", "-c", "locked", "--user", "milla23",
         "--role", "ta", "--state", "active", "--force", "-y"],
    )
    assert result.exit_code == 0, result.output
    # Rich wraps stderr at 80 cols, so match a fragment that can't straddle it.
    assert "proceeding." in result.output


def test_readonly_course_allows_dry_run(write_config, mock_responses):
    _config(write_config, readonly=True)
    result = runner.invoke(
        app,
        ["enrollments", "add", "-c", "locked", "--user", "milla23",
         "--role", "ta", "--state", "active", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    assert not mock_responses.calls


def test_readonly_does_not_block_reads(write_config, mock_responses, api):
    _config(write_config, readonly=True)
    mock_responses.get(f"{api}/courses/55/enrollments", json=[_enrollment()])
    result = runner.invoke(app, ["enrollments", "list", "-c", "locked"])
    assert result.exit_code == 0, result.output


def test_non_readonly_course_is_unaffected(write_config, mock_responses, api):
    _config(write_config, readonly=False)
    mock_responses.post(f"{api}/courses/55/enrollments", json={"id": 1})
    mock_responses.get(f"{api}/courses/55/enrollments", json=[_enrollment(id=1)])
    result = runner.invoke(
        app,
        ["enrollments", "add", "-c", "locked", "--user", "milla23",
         "--role", "ta", "--state", "active", "-y"],
    )
    assert result.exit_code == 0, result.output


# -- unchanged read commands --------------------------------------------


def test_list_still_works(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(f"{api}/courses/99/enrollments", json=[_enrollment()])
    result = runner.invoke(app, ["enrollments", "list", "-c", "sandbox"])
    assert result.exit_code == 0, result.output
    assert "Milla Silvester" in result.output


def test_summary_still_works(write_config, mock_responses, api):
    _config(write_config)
    mock_responses.get(
        f"{api}/courses/99/enrollments",
        json=[_enrollment(), _enrollment(id=2, type="StudentEnrollment")],
    )
    result = runner.invoke(app, ["enrollments", "summary", "-c", "sandbox", "-o", "json"])
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.output)) == 2
