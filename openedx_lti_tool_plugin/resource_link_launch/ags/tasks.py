"""Celery Tasks.

Attributes:
    MODULE_PATH (str): This module absolute path.

"""
import logging

from celery import shared_task
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from opaque_keys.edx.keys import CourseKey, UsageKey

from openedx_lti_tool_plugin.edxapp_wrapper.grades_module import course_grade_factory
from openedx_lti_tool_plugin.edxapp_wrapper.modulestore_module import modulestore
from openedx_lti_tool_plugin.models import LtiProfile
from openedx_lti_tool_plugin.resource_link_launch.ags import MODULE_PATH
from openedx_lti_tool_plugin.resource_link_launch.ags.models import LtiActivityLineitem, LtiGradedResource

log = logging.getLogger(__name__)
MODULE_PATH = f'{MODULE_PATH}.tasks'
AGS_CLAIM_ENDPOINT = 'https://purl.imsglobal.org/spec/lti-ags/claim/endpoint'
AGS_SCORE_SCOPE = 'https://purl.imsglobal.org/spec/lti-ags/scope/score'
AGS_LINEITEM_SCOPE = 'https://purl.imsglobal.org/spec/lti-ags/scope/lineitem'


def get_graded_problems(course_key: CourseKey) -> list:
    """Return all graded problem blocks in the course.

    Args:
        course_key: CourseKey of the launched course.

    Returns:
        List of graded problem blocks.

    """
    items = modulestore().get_items(course_key, qualifiers={'category': 'problem'})
    return [item for item in items if item.graded or item.weight]


@shared_task(name=f'{MODULE_PATH}.setup_problem_lineitems')
def setup_problem_lineitems(
    lti_profile_id: int,
    resource_id: str,
    context_id: str,
    lineitems_url: str,
):
    """Create per-problem Moodle lineitems and per-user LtiGradedResource records.

    Runs asynchronously after a course launch with FULL grade sync. For each graded
    problem in the course, creates (once, shared across users) a Moodle lineitem via
    pylti1p3's ``find_or_create_lineitem`` and a per-user ``LtiGradedResource`` so that
    ``send_problem_score_update`` can post that problem's score to its own column.

    The AGS message is rebuilt from a JWT carrying the ``lineitems`` collection URL,
    mirroring ``LtiGradedResource.publish_score``.

    Args:
        lti_profile_id: ID of the launching user's LtiProfile.
        resource_id: The launched Open edX course ID.
        context_id: LTI context claim id (the Moodle course).
        lineitems_url: AGS lineitems collection URL from the launch JWT.

    """
    from pylti1p3.contrib.django import DjangoDbToolConf, DjangoMessageLaunch  # pylint: disable=import-outside-toplevel
    from pylti1p3.lineitem import LineItem  # pylint: disable=import-outside-toplevel

    lti_profile = LtiProfile.objects.filter(id=lti_profile_id).first()
    if not lti_profile:
        return

    problems = get_graded_problems(CourseKey.from_string(resource_id))

    # JWT carrying the lineitems collection URL — mirrors publish_score_jwt.
    jwt = {
        'body': {
            'iss': lti_profile.platform_id,
            'aud': lti_profile.client_id,
            AGS_CLAIM_ENDPOINT: {
                'lineitems': lineitems_url,
                'scope': {AGS_LINEITEM_SCOPE, AGS_SCORE_SCOPE},
            },
        },
    }
    ags = DjangoMessageLaunch(request=None, tool_config=DjangoDbToolConf())\
        .set_auto_validation(enable=False)\
        .set_jwt(jwt)\
        .set_restored()\
        .validate_registration()\
        .get_ags()

    for problem in problems:
        problem_id = str(problem.location)
        label = problem.display_name or problem_id

        activity_lineitem, created = LtiActivityLineitem.objects.get_or_create(
            platform_id=lti_profile.platform_id,
            context_id=context_id,
            problem_id=problem_id,
            defaults={'resource_id': resource_id, 'label': label},
        )

        if created or not activity_lineitem.lineitem:
            lineitem = LineItem()
            lineitem.set_tag(problem_id)
            lineitem.set_label(label)
            lineitem.set_score_maximum(float(problem.weight) if problem.weight else 1.0)
            activity_lineitem.lineitem = ags.find_or_create_lineitem(lineitem, find_by='tag').get_id()
            activity_lineitem.save()

        try:
            LtiGradedResource.objects.get_or_create(
                lti_profile=lti_profile,
                context_key=problem_id,
                lineitem=activity_lineitem.lineitem,
            )
        except ValidationError as exc:
            log.warning(
                'LTI AGS: skipping LtiGradedResource for problem %s: %s',
                problem_id,
                exc.messages,
            )


@shared_task(name=f'{MODULE_PATH}.send_problem_score_update')
def send_problem_score_update(
    problem_weighted_earned: str,
    problem_weighted_possible: str,
    user_id: str,
    problem_id: str,
):
    """Send problem score update task.

    Task to update the AGS score of a problem asynchronously.

    Args:
        problem_weighted_earned: Grade earned for the problem.
        problem_weighted_possible: Grade possible for the problem.
        user_id: Grading user ID.
        problem_id: Problem ID.

    """
    for graded_resource in LtiGradedResource.objects.all_from_user_id(
        user_id=user_id,
        context_key=problem_id,
    ):
        log.info(
            'LTI AGS: Sending AGS update for problem %s with user %s',
            problem_id,
            user_id,
        )
        graded_resource.publish_score(
            problem_weighted_earned,
            problem_weighted_possible,
        )


@shared_task(name=f'{MODULE_PATH}.send_vertical_score_update')
def send_vertical_score_update(
    user_id: str,
    course_id: str,
    problem_id: str,
):
    """Send vertical score update task.

    Task to obtain a vertical's accumulated grade and update the AGS score asynchronously.
    This is a task that would be executed whenever a problem score is updated. We decided
    to do it this way because there is no way of telling if a score of a unit was changed.

    Args:
        user_id: Grading user ID.
        course_id: Context course id string.
        problem_id: Problem ID.

    """
    user = get_user_model().objects.get(id=user_id)
    problem_descriptor = modulestore().get_item(UsageKey.from_string(problem_id))
    vertical_key = problem_descriptor.parent
    vertical_graded_resources = LtiGradedResource.objects.all_from_user_id(
        user_id=user.id,
        context_key=str(vertical_key),
    )

    if not vertical_graded_resources:
        return

    course_grade = course_grade_factory().read(
        user,
        modulestore().get_course(CourseKey.from_string(course_id)),
    )
    earned, possible = course_grade.score_for_module(vertical_key)

    for graded_resource in vertical_graded_resources:
        log.info(
            'LTI AGS: Sending AGS update for unit %s with user %s',
            str(vertical_key),
            user_id,
        )
        graded_resource.publish_score(
            earned,
            possible,
        )
