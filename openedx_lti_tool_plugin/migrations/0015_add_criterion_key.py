from django.db import migrations, models

import openedx_lti_tool_plugin.resource_link_launch.ags.validators


class Migration(migrations.Migration):
    """Add per-criterion AGS relay support.

    `criterion_key` lets a single Open edX block (`problem_id`/`context_key`) map to several
    Moodle lineitems instead of one — the empty-string default preserves the existing
    single-lineitem-per-block rows and behavior exactly. `resource_link_id` and `lineitems_url`
    on `LtiGradedResource` capture launch-only data (the Moodle activity id and the AGS
    lineitems collection URL) so a later, asynchronous per-criterion relay can still reach it —
    see `LtiGradedResource`'s field help text for why. `context_key` gets a standalone index
    since it is now looked up on every relayed score, not just at launch.
    """

    dependencies = [
        ('openedx_lti_tool_plugin', '0014_ltiactivitylineitem_resource_link_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='ltiactivitylineitem',
            name='criterion_key',
            field=models.CharField(
                blank=True,
                default='',
                help_text=(
                    'Identifies one internal AGS line item within `problem_id` (e.g. one Muzzy '
                    'Lane rubric criterion), sourced from that line item\'s own '
                    '`resource_id`/`tag`. Empty string means "the whole problem" — today\'s '
                    'single-lineitem-per-problem semantics, unchanged for every existing row '
                    'and every block that never has more than one internal line item.'
                ),
                max_length=255,
            ),
        ),
        migrations.AlterUniqueTogether(
            name='ltiactivitylineitem',
            unique_together={('platform_id', 'resource_link_id', 'problem_id', 'criterion_key')},
        ),
        migrations.AddField(
            model_name='ltigradedresource',
            name='criterion_key',
            field=models.CharField(
                blank=True,
                default='',
                help_text=(
                    'Identifies one internal AGS line item within `context_key` (e.g. one Muzzy '
                    'Lane rubric criterion). Empty string ("") is the coupled/collapsed record '
                    'every launch has always created — today\'s single-score semantics, '
                    'unchanged. Deliberately has no validator (unlike `context_key`): it is not '
                    'itself a CourseKey/UsageKey, just an opaque tag borrowed from the source '
                    'line item, and adding one would be the wrong kind of check for what this '
                    'field actually holds.'
                ),
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name='ltigradedresource',
            name='resource_link_id',
            field=models.CharField(
                blank=True,
                default='',
                help_text=(
                    'LTI resource link id (the Moodle activity/placement) from the launch that '
                    'created this record. Captured here because it is only available on the '
                    'launch request, while a per-criterion relay runs later, asynchronously, '
                    'off a score change — by then the original launch request is long gone, so '
                    'anything needed at relay time that only the launch claims carry has to be '
                    'stored, not recomputed.'
                ),
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name='ltigradedresource',
            name='lineitems_url',
            field=models.URLField(
                blank=True,
                default='',
                help_text=(
                    'AGS `lineitems` collection URL from the launch claims — the endpoint used '
                    'to create additional per-criterion lineitems later. Same reasoning as '
                    '`resource_link_id`: only available at launch time, needed again later.'
                ),
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name='ltigradedresource',
            name='context_id',
            field=models.CharField(
                blank=True,
                default='',
                help_text=(
                    'LTI context claim id (the Moodle course, as opposed to `resource_link_id`, '
                    'the specific activity within it) from the launch that created this record. '
                    'Same reasoning as `resource_link_id`/`lineitems_url`: only available at '
                    'launch time, needed again later — here, to fill in the informational '
                    '(non-unique-key) `LtiActivityLineitem.context_id` field when a '
                    'per-criterion lineitem is created.'
                ),
                max_length=255,
            ),
        ),
        migrations.AlterField(
            model_name='ltigradedresource',
            name='context_key',
            field=models.CharField(
                db_index=True,
                help_text='The opaque key string of the resource.',
                max_length=255,
                validators=[openedx_lti_tool_plugin.resource_link_launch.ags.validators.validate_context_key],
            ),
        ),
        migrations.AlterUniqueTogether(
            name='ltigradedresource',
            unique_together={('lti_profile', 'context_key', 'lineitem', 'criterion_key')},
        ),
    ]
