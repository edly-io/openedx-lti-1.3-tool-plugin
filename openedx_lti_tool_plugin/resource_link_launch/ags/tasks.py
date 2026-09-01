"""Celery Tasks.

Attributes:
    MODULE_PATH (str): This module absolute path.

"""
import logging
from typing import Optional

from celery import shared_task
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import CourseKey, UsageKey

from openedx_lti_tool_plugin.edxapp_wrapper.grades_module import course_grade_factory
from openedx_lti_tool_plugin.edxapp_wrapper.modulestore_module import modulestore
from openedx_lti_tool_plugin.models import LtiProfile, LtiToolConfiguration
from openedx_lti_tool_plugin.resource_link_launch.ags import MODULE_PATH
from openedx_lti_tool_plugin.resource_link_launch.ags.models import LtiActivityLineitem, LtiGradedResource

log = logging.getLogger(__name__)
MODULE_PATH = f'{MODULE_PATH}.tasks'
AGS_CLAIM_ENDPOINT = 'https://purl.imsglobal.org/spec/lti-ags/claim/endpoint'
AGS_SCORE_SCOPE = 'https://purl.imsglobal.org/spec/lti-ags/scope/score'
AGS_LINEITEM_SCOPE = 'https://purl.imsglobal.org/spec/lti-ags/scope/lineitem'


def is_gradable_block(block) -> bool:
    """Return True if a block produces a score and is graded or weighted.

    Covers native ``problem`` blocks, consumed LTI tools (``lti_consumer``) and any
    other scored XBlock, so each becomes its own lineitem on the target platform.
    """
    return bool(
        getattr(block, 'has_score', False)
        and (getattr(block, 'graded', False) or getattr(block, 'weight', None))
    )


def get_gradable_blocks(course_key: CourseKey) -> list:
    """Return all gradable, scored blocks in the course.

    Args:
        course_key: CourseKey of the launched course.

    Returns:
        List of gradable block instances.

    """
    return [block for block in modulestore().get_items(course_key) if is_gradable_block(block)]


def get_gradable_blocks_for_resource(resource_id: str) -> list:
    """Return the gradable blocks for a launched resource (course OR container block).

    - Course launch → every gradable block in the course.
    - Container block launch (section/subsection/unit) → the gradable blocks within it,
      so embedding a unit creates one lineitem per problem inside that unit.
    - Leaf block launch (a single problem) → empty list: its own coupled lineitem
      (created in ``handle_ags``) already covers it, so we don't split it.

    Args:
        resource_id: Launched course or block ID.

    Returns:
        List of gradable block instances.

    """
    try:
        return get_gradable_blocks(CourseKey.from_string(resource_id))
    except InvalidKeyError:
        pass

    root = modulestore().get_item(UsageKey.from_string(resource_id))
    if not root.get_children():
        return []

    gradable = []

    def collect(block):
        for child in block.get_children():
            if is_gradable_block(child):
                gradable.append(child)
            collect(child)

    collect(root)

    return gradable


def get_ags_for_lineitems_url(iss: str, aud: str, lineitems_url: str):
    """Build a pylti1p3 AGS service object scoped to a `lineitems` collection URL.

    Shared by `setup_problem_lineitems` and `relay_criterion_scores` — both need to call
    Moodle's `find_or_create_lineitem`, and the only thing that differs between them is
    which `lineitems_url` they're pointed at; the JWT-rebuilding dance around it (mirroring
    `LtiGradedResource.publish_score_jwt`, since there's no live launch request here to
    reuse) is identical either way.

    Args:
        iss: LTI platform issuer.
        aud: LTI platform audience (client id).
        lineitems_url: AGS lineitems collection URL.

    Returns:
        A pylti1p3 AGS service object with `find_or_create_lineitem`.

    """
    from pylti1p3.contrib.django import DjangoDbToolConf, DjangoMessageLaunch  # pylint: disable=import-outside-toplevel

    jwt = {
        'body': {
            'iss': iss,
            'aud': aud,
            AGS_CLAIM_ENDPOINT: {
                'lineitems': lineitems_url,
                'scope': {AGS_LINEITEM_SCOPE, AGS_SCORE_SCOPE},
            },
        },
    }
    return DjangoMessageLaunch(request=None, tool_config=DjangoDbToolConf())\
        .set_auto_validation(enable=False)\
        .set_jwt(jwt)\
        .set_restored()\
        .validate_registration()\
        .get_ags()


@shared_task(name=f'{MODULE_PATH}.setup_problem_lineitems')
def setup_problem_lineitems(
    lti_profile_id: int,
    resource_id: str,
    context_id: str,
    resource_link_id: str,
    lineitems_url: str,
):
    """Create per-problem target lineitems and per-user LtiGradedResource records.

    Only used in **per-problem** passback mode (Moodle). For each gradable block in the
    launched content (native problems as well as consumed LTI tools and other scored
    blocks), creates (once, shared across users of the same activity) a lineitem on the
    platform via pylti1p3's ``find_or_create_lineitem`` and a per-user
    ``LtiGradedResource`` so its score posts to its own column.

    The lineitem is keyed and tagged by ``resource_link_id`` so that two platform
    activities embedding the same Open edX problem get separate columns instead of one.

    Args:
        lti_profile_id: ID of the launching user's LtiProfile.
        resource_id: The launched Open edX course/content ID.
        context_id: LTI context claim id (the platform's course/context).
        resource_link_id: LTI resource link id (the platform activity/placement).
        lineitems_url: AGS lineitems collection URL from the launch JWT.

    """
    from pylti1p3.lineitem import LineItem  # pylint: disable=import-outside-toplevel

    lti_profile = LtiProfile.objects.filter(id=lti_profile_id).first()
    if not lti_profile:
        return

    blocks = get_gradable_blocks_for_resource(resource_id)
    ags = get_ags_for_lineitems_url(lti_profile.platform_id, lti_profile.client_id, lineitems_url)

    for block in blocks:
        block_id = str(block.location)
        label = block.display_name or block_id

        activity_lineitem, created = LtiActivityLineitem.objects.get_or_create(
            platform_id=lti_profile.platform_id,
            resource_link_id=resource_link_id,
            problem_id=block_id,
            defaults={'context_id': context_id, 'resource_id': resource_id, 'label': label},
        )

        if created or not activity_lineitem.lineitem:
            lineitem = LineItem()
            # Tag per (activity, problem) so distinct placements don't share a lineitem.
            lineitem.set_tag(f'{resource_link_id}:{block_id}' if resource_link_id else block_id)
            lineitem.set_label(label)
            lineitem.set_score_maximum(float(getattr(block, 'weight', None) or 1.0))
            activity_lineitem.lineitem = ags.find_or_create_lineitem(lineitem, find_by='tag').get_id()
            activity_lineitem.save()

        # criterion_key='' is explicit: this is the coupled (per-problem) record for this
        # block, the same role handle_ags's own coupled record plays for a leaf launch — and
        # if this block later turns out to qualify for per-criterion relay (get_multi_line_item_
        # lti_configuration), this is the row relay_criterion_scores reads lineitems_url from.
        try:
            graded_resource, resource_created = LtiGradedResource.objects.get_or_create(
                lti_profile=lti_profile,
                context_key=block_id,
                lineitem=activity_lineitem.lineitem,
                criterion_key='',
            )
        except ValidationError as exc:
            log.warning(
                'LTI AGS: skipping LtiGradedResource for block %s: %s',
                block_id,
                exc.messages,
            )
            continue

        # Same capture-and-backfill as handle_ags's own coupled record, and for the same
        # reason: resource_link_id/lineitems_url/context_id only exist on the launch request
        # (this function's own arguments, here — there's no live request to re-read them from
        # later), but a per-criterion relay needs lineitems_url long after this task has
        # finished. Without this, a block reached only through a container launch (never
        # through handle_ags's own leaf-launch path) would keep lineitems_url='' forever, and
        # its first real grade would crash relay_criterion_scores with an HTTP call against an
        # empty URL.
        if resource_created or not graded_resource.lineitems_url or not graded_resource.resource_link_id:
            graded_resource.lineitems_url = lineitems_url
            graded_resource.resource_link_id = resource_link_id
            graded_resource.context_id = context_id
            try:
                graded_resource.save(update_fields=['lineitems_url', 'resource_link_id', 'context_id'])
            except ValidationError as exc:
                log.warning(
                    'LTI AGS: skipping lineitems_url backfill for block %s: %s',
                    block_id,
                    exc.messages,
                )


def get_multi_line_item_lti_configuration(block, lti_profile: LtiProfile):
    """Return this block's LtiConfiguration if it qualifies for per-criterion Moodle relay.

    Three independent, unrelated conditions all have to hold, so none of them alone is a safe
    signal on its own:

    - The block is an ``lti_consumer`` block at all (native problems, ORA, etc. never qualify).
    - Its own AGS mode is ``programmatic`` — an ``lti_consumer``-side setting meaning "the tool
      (e.g. Muzzy Lane) manages its own AGS line items", which is a *precondition* for having
      more than one, not by itself a request to relay them onward to Moodle.
    - The Moodle-facing ``LtiToolConfiguration`` for *this launch* has opted into per-problem
      passback (``uses_per_problem_passback()``) — the actual, deliberate "yes, fan these out"
      decision. Reusing this existing flag (rather than adding a new one) means an operator who
      wants ``programmatic`` mode for some other reason, with a ``coupled``-mode Moodle tool,
      is correctly left untouched.

    Args:
        block: The loaded XBlock instance for a location in `send_score_updates`'s ancestor walk.
        lti_profile: The launching user's LtiProfile.

    Returns:
        The block's `lti_consumer.models.LtiConfiguration` if it qualifies, else None.

    """
    if getattr(block, 'category', None) != 'lti_consumer':
        return None

    try:
        # Lazy, guarded import: xblock-lti-consumer is a separate installable plugin, and this
        # whole check is meaningless (and its models unavailable) on any Open edX instance that
        # doesn't have it installed.
        from lti_consumer.models import LtiConfiguration  # pylint: disable=import-outside-toplevel,import-error
    except ImportError:
        return None

    lti_configuration = LtiConfiguration.objects.filter(location=block.location).first()
    if not lti_configuration or lti_configuration.get_lti_advantage_ags_mode() != 'programmatic':
        return None

    from pylti1p3.contrib.django import DjangoDbToolConf  # pylint: disable=import-outside-toplevel
    from pylti1p3.exception import LtiException  # pylint: disable=import-outside-toplevel

    try:
        # get_lti_tool raises LtiException (not just returning None) when iss/aud don't match
        # any registration. That should never actually happen here — this profile already
        # launched successfully through this exact iss/aud, or the coupled LtiGradedResource
        # this function's caller already found wouldn't exist — but this runs inside a Celery
        # task, not a request with an outer LtiException handler, so it's caught explicitly
        # rather than left to crash the task.
        lti_tool_configuration = LtiToolConfiguration.objects.get(
            lti_tool=DjangoDbToolConf().get_lti_tool(lti_profile.platform_id, lti_profile.client_id),
        )
    except (LtiToolConfiguration.DoesNotExist, LtiException):
        return None

    if not lti_tool_configuration.uses_per_problem_passback():
        return None

    return lti_configuration


def get_external_user_id(lti_profile: LtiProfile) -> Optional[str]:
    """Return this profile's xblock-lti-consumer external user id, creating one if needed.

    ``LtiAgsScore.user_id`` is Muzzy Lane's own opaque identifier for the learner (LTI's
    "external user id"), not an Open edX user id — the two vocabularies never overlap, so
    scores can't be looked up by `lti_profile.user_id` directly. Reuses
    ``compat.batch_get_or_create_externalids`` (the same helper `xblock-lti-consumer` itself
    uses for this, see `lti_consumer/plugin/views.py`'s `attach_external_user_ids`) rather than
    querying `ExternalId` directly, so this doesn't depend on knowing that model's own field
    names — get-or-create semantics mean this is safe to call even for a learner who has never
    actually launched Muzzy Lane; the later `LtiAgsScore` lookup then simply finds nothing.

    Args:
        lti_profile: The launching user's LtiProfile.

    Returns:
        The external user id string, or None if xblock-lti-consumer isn't installed.

    """
    try:
        from lti_consumer.plugin import compat as lti_consumer_compat  # pylint: disable=import-outside-toplevel,import-error
    except ImportError:
        return None

    external_ids = lti_consumer_compat.batch_get_or_create_externalids([lti_profile.user])
    external_id = external_ids.get(lti_profile.user.id)

    return str(external_id.external_user_id) if external_id else None


def relay_criterion_scores(
    lti_profile: LtiProfile,
    coupled_resource: LtiGradedResource,
    lti_configuration,
    block,
):
    """Publish each of this block's AGS line-item scores to its own Moodle column.

    Reads directly from `LtiAgsScore` — a plain, synchronous row per (line item, user); that
    model's own `unique_together` guarantees at most one — rather than through
    `course_grade_factory`, which is Open edX's cached/aggregated grade view. That distinction
    matters: it means a later-running call here can never see an *older* value than an earlier
    one already published, which is the most plausible explanation for the original bug's
    non-determinism (identical inputs producing different outcomes across runs) — reading
    through a cache that hadn't caught up, not merely "last write wins".

    Called once per criterion save, and republishes *every* currently-known criterion for the
    block each time, not just the one that changed, because the native signal that triggers this
    doesn't carry per-criterion identity. A burst of N criteria posted close together therefore
    produces up to N redundant re-publishes of already-settled values — harmless, not racy:
    `LtiGradedResource.publish_score`'s own `last_score_given` check skips re-sending a value
    that hasn't changed, so the only cost is a few wasted lookups.

    No due-date or `FullyGraded` check happens here: both are already guaranteed by the fact
    that this only ever runs after `xblock-lti-consumer`'s own `publish_grade_on_score_update`
    succeeded (see `send_score_updates`, which is this function's only caller).

    Args:
        lti_profile: The launching user's LtiProfile.
        coupled_resource: This block's coupled (`criterion_key=''`) LtiGradedResource — the
            source of `lineitems_url`, captured at launch since this call happens well after
            the launch request that carried it has ended.
        lti_configuration: This block's `lti_consumer.models.LtiConfiguration`.
        block: The loaded XBlock instance, for its `display_name` (labeling only).

    """
    from lti_consumer.models import LtiAgsScore  # pylint: disable=import-outside-toplevel,import-error

    external_user_id = get_external_user_id(lti_profile)
    if not external_user_id:
        return

    scores = LtiAgsScore.objects.filter(
        line_item__lti_configuration=lti_configuration,
        user_id=external_user_id,
        grading_progress=LtiAgsScore.FULLY_GRADED,
        score_given__isnull=False,
        score_maximum__gt=0,
    ).select_related('line_item')
    if not scores:
        return

    from pylti1p3.lineitem import LineItem  # pylint: disable=import-outside-toplevel

    ags = get_ags_for_lineitems_url(
        lti_profile.platform_id, lti_profile.client_id, coupled_resource.lineitems_url,
    )

    block_id = str(lti_configuration.location)
    # Same fallback as setup_problem_lineitems' own per-problem labels.
    block_label = block.display_name or block_id
    # LtiActivityLineitem is shared across every user of the same placement (see its own
    # docstring), so this has to identify the placement itself, not this one user's launch.
    # resource_link_id is the natural choice, but it's a field this fix introduced — existing
    # LtiGradedResource rows only get it backfilled on relaunch (see handle_ags), so it can
    # still be '' here for a user who hasn't relaunched since this shipped. Falling back to
    # coupled_resource.lineitem avoids two different placements of the same block colliding
    # onto one shared lineitem during that window: lineitem has existed, and been distinct per
    # placement, since the coupled record was first created — long before resource_link_id did.
    placement_key = coupled_resource.resource_link_id or coupled_resource.lineitem

    for score in scores:
        line_item = score.line_item
        # Muzzy Lane's own identifier for this specific criterion — resource_id is the primary
        # source; tag is a fallback for a line item created without one set.
        criterion_key = line_item.resource_id or line_item.tag
        if not criterion_key:
            continue
        criterion_label = line_item.label or criterion_key

        activity_lineitem, created = LtiActivityLineitem.objects.get_or_create(
            platform_id=lti_profile.platform_id,
            resource_link_id=placement_key,
            problem_id=block_id,
            criterion_key=criterion_key,
            defaults={
                'context_id': coupled_resource.context_id,
                # The launched Open edX resource, matching setup_problem_lineitems' own use of
                # this field — not block_id (that's what problem_id already holds).
                'resource_id': coupled_resource.context_key,
                'label': f'{block_label} — {criterion_label}',
            },
        )

        if created or not activity_lineitem.lineitem:
            lineitem = LineItem()
            # Tag per (placement, problem, criterion) — same shape as setup_problem_lineitems'
            # own tag, with the criterion appended so distinct criteria never share a lineitem.
            lineitem.set_tag(f'{placement_key}:{block_id}:{criterion_key}')
            lineitem.set_label(f'{block_label} — {criterion_label}')
            # Normalized to percent (max 100), not the source line item's own score_maximum:
            # Muzzy Lane's own possible-points can vary per learner (branching/looping), so only
            # a percentage is safe to compare and post consistently across attempts.
            lineitem.set_score_maximum(100.0)
            activity_lineitem.lineitem = ags.find_or_create_lineitem(lineitem, find_by='tag').get_id()
            activity_lineitem.save()

        try:
            graded_resource, _created = LtiGradedResource.objects.get_or_create(
                lti_profile=lti_profile,
                context_key=block_id,
                lineitem=activity_lineitem.lineitem,
                criterion_key=criterion_key,
            )
        except ValidationError as exc:
            log.warning(
                'LTI AGS: skipping per-criterion LtiGradedResource for block %s criterion %s: %s',
                block_id,
                criterion_key,
                exc.messages,
            )
            continue

        # Cap at score_maximum (AGS allows a tool to send a score higher than its own declared
        # maximum) and convert to a percent — same capping `xblock-lti-consumer`'s own
        # `publish_grade_on_score_update` applies before writing into Open edX's gradebook.
        percent = min(score.score_given, score.score_maximum) / score.score_maximum * 100
        log.info(
            'LTI AGS: Sending per-criterion AGS update for %s criterion %s with user %s',
            block_id,
            criterion_key,
            lti_profile.user_id,
        )
        graded_resource.publish_score(percent, 100.0)


@shared_task(name=f'{MODULE_PATH}.send_score_updates')
def send_score_updates(
    user_id: str,
    course_id: str,
    problem_id: str,
):
    """Publish AGS scores for every launched resource affected by a grade change.

    A grade change in Open edX is only ``(user, block)`` — it carries no notion of which
    platform activity the learner launched. So on each change we walk the changed block
    and its ancestors (unit, subsection, section) up to — but not including — the course,
    and for every level that has a coupled ``LtiGradedResource`` for this user we post that
    level's score to its lineitem. This serves both:

    - **coupled** records (context = a launched unit/subsection/component) -> the launched
      resource's aggregate lands in its single per-placement column, and
    - **per-problem** records (context = a leaf block) -> the block's own score.

    For a block that qualifies for per-criterion relay (see
    ``get_multi_line_item_lti_configuration``), the coupled record is *not* published to at
    all — ``relay_criterion_scores`` owns that block's Moodle relay instead, publishing one
    column per internal AGS line item rather than one collapsed column. Every lookup here is
    explicitly filtered to ``criterion_key=''`` (the coupled record) for exactly this reason:
    ``LtiGradedResourceManager.all_from_user_id`` is not criterion_key-aware, so without this
    filter this loop would also find and overwrite every per-criterion record for a block with
    the block's single collapsed score — a defensive backstop, not the only thing preventing
    that; the primary guarantee is that a block only ever goes down one branch below, never both.

    The course level is handled separately by ``publish_course_score``.

    Args:
        user_id: Grading user ID.
        course_id: Context course id string.
        problem_id: Usage id of the block whose score changed.

    """
    lti_profile = LtiProfile.objects.filter(user__id=user_id).first()
    if not lti_profile:
        return

    try:
        usage_key = UsageKey.from_string(problem_id)
    except InvalidKeyError:
        return

    user = get_user_model().objects.filter(id=user_id).first()
    if not user:
        return

    course_grade = course_grade_factory().read(
        user,
        modulestore().get_course(CourseKey.from_string(course_id)),
    )

    # Collect the changed block and its ancestors, up to (not including) the course. Blocks are
    # kept alongside their locations (not just re-fetched later) so the per-criterion check below
    # doesn't need a second modulestore lookup for every location.
    locations = []
    block = modulestore().get_item(usage_key)
    while block is not None and block.location.block_type != 'course':
        locations.append((block.location, block))
        parent = getattr(block, 'parent', None)
        block = modulestore().get_item(parent) if parent else None

    for location, location_block in locations:
        # criterion_key='' is explicit, not incidental: see this function's own docstring.
        coupled_resources = LtiGradedResource.objects.all_from_user_id(
            user_id=user_id,
            context_key=str(location),
        ).filter(criterion_key='')
        if not coupled_resources:
            continue

        lti_configuration = get_multi_line_item_lti_configuration(location_block, lti_profile)

        if lti_configuration:
            # One relay per coupled resource, not just the first: the same block can be
            # embedded via more than one Moodle placement (LtiActivityLineitem's own
            # docstring — "distinct activities... in separate gradebook columns instead of
            # collapsing into one"), each with its own coupled record and its own
            # lineitems_url. Picking only coupled_resources[0] would silently relay one
            # placement and drop every other one.
            for coupled_resource in coupled_resources:
                relay_criterion_scores(lti_profile, coupled_resource, lti_configuration, location_block)
            continue

        earned, possible = course_grade.score_for_block(location)
        for graded_resource in coupled_resources:
            log.info(
                'LTI AGS: Sending AGS update for %s with user %s',
                str(location),
                user_id,
            )
            graded_resource.publish_score(earned, possible)
