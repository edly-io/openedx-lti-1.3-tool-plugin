"""Django Models."""
from __future__ import annotations

import hashlib
import logging
import math
from datetime import datetime, timezone
from typing import Optional, Union

from django.db import models
from django.db.models import QuerySet
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from pylti1p3.contrib.django import DjangoDbToolConf, DjangoMessageLaunch
from pylti1p3.exception import LtiException
from pylti1p3.grade import Grade
from requests.exceptions import RequestException

from openedx_lti_tool_plugin.apps import OpenEdxLtiToolPluginConfig as app_config
from openedx_lti_tool_plugin.models import LtiProfile
from openedx_lti_tool_plugin.resource_link_launch.ags.validators import validate_context_key

log = logging.getLogger(__name__)


def compute_unique_key(*parts: str) -> str:
    r"""Return a fixed-length (64-char) digest of a composite natural key.

    Both models below need a `criterion_key` dimension added to a key that already spans
    3-4 CharField(255)/URLField(255) columns. MySQL/InnoDB limits a composite index to 3072
    bytes total; at utf8mb4 (4 bytes/char, plus a length-prefix byte per variable-length
    column) three such columns alone already use ~3060-3066 of that budget — there is no
    room left for a fourth, at any length, without this. A joined-with-a-separator hash of
    the real columns sidesteps the limit entirely while keeping the natural columns
    themselves as plain, queryable data (lookups still filter on the real fields; only the
    DB-level uniqueness constraint is on this derived one). `\x1f` (ASCII unit separator) is
    used as the join separator specifically because it practically never appears in any of
    the real values (issuer URLs, opaque keys, tags), avoiding ambiguous concatenations like
    "ab"+"c" vs "a"+"bc".

    Args:
        *parts: The natural key's component values, in a fixed, caller-defined order.

    Returns:
        A 64-character hex SHA-256 digest.

    """
    return hashlib.sha256('\x1f'.join(str(part) for part in parts).encode()).hexdigest()


class LtiActivityLineitem(models.Model):
    """Per-problem lineitem mapping for per-problem passback mode (Moodle).

    Created once per (platform, resource link, problem) and shared across all users who
    launch that same platform activity. Keying by ``resource_link_id`` (the platform
    activity/placement) is what keeps distinct activities that embed the same Open edX
    problem in separate gradebook columns instead of collapsing into one. ``context_id``
    is kept as data but left out of the unique key (a four-CharField composite exceeds
    MySQL's 3072-byte index limit, and the resource link already implies the context).
    """

    platform_id = models.CharField(
        max_length=255,
        help_text=_('LTI platform issuer (iss) — the Moodle server.'),
    )
    context_id = models.CharField(
        max_length=255,
        help_text=_('LTI context claim id — the specific Moodle course.'),
    )
    resource_link_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text=_('LTI resource link id — the specific platform activity/placement.'),
    )
    resource_id = models.CharField(
        max_length=255,
        help_text=_('The launched Open edX course/content ID.'),
    )
    problem_id = models.CharField(
        max_length=255,
        help_text=_('Usage key of the individual graded problem.'),
    )
    lineitem = models.URLField(
        max_length=255,
        blank=True,
        default='',
        help_text=_('Pre-created Moodle lineitem URL for this problem.'),
    )
    label = models.CharField(max_length=255, blank=True, default='')
    criterion_key = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text=_(
            'Identifies one internal AGS line item within `problem_id` (e.g. one Muzzy Lane '
            'rubric criterion), sourced from that line item\'s own `resource_id`/`tag`. Empty '
            'string means "the whole problem" — today\'s single-lineitem-per-problem semantics, '
            'unchanged for every existing row and every block that never has more than one '
            'internal line item.',
        ),
    )
    unique_key = models.CharField(
        max_length=64,
        unique=True,
        editable=False,
        default='',
        help_text=_(
            'SHA-256 digest of (platform_id, resource_link_id, problem_id, criterion_key), '
            'auto-computed in save(). This — not those four columns directly — is what '
            'enforces uniqueness; see compute_unique_key() for why. Lookups still filter on '
            'the real columns; only the DB-level constraint moved to this field.',
        ),
    )

    class Meta:
        """Model metadata options."""

        app_label = app_config.name
        verbose_name = 'LTI activity lineitem'
        verbose_name_plural = 'LTI activity lineitems'

    def __str__(self) -> str:
        """Model string representation."""
        return f'<LtiActivityLineitem, ID: {self.id}>'

    def save(self, *args: tuple, **kwargs: dict):
        """Model save method.

        Computes `unique_key` from the natural key fields before every save, so callers
        (`get_or_create`, direct `.save()`, admin edits) never need to compute or pass it
        themselves — it can't drift out of sync with the fields it's derived from.

        Args:
            *args: Variable length argument list.
            **kwargs: Arbitrary keyword arguments.

        """
        self.unique_key = compute_unique_key(
            self.platform_id, self.resource_link_id, self.problem_id, self.criterion_key,
        )
        super().save(*args, **kwargs)


class LtiGradedResourceManager(models.Manager):
    """A manager for the LtiGradedResource model."""

    def all_from_user_id(self, user_id: int, context_key: str) -> Optional[QuerySet]:
        """
        Retrieve all instances for a user ID and context key.

        Deliberately not filtered by `criterion_key`: a block with per-criterion records has
        several rows sharing one `context_key`, distinguished only by `criterion_key`. A caller
        that wants just the coupled/collapsed record (`criterion_key=''`) must filter for that
        explicitly — see `send_score_updates`, which does, precisely so it never republishes the
        collapsed value onto a per-criterion record.

        Args:
            user_id: User ID.
            context_key: Graded resource opaque key string.

        Returns:
            LtiGradedResourceManager query or None.

        """
        return self.filter(
            lti_profile=LtiProfile.objects.filter(user__id=user_id).first(),
            context_key=context_key,
        )


class LtiGradedResource(models.Model):
    """LTI graded resource.

    A unique representation of a LTI graded resource.

    """

    objects = LtiGradedResourceManager()
    lti_profile = models.ForeignKey(
        LtiProfile,
        on_delete=models.CASCADE,
        related_name='openedx_lti_tool_plugin_graded_resource',
        help_text=_('The LTI profile that launched the resource.'),
    )
    context_key = models.CharField(
        max_length=255,
        help_text=_('The opaque key string of the resource.'),
        validators=[validate_context_key],
        db_index=True,
    )
    lineitem = models.URLField(
        max_length=255,
        help_text=_('The AGS lineitem URL.'),
    )
    last_score_given = models.FloatField(
        null=True,
        blank=True,
        help_text=_('Last score value successfully sent to the platform (used to skip redundant publishes).'),
    )
    last_score_maximum = models.FloatField(
        null=True,
        blank=True,
        help_text=_('Score maximum of the last score successfully sent to the platform.'),
    )
    criterion_key = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text=_(
            'Identifies one internal AGS line item within `context_key` (e.g. one Muzzy Lane '
            'rubric criterion). Empty string ("") is the coupled/collapsed record every launch '
            'has always created — today\'s single-score semantics, unchanged. Deliberately has '
            'no validator (unlike `context_key`): it is not itself a CourseKey/UsageKey, just an '
            'opaque tag borrowed from the source line item, and adding one would be the wrong '
            'kind of check for what this field actually holds.',
        ),
    )
    resource_link_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text=_(
            'LTI resource link id (the Moodle activity/placement) from the launch that created '
            'this record. Captured here because it is only available on the launch request, '
            'while a per-criterion relay runs later, asynchronously, off a score change — by '
            'then the original launch request is long gone, so anything needed at relay time '
            'that only the launch claims carry has to be stored, not recomputed.',
        ),
    )
    lineitems_url = models.URLField(
        max_length=255,
        blank=True,
        default='',
        help_text=_(
            'AGS `lineitems` collection URL from the launch claims — the endpoint used to '
            'create additional per-criterion lineitems later. Same reasoning as '
            '`resource_link_id`: only available at launch time, needed again later.',
        ),
    )
    context_id = models.CharField(
        max_length=255,
        blank=True,
        default='',
        help_text=_(
            'LTI context claim id (the Moodle course, as opposed to `resource_link_id`, the '
            'specific activity within it) from the launch that created this record. Same '
            'reasoning as `resource_link_id`/`lineitems_url`: only available at launch time, '
            'needed again later — here, to fill in the informational (non-unique-key) '
            '`LtiActivityLineitem.context_id` field when a per-criterion lineitem is created.',
        ),
    )
    unique_key = models.CharField(
        max_length=64,
        unique=True,
        editable=False,
        default='',
        help_text=_(
            'SHA-256 digest of (lti_profile_id, context_key, lineitem, criterion_key), '
            'auto-computed in save(). This — not those four columns directly — is what '
            'enforces uniqueness; see compute_unique_key() for why. Lookups still filter on '
            'the real columns; only the DB-level constraint moved to this field.',
        ),
    )

    class Meta:
        """Model metadata options."""

        app_label = app_config.name
        verbose_name = 'LTI graded resource'
        verbose_name_plural = 'LTI graded resources'

    def __str__(self) -> str:
        """Model string representation."""
        return f'<LtiGradedResource, ID: {self.id}>'

    def save(self, *args: tuple, **kwargs: dict):
        """Model save method.

        Computes `unique_key` before validating/saving, so `full_clean`'s own uniqueness
        check runs against the field that actually enforces it, and so callers never need to
        compute or pass it themselves.

        Args:
            *args: Variable length argument list.
            **kwargs: Arbitrary keyword arguments.

        """
        self.unique_key = compute_unique_key(
            self.lti_profile_id, self.context_key, self.lineitem, self.criterion_key,
        )
        self.full_clean()
        super().save(*args, **kwargs)

    @cached_property
    def publish_score_jwt(self) -> dict:
        """dict: JWT payload for LTI AGS score publish request."""
        return {
            'body': {
                'iss': self.lti_profile.platform_id,
                'aud': self.lti_profile.client_id,
                'https://purl.imsglobal.org/spec/lti-ags/claim/endpoint': {
                    'lineitem': self.lineitem,
                    'scope': {
                        'https://purl.imsglobal.org/spec/lti-ags/scope/lineitem',
                        'https://purl.imsglobal.org/spec/lti-ags/scope/score',
                    },
                },
            },
        }

    def publish_score(
        self,
        given_score: Union[int, float],
        score_maximum: Union[int, float],
        activity_progress: str = 'Submitted',
        grading_progress: str = 'FullyGraded',
        timestamp: Optional[datetime] = None,
        event_id: str = '',
    ):
        """
        Publish score to the LTI platform.

        Args:
            given_score: Given score.
            score_maximum: Score maximum.
            activity_progress: Status of the activity's completion.
            grading_progress: Status of the grading process.
            timestamp: Score datetime.
            event_id: Optional ID for this event.

        Raises:
            LtiException: Invalid score data.
            RequestException: LTI AGS score publish request failure.

        .. _LTI Assignment and Grade Services Specification - Score publish service:
            https://www.imsglobal.org/spec/lti-ags/v2p0/#score-publish-service

        """
        given = float(given_score)
        maximum = float(score_maximum)
        # Skip redundant publishes: if the score is unchanged since the last successful
        # send there is nothing new for the platform to record. Mirrors Moodle's
        # enrol_lti `lastgrade` guard and avoids needless AGS traffic.
        if (
            self.last_score_given is not None
            and self.last_score_maximum is not None
            and math.isclose(self.last_score_given, given, rel_tol=1e-9, abs_tol=1e-12)
            and math.isclose(self.last_score_maximum, maximum, rel_tol=1e-9, abs_tol=1e-12)
        ):
            log.info(
                'LTI AGS score unchanged for user %s lineitem %s; skipping publish.',
                self.lti_profile.subject_id,
                self.lineitem,
            )
            return

        # Evaluate per call: a datetime default in the signature is bound once at import
        # time, so every call would reuse that stale timestamp and the platform (e.g.
        # Moodle) would reject later updates as "not newer" (409).
        if timestamp is None:
            timestamp = datetime.now(tz=timezone.utc)

        log_extra = {
            'event_id': event_id,
            'given_score': given_score,
            'score_maximum': score_maximum,
            'activity_progress': activity_progress,
            'grading_progress': grading_progress,
            'user_id': self.lti_profile.subject_id,
            'timestamp': str(timestamp),
            'jwt': self.publish_score_jwt,
        }

        try:
            log.info(f'LTI AGS score publish request started: {log_extra}')
            # Create pylti1.3 DjangoMessageLaunch object.
            message = DjangoMessageLaunch(request=None, tool_config=DjangoDbToolConf())\
                .set_auto_validation(enable=False)\
                .set_jwt(self.publish_score_jwt)\
                .set_restored()\
                .validate_registration()
            # Create Grade object for pylti1.3 AssignmentsGradeService.
            grade = Grade()\
                .set_score_given(given_score)\
                .set_score_maximum(score_maximum)\
                .set_timestamp(timestamp.isoformat())\
                .set_activity_progress(activity_progress)\
                .set_grading_progress(grading_progress)\
                .set_user_id(self.lti_profile.subject_id)
            # Send score publish request to LTI platform.
            message.get_ags().put_grade(grade)
            log.info(f'LTI AGS score publish request success: {log_extra}')
            # Record the sent score so identical subsequent publishes are skipped.
            self.last_score_given = given
            self.last_score_maximum = maximum
            self.save(update_fields=['last_score_given', 'last_score_maximum'])
        except LtiException as exc:
            log_extra['exception'] = str(exc)
            log.error(f'LTI AGS score publish request failure: {log_extra}')
            raise
        except RequestException as exc:
            log_extra['exception'] = str(exc)
            log_extra['request'] = getattr(exc.request, '__dict__', {})
            log_extra['response'] = getattr(exc.response, '__dict__', {})
            log.error(f'LTI AGS score publish request failure: {log_extra}')
            raise
